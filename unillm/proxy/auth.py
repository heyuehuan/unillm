"""
Authentication module for UniLLM

Uses environment variable UNILLM_API_KEYS to store allowed API keys.
Format: comma-separated list of keys, e.g., "sk-key1,sk-key2,sk-key3"
"""

import os
from typing import Optional
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from unillm._logging import verbose_proxy_logger
from unillm.types import UserAPIKeyAuth


# Security scheme
security = HTTPBearer(auto_error=False)


def get_allowed_keys() -> set:
    """
    Get the set of allowed API keys from environment variables.
    
    Supports both UNILLM_API_KEYS (comma-separated list) and 
    UNILLM_MASTER_KEY (single master key).
    """
    allowed_keys = set()
    
    # Get master key
    master_key = os.getenv("UNILLM_MASTER_KEY", "")
    if master_key:
        allowed_keys.add(master_key)
    
    # Also support LITELLM_MASTER_KEY for backward compatibility
    litellm_master_key = os.getenv("LITELLM_MASTER_KEY", "")
    if litellm_master_key:
        allowed_keys.add(litellm_master_key)
    
    # Get additional API keys (comma-separated)
    api_keys_str = os.getenv("UNILLM_API_KEYS", "")
    if api_keys_str:
        keys = [k.strip() for k in api_keys_str.split(",") if k.strip()]
        allowed_keys.update(keys)
    
    return allowed_keys


async def user_api_key_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> UserAPIKeyAuth:
    """
    Authenticate the user based on their API key.
    
    The API key can be provided via:
    1. Authorization: Bearer <api_key> header
    2. x-api-key header
    """
    api_key: Optional[str] = None
    
    # Try to get the API key from Bearer token
    if credentials is not None:
        api_key = credentials.credentials
    
    # Fall back to x-api-key header
    if api_key is None:
        api_key = request.headers.get("x-api-key")
    
    # Check if API key is provided
    if api_key is None:
        verbose_proxy_logger.warning("No API key provided in request")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Provide via 'Authorization: Bearer <key>' or 'x-api-key' header.",
        )
    
    # Get allowed keys
    allowed_keys = get_allowed_keys()
    
    # If no keys are configured, allow all requests (development mode)
    if not allowed_keys:
        verbose_proxy_logger.warning("No API keys configured - allowing all requests (development mode)")
        return UserAPIKeyAuth(api_key=api_key, valid=True)
    
    # Validate the API key
    if api_key not in allowed_keys:
        verbose_proxy_logger.warning(f"Invalid API key provided: {api_key[:8]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    
    verbose_proxy_logger.debug(f"API key authenticated: {api_key[:8]}...")
    return UserAPIKeyAuth(api_key=api_key, valid=True)
