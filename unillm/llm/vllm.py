"""
vLLM handler for UniLLM

Forwards requests to a vLLM server (or any OpenAI-compatible endpoint).
vLLM already speaks the OpenAI API, so this handler is a thin HTTP proxy.

Usage in config:
  model_list:
    - model_name: tinyllama
      unillm_params:
        model: Qwen/Qwen2.5-0.5B-Instruct
        model_type: vllm
        base_url: http://localhost:8000
        api_key: ""   # optional, only if vllm started with --api-key
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import httpx

from unillm._logging import verbose_proxy_logger
from unillm.llm.params import CHAT_LOGPROB_PARAMS, TEXT_LOGPROB_PARAMS
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


class VLLMHandler:
    """
    Handler for vLLM (or any OpenAI-compatible) inference server.

    Forwards requests directly to the server's /v1/chat/completions and
    /v1/completions endpoints and maps the response back to UniLLM types.
    """

    # vLLM speaks the OpenAI API natively, so logprobs need no translation in either
    # direction — including the legacy flat shape on /v1/completions, which the
    # Gemini-backed handlers cannot produce.
    SUPPORTED_CHAT_PARAMS = frozenset(CHAT_LOGPROB_PARAMS)
    SUPPORTED_TEXT_PARAMS = frozenset(TEXT_LOGPROB_PARAMS)

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
        return self._http_client

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_chat_request(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        top_p: Optional[float],
        max_tokens: Optional[int],
        stop: Optional[Union[str, List[str]]],
        stream: bool,
        n: Optional[int] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        user: Optional[str] = None,
        logprobs: Optional[bool] = None,
        top_logprobs: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if stream:
            # Without this the OpenAI streaming protocol omits usage from all
            # chunks, and streamed requests would be metered as 0 tokens / $0.
            body["stream_options"] = {"include_usage": True}
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if stop is not None:
            body["stop"] = stop
        # n defaults to 1 upstream; only forward an explicit multi-completion ask
        # so default request bodies stay unchanged.
        if n is not None and n != 1:
            body["n"] = n
        if presence_penalty is not None:
            body["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            body["frequency_penalty"] = frequency_penalty
        if user is not None:
            body["user"] = user
        if logprobs is not None:
            body["logprobs"] = logprobs
        # vLLM rejects top_logprobs unless logprobs is also true, so only forward it
        # alongside an enabled logprobs rather than letting the server 400.
        if top_logprobs is not None and logprobs:
            body["top_logprobs"] = top_logprobs
        return body

    @staticmethod
    def _parse_logprobs(raw: Optional[Dict[str, Any]]) -> Optional[ChoiceLogprobs]:
        """Map an OpenAI-shaped choice.logprobs object into UniLLM types."""
        if not raw:
            return None
        content = []
        for entry in raw.get("content") or []:
            content.append(ChatCompletionTokenLogprob(
                token=entry.get("token", ""),
                logprob=entry.get("logprob", 0.0),
                bytes=entry.get("bytes"),
                top_logprobs=[
                    TopLogprob(
                        token=alt.get("token", ""),
                        logprob=alt.get("logprob", 0.0),
                        bytes=alt.get("bytes"),
                    )
                    for alt in entry.get("top_logprobs") or []
                ],
            ))
        return ChoiceLogprobs(content=content) if content else None

    def _parse_chat_response(self, data: Dict[str, Any]) -> ChatCompletionResponse:
        """Parse an OpenAI-format chat completion response into UniLLM types."""
        choices = []
        for c in data.get("choices", []):
            msg = c.get("message", {})
            choices.append(Choice(
                index=c.get("index", 0),
                message=Message(
                    role=msg.get("role", "assistant"),
                    content=msg.get("content"),
                ),
                finish_reason=c.get("finish_reason"),
                logprobs=self._parse_logprobs(c.get("logprobs")),
            ))

        usage_data = data.get("usage") or {}
        prompt = usage_data.get("prompt_tokens", 0)
        completion = usage_data.get("completion_tokens", 0)
        usage = Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=usage_data.get("total_tokens", prompt + completion),
        )

        return ChatCompletionResponse(
            id=data.get("id", ""),
            model=data.get("model", ""),
            choices=choices,
            usage=usage,
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
        user: Optional[str] = None,
        stream: bool = False,
        logprobs: Optional[bool] = None,
        top_logprobs: Optional[int] = None,
        **kwargs,
    ) -> Union[ChatCompletionResponse, AsyncIterator[str]]:
        """Forward a chat completion request to the vLLM server."""
        url = f"{self.base_url}/v1/chat/completions"
        body = self._build_chat_request(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
            stream=stream,
            n=n,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            user=user,
            logprobs=logprobs,
            top_logprobs=top_logprobs,
        )

        verbose_proxy_logger.debug(f"vLLM request: POST {url} model={model}")

        client = await self._get_http_client()

        if stream:
            return self._stream_response(client, url, body)

        response = await client.post(url, headers=self._get_headers(), json=body)
        if response.status_code != 200:
            verbose_proxy_logger.error(f"vLLM error response: {response.text}")
        response.raise_for_status()
        return self._parse_chat_response(response.json())

    async def _stream_response(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: Dict[str, Any],
    ) -> AsyncIterator[str]:
        """Pass through the SSE stream from vLLM directly to the caller."""
        async with client.stream("POST", url, headers=self._get_headers(), json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield f"{line}\n\n"
                elif line.strip():
                    yield f"{line}\n\n"

    async def text_completion(
        self,
        model: str,
        prompt: Union[str, List[str]],
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        n: Optional[int] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        user: Optional[str] = None,
        stream: bool = False,
        logprobs: Optional[int] = None,
        **kwargs,
    ) -> Union[CompletionResponse, AsyncIterator[str]]:
        """Forward a text completion request to the vLLM server."""
        url = f"{self.base_url}/v1/completions"
        body: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
        }
        if stream:
            # Without this the OpenAI streaming protocol omits usage from all
            # chunks, and streamed requests would be metered as 0 tokens / $0.
            body["stream_options"] = {"include_usage": True}
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if stop is not None:
            body["stop"] = stop
        if n is not None and n != 1:
            body["n"] = n
        if presence_penalty is not None:
            body["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            body["frequency_penalty"] = frequency_penalty
        if user is not None:
            body["user"] = user
        if logprobs is not None:
            body["logprobs"] = logprobs

        verbose_proxy_logger.debug(f"vLLM request: POST {url} model={model}")

        client = await self._get_http_client()

        if stream:
            return self._stream_response(client, url, body)

        response = await client.post(url, headers=self._get_headers(), json=body)
        if response.status_code != 200:
            verbose_proxy_logger.error(f"vLLM error response: {response.text}")
        response.raise_for_status()

        data = response.json()
        choices = []
        for c in data.get("choices", []):
            choices.append(TextChoice(
                index=c.get("index", 0),
                text=c.get("text", ""),
                finish_reason=c.get("finish_reason"),
                # Legacy flat shape; vLLM emits it exactly as OpenAI does, so it
                # passes through untouched.
                logprobs=c.get("logprobs"),
            ))

        usage_data = data.get("usage") or {}
        prompt_tokens = usage_data.get("prompt_tokens", 0)
        completion_tokens = usage_data.get("completion_tokens", 0)

        return CompletionResponse(
            id=data.get("id", ""),
            model=data.get("model", ""),
            choices=choices,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=usage_data.get("total_tokens", prompt_tokens + completion_tokens),
            ),
        )

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
