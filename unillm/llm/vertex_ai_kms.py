"""
Vertex AI KMS handler for UniLLM

Uses the Vertex AI Python SDK with Customer-Managed Encryption Keys (CMEK).
The SDK is initialized with encryption_spec_key_name which applies CMEK
to operations that support it.

Reference: https://cloud.google.com/vertex-ai/docs/general/cmek

Usage in config:
  model_list:
    - model_name: gemini-2.5-flash-kms
      unillm_params:
        model: gemini-2.5-flash-lite
        model_type: vertex-ai-kms
        project: my-project
        location: us-central1
        kms_key_name: projects/PROJECT_ID/locations/LOCATION_ID/keyRings/KEY_RING/cryptoKeys/KEY_NAME

Note: vertexai.init() is a process-global call. Only one (project, location, kms_key_name)
combination can be active at a time. If multiple KMS models with different projects or
locations are configured, they will re-initialize the global SDK on each switch.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import vertexai
from vertexai.generative_models import GenerativeModel, Content, Part

from unillm._logging import verbose_proxy_logger
from unillm.llm import finish_reasons
from unillm.llm.params import CHAT_LOGPROB_PARAMS
from unillm.types import (
    ChatCompletionResponse,
    ChatCompletionTokenLogprob,
    Choice,
    ChoiceLogprobs,
    CompletionResponse,
    Message,
    TextChoice,
    TopLogprob,
    Usage,
)


# vertexai.init() sets process-global state. Track what's currently initialized
# so we only re-init when the config actually changes.
_global_vertexai_lock = threading.Lock()
_global_vertexai_config: Optional[tuple] = None  # (project, location, kms_key_name)


def _ensure_vertexai_initialized(
    project: Optional[str],
    location: str,
    kms_key_name: Optional[str],
) -> bool:
    """
    Call vertexai.init() only when the config has changed. Returns True if re-initialized.

    Caller must hold _global_vertexai_lock: the init and any GenerativeModel
    construction that depends on it belong to the same critical section, otherwise
    a concurrent request for a different KMS config can re-init in between and the
    model ends up bound to the wrong project/key.
    """
    global _global_vertexai_config
    config = (project, location, kms_key_name)
    if _global_vertexai_config == config:
        return False
    verbose_proxy_logger.info(
        f"Initializing Vertex AI SDK: project={project}, location={location}, "
        f"kms_key_name={kms_key_name}"
    )
    vertexai.init(
        project=project,
        location=location,
        encryption_spec_key_name=kms_key_name,
    )
    _global_vertexai_config = config
    verbose_proxy_logger.info("Vertex AI SDK initialized with CMEK configuration")
    return True


def _bind_prediction_client(model: GenerativeModel) -> None:
    """
    Resolve a model's prediction client while the SDK still holds the config it was
    built for.

    The SDK exposes the client as a cached_property, so it is otherwise created on
    the first generate_content call — by then another request may have re-inited
    the global config, and the client would capture that one instead. Touching the
    attribute here pins it. Only the sync client is warmed, because that is the one
    both the blocking and the streaming path use. A missing attribute means an SDK
    that names it differently — nothing to pin, not a failed request.
    """
    try:
        model._prediction_client
    except AttributeError:
        pass


class VertexAIKMSHandler:
    """
    Handler for Vertex AI Gemini API calls with CMEK support using the Vertex AI SDK.

    Uses vertexai.init() with encryption_spec_key_name to configure CMEK
    at the SDK level for all supported operations.
    """

    # Same Gemini capability as the REST handler, reached through the SDK's
    # snake_case generation config. Per-model availability is a config question —
    # see unillm/llm/params.py.
    SUPPORTED_CHAT_PARAMS = frozenset(CHAT_LOGPROB_PARAMS)
    # text_completion here is a chat call reshaped into the legacy format, which has
    # no place to carry per-token logprobs faithfully.
    SUPPORTED_TEXT_PARAMS = frozenset()

    def __init__(
        self,
        project: Optional[str] = None,
        location: str = "us-central1",
        kms_key_name: Optional[str] = None,
    ):
        """
        Initialize VertexAIKMSHandler with CMEK configuration.

        Args:
            project: Google Cloud project ID
            location: Vertex AI location (default: us-central1)
            kms_key_name: Full resource name of the Cloud KMS key
                Format: projects/PROJECT_ID/locations/LOCATION_ID/keyRings/KEY_RING/cryptoKeys/KEY_NAME
        """
        self.project = project
        self.location = location
        self.kms_key_name = kms_key_name
        # Keyed by the full config a model is bound to, not just its name: two KMS
        # models can differ only in project or key, and an entry built under one
        # config must never be handed to a request asking for another. Bounded
        # because system prompts can be unique per request; models are cheap to
        # rebuild, so a full clear is fine.
        self._models: Dict[tuple, GenerativeModel] = {}
        self._models_cache_max = 128

    def _get_model(self, model_name: str, project: Optional[str], location: str,
                   kms_key_name: Optional[str],
                   system_instruction: Optional[str] = None) -> GenerativeModel:
        """
        Get or create a GenerativeModel bound to this request's Vertex config.

        vertexai.init() writes process-global state, and a GenerativeModel reads
        that state twice: at construction, for the project and location baked into
        its resource name, and again on first use, when its prediction client is
        built and captures credentials and endpoint. Only the first happened under
        the lock. A request for a different project could re-init in the gap, and
        the model then built its client from the other request's configuration —
        wrong project, wrong CMEK key, for the whole life of that cached model.

        So the client is warmed here, inside the same critical section as the init
        and the construction. Once warmed, an entry no longer consults the global
        config at all, which is what makes it safe to keep entries for several
        configs side by side rather than clearing the cache on every switch.
        """
        cache_key = (project, location, kms_key_name, model_name, system_instruction)
        with _global_vertexai_lock:
            model = self._models.get(cache_key)
            if model is not None:
                return model
            _ensure_vertexai_initialized(project, location, kms_key_name)
            model = (
                GenerativeModel(model_name, system_instruction=system_instruction)
                if system_instruction
                else GenerativeModel(model_name)
            )
            _bind_prediction_client(model)
            if len(self._models) >= self._models_cache_max:
                self._models.clear()
            self._models[cache_key] = model
            verbose_proxy_logger.debug(f"Created GenerativeModel for {cache_key}")
            return model

    def _convert_messages_to_contents(
        self, messages: List[Dict[str, Any]]
    ) -> tuple[Optional[str], List[Content]]:
        """
        Convert OpenAI message format to Vertex AI Content format.
        Returns (system_instruction, contents)
        """
        system_instruction = None
        contents = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                if isinstance(content, str):
                    system_instruction = content
                continue

            vertex_role = "user" if role == "user" else "model"

            parts = []
            if isinstance(content, str):
                parts.append(Part.from_text(content))
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append(Part.from_text(item.get("text", "")))
                        elif item.get("type") == "image_url":
                            # TODO: Add proper image handling
                            pass
                    else:
                        parts.append(Part.from_text(str(item)))

            if parts:
                contents.append(Content(role=vertex_role, parts=parts))

        return system_instruction, contents

    def _build_generation_config(
        self,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        n: Optional[int] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        logprobs: Optional[bool] = None,
        top_logprobs: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build Vertex AI generation config from OpenAI parameters"""
        config = {}

        if temperature is not None:
            config["temperature"] = temperature
        if top_p is not None:
            config["top_p"] = top_p
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        if stop is not None:
            if isinstance(stop, str):
                config["stop_sequences"] = [stop]
            else:
                config["stop_sequences"] = stop
        # Only send candidate_count when the client actually asked for multiple
        # completions (n defaults to 1), keeping default requests unchanged.
        if n is not None and n > 1:
            config["candidate_count"] = n
        if presence_penalty is not None:
            config["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            config["frequency_penalty"] = frequency_penalty
        # Same two-field split as the REST API, in the SDK's snake_case spelling:
        # response_logprobs is the on/off bool, logprobs the top-k count.
        if logprobs is not None:
            config["response_logprobs"] = logprobs
        # Same 1-based top-k as the REST API: top_logprobs=0 asks for no alternatives,
        # which Vertex expresses as an absent field, not logprobs=0.
        if logprobs and top_logprobs:
            config["logprobs"] = top_logprobs

        return config

    @staticmethod
    def _convert_logprobs(logprobs_result: Any) -> Optional[ChoiceLogprobs]:
        """
        Convert the SDK's logprobs_result into OpenAI's ChoiceLogprobs.

        Same zip-by-position mapping as the REST handler, but reading proto attributes
        instead of dict keys. The field is absent entirely on responses that did not
        request logprobs, and unset protos read as empty rather than None, so both are
        treated as "no logprobs".
        """
        if logprobs_result is None:
            return None
        chosen = getattr(logprobs_result, "chosen_candidates", None)
        if not chosen:
            return None

        top_candidates = getattr(logprobs_result, "top_candidates", None) or []
        content = []
        for index, candidate in enumerate(chosen):
            alternatives = []
            if index < len(top_candidates):
                for alt in getattr(top_candidates[index], "candidates", None) or []:
                    alternatives.append(TopLogprob(
                        token=getattr(alt, "token", ""),
                        logprob=getattr(alt, "log_probability", 0.0),
                    ))
            content.append(ChatCompletionTokenLogprob(
                token=getattr(candidate, "token", ""),
                logprob=getattr(candidate, "log_probability", 0.0),
                top_logprobs=alternatives,
            ))

        return ChoiceLogprobs(content=content)

    def _convert_response_to_openai(
        self, response: Any, model: str
    ) -> ChatCompletionResponse:
        """Convert Vertex AI response to OpenAI format"""
        choices = []

        for i, candidate in enumerate(response.candidates):
            text_parts = []
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if hasattr(part, 'text') and part.text:
                        text_parts.append(part.text)

            finish_reason = finish_reasons.from_enum(
                getattr(candidate, "finish_reason", None)
            ) or "stop"

            choices.append(Choice(
                index=getattr(candidate, "index", None) or i,
                message=Message(
                    role="assistant",
                    content="".join(text_parts) if text_parts else None
                ),
                finish_reason=finish_reason,
                logprobs=self._convert_logprobs(getattr(candidate, "logprobs_result", None)),
            ))

        usage = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            prompt = getattr(response.usage_metadata, 'prompt_token_count', 0)
            completion = getattr(response.usage_metadata, 'candidates_token_count', 0)
            usage = Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=getattr(response.usage_metadata, 'total_token_count', prompt + completion),
            )

        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            model=model,
            choices=choices,
            usage=usage
        )

    async def chat_completion(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        n: Optional[int] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        stream: bool = False,
        project: Optional[str] = None,
        location: Optional[str] = None,
        kms_key_name: Optional[str] = None,
        logprobs: Optional[bool] = None,
        top_logprobs: Optional[int] = None,
        **kwargs,
    ) -> Union[ChatCompletionResponse, AsyncIterator[str]]:
        """
        Make a chat completion request to Vertex AI Gemini with CMEK support.
        """
        # Resolve effective config from per-call overrides or instance defaults.
        # Never mutate self.* — these are local to this call only.
        effective_project = project or self.project
        effective_location = location or self.location
        effective_kms_key_name = kms_key_name or self.kms_key_name

        system_instruction, contents = self._convert_messages_to_contents(messages)
        generation_config = self._build_generation_config(
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            n=n,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
        )

        verbose_proxy_logger.debug(f"Vertex AI KMS request for model: {model}")
        verbose_proxy_logger.debug(f"System instruction: {system_instruction}")
        verbose_proxy_logger.debug(f"Contents count: {len(contents)}")
        verbose_proxy_logger.debug(f"Generation config: {generation_config}")

        model_obj = self._get_model(model, effective_project, effective_location,
                                    effective_kms_key_name, system_instruction=system_instruction)

        try:
            if stream:
                return self._stream_response(model_obj, contents, generation_config, model)
            else:
                response = await asyncio.to_thread(
                    lambda: model_obj.generate_content(
                        contents,
                        generation_config=generation_config or None,
                    )
                )
                return self._convert_response_to_openai(response, model)

        except Exception as e:
            verbose_proxy_logger.error(f"Vertex AI KMS error: {e}")
            raise

    async def _stream_response(
        self,
        model_obj: GenerativeModel,
        contents: List[Content],
        generation_config: Dict[str, Any],
        model: str,
    ) -> AsyncIterator[str]:
        """
        Stream response from Vertex AI.

        The Vertex AI SDK's streaming iterator is synchronous, so we run it in a
        background thread and bridge chunks to the async generator via a Queue.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()
        stop = threading.Event()  # set when the consumer goes away (e.g. client disconnect)

        def _produce():
            try:
                stream = model_obj.generate_content(
                    contents,
                    generation_config=generation_config or None,
                    stream=True,
                )
                for chunk in stream:
                    if stop.is_set():
                        return
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            except Exception as exc:
                if not stop.is_set():
                    asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
            finally:
                if not stop.is_set():
                    asyncio.run_coroutine_threadsafe(queue.put(_sentinel), loop).result()

        thread = threading.Thread(target=_produce, daemon=True)
        thread.start()

        def _wrap(choice: Dict[str, Any]) -> str:
            return "data: " + json.dumps({
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [choice],
            }) + "\n\n"

        last_usage_metadata = None
        # Finish reason per candidate, in the order the candidates first appeared,
        # so the closing chunks can report what actually happened to each one.
        finish_by_index: "OrderedDict[int, Optional[str]]" = OrderedDict()
        try:
            while True:
                item = await queue.get()
                if item is _sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                chunk = item
                if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                    last_usage_metadata = chunk.usage_metadata
                if chunk.candidates:
                    for candidate in chunk.candidates:
                        # The candidate's own index, not its position in this chunk:
                        # with n > 1 a chunk carries only the candidates that produced
                        # tokens, so counting positions merges separate completions.
                        index = getattr(candidate, "index", None) or 0
                        reason = finish_reasons.from_enum(
                            getattr(candidate, "finish_reason", None)
                        )
                        finish_by_index.setdefault(index, None)
                        if reason is not None:
                            finish_by_index[index] = reason

                        # Logprobs arrive once per candidate, but a candidate can span
                        # several parts. Attach them to the first chunk emitted for
                        # this candidate so they aren't repeated for every part.
                        pending_logprobs = self._convert_logprobs(
                            getattr(candidate, "logprobs_result", None)
                        )
                        parts = candidate.content.parts if candidate.content else None
                        for part in (parts or []):
                            if not (hasattr(part, 'text') and part.text):
                                continue
                            choice: Dict[str, Any] = {
                                "index": index,
                                "delta": {"content": part.text},
                                "finish_reason": None,
                            }
                            if pending_logprobs is not None:
                                choice["logprobs"] = pending_logprobs.model_dump()
                                pending_logprobs = None
                            yield _wrap(choice)

                        # A candidate can carry logprobs on a chunk with no text —
                        # the final one, typically, which only reports the finish
                        # reason. Emitting an empty delta keeps them rather than
                        # dropping the last position's alternatives on the floor.
                        if pending_logprobs is not None:
                            yield _wrap({
                                "index": index,
                                "delta": {},
                                "finish_reason": None,
                                "logprobs": pending_logprobs.model_dump(),
                            })
        finally:
            # Tell the producer to stop and wait for it off the event loop — the old
            # blocking join(30) could stall the whole loop for up to 30s when a
            # client disconnected mid-stream.
            stop.set()
            await asyncio.to_thread(thread.join, 30)

        # One closing chunk per candidate, carrying the reason Vertex actually gave.
        # This used to be a single hardcoded "stop", so a response cut short by the
        # token limit or blocked by a safety filter was reported to the client as a
        # normal completion — and with n > 1 only one candidate was closed at all.
        if not finish_by_index:
            finish_by_index[0] = None
        final_chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": index,
                "delta": {},
                "finish_reason": reason or "stop",
            } for index, reason in finish_by_index.items()],
        }
        if last_usage_metadata:
            prompt = getattr(last_usage_metadata, 'prompt_token_count', 0)
            completion = getattr(last_usage_metadata, 'candidates_token_count', 0)
            final_chunk["usage"] = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": getattr(last_usage_metadata, 'total_token_count', prompt + completion),
            }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    async def text_completion(
        self,
        model: str,
        prompt: Union[str, List[str]],
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        stream: bool = False,
        project: Optional[str] = None,
        location: Optional[str] = None,
        kms_key_name: Optional[str] = None,
        **kwargs,
    ) -> Union[CompletionResponse, AsyncIterator[str]]:
        """
        Make a text completion request to Vertex AI Gemini with CMEK support.
        Converts the prompt to a chat format internally.
        """
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = [{"role": "user", "content": prompt[0] if prompt else ""}]

        response = await self.chat_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            stream=stream,
            project=project,
            location=location,
            kms_key_name=kms_key_name,
            **kwargs,
        )

        if stream:
            return self._convert_chat_stream_to_text_stream(response, model)
        else:
            return self._convert_chat_to_text_response(response, model)

    def _convert_chat_to_text_response(
        self, chat_response: ChatCompletionResponse, model: str
    ) -> CompletionResponse:
        """Convert chat completion response to text completion format"""
        text_choices = []
        for choice in chat_response.choices:
            text = ""
            if choice.message and choice.message.content:
                text = choice.message.content
            text_choices.append(TextChoice(
                index=choice.index,
                text=text,
                finish_reason=choice.finish_reason,
            ))

        return CompletionResponse(
            id=chat_response.id.replace("chatcmpl-", "cmpl-"),
            model=model,
            choices=text_choices,
            usage=chat_response.usage,
        )

    async def _convert_chat_stream_to_text_stream(
        self, chat_stream: AsyncIterator[str], model: str
    ) -> AsyncIterator[str]:
        """Convert chat streaming response to text completion format"""
        async for chunk in chat_stream:
            if chunk.startswith("data: "):
                data = chunk[6:].strip()
                if data == "[DONE]":
                    yield "data: [DONE]\n\n"
                    break

                try:
                    chat_chunk = json.loads(data)
                    text_chunk = {
                        "id": chat_chunk.get("id", "").replace("chatcmpl-", "cmpl-"),
                        "object": "text_completion",
                        "created": chat_chunk.get("created", int(time.time())),
                        "model": model,
                        "choices": [
                            {
                                "index": c.get("index", 0),
                                "text": c.get("delta", {}).get("content", ""),
                                "finish_reason": c.get("finish_reason"),
                            }
                            for c in chat_chunk.get("choices", [])
                        ],
                    }
                    # Token counts arrive on the final chunk and were being dropped
                    # here, so a streamed /v1/completions call logged zero tokens and
                    # zero cost — the usage the caller asked for never reached them
                    # either. Carry the key through only when the backend sent it.
                    if chat_chunk.get("usage") is not None:
                        text_chunk["usage"] = chat_chunk["usage"]
                    yield f"data: {json.dumps(text_chunk)}\n\n"
                except json.JSONDecodeError:
                    continue

    async def close(self):
        """Close the handler"""
        self._models.clear()


# Global handler instance
vertex_ai_kms_handler = VertexAIKMSHandler()
