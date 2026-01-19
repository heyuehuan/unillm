"""
Type definitions for UniLLM
"""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field
from datetime import datetime
import time


class Message(BaseModel):
    """OpenAI-compatible message format"""
    role: Literal["system", "user", "assistant", "function", "tool"]
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    name: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "Hi there! How can I help you?"},
            ]
        }
    }


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request"""
    model: str = Field(..., description="Model ID to use", examples=["gemini-2.5-flash-lite"])
    messages: List[Message] = Field(..., description="List of messages in the conversation")
    temperature: Optional[float] = Field(None, description="Sampling temperature (0-2)", ge=0, le=2)
    top_p: Optional[float] = Field(None, description="Nucleus sampling parameter", ge=0, le=1)
    n: Optional[int] = Field(1, description="Number of completions to generate")
    stream: Optional[bool] = Field(False, description="Whether to stream the response")
    stop: Optional[Union[str, List[str]]] = Field(None, description="Stop sequences")
    max_tokens: Optional[int] = Field(None, description="Maximum tokens to generate")
    presence_penalty: Optional[float] = Field(None, description="Presence penalty (-2 to 2)")
    frequency_penalty: Optional[float] = Field(None, description="Frequency penalty (-2 to 2)")
    user: Optional[str] = Field(None, description="Unique user identifier")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "model": "gemini-2.5-flash-lite",
                    "messages": [{"role": "user", "content": "Hello!"}],
                    "max_tokens": 100,
                    "temperature": 0.7
                }
            ]
        }
    }


class CompletionRequest(BaseModel):
    """OpenAI-compatible completion request"""
    model: str = Field(..., description="Model ID to use", examples=["gemini-2.5-flash-lite"])
    prompt: Union[str, List[str]] = Field(..., description="The prompt to complete", examples=["Once upon a time"])
    temperature: Optional[float] = Field(None, description="Sampling temperature (0-2)", ge=0, le=2)
    top_p: Optional[float] = Field(None, description="Nucleus sampling parameter", ge=0, le=1)
    n: Optional[int] = Field(1, description="Number of completions to generate")
    stream: Optional[bool] = Field(False, description="Whether to stream the response")
    stop: Optional[Union[str, List[str]]] = Field(None, description="Stop sequences")
    max_tokens: Optional[int] = Field(None, description="Maximum tokens to generate")
    presence_penalty: Optional[float] = Field(None, description="Presence penalty (-2 to 2)")
    frequency_penalty: Optional[float] = Field(None, description="Frequency penalty (-2 to 2)")
    user: Optional[str] = Field(None, description="Unique user identifier")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "model": "gemini-2.5-flash-lite",
                    "prompt": "Once upon a time",
                    "max_tokens": 50,
                    "temperature": 0.7
                }
            ]
        }
    }


class Usage(BaseModel):
    """Token usage information"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    """Chat completion choice"""
    index: int = 0
    message: Optional[Message] = None
    delta: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None


class TextChoice(BaseModel):
    """Text completion choice"""
    index: int = 0
    text: str = ""
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response"""
    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[Choice]
    usage: Optional[Usage] = None


class CompletionResponse(BaseModel):
    """OpenAI-compatible completion response"""
    id: str
    object: str = "text_completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[TextChoice]
    usage: Optional[Usage] = None


class ModelInfo(BaseModel):
    """Model information"""
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "vertex_ai"


class ModelListResponse(BaseModel):
    """List of models response"""
    object: str = "list"
    data: List[ModelInfo]


class ErrorResponse(BaseModel):
    """Error response"""
    error: Dict[str, Any]


class UserAPIKeyAuth(BaseModel):
    """User API key authentication result"""
    api_key: str
    valid: bool = True
    user_id: Optional[str] = None
