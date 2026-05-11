"""
UniLLM Proxy Server

A minimal OpenAI-compatible API proxy for Vertex AI Gemini.
"""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from unillm import __version__
from unillm._logging import verbose_proxy_logger, set_verbose
from unillm.proxy.auth import user_api_key_auth, set_general_settings
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


# Model type constants
MODEL_TYPE_VERTEX_AI = "vertex-ai"
MODEL_TYPE_VERTEX_AI_KMS = "vertex-ai-kms"

# Global configuration
model_list: List[Dict[str, Any]] = []
general_settings: Dict[str, Any] = {}
vertex_handlers: Dict[str, VertexAIHandler] = {}


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
        
        # Initialize handlers for each model
        for model_config in self.model_list:
            model_name = model_config.get("model_name")
            params = model_config.get("litellm_params", {})
            
            # For backward compatibility with litellm config
            unillm_params = model_config.get("unillm_params", params)
            
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

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": __version__}


@app.get("/")
async def root():
    """Root endpoint - redirects to docs"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


# Model endpoints
@app.get("/v1/models", dependencies=[Depends(user_api_key_auth)])
@app.get("/models", dependencies=[Depends(user_api_key_auth)])
async def list_models(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> ModelListResponse:
    """
    List available models.
    
    Returns a list of models configured in the proxy.
    """
    models = []
    for model_config in model_list:
        model_name = model_config.get("model_name", "")
        models.append(ModelInfo(
            id=model_name,
            object="model",
            created=int(time.time()),
            owned_by="vertex_ai",
        ))
    
    return ModelListResponse(object="list", data=models)


@app.get("/v1/models/{model_id}", dependencies=[Depends(user_api_key_auth)])
@app.get("/models/{model_id}", dependencies=[Depends(user_api_key_auth)])
async def get_model(
    model_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> ModelInfo:
    """
    Get information about a specific model.
    """
    # Check if model exists
    model_config = proxy_config.get_model_config(model_id)
    if model_config is None:
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


async def _read_request_body(request: Request) -> Dict[str, Any]:
    """Read and parse the request body"""
    body = await request.body()
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON: {str(e)}",
        )


def _get_handler_for_model(model_name: str) -> VertexAIHandler:
    """Get the appropriate handler for a model"""
    if model_name in vertex_handlers:
        handler = vertex_handlers[model_name]
        verbose_proxy_logger.debug(f"Using cached handler for model '{model_name}': {type(handler).__name__}")
        return handler
    
    # Try to find a matching model configuration
    model_config = proxy_config.get_model_config(model_name)
    if model_config:
        params = model_config.get("litellm_params", model_config.get("unillm_params", {}))
        model_type = params.get("model_type", MODEL_TYPE_VERTEX_AI)
        verbose_proxy_logger.debug(f"Model '{model_name}' has model_type: {model_type}")
        
        # Route to appropriate handler based on model_type
        if model_type == MODEL_TYPE_VERTEX_AI_KMS:
            return VertexAIKMSHandler(
                project=params.get("project"),
                location=params.get("location", "us-central1"),
                kms_key_name=params.get("kms_key_name"),
            )
        else:
            return VertexAIHandler(
                project=params.get("project"),
                location=params.get("location", "us-central1"),
            )
    
    # Return default handler
    return VertexAIHandler()


def _get_actual_model_name(model_name: str) -> str:
    """Get the actual model name to use with Vertex AI"""
    model_config = proxy_config.get_model_config(model_name)
    if model_config:
        params = model_config.get("litellm_params", model_config.get("unillm_params", {}))
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
        return model_config.get("litellm_params", model_config.get("unillm_params", {}))
    return {}


async def _rewrite_model_in_stream(stream, model_alias: str):
    """Rewrite the model field in each SSE chunk to use the configured alias."""
    async for chunk in stream:
        if chunk.startswith("data: "):
            data = chunk[6:].strip()
            if data == "[DONE]":
                yield chunk
                continue
            try:
                parsed = json.loads(data)
                parsed["model"] = model_alias
                yield f"data: {json.dumps(parsed)}\n\n"
            except json.JSONDecodeError:
                yield chunk
        else:
            yield chunk


# Chat completions endpoint
@app.post("/v1/chat/completions", dependencies=[Depends(user_api_key_auth)])
@app.post("/chat/completions", dependencies=[Depends(user_api_key_auth)])
async def chat_completions(
    request_body: ChatCompletionRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Create a chat completion.
    
    Follows the OpenAI Chat Completions API specification.
    https://platform.openai.com/docs/api-reference/chat/create
    """
    # Extract parameters from request body
    model = request_body.model
    messages = [msg.model_dump(exclude_none=True) for msg in request_body.messages]
    stream = request_body.stream or False
    temperature = request_body.temperature
    top_p = request_body.top_p
    max_tokens = request_body.max_tokens
    stop = request_body.stop
    
    # Get handler and model config
    handler = _get_handler_for_model(model)
    actual_model = _get_actual_model_name(model)
    model_params = _get_model_params(model)
    
    verbose_proxy_logger.debug(f"Chat completion request for model: {model} -> {actual_model}")
    
    try:
        # Build kwargs for handler - include kms_key_name if present (for vertex-ai-kms)
        handler_kwargs = {
            "model": actual_model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stop": stop,
            "stream": stream,
            "project": model_params.get("project"),
            "location": model_params.get("location"),
        }
        
        # Add kms_key_name if present (for vertex-ai-kms handler)
        if model_params.get("kms_key_name"):
            handler_kwargs["kms_key_name"] = model_params.get("kms_key_name")
        
        response = await handler.chat_completion(**handler_kwargs)
        
        if stream:
            return StreamingResponse(
                _rewrite_model_in_stream(response, model),
                media_type="text/event-stream",
            )
        else:
            # Update model name in response to match request
            response.model = model

            # Convert to dict to add SSH info
            response_dict = response.model_dump()

            # Add SSH verification info if available
            if user_api_key_dict.ssh_username:
                response_dict["user"] = user_api_key_dict.ssh_username
            if user_api_key_dict.ssh_warning:
                response_dict["warning"] = user_api_key_dict.ssh_warning

            return response_dict

    except Exception as e:
        verbose_proxy_logger.exception(f"Error in chat completion: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


# Text completions endpoint
@app.post("/v1/completions", dependencies=[Depends(user_api_key_auth)])
@app.post("/completions", dependencies=[Depends(user_api_key_auth)])
async def completions(
    request_body: CompletionRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Create a text completion.
    
    Follows the OpenAI Completions API specification.
    https://platform.openai.com/docs/api-reference/completions/create
    """
    # Extract parameters from request body
    model = request_body.model
    prompt = request_body.prompt
    stream = request_body.stream or False
    temperature = request_body.temperature
    top_p = request_body.top_p
    max_tokens = request_body.max_tokens
    stop = request_body.stop
    
    # Get handler and model config
    handler = _get_handler_for_model(model)
    actual_model = _get_actual_model_name(model)
    model_params = _get_model_params(model)
    
    verbose_proxy_logger.debug(f"Text completion request for model: {model} -> {actual_model}")
    
    try:
        # Build kwargs for handler - include kms_key_name if present (for vertex-ai-kms)
        handler_kwargs = {
            "model": actual_model,
            "prompt": prompt,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stop": stop,
            "stream": stream,
            "project": model_params.get("project"),
            "location": model_params.get("location"),
        }
        
        # Add kms_key_name if present (for vertex-ai-kms handler)
        if model_params.get("kms_key_name"):
            handler_kwargs["kms_key_name"] = model_params.get("kms_key_name")
        
        response = await handler.text_completion(**handler_kwargs)
        
        if stream:
            return StreamingResponse(
                _rewrite_model_in_stream(response, model),
                media_type="text/event-stream",
            )
        else:
            # Update model name in response to match request
            response.model = model

            # Convert to dict to add SSH info
            response_dict = response.model_dump()

            # Add SSH verification info if available
            if user_api_key_dict.ssh_username:
                response_dict["user"] = user_api_key_dict.ssh_username
            if user_api_key_dict.ssh_warning:
                response_dict["warning"] = user_api_key_dict.ssh_warning

            return response_dict

    except Exception as e:
        verbose_proxy_logger.exception(f"Error in text completion: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


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
