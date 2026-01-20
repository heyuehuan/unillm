"""
Authentication module for UniLLM

Uses environment variable UNILLM_API_KEYS to store allowed API keys.
Format: comma-separated list of keys, e.g., "sk-key1,sk-key2,sk-key3"

SSH Key Authentication:
When ssh_required is set to 'enforce' or 'warning' in general_settings,
API keys can include SSH signatures for enhanced security.
Format: original-api-key||key-name||base64-signature
"""

import os
from typing import Dict, Optional
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from unillm._logging import verbose_proxy_logger
from unillm.types import UserAPIKeyAuth
from unillm.proxy.ssh_auth import (
    get_ssh_mode,
    verify_api_key_ssh,
    get_original_api_key,
    SSH_MODE_NONE,
    SSH_MODE_ENFORCE,
)


# Security scheme
security = HTTPBearer(auto_error=False)

# Global reference to general_settings (set by proxy_server)
_general_settings: Dict = {}


def set_general_settings(settings: Dict):
    """Set the general settings reference for SSH mode lookup."""
    global _general_settings
    _general_settings = settings


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
    
    # Get SSH verification mode
    ssh_mode = get_ssh_mode(_general_settings)
    
    # Extract original API key (without SSH signature parts) for validation
    original_api_key = get_original_api_key(api_key)
    
    # Get allowed keys
    allowed_keys = get_allowed_keys()
    
    # If no keys are configured, allow all requests (development mode)
    if not allowed_keys:
        verbose_proxy_logger.warning("No API keys configured - allowing all requests (development mode)")
        # Still perform SSH verification if enabled
        if ssh_mode != SSH_MODE_NONE:
            ssh_result = verify_api_key_ssh(api_key, ssh_mode)
            if ssh_mode == SSH_MODE_ENFORCE and not ssh_result.verified:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=ssh_result.error or "SSH key verification failed",
                )
            return UserAPIKeyAuth(
                api_key=original_api_key,
                valid=True,
                ssh_verified=ssh_result.verified,
                ssh_username=ssh_result.username,
                ssh_key_name=ssh_result.key_name,
                ssh_warning=ssh_result.warning,
            )
        return UserAPIKeyAuth(api_key=original_api_key, valid=True)
    
    # Validate the original API key (without SSH signature)
    if original_api_key not in allowed_keys:
        verbose_proxy_logger.warning(f"Invalid API key provided: {original_api_key[:8]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    
    # Perform SSH verification if enabled
    if ssh_mode != SSH_MODE_NONE:
        ssh_result = verify_api_key_ssh(api_key, ssh_mode)
        
        # In enforce mode, reject if SSH verification fails
        if ssh_mode == SSH_MODE_ENFORCE and not ssh_result.verified:
            verbose_proxy_logger.warning(
                f"SSH verification failed for key {original_api_key[:8]}...: {ssh_result.error}"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ssh_result.error or "SSH key verification failed",
            )
        
        verbose_proxy_logger.debug(
            f"API key authenticated: {original_api_key[:8]}... "
            f"SSH verified: {ssh_result.verified}, user: {ssh_result.username}"
        )
        return UserAPIKeyAuth(
            api_key=original_api_key,
            valid=True,
            user_id=ssh_result.username,
            ssh_verified=ssh_result.verified,
            ssh_username=ssh_result.username,
            ssh_key_name=ssh_result.key_name,
            ssh_warning=ssh_result.warning,
        )
    
    verbose_proxy_logger.debug(f"API key authenticated: {original_api_key[:8]}...")
    return UserAPIKeyAuth(api_key=original_api_key, valid=True)
