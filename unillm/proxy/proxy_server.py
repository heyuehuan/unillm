"""
UniLLM Proxy Server

A minimal OpenAI-compatible API proxy for Vertex AI Gemini.
"""

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, NamedTuple, Optional

import httpx
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from unillm import __version__
from unillm._logging import verbose_proxy_logger, set_verbose
from unillm.db import get_db
from unillm.db.database import init_db, SessionLocal
from unillm.db import crud
from unillm.proxy.auth import (
    user_api_key_auth,
    set_general_settings,
    enforce_model_access,
    _check_model_access,
)
from unillm.proxy.api_routes import router as api_router, _client_ip
from unillm.proxy import ratelimit
from unillm.proxy import server_settings
from unillm.proxy.security_headers import apply_security_headers, DOCS_PATHS
from unillm.types import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionRequest,
    CompletionResponse,
    ModelInfo,
    ModelListResponse,
    UserAPIKeyAuth,
)
from unillm.llm.vertex_ai import VertexAIHandler
from unillm.llm.vertex_ai_kms import VertexAIKMSHandler
from unillm.llm.vllm import VLLMHandler
from unillm.llm.params import (
    UnsupportedParamsError,
    enabled_optional_params,
    resolve_optional_params,
)
from unillm.llm.logprobs import (
    COMPACT_FORMAT,
    OPENAI_FORMAT,
    LogprobsBudget,
    compact_choice_logprobs,
    compact_stream_chunk,
    filter_choice_logprobs,
    filter_legacy_logprobs,
    last_n_choice_logprobs,
    last_n_logprobs_payload,
    LastNWindow,
    filter_stream_chunk,
    min_bytes_estimate,
    min_logprob_for,
)


# Model type constants
MODEL_TYPE_VERTEX_AI = "vertex-ai"
MODEL_TYPE_VERTEX_AI_KMS = "vertex-ai-kms"
MODEL_TYPE_VLLM = "vllm"

# Global configuration
model_list: List[Dict[str, Any]] = []
general_settings: Dict[str, Any] = {}
vertex_handlers: Dict[str, VertexAIHandler] = {}


