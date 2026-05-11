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
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import vertexai
from vertexai.generative_models import GenerativeModel, Content, Part

from unillm._logging import verbose_proxy_logger
from unillm.types import (
    ChatCompletionResponse,
    Choice,
    CompletionResponse,
    Message,
    TextChoice,
    Usage,
)


class VertexAIKMSHandler:
    """
    Handler for Vertex AI Gemini API calls with CMEK support using the Vertex AI SDK.
    
    Uses vertexai.init() with encryption_spec_key_name to configure CMEK
    at the SDK level for all supported operations.
    """
    
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
        self._initialized = False
        self._models: Dict[str, GenerativeModel] = {}
    
    def _ensure_initialized(self):
        """Initialize the Vertex AI SDK with CMEK configuration"""
        if self._initialized:
            return
        
        verbose_proxy_logger.info(
            f"Initializing Vertex AI SDK with project={self.project}, "
            f"location={self.location}, kms_key_name={self.kms_key_name}"
        )
        
        # Initialize Vertex AI with CMEK
        vertexai.init(
            project=self.project,
            location=self.location,
            encryption_spec_key_name=self.kms_key_name,
        )
        
        self._initialized = True
        verbose_proxy_logger.info("Vertex AI SDK initialized with CMEK configuration")
    
    def _get_model(self, model_name: str) -> GenerativeModel:
        """Get or create a GenerativeModel instance"""
        if model_name not in self._models:
            self._ensure_initialized()
            self._models[model_name] = GenerativeModel(model_name)
            verbose_proxy_logger.debug(f"Created GenerativeModel for {model_name}")
        return self._models[model_name]
    
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
                # System messages become system_instruction
                if isinstance(content, str):
                    system_instruction = content
                continue
            
            # Map OpenAI roles to Vertex AI roles
            vertex_role = "user" if role == "user" else "model"
            
            # Convert content to parts
            parts = []
            if isinstance(content, str):
                parts.append(Part.from_text(content))
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append(Part.from_text(item.get("text", "")))
                        elif item.get("type") == "image_url":
                            # Handle image content - for now just skip
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
        
        return config
    
    def _convert_response_to_openai(
        self, response: Any, model: str
    ) -> ChatCompletionResponse:
        """Convert Vertex AI response to OpenAI format"""
        choices = []
        
        for i, candidate in enumerate(response.candidates):
            # Extract text from parts
            text_parts = []
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if hasattr(part, 'text') and part.text:
                        text_parts.append(part.text)
            
            # Map finish reasons
            finish_reason = "stop"
            if hasattr(candidate, 'finish_reason'):
                finish_reason_map = {
                    1: "stop",      # STOP
                    2: "length",    # MAX_TOKENS
                    3: "content_filter",  # SAFETY
                    4: "content_filter",  # RECITATION
                }
                finish_reason = finish_reason_map.get(candidate.finish_reason, "stop")
            
            choices.append(Choice(
                index=i,
                message=Message(
                    role="assistant",
                    content="".join(text_parts) if text_parts else None
                ),
                finish_reason=finish_reason
            ))
        
        # Extract usage metadata
        usage = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage = Usage(
                prompt_tokens=getattr(response.usage_metadata, 'prompt_token_count', 0),
                completion_tokens=getattr(response.usage_metadata, 'candidates_token_count', 0),
                total_tokens=getattr(response.usage_metadata, 'total_token_count', 0),
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
        stream: bool = False,
        project: Optional[str] = None,
        location: Optional[str] = None,
        kms_key_name: Optional[str] = None,
        **kwargs,
    ) -> Union[ChatCompletionResponse, AsyncIterator[str]]:
        """
        Make a chat completion request to Vertex AI Gemini with CMEK support.
        
        Uses the Vertex AI SDK which has been initialized with encryption_spec_key_name.
        """
        # Override project/location/kms if provided
        if project and project != self.project:
            self.project = project
            self._initialized = False
        if location and location != self.location:
            self.location = location
            self._initialized = False
        if kms_key_name and kms_key_name != self.kms_key_name:
            self.kms_key_name = kms_key_name
            self._initialized = False
        
        # Get the model (this ensures SDK is initialized)
        generative_model = self._get_model(model)
        
        # Convert messages to Vertex AI format
        system_instruction, contents = self._convert_messages_to_contents(messages)
        
        # Build generation config
        generation_config = self._build_generation_config(
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
        )
        
        verbose_proxy_logger.debug(f"Vertex AI KMS request for model: {model}")
        verbose_proxy_logger.debug(f"System instruction: {system_instruction}")
        verbose_proxy_logger.debug(f"Contents count: {len(contents)}")
        verbose_proxy_logger.debug(f"Generation config: {generation_config}")
        
        try:
            if stream:
                return self._stream_response(
                    generative_model, contents, system_instruction, generation_config, model
                )
            else:
                # Use generate_content with system_instruction if provided
                if system_instruction:
                    # Create a new model with system instruction
                    model_with_system = GenerativeModel(
                        model,
                        system_instruction=system_instruction,
                    )
                    response = model_with_system.generate_content(
                        contents,
                        generation_config=generation_config if generation_config else None,
                    )
                else:
                    response = generative_model.generate_content(
                        contents,
                        generation_config=generation_config if generation_config else None,
                    )
                
                return self._convert_response_to_openai(response, model)
        
        except Exception as e:
            verbose_proxy_logger.error(f"Vertex AI KMS error: {e}")
            raise
    
    async def _stream_response(
        self,
        generative_model: GenerativeModel,
        contents: List[Content],
        system_instruction: Optional[str],
        generation_config: Dict[str, Any],
        model: str,
    ) -> AsyncIterator[str]:
        """Stream response from Vertex AI"""
        try:
            if system_instruction:
                model_with_system = GenerativeModel(
                    model,
                    system_instruction=system_instruction,
                )
                response_stream = model_with_system.generate_content(
                    contents,
                    generation_config=generation_config if generation_config else None,
                    stream=True,
                )
            else:
                response_stream = generative_model.generate_content(
                    contents,
                    generation_config=generation_config if generation_config else None,
                    stream=True,
                )

            last_usage_metadata = None
            for chunk in response_stream:
                if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                    last_usage_metadata = chunk.usage_metadata
                if chunk.candidates:
                    for candidate in chunk.candidates:
                        if candidate.content and candidate.content.parts:
                            for part in candidate.content.parts:
                                if hasattr(part, 'text') and part.text:
                                    openai_chunk = {
                                        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": model,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": part.text},
                                            "finish_reason": None,
                                        }],
                                    }
                                    yield f"data: {json.dumps(openai_chunk)}\n\n"

            # Send final chunk with finish_reason and usage
            final_chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
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
            
        except Exception as e:
            verbose_proxy_logger.error(f"Vertex AI KMS streaming error: {e}")
            raise
    
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
        # Convert prompt to messages format
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
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
                    yield f"data: {json.dumps(text_chunk)}\n\n"
                except json.JSONDecodeError:
                    continue
    
    async def close(self):
        """Close the handler (no resources to clean up for SDK-based handler)"""
        self._models.clear()
        self._initialized = False


# Global handler instance
vertex_ai_kms_handler = VertexAIKMSHandler()
