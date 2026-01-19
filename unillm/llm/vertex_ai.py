"""
Vertex AI Gemini handler for UniLLM

Uses Google Application Default Credentials for authentication.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

import google.auth
import google.auth.transport.requests
import httpx
from google.auth.credentials import Credentials

from unillm._logging import verbose_proxy_logger
from unillm.types import (
    ChatCompletionResponse,
    Choice,
    CompletionResponse,
    Message,
    TextChoice,
    Usage,
)


class VertexAIHandler:
    """Handler for Vertex AI Gemini API calls using Application Default Credentials"""
    
    def __init__(
        self,
        project: Optional[str] = None,
        location: str = "us-central1",
    ):
        self.project = project
        self.location = location
        self._credentials: Optional[Credentials] = None
        self._http_client: Optional[httpx.AsyncClient] = None
    
    def _get_credentials(self) -> Credentials:
        """Get or refresh Google Cloud credentials"""
        if self._credentials is None:
            self._credentials, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if self.project is None:
                self.project = project
        
        # Refresh credentials if expired
        if self._credentials.expired or not self._credentials.token:
            request = google.auth.transport.requests.Request()
            self._credentials.refresh(request)
        
        return self._credentials
    
    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client"""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
        return self._http_client
    
    def _get_api_url(self, model: str, stream: bool = False) -> str:
        """Build the Vertex AI API URL"""
        # Remove provider prefix if present
        if model.startswith("vertex_ai/"):
            model = model[len("vertex_ai/"):]
        
        endpoint = "streamGenerateContent" if stream else "generateContent"
        
        base_url = f"https://{self.location}-aiplatform.googleapis.com/v1"
        return f"{base_url}/projects/{self.project}/locations/{self.location}/publishers/google/models/{model}:{endpoint}"
    
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
        
        return config
    
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
                finish_reason=finish_reason
            ))
        
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            model=model,
            choices=choices,
            usage=Usage(
                prompt_tokens=usage_metadata.get("promptTokenCount", 0),
                completion_tokens=usage_metadata.get("candidatesTokenCount", 0),
                total_tokens=usage_metadata.get("totalTokenCount", 0),
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
        stream: bool = False,
        project: Optional[str] = None,
        location: Optional[str] = None,
        **kwargs,
    ) -> Union[ChatCompletionResponse, AsyncIterator[str]]:
        """
        Make a chat completion request to Vertex AI Gemini.
        """
        # Use provided project/location or defaults
        orig_project = self.project
        orig_location = self.location
        
        if project:
            self.project = project
        if location:
            self.location = location
        
        try:
            # Get credentials
            credentials = self._get_credentials()
            
            # Build request
            system_instruction, contents = self._convert_messages_to_gemini_format(messages)
            generation_config = self._build_generation_config(
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                stop=stop,
            )
            
            request_body = {"contents": contents}
            if system_instruction:
                request_body["systemInstruction"] = system_instruction
            if generation_config:
                request_body["generationConfig"] = generation_config
            
            # Build URL
            url = self._get_api_url(model, stream=stream)
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
                response.raise_for_status()
                gemini_response = response.json()
                return self._convert_gemini_response_to_openai(gemini_response, model)
        
        finally:
            self.project = orig_project
            self.location = orig_location
    
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
                    if data.strip() == "[DONE]":
                        yield "data: [DONE]\n\n"
                        break
                    
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
            for part in parts:
                if "text" in part:
                    delta["content"] = part["text"]
            
            finish_reason = candidate.get("finishReason")
            if finish_reason:
                finish_reason_map = {
                    "STOP": "stop",
                    "MAX_TOKENS": "length",
                    "SAFETY": "content_filter",
                }
                finish_reason = finish_reason_map.get(finish_reason, "stop")
            
            choices.append({
                "index": i,
                "delta": delta,
                "finish_reason": finish_reason,
            })
        
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": choices,
        }
    
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
