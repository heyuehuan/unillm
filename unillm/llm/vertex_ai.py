"""
Vertex AI Gemini handler for UniLLM

Uses Google Application Default Credentials for authentication.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

import google.auth
import google.auth.transport.requests
import httpx
from google.auth.credentials import Credentials

from unillm._logging import verbose_proxy_logger
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


class VertexAIHandler:
    """Handler for Vertex AI Gemini API calls using Application Default Credentials"""

    # Vertex serves logprobs via generationConfig.responseLogprobs, but only on some
    # models — gemini-2.0/2.5-flash do, gemini-3.x reply "Logprobs is not supported
    # for this model". Which ones is a config question (supports_logprobs), not
    # something to pin to a model list here.
    SUPPORTED_CHAT_PARAMS = frozenset(CHAT_LOGPROB_PARAMS)
    # /v1/completions is synthesized from a chat call here, and Gemini's per-token
    # output cannot be reshaped into the legacy flat {tokens, token_logprobs,
    # text_offset} form without inventing byte offsets. Declared unsupported rather
    # than answered with a plausible-looking approximation.
    SUPPORTED_TEXT_PARAMS = frozenset()

    def __init__(
        self,
        project: Optional[str] = None,
        location: str = "us-central1",
    ):
        self.project = project
        self.location = location
        self._credentials: Optional[Credentials] = None
        # Cached ADC-default project: google.auth.default() only runs once, but the
        # project it resolves must remain available for every subsequent request.
        self._adc_project: Optional[str] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._cred_lock = asyncio.Lock()

    async def _get_credentials_async(self) -> Tuple[Credentials, Optional[str]]:
        """
        Resolve credentials without blocking the event loop.

        Token refresh is a synchronous HTTP call to Google's token endpoint; run it
        in a worker thread. The lock serializes refreshes so concurrent requests
        don't all refresh (google.auth credentials are not thread-safe to refresh
        concurrently). When the token is valid this is just two attribute checks.
        """
        async with self._cred_lock:
            return await asyncio.to_thread(self._get_credentials)

    def _get_credentials(self) -> Tuple[Credentials, Optional[str]]:
        """Get or refresh Google Cloud credentials. Returns (credentials, adc_project)."""
        if self._credentials is None:
            self._credentials, self._adc_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

        # Refresh credentials if expired
        if self._credentials.expired or not self._credentials.token:
            request = google.auth.transport.requests.Request()
            self._credentials.refresh(request)

        return self._credentials, self._adc_project
    
    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client"""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
        return self._http_client
    
    def _get_api_url(self, model: str, project: str, location: str, stream: bool = False) -> str:
        """Build the Vertex AI API URL"""
        # Remove provider prefix if present
        if model.startswith("vertex_ai/"):
            model = model[len("vertex_ai/"):]

        endpoint = "streamGenerateContent" if stream else "generateContent"

        base_url = f"https://{location}-aiplatform.googleapis.com/v1"
        return f"{base_url}/projects/{project}/locations/{location}/publishers/google/models/{model}:{endpoint}"
    
    def _convert_messages_to_gemini_format(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[Optional[Dict], List[Dict]]:
        """
        Convert OpenAI message format to Gemini format.
        Returns (system_instruction, contents)
        """
        system_instruction = None
        contents = []
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if role == "system":
                # System messages become system_instruction
                if isinstance(content, str):
                    system_instruction = {"parts": [{"text": content}]}
                continue
            
            # Map OpenAI roles to Gemini roles
            gemini_role = "user" if role == "user" else "model"
            
            # Convert content to parts
            parts = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append({"text": item.get("text", "")})
                        elif item.get("type") == "image_url":
                            # Handle image content
                            image_url = item.get("image_url", {})
                            url = image_url.get("url", "") if isinstance(image_url, dict) else ""
                            if url.startswith("data:"):
                                # Base64 encoded image
                                mime_type, base64_data = url.split(";base64,")
                                mime_type = mime_type.replace("data:", "")
                                parts.append({
                                    "inline_data": {
                                        "mime_type": mime_type,
                                        "data": base64_data
                                    }
                                })
                    else:
                        parts.append({"text": str(item)})
            
            if parts:
                contents.append({"role": gemini_role, "parts": parts})
        
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
        """Build Gemini generation config from OpenAI parameters"""
        config = {}

        if temperature is not None:
            config["temperature"] = temperature
        if top_p is not None:
            config["topP"] = top_p
        if max_tokens is not None:
            config["maxOutputTokens"] = max_tokens
        if stop is not None:
            if isinstance(stop, str):
                config["stopSequences"] = [stop]
            else:
                config["stopSequences"] = stop
        # Only send candidateCount when the client actually asked for multiple
        # completions (n defaults to 1), keeping default requests unchanged.
        if n is not None and n > 1:
            config["candidateCount"] = n
        if presence_penalty is not None:
            config["presencePenalty"] = presence_penalty
        if frequency_penalty is not None:
            config["frequencyPenalty"] = frequency_penalty
        # OpenAI's bool `logprobs` and int `top_logprobs` collapse onto Gemini's
        # `responseLogprobs` (bool) and `logprobs` (top-k count) — same split, but the
        # name `logprobs` means different things on the two sides.
        if logprobs is not None:
            config["responseLogprobs"] = logprobs
        # Gemini's top-k count starts at 1, while OpenAI's top_logprobs=0 is a valid
        # request meaning "no alternatives" — expressed here by omitting the field
        # rather than forwarding a 0 Vertex rejects.
        if logprobs and top_logprobs:
            config["logprobs"] = top_logprobs

        return config

    @staticmethod
    def _convert_logprobs(logprobs_result: Optional[Dict[str, Any]]) -> Optional[ChoiceLogprobs]:
        """
        Convert Gemini's logprobsResult into OpenAI's ChoiceLogprobs.

        Gemini returns two parallel lists: chosenCandidates (the emitted token at each
        position) and topCandidates (the alternatives considered there). OpenAI nests
        the alternatives under each chosen token, so they are zipped by position.
        topCandidates is absent when the request did not ask for top-k, and can be
        shorter than chosenCandidates, so it is indexed defensively.
        """
        if not logprobs_result:
            return None
        chosen = logprobs_result.get("chosenCandidates")
        if not chosen:
            # `avgLogprobs` (a single float) is always present on candidates and is
            # not per-token data — deliberately not mapped to anything here.
            return None

        top_candidates = logprobs_result.get("topCandidates") or []
        content = []
        for index, candidate in enumerate(chosen):
            alternatives = []
            if index < len(top_candidates):
                for alt in top_candidates[index].get("candidates") or []:
                    alternatives.append(TopLogprob(
                        token=alt.get("token", ""),
                        logprob=alt.get("logProbability", 0.0),
                    ))
            content.append(ChatCompletionTokenLogprob(
                token=candidate.get("token", ""),
                logprob=candidate.get("logProbability", 0.0),
                top_logprobs=alternatives,
            ))

        return ChoiceLogprobs(content=content)

    def _convert_gemini_response_to_openai(
        self, gemini_response: Dict[str, Any], model: str
    ) -> ChatCompletionResponse:
        """Convert Gemini response to OpenAI format"""
        candidates = gemini_response.get("candidates", [])
        usage_metadata = gemini_response.get("usageMetadata", {})
        
        choices = []
        for i, candidate in enumerate(candidates):
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            
            # Extract text from parts
            text_parts = []
            for part in parts:
                if "text" in part:
                    text_parts.append(part["text"])
            
            finish_reason = candidate.get("finishReason", "stop")
            # Map Gemini finish reasons to OpenAI
            finish_reason_map = {
                "STOP": "stop",
                "MAX_TOKENS": "length",
                "SAFETY": "content_filter",
                "RECITATION": "content_filter",
            }
            finish_reason = finish_reason_map.get(finish_reason, "stop")
            
            choices.append(Choice(
                index=i,
                message=Message(
                    role="assistant",
                    content="".join(text_parts) if text_parts else None
                ),
                finish_reason=finish_reason,
                logprobs=self._convert_logprobs(candidate.get("logprobsResult")),
            ))

        prompt_tokens = usage_metadata.get("promptTokenCount", 0)
        completion_tokens = usage_metadata.get("candidatesTokenCount", 0)
        total_tokens = usage_metadata.get("totalTokenCount", prompt_tokens + completion_tokens)

        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            model=model,
            choices=choices,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
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
        logprobs: Optional[bool] = None,
        top_logprobs: Optional[int] = None,
        **kwargs,
    ) -> Union[ChatCompletionResponse, AsyncIterator[str]]:
        """
        Make a chat completion request to Vertex AI Gemini.

        Effective project/location are resolved as call-locals (never written back to
        self) so concurrent requests with different overrides can't interleave.
        """
        # Get credentials (and the ADC-default project, if any)
        credentials, adc_project = await self._get_credentials_async()

        effective_project = project or self.project or adc_project
        effective_location = location or self.location

        # Build request
        system_instruction, contents = self._convert_messages_to_gemini_format(messages)
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

        request_body = {"contents": contents}
        if system_instruction:
            request_body["systemInstruction"] = system_instruction
        if generation_config:
            request_body["generationConfig"] = generation_config

        # Build URL
        url = self._get_api_url(model, effective_project, effective_location, stream=stream)
        if stream:
            url += "?alt=sse"

        # Make request
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

        client = await self._get_http_client()

        verbose_proxy_logger.debug(f"Vertex AI request URL: {url}")
        verbose_proxy_logger.debug(f"Vertex AI request body: {json.dumps(request_body)[:500]}...")

        if stream:
            return self._stream_response(client, url, headers, request_body, model)
        else:
            response = await client.post(url, headers=headers, json=request_body)
            if response.status_code != 200:
                error_text = response.text
                verbose_proxy_logger.error(f"Vertex AI error response: {error_text}")
            response.raise_for_status()
            gemini_response = response.json()
            return self._convert_gemini_response_to_openai(gemini_response, model)
    
    async def _stream_response(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Dict[str, str],
        request_body: Dict[str, Any],
        model: str,
    ) -> AsyncIterator[str]:
        """Stream response from Vertex AI"""
        async with client.stream("POST", url, headers=headers, json=request_body) as response:
            response.raise_for_status()
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    try:
                        gemini_chunk = json.loads(data)
                        openai_chunk = self._convert_stream_chunk(gemini_chunk, model)
                        yield f"data: {json.dumps(openai_chunk)}\n\n"
                    except json.JSONDecodeError:
                        continue

            yield "data: [DONE]\n\n"
    
    def _convert_stream_chunk(
        self, gemini_chunk: Dict[str, Any], model: str
    ) -> Dict[str, Any]:
        """Convert a Gemini streaming chunk to OpenAI format"""
        candidates = gemini_chunk.get("candidates", [])
        
        choices = []
        for i, candidate in enumerate(candidates):
            content = candidate.get("content", {})
            parts = content.get("parts", [])

            delta = {}
            text_parts = [part["text"] for part in parts if "text" in part]
            if text_parts:
                delta["content"] = "".join(text_parts)

            finish_reason = candidate.get("finishReason")
            if finish_reason:
                finish_reason_map = {
                    "STOP": "stop",
                    "MAX_TOKENS": "length",
                    "SAFETY": "content_filter",
                }
                finish_reason = finish_reason_map.get(finish_reason, "stop")
            
            choice: Dict[str, Any] = {
                "index": i,
                "delta": delta,
                "finish_reason": finish_reason,
            }
            # In OpenAI's streaming format logprobs sit alongside `delta`, not inside
            # it, and cover only the tokens carried by this chunk.
            chunk_logprobs = self._convert_logprobs(candidate.get("logprobsResult"))
            if chunk_logprobs is not None:
                choice["logprobs"] = chunk_logprobs.model_dump()
            choices.append(choice)

        chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": choices,
        }

        usage_metadata = gemini_chunk.get("usageMetadata", {})
        if usage_metadata:
            prompt = usage_metadata.get("promptTokenCount", 0)
            completion = usage_metadata.get("candidatesTokenCount", 0)
            chunk["usage"] = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": usage_metadata.get("totalTokenCount", prompt + completion),
            }

        return chunk
    
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
        **kwargs,
    ) -> Union[CompletionResponse, AsyncIterator[str]]:
        """
        Make a text completion request to Vertex AI Gemini.
        Converts the prompt to a chat format internally.
        """
        # Convert prompt to messages format
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
            # Handle list of prompts (just use the first one)
            messages = [{"role": "user", "content": prompt[0] if prompt else ""}]
        
        # Call chat completion
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
            **kwargs,
        )
        
        if stream:
            # Convert streaming chat response to text completion format
            return self._convert_chat_stream_to_text_stream(response, model)
        else:
            # Convert chat response to text completion format
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
                    # Convert to text completion format
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
                    yield f"data: {json.dumps(text_chunk)}\n\n"
                except json.JSONDecodeError:
                    continue
    
    async def close(self):
        """Close the HTTP client"""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


# Global handler instance
vertex_ai_handler = VertexAIHandler()