def _model_config_params(model_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parameters of a model config entry, with one precedence used everywhere:
    unillm_params first, litellm_params as the backward-compatible fallback.
    """
    return model_config.get("unillm_params", model_config.get("litellm_params", {}))


class ProxyConfig:
    """Proxy configuration manager"""
    
    def __init__(self):
        self.model_list: List[Dict[str, Any]] = []
        self.general_settings: Dict[str, Any] = {}
    
    async def load_config(self, config_file_path: str) -> None:
        """Load configuration from YAML file"""
        global model_list, general_settings, vertex_handlers
        
        if not os.path.exists(config_file_path):
            verbose_proxy_logger.warning(f"Config file not found: {config_file_path}")
            return
        
        with open(config_file_path, "r") as f:
            config = yaml.safe_load(f)
        
        # Load model list
        self.model_list = config.get("model_list", [])
        model_list = self.model_list
        
        # Load general settings
        self.general_settings = config.get("general_settings", {})
        general_settings = self.general_settings
        
        # Set general settings for auth module (needed for SSH verification)
        set_general_settings(self.general_settings)
        # ...and as the fallback tier under any admin-set database overrides
        server_settings.set_config_settings(self.general_settings)
        
        # Initialize handlers for each model
        for model_config in self.model_list:
            model_name = model_config.get("model_name")
            # unillm_params preferred, litellm_params kept for backward compatibility
            unillm_params = _model_config_params(model_config)

            project = unillm_params.get("project")
            location = unillm_params.get("location", "us-central1")
            model_type = unillm_params.get("model_type", MODEL_TYPE_VERTEX_AI)
            
            # Route to appropriate handler based on model_type
            if model_type == MODEL_TYPE_VERTEX_AI_KMS:
                kms_key_name = unillm_params.get("kms_key_name")
                vertex_handlers[model_name] = VertexAIKMSHandler(
                    project=project,
                    location=location,
                    kms_key_name=kms_key_name,
                )
                verbose_proxy_logger.info(
                    f"Initialized KMS handler for model '{model_name}' with KMS key"
                )
            elif model_type == MODEL_TYPE_VLLM:
                vertex_handlers[model_name] = VLLMHandler(
                    base_url=unillm_params.get("base_url", "http://localhost:8000"),
                    api_key=unillm_params.get("api_key"),
                )
                verbose_proxy_logger.info(
                    f"Initialized vLLM handler for model '{model_name}' "
                    f"at {unillm_params.get('base_url', 'http://localhost:8000')}"
                )
            else:
                # Default: vertex-ai
                vertex_handlers[model_name] = VertexAIHandler(
                    project=project,
                    location=location,
                )
        
        verbose_proxy_logger.info(f"Loaded {len(self.model_list)} models from config")
    
    def get_model_config(self, model_name: str) -> Optional[Dict[str, Any]]:
        """Get configuration for a specific model"""
        for model_config in self.model_list:
            if model_config.get("model_name") == model_name:
                return model_config
        return None


# Global proxy config instance
proxy_config = ProxyConfig()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    verbose_proxy_logger.info(f"UniLLM Proxy v{__version__} starting...")

    # Initialize database
    init_db()

    # Seed first admin from env vars if no admin exists yet
    db = next(get_db())
    try:
        api_key = crud.seed_admin_if_needed(db)
        if api_key:
            # Print once to stdout with a banner — deliberately NOT sent through the
            # logger, to keep the secret out of log files/aggregators.
            print("=" * 60)
            print(f"  Admin user '{os.getenv('UNILLM_ADMIN_USERNAME')}' created.")
            print(f"  API key (shown once, store it now): {api_key}")
            print("=" * 60)
    finally:
        db.close()

    # Load config if provided
    config_path = os.getenv("UNILLM_CONFIG", "")
    if config_path and os.path.exists(config_path):
        await proxy_config.load_config(config_path)
    
    yield
    
    # Shutdown
    verbose_proxy_logger.info("UniLLM Proxy shutting down...")
    # Close all handlers
    for handler in vertex_handlers.values():
        await handler.close()


# Create FastAPI app
app = FastAPI(
    title="UniLLM Proxy",
    description="A minimal OpenAI-compatible API proxy for Vertex AI Gemini",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",      # Swagger UI at /docs
    redoc_url="/redoc",    # ReDoc at /redoc
    openapi_url="/openapi.json",
)

# Add CORS middleware. Default to same-origin only; set UNILLM_CORS_ORIGINS to a
# comma-separated allowlist (e.g. "https://app.example.com") to permit cross-origin use.
# Bearer-token auth does not need credentialed CORS, so credentials stay off unless an
# explicit origin allowlist is configured.
_cors_origins = [o.strip() for o in os.getenv("UNILLM_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Throttle the public docs endpoints. They are intentionally unauthenticated —
# an OpenAPI schema is not a secret and self-hosted users expect /docs to work —
# but "public" should not mean "free to scrape at any rate". Registered before the
# header middleware so the 429 it returns still gets the security headers.
_THROTTLED_PUBLIC_PATHS = DOCS_PATHS | {"/openapi.json"}


@app.middleware("http")
async def _throttle_public_docs(request: Request, call_next):
    if request.url.path in _THROTTLED_PUBLIC_PATHS:
        retry_after = ratelimit.docs_limiter.hit(_client_ip(request))
        if retry_after is not None:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Outermost middleware, so every response — including errors — carries the headers."""
    return apply_security_headers(request, await call_next(request))


# Management API routes
app.include_router(api_router)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": __version__}


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
_STATIC_INDEX = os.path.join(_STATIC_DIR, "index.html")

if os.path.isdir(_STATIC_DIR):
    _assets_dir = os.path.join(_STATIC_DIR, "assets")
    if os.path.isdir(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")


@app.get("/")
async def root():
    if os.path.isfile(_STATIC_INDEX):
        return FileResponse(_STATIC_INDEX)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


# Model endpoints
@app.get("/v1/models")
@app.get("/models")
async def list_models(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> ModelListResponse:
    """
    List available models.
    
    Returns a list of models configured in the proxy.
    """
    allowed = user_api_key_dict.allowed_models
    models = []
    for model_config in model_list:
        model_name = model_config.get("model_name", "")
        # None or ["all"] = unrestricted; otherwise only surface permitted models.
        if not _check_model_access(allowed, model_name):
            continue
        models.append(ModelInfo(
            id=model_name,
            object="model",
            created=int(time.time()),
            owned_by="vertex_ai",
        ))

    return ModelListResponse(object="list", data=models)


@app.get("/v1/models/{model_id}")
@app.get("/models/{model_id}")
async def get_model(
    model_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> ModelInfo:
    """
    Get information about a specific model.
    """
    # Check if model exists — and hide models the key has no access to (404, not 403,
    # so restricted keys can't enumerate the configured model list).
    model_config = proxy_config.get_model_config(model_id)
    if model_config is None or not _check_model_access(user_api_key_dict.allowed_models, model_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{model_id}' not found",
        )
    
    return ModelInfo(
        id=model_id,
        object="model",
        created=int(time.time()),
        owned_by="vertex_ai",
    )


def _get_handler_for_model(model_name: str) -> VertexAIHandler:
    """
    Return the cached handler for a configured model.

    Only models declared in model_list are served. Unknown model names are rejected
    with a 404 rather than being forwarded to Vertex AI under the proxy's default
    credentials (which also used to leak a per-request, never-closed HTTP client).
    """
    if model_name in vertex_handlers:
        handler = vertex_handlers[model_name]
        verbose_proxy_logger.debug(f"Using cached handler for model '{model_name}': {type(handler).__name__}")
        return handler

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Model '{model_name}' is not configured",
    )


def _get_actual_model_name(model_name: str) -> str:
    """Get the actual model name to use with Vertex AI"""
    model_config = proxy_config.get_model_config(model_name)
    if model_config:
        params = _model_config_params(model_config)
        actual_model = params.get("model", model_name)
        # Remove vertex_ai/ prefix if present
        if actual_model.startswith("vertex_ai/"):
            actual_model = actual_model[len("vertex_ai/"):]
        return actual_model
    return model_name


def _get_model_params(model_name: str) -> Dict[str, Any]:
    """Get model-specific parameters"""
    model_config = proxy_config.get_model_config(model_name)
    if model_config:
        return _model_config_params(model_config)
    return {}


def _get_model_type(model_name: str) -> str:
    params = _get_model_params(model_name)
    return params.get("model_type", MODEL_TYPE_VERTEX_AI)


def _drop_params_enabled() -> bool:
    """
    Whether unsupported params are stripped instead of rejected.

    Mirrors litellm's `litellm_settings: drop_params`. Off by default: answering a
    logprobs request with a 200 that has no logprobs is worse than a 400, because
    nothing in the response tells the caller their request was not honored.
    """
    return bool(general_settings.get("drop_params", False))


def _resolve_optional_params(
    handler: Any,
    requested: Dict[str, Any],
    *,
    model: str,
    backend_model: str,
    model_type: str,
    text: bool = False,
) -> Dict[str, Any]:
    """
    Narrow client-requested optional params to what this model actually serves.

    `requested` must contain only params the client explicitly set — passing a param
    the caller never sent would make it fail on a default it never chose.
    """
    attr = "SUPPORTED_TEXT_PARAMS" if text else "SUPPORTED_CHAT_PARAMS"
    return resolve_optional_params(
        requested,
        capable=getattr(handler, attr, frozenset()),
        enabled=enabled_optional_params(_get_model_params(model), text=text),
        model=backend_model,
        provider=model_type,
        drop_params=_drop_params_enabled(),
    )


def _client_unsupported_params_detail(model_alias: str, exc: UnsupportedParamsError) -> str:
    """
    The caller-facing half of an UnsupportedParamsError.

    `exc.message` names the backend model and the config key that turns the param on —
    operator detail that belongs in the request log, not in an API response. Clients
    get their own alias and the params they asked for, the same way upstream errors are
    sanitized before they leave the proxy.
    """
    return f"Model '{model_alias}' does not support parameters: {exc.params}"


def _base_log_fields(request_id, auth, ip_address, backend_model, model_type, stream, labels=None) -> Dict[str, Any]:
    """Assemble the per-request log fields shared by streaming and non-streaming paths."""
    return {
        "request_id": request_id,
        # API-key auth is project-scoped; there is no individual user to attribute, so
        # leave user_id null rather than misfiling the project id here.
        "user_id": None,
        "project_id": auth.project_id,
        "api_key_name": auth.api_key_name,
        "api_key_prefix": auth.api_key[:8] if auth.api_key else None,
        "ssh_username": auth.ssh_username,
        "ip_address": ip_address,
        "backend_model": backend_model,
        "model_type": model_type,
        "stream": stream,
        "labels": labels,
    }


def _upstream_status(exc: Exception) -> int:
    """HTTP status to record for an upstream failure."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return 500


def _sanitized_http_exception(exc: Exception) -> HTTPException:
    """Map an upstream/handler exception to a client-safe HTTPException without leaking internals."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if 400 <= code < 500:
            return HTTPException(status_code=code, detail="Upstream model provider rejected the request")
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream model provider error")
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Upstream model provider unavailable")
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


def _write_request_log(model: str, prompt_tokens: int, completion_tokens: int, **fields) -> None:
    """
    Persist a request log on a fresh DB session.

    Background tasks (and streaming finalizers) run after the request's own session is
    closed, so we must not reuse it. Cost is computed here from the pricing table.
    """
    db = SessionLocal()
    try:
        cost = crud.compute_cost(db, model, prompt_tokens, completion_tokens)
        crud.create_request_log(
            db=db, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost_usd=cost, **fields,
        )
    except Exception as e:  # never let logging break the response
        verbose_proxy_logger.warning(f"Failed to write request log: {e}")
    finally:
        db.close()


async def _prime_stream(stream):
    """
    Await the first chunk of an upstream stream before the response starts.

    StreamingResponse sends 200 headers before iterating the body, so upstream
    failures (bad model, provider down, quota) must be raised here — while the
    endpoint can still map them to a real HTTP error — instead of surfacing as a
    dead connection on an already-started 200. Returns an equivalent stream with
    the first chunk reattached.
    """
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        async def _empty():
            return
            yield  # pragma: no cover — makes this an async generator
        return _empty()

    async def _chain():
        yield first
        async for chunk in stream:
            yield chunk

    return _chain()


async def _stream_with_logging(
    stream,
    model_alias: str,
    log_fields: Dict[str, Any],
    start_time: float,
    min_logprob: Optional[float] = None,
    compact_logprobs: bool = False,
    budget: Optional[LogprobsBudget] = None,
    last_n: Optional[int] = None,
):
    """
    Wrap an SSE stream: rewrite the model alias, capture the final usage numbers, and
    write exactly one request log when the stream ends (success or error). This is what
    makes streaming requests metered — previously they were logged as 200/0-tokens up front.

    `min_logprob` thins each chunk's logprob alternatives, `compact_logprobs` rewrites
    them into the flat {token: logprob} shape, `budget` stops emitting them once the
    request's size cap is spent, and `last_n` keeps only the final N positions. All four
    ride along here because every chunk is already parsed and re-serialized to rewrite
    the alias, so they cost no extra pass; with none of them set the chunks come out
    exactly as before.

    The budget is shared across chunks on purpose: the cap is on the whole response, so
    each chunk spends from what earlier chunks left.

    `last_n` changes the timing rather than just the content. Which positions are the
    last N is unknowable until the stream ends, so logprobs are stripped from every
    chunk on the way past, held in a rolling window, and emitted in one extra chunk at
    the end. The other three transforms then run once, on that final window, so they
    measure and shape what actually goes out. Content deltas are untouched and still
    stream live.
    """
    prompt_tokens = 0
    completion_tokens = 0
    status_code = 200
    error_message: Optional[str] = None
    window = LastNWindow(last_n) if last_n else None
    flushed = False

    def _final_chunk() -> Optional[str]:
        """The trailing logprobs chunk, shaped by the same transforms as a whole response."""
        chunk = window.flush() if window is not None else None
        if chunk is None:
            return None
        if min_logprob is not None:
            filter_stream_chunk(chunk, min_logprob)
        if compact_logprobs:
            compact_stream_chunk(chunk)
        if budget is not None:
            budget.apply_to_stream_chunk(chunk)
        return f"data: {json.dumps(chunk)}\n\n"

    try:
        async for chunk in stream:
            if chunk.startswith("data: "):
                data = chunk[6:].strip()
                if data == "[DONE]":
                    # The window has to go out before the terminator, or a client that
                    # stops reading at [DONE] never sees the logprobs it asked for.
                    if window is not None and not flushed:
                        flushed = True
                        final = _final_chunk()
                        if final is not None:
                            yield final
                    yield chunk
                    continue
                try:
                    parsed = json.loads(data)
                    parsed["model"] = model_alias
                    if window is not None:
                        window.capture(parsed)
                    else:
                        if min_logprob is not None:
                            filter_stream_chunk(parsed, min_logprob)
                        if compact_logprobs:
                            compact_stream_chunk(parsed)
                        if budget is not None:
                            budget.apply_to_stream_chunk(parsed)
                    usage = parsed.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        completion_tokens = usage.get("completion_tokens", completion_tokens)
                    yield f"data: {json.dumps(parsed)}\n\n"
                except json.JSONDecodeError:
                    yield chunk
            else:
                yield chunk
        # Not every backend terminates with [DONE]; without this the window would be
        # collected silently and the caller would get a 200 with no logprobs at all.
        if window is not None and not flushed:
            flushed = True
            final = _final_chunk()
            if final is not None:
                yield final
    except Exception as e:
        status_code = _upstream_status(e)
        error_message = str(e)
        # The 200 header is already out; emit a sanitized SSE error event so the
        # client sees a structured failure rather than a bare connection drop.
        yield f"data: {json.dumps({'error': {'message': 'Upstream model provider error', 'type': 'upstream_error'}})}\n\n"
        raise
    finally:
        _write_request_log(
            model=model_alias, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            status_code=status_code, error_message=error_message,
            latency_ms=int((time.time() - start_time) * 1000),
            **log_fields,
        )



def _returns_logprobs(request_body) -> bool:
    """
    Whether this request will actually produce logprobs.

    The two endpoints spell the switch differently. Chat takes a bool, so only `true`
    counts. Legacy completions take a count that doubles as the switch, where `0` is a
    real request for the chosen tokens' own logprobs with no alternatives — so there
    anything non-None counts. `bool` is checked first because it is a subclass of `int`
    and `False == 0`.
    """
    value = getattr(request_body, "logprobs", None)
    if isinstance(value, bool):
        return value
    return value is not None


class _LogprobsPlan(NamedTuple):
    """Everything the proxy decided about this request's logprobs, resolved once."""
    min_logprob: Optional[float]
    response_format: str
    last_n: Optional[int]
    budget: LogprobsBudget
    rejection: Optional[str]


def _logprobs_response_plan(request_body, max_tokens: Optional[int], top_logprobs: Optional[int],
                            wire_format: Optional[str] = None) -> "_LogprobsPlan":
    """
    Work out how this request's logprobs will be shaped and sized before calling out.

    `rejection` is a message when the request cannot possibly fit under the server's
    size cap, so the caller can fail it before paying for inference; it is None
    otherwise.

    `wire_format` overrides what the size estimate assumes. The legacy /v1/completions
    endpoint always returns the flat shape, whatever the caller asks for, so estimating
    it as the bulkier OpenAI chat shape would reject requests that fit three times over.

    The pre-flight check deliberately uses a *lower* bound on the response size, not an
    average. An average would reject requests that were going to fit, which is a worse
    failure than truncating a response that turned out too big — and truncation still
    catches those, exactly, after the fact.
    """
    min_logprob = min_logprob_for(getattr(request_body, "logprobs_min_p", None))
    response_format = wire_format or getattr(request_body, "logprobs_format", None) or OPENAI_FORMAT
    last_n = getattr(request_body, "logprobs_last_n", None)
    if not _returns_logprobs(request_body):
        return _LogprobsPlan(min_logprob, response_format, None, LogprobsBudget(None), None)

    max_bytes = server_settings.get_setting_cached(server_settings.LOGPROBS_MAX_BYTES)
    floor = min_bytes_estimate(max_tokens, top_logprobs, response_format, last_n=last_n)
    rejection = None
    if floor is not None and floor > max_bytes:
        rejection = (
            f"This request would return at least {floor} bytes of logprobs, over the "
            f"server limit of {max_bytes} bytes per request. Lower 'max_tokens' or "
            f"'top_logprobs', set 'logprobs_last_n' to return only the final positions, "
            f"or use 'logprobs_format': 'compact', which is about a third the size."
        )
    return _LogprobsPlan(min_logprob, response_format, last_n, LogprobsBudget(max_bytes), rejection)


# Chat completions endpoint
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(
    request: Request,
    request_body: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Create a chat completion.

    Follows the OpenAI Chat Completions API specification.
    https://platform.openai.com/docs/api-reference/chat/create
    """
    start_time = time.time()
    request_id = str(uuid.uuid4())
    ip_address = _client_ip(request)

    model = request_body.model
    labels = request_body.labels
    messages = [msg.model_dump(exclude_none=True) for msg in request_body.messages]
    stream = request_body.stream or False
    temperature = request_body.temperature
    top_p = request_body.top_p
    max_tokens = request_body.max_tokens
    stop = request_body.stop
    n = request_body.n
    presence_penalty = request_body.presence_penalty
    frequency_penalty = request_body.frequency_penalty

    enforce_model_access(user_api_key_dict, model)

    handler = _get_handler_for_model(model)
    actual_model = _get_actual_model_name(model)
    model_params = _get_model_params(model)
    model_type = _get_model_type(model)

    verbose_proxy_logger.debug(f"Chat completion request_id={request_id} model={model} -> {actual_model}")

    log_fields = _base_log_fields(request_id, user_api_key_dict, ip_address,
                                  actual_model, model_type, stream, labels)

    def _log(status_code: int, prompt_tokens: int = 0, completion_tokens: int = 0, error_message: Optional[str] = None):
        background_tasks.add_task(
            _write_request_log,
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            status_code=status_code, error_message=error_message,
            latency_ms=int((time.time() - start_time) * 1000),
            **log_fields,
        )

    # Only params the client actually asked for are candidates for rejection. An
    # explicit `logprobs: false` is a request for *no* logprobs, so it must not fail
    # against a model that doesn't serve them — and top_logprobs can't appear without
    # a true logprobs (enforced in ChatCompletionRequest).
    requested_optional = {}
    if request_body.logprobs:
        requested_optional["logprobs"] = request_body.logprobs
        if request_body.top_logprobs is not None:
            requested_optional["top_logprobs"] = request_body.top_logprobs
    try:
        optional_params = _resolve_optional_params(
            handler, requested_optional,
            model=model, backend_model=actual_model, model_type=model_type,
        )
    except UnsupportedParamsError as e:
        _log(status_code=400, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_unsupported_params_detail(model, e),
        )

    # Resolved once per request: the floor, the wire format and the size cap are all
    # response-side concerns the proxy applies itself, so none of them reach the backend
    # and none belong in handler_kwargs.
    plan = _logprobs_response_plan(request_body, max_tokens, request_body.top_logprobs)
    if plan.rejection:
        _log(status_code=413, error_message=plan.rejection)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=plan.rejection)

    try:
        handler_kwargs = {
            "model": actual_model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stop": stop,
            "n": n,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "user": request_body.user,
            "stream": stream,
            "project": model_params.get("project"),
            "location": model_params.get("location"),
            **optional_params,
        }
        if model_params.get("kms_key_name"):
            handler_kwargs["kms_key_name"] = model_params.get("kms_key_name")

        response = await handler.chat_completion(**handler_kwargs)

        if stream:
            # Prime first so upstream failures become proper HTTP errors (not a
            # broken 200); the wrapper then writes the log once usage is known.
            response = await _prime_stream(response)
            return StreamingResponse(
                _stream_with_logging(
                    response, model, log_fields, start_time, plan.min_logprob,
                    compact_logprobs=plan.response_format == COMPACT_FORMAT,
                    budget=plan.budget,
                    last_n=plan.last_n,
                ),
                media_type="text/event-stream",
            )

        # Order matters. Slice to the requested window first so nothing downstream pays
        # for positions that will not be sent; thin next so the compact form and the
        # byte count both reflect what actually goes out; charge the budget last,
        # against the final shape.
        for choice in response.choices:
            logprobs = last_n_choice_logprobs(choice.logprobs, plan.last_n)
            logprobs = filter_choice_logprobs(logprobs, plan.min_logprob)
            if plan.response_format == COMPACT_FORMAT:
                logprobs = compact_choice_logprobs(logprobs)
            choice.logprobs = plan.budget.apply_to_choice(logprobs)

        response.model = model
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        _log(status_code=200, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

        response_dict = response.model_dump()
        if user_api_key_dict.ssh_username:
            response_dict["user"] = user_api_key_dict.ssh_username
        if user_api_key_dict.ssh_warning:
            response_dict["warning"] = user_api_key_dict.ssh_warning
        return response_dict

    except HTTPException:
        raise
    except Exception as e:
        _log(status_code=_upstream_status(e), error_message=str(e))
        verbose_proxy_logger.exception(f"Error in chat completion request_id={request_id}: {e}")
        raise _sanitized_http_exception(e)


# Text completions endpoint
@app.post("/v1/completions")
@app.post("/completions")
async def completions(
    request: Request,
    request_body: CompletionRequest,
    background_tasks: BackgroundTasks,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Create a text completion.

    Follows the OpenAI Completions API specification.
    https://platform.openai.com/docs/api-reference/completions/create
    """
    start_time = time.time()
    request_id = str(uuid.uuid4())
    ip_address = _client_ip(request)

    model = request_body.model
    prompt = request_body.prompt
    stream = request_body.stream or False
    temperature = request_body.temperature
    top_p = request_body.top_p
    max_tokens = request_body.max_tokens
    stop = request_body.stop
    n = request_body.n
    presence_penalty = request_body.presence_penalty
    frequency_penalty = request_body.frequency_penalty

    enforce_model_access(user_api_key_dict, model)

    handler = _get_handler_for_model(model)
    actual_model = _get_actual_model_name(model)
    model_params = _get_model_params(model)
    model_type = _get_model_type(model)

    verbose_proxy_logger.debug(f"Text completion request_id={request_id} model={model} -> {actual_model}")

    log_fields = _base_log_fields(request_id, user_api_key_dict, ip_address,
                                  actual_model, model_type, stream, labels=None)

    def _log(status_code: int, prompt_tokens: int = 0, completion_tokens: int = 0, error_message: Optional[str] = None):
        background_tasks.add_task(
            _write_request_log,
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            status_code=status_code, error_message=error_message,
            latency_ms=int((time.time() - start_time) * 1000),
            **log_fields,
        )

    requested_optional = (
        {"logprobs": request_body.logprobs} if request_body.logprobs is not None else {}
    )
    try:
        optional_params = _resolve_optional_params(
            handler, requested_optional,
            model=model, backend_model=actual_model, model_type=model_type, text=True,
        )
    except UnsupportedParamsError as e:
        _log(status_code=400, error_message=e.message)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_unsupported_params_detail(model, e),
        )

    # `logprobs` is a count here, not a bool, and doubles as the per-position width.
    plan = _logprobs_response_plan(request_body, max_tokens, request_body.logprobs,
                                   wire_format=COMPACT_FORMAT)
    if plan.rejection:
        _log(status_code=413, error_message=plan.rejection)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=plan.rejection)

    try:
        handler_kwargs = {
            "model": actual_model,
            "prompt": prompt,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stop": stop,
            "n": n,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "user": request_body.user,
            "stream": stream,
            "project": model_params.get("project"),
            "location": model_params.get("location"),
            **optional_params,
        }
        if model_params.get("kms_key_name"):
            handler_kwargs["kms_key_name"] = model_params.get("kms_key_name")

        response = await handler.text_completion(**handler_kwargs)

        if stream:
            # Prime first so upstream failures become proper HTTP errors (not a
            # broken 200); the wrapper then writes the log once usage is known.
            response = await _prime_stream(response)
            return StreamingResponse(
                _stream_with_logging(
                    response, model, log_fields, start_time, plan.min_logprob,
                    budget=plan.budget,
                    last_n=plan.last_n,
                ),
                media_type="text/event-stream",
            )

        for choice in response.choices:
            logprobs = last_n_logprobs_payload(choice.logprobs, plan.last_n)
            logprobs = filter_legacy_logprobs(logprobs, plan.min_logprob)
            choice.logprobs = plan.budget.apply_to_payload(logprobs)

        response.model = model
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        _log(status_code=200, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

        response_dict = response.model_dump()
        if user_api_key_dict.ssh_username:
            response_dict["user"] = user_api_key_dict.ssh_username
        if user_api_key_dict.ssh_warning:
            response_dict["warning"] = user_api_key_dict.ssh_warning
        return response_dict

    except HTTPException:
        raise
    except Exception as e:
        _log(status_code=_upstream_status(e), error_message=str(e))
        verbose_proxy_logger.exception(f"Error in text completion request_id={request_id}: {e}")
        raise _sanitized_http_exception(e)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    # Don't serve the SPA shell for unmatched API/docs routes — return a real 404 so
    # clients and monitoring see the correct status instead of a 200 + HTML page.
    if full_path.startswith(("api/", "v1/", "models", "docs", "redoc", "openapi.json", "health")):
        raise HTTPException(status_code=404, detail="Not found")
    if os.path.isfile(_STATIC_INDEX):
        return FileResponse(_STATIC_INDEX)
    raise HTTPException(status_code=404, detail="Not found")


# Function to run the server
def run_server(
    host: str = "0.0.0.0",
    port: int = 4000,
    config: Optional[str] = None,
    debug: bool = False,
):
    """Run the UniLLM proxy server"""
    import uvicorn
    
    if debug:
        set_verbose(True)
    
    if config:
        os.environ["UNILLM_CONFIG"] = config
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="debug" if debug else "info",
    )
