"""
Type definitions for UniLLM
"""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, model_serializer, model_validator
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
    # Bounded: n multiplies the work a single request costs the backend, and an
    # unbounded value is a cheap way to turn one request into an expensive one.
    # 128 is the ceiling OpenAI documents.
    n: Optional[int] = Field(1, description="Number of completions to generate (1-128)", ge=1, le=128)
    stream: Optional[bool] = Field(False, description="Whether to stream the response")
    stop: Optional[Union[str, List[str]]] = Field(None, description="Stop sequences")
    max_tokens: Optional[int] = Field(None, description="Maximum tokens to generate")
    presence_penalty: Optional[float] = Field(None, description="Presence penalty (-2 to 2)", ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(None, description="Frequency penalty (-2 to 2)", ge=-2, le=2)
    logprobs: Optional[bool] = Field(None, description="Return log probabilities of the output tokens. Only served by models configured with supports_logprobs")
    top_logprobs: Optional[int] = Field(None, description="Number of most likely tokens to return at each position, each with a log probability. Requires logprobs=true", ge=0, le=20)
    logprobs_min_p: Optional[float] = Field(None, description="Drop returned alternatives whose probability is below this floor (0-1). Thins top_logprobs at confident positions; the chosen token's own logprob is always kept. UniLLM-only, applied to the response rather than forwarded", ge=0, le=1)
    logprobs_last_n: Optional[int] = Field(None, description="Return logprobs for only the final N generated positions instead of every position. Use 1 for the last token alone. Requires logprobs=true. UniLLM-only, applied to the response rather than forwarded", ge=1)
    logprobs_format: Optional[Literal["openai", "compact"]] = Field(None, description="Wire format for returned logprobs. 'openai' (default) is the standard array-of-objects shape; 'compact' returns parallel arrays with alternatives as a {token: logprob} map, about a third of the bytes, at the cost of the per-token 'bytes' field. UniLLM-only")
    user: Optional[str] = Field(None, description="Unique user identifier")
    labels: Optional[Dict[str, str]] = Field(None, description="Optional labels for request logging (UniLLM-only, not forwarded to backends)")

    @model_validator(mode="after")
    def _top_logprobs_requires_logprobs(self):
        """
        Reject top_logprobs without logprobs, as OpenAI does.

        Backends disagree on this combination — vLLM 400s, Gemini quietly returns no
        alternatives — so it is settled here instead of surfacing as a provider-shaped
        error the caller has to decode.
        """
        if self.top_logprobs is not None and not self.logprobs:
            raise ValueError(
                "'top_logprobs' is only allowed when 'logprobs' is true"
            )
        # A floor with no alternatives to apply it to is a no-op the caller almost
        # certainly did not intend, and this API fails loudly on unhonored params
        # rather than returning a 200 that quietly ignored one.
        if self.logprobs_min_p is not None and not self.top_logprobs:
            raise ValueError(
                "'logprobs_min_p' filters the alternatives returned by 'top_logprobs', "
                "so it requires a non-zero 'top_logprobs'"
            )
        # Same reasoning: a format for logprobs that were never requested is a
        # parameter the response cannot honor.
        if self.logprobs_format is not None and not self.logprobs:
            raise ValueError(
                "'logprobs_format' describes how logprobs are returned, "
                "so it requires 'logprobs' to be true"
            )
        if self.logprobs_last_n is not None and not self.logprobs:
            raise ValueError(
                "'logprobs_last_n' selects which logprob positions are returned, "
                "so it requires 'logprobs' to be true"
            )
        return self

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
    # Bounded: n multiplies the work a single request costs the backend, and an
    # unbounded value is a cheap way to turn one request into an expensive one.
    # 128 is the ceiling OpenAI documents.
    n: Optional[int] = Field(1, description="Number of completions to generate (1-128)", ge=1, le=128)
    stream: Optional[bool] = Field(False, description="Whether to stream the response")
    stop: Optional[Union[str, List[str]]] = Field(None, description="Stop sequences")
    max_tokens: Optional[int] = Field(None, description="Maximum tokens to generate")
    presence_penalty: Optional[float] = Field(None, description="Presence penalty (-2 to 2)", ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(None, description="Frequency penalty (-2 to 2)", ge=-2, le=2)
    logprobs: Optional[int] = Field(None, description="Include log probabilities on the N most likely tokens. Legacy completions use an int here, not a bool", ge=0, le=20)
    logprobs_min_p: Optional[float] = Field(None, description="Drop returned alternatives whose probability is below this floor (0-1). Requires a non-zero logprobs. UniLLM-only, applied to the response rather than forwarded", ge=0, le=1)
    logprobs_last_n: Optional[int] = Field(None, description="Return logprobs for only the final N generated positions instead of every position. Use 1 for the last token alone. Requires a non-zero logprobs. UniLLM-only, applied to the response rather than forwarded", ge=1)
    logprobs_format: Optional[Literal["openai", "compact"]] = Field(None, description="Accepted for symmetry with /v1/chat/completions. Legacy completions already return the flat {token: logprob} shape, so this has no effect here. UniLLM-only")
    user: Optional[str] = Field(None, description="Unique user identifier")

    @model_validator(mode="after")
    def _min_p_requires_logprobs(self):
        """Same rule as chat: a floor needs alternatives to filter."""
        if self.logprobs_min_p is not None and not self.logprobs:
            raise ValueError(
                "'logprobs_min_p' filters the alternatives returned by 'logprobs', "
                "so it requires a non-zero 'logprobs'"
            )
        # `is None`, not falsiness: `logprobs: 0` is a real request here for the chosen
        # tokens' own logprobs with no alternatives, and narrowing that to the last N
        # positions is exactly the cheapest useful thing this endpoint can do.
        if self.logprobs_last_n is not None and self.logprobs is None:
            raise ValueError(
                "'logprobs_last_n' selects which logprob positions are returned, "
                "so it requires 'logprobs'"
            )
        return self

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


class TopLogprob(BaseModel):
    """An alternative token considered at one position, with its log probability."""
    token: str
    logprob: float
    # UTF-8 bytes of the token. vLLM supplies these; Gemini does not, so this stays
    # None rather than being faked from the decoded string, which would be wrong for
    # tokens that are partial multi-byte sequences.
    bytes: Optional[List[int]] = None


class ChatCompletionTokenLogprob(BaseModel):
    """The chosen token at one position, plus the alternatives ranked below it."""
    token: str
    logprob: float
    bytes: Optional[List[int]] = None
    top_logprobs: List[TopLogprob] = Field(default_factory=list)


class ChoiceLogprobs(BaseModel):
    """
    Per-choice log probabilities, in one of two shapes.

    `content` is OpenAI's chat shape: one object per generated position, each holding
    a list of alternative objects. It is what any OpenAI SDK expects to parse.

    The remaining fields are UniLLM's compact shape, returned only when the caller asks
    for `logprobs_format: "compact"`. It mirrors the flat layout the legacy
    /v1/completions endpoint already uses — three parallel arrays, with alternatives as
    a plain {token: logprob} map — which is roughly a third of the bytes.

    Exactly one of the two is populated. Unset fields are dropped on serialization so a
    response only ever shows the shape that was actually asked for.
    """
    content: Optional[List[ChatCompletionTokenLogprob]] = None

    # --- compact shape ---
    format: Optional[Literal["compact"]] = None
    tokens: Optional[List[str]] = None
    token_logprobs: Optional[List[float]] = None
    top_logprobs: Optional[List[Dict[str, float]]] = None

    # --- set when the size cap cut the response short, in either shape ---
    truncated: Optional[bool] = None
    truncated_at: Optional[int] = Field(
        None, description="Number of positions returned before the size cap stopped the response"
    )

    @model_serializer(mode="wrap")
    def _drop_unset(self, handler):
        """
        Omit null fields.

        Without this, every ordinary OpenAI-shaped response would also carry six null
        keys for a compact shape it is not using, and clients that iterate the object
        would have to know which nulls are meaningful.
        """
        return {k: v for k, v in handler(self).items() if v is not None}


class Choice(BaseModel):
    """Chat completion choice"""
    index: int = 0
    message: Optional[Message] = None
    delta: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    logprobs: Optional[ChoiceLogprobs] = None


class TextChoice(BaseModel):
    """Text completion choice"""
    index: int = 0
    text: str = ""
    finish_reason: Optional[str] = None
    # Legacy completions carry a flat, differently-shaped logprobs object
    # ({tokens, token_logprobs, top_logprobs, text_offset}) rather than ChoiceLogprobs.
    # Passed through as-is from backends that speak it natively.
    logprobs: Optional[Dict[str, Any]] = None


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


class SSHKeyInfo(BaseModel):
    """SSH key registration info"""
    key_name: str
    public_key: str
    username: str


class SSHVerificationResult(BaseModel):
    """Result of SSH key signature verification"""
    verified: bool = False
    username: Optional[str] = None
    key_name: Optional[str] = None
    warning: Optional[str] = None
    error: Optional[str] = None


class UserAPIKeyAuth(BaseModel):
    """User API key authentication result"""
    api_key: str
    valid: bool = True
    user_id: Optional[str] = None
    # DB-resolved fields
    project_id: Optional[int] = None
    api_key_name: Optional[str] = None
    allowed_models: Optional[List[str]] = None  # None = env-var key (no model restriction)
    # SSH verification fields
    ssh_verified: bool = False
    ssh_username: Optional[str] = None
    ssh_key_name: Optional[str] = None
    ssh_warning: Optional[str] = None
