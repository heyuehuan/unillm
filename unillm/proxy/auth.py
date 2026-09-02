"""
Authentication module for UniLLM

Auth order:
  1. DB API key lookup (key_hash match)
  2. Env var fallback (UNILLM_MASTER_KEY / UNILLM_API_KEYS)
  3. Reject — or allow-all if UNILLM_DEV_MODE=true is explicitly set
     (dev mode is disabled automatically when the database is in use, i.e.
     contains any user or API key)

SSH verification runs on top of key auth when ssh_required is configured.
"""

import hashlib
import hmac
import os
from typing import Dict, List, Optional

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from unillm._logging import verbose_proxy_logger
from unillm.db import get_db
from unillm.db import crud
from unillm.types import UserAPIKeyAuth
from unillm.proxy.ssh_auth import (
    get_ssh_mode,
    verify_api_key_ssh,
    get_original_api_key,
    SSH_MODE_NONE,
    SSH_MODE_ENFORCE,
)


security = HTTPBearer(auto_error=False)

_general_settings: Dict = {}


def set_general_settings(settings: Dict):
    global _general_settings
    _general_settings = settings


def _is_dev_mode_allowed(db: Optional[Session] = None) -> bool:
    """
    Dev mode (allow-all when no keys configured) requires UNILLM_DEV_MODE=true
    to be explicitly set. It is never active when the database is actually in
    use: the default deployment runs on SQLite without DATABASE_URL being set,
    so the env-var check alone would let an invalid key fall through to
    allow-all on a live instance full of real users and keys.
    """
    if os.getenv("UNILLM_DEV_MODE", "").lower() != "true":
        return False
    if os.getenv("DATABASE_URL"):
        return False
    if db is not None:
        try:
            from unillm.db.models import APIKey, User
            if db.query(User.id).first() is not None or db.query(APIKey.id).first() is not None:
                return False
        except Exception:
            # DB unreachable/uninitialized — treat it as absent.
            pass
    return True


def get_env_allowed_keys() -> set:
    allowed = set()
    for var in ("UNILLM_MASTER_KEY", "LITELLM_MASTER_KEY"):
        val = os.getenv(var, "")
        if val:
            allowed.add(val)
    api_keys_str = os.getenv("UNILLM_API_KEYS", "")
    if api_keys_str:
        allowed.update(k.strip() for k in api_keys_str.split(",") if k.strip())
    return allowed


def _matches_any_env_key(candidate: str, allowed: set) -> bool:
    """
    Compare a presented key against the configured ones without leaking timing.

    `candidate in allowed` hashes the string and compares byte by byte, bailing at
    the first difference, which lets an attacker who can time the response recover
    the key one character at a time. compare_digest takes the same time whatever
    the input, and the loop deliberately does not stop early so the answer does not
    depend on which key matched.
    """
    matched = False
    for key in allowed:
        matched |= hmac.compare_digest(candidate, key)
    return matched


def _key_fingerprint(api_key: str) -> str:
    """
    A short, non-reversible tag for a key, safe to write to a log.

    The leading characters of a rejected key used to go into the log at WARNING.
    That is a fragment of somebody's secret sitting in a file with much weaker
    access control than the key store, and an attacker who can read logs gets a
    head start on the rest. A hash identifies the key across log lines without
    carrying any of it.
    """
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def _check_model_access(allowed_models: Optional[List[str]], requested_model: str) -> bool:
    """
    Check if the requested model is permitted by the API key.
    - None (env-var key): no restriction
    - ["all"]: no restriction
    - []: no models allowed
    - ["model-a", ...]: only listed models
    """
    if allowed_models is None:
        return True
    if "all" in allowed_models:
        return True
    return requested_model in allowed_models


async def user_api_key_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
    db: Session = Depends(get_db),
) -> UserAPIKeyAuth:
    """
    Authenticate the request. Resolves project, key name, and SSH identity.
    Model access is enforced in the endpoint after the requested model is known.
    """
    api_key: Optional[str] = None

    if credentials is not None:
        api_key = credentials.credentials
    if api_key is None:
        api_key = request.headers.get("x-api-key")

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Provide via 'Authorization: Bearer <key>' or 'x-api-key' header.",
        )

    ssh_mode = get_ssh_mode(_general_settings)
    original_api_key = get_original_api_key(api_key)

    # --- 1. DB lookup ---
    db_key = crud.get_api_key_by_value(db, original_api_key)
    if db_key:
        owner = crud.get_project_owner(db, db_key.project_id)
        if owner is not None and not owner.active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account has been disabled",
            )
        project = crud.get_project_by_id(db, db_key.project_id)
        if project is not None and project.archived:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This project has been archived",
            )
        crud.touch_api_key(db, db_key)
        auth_result = UserAPIKeyAuth(
            api_key=original_api_key,
            valid=True,
            project_id=db_key.project_id,
            api_key_name=db_key.name,
            allowed_models=db_key.allowed_models,
        )
        return await _apply_ssh(api_key, ssh_mode, auth_result, db)

    # --- 2. Env var fallback ---
    env_keys = get_env_allowed_keys()
    if env_keys:
        if not _matches_any_env_key(original_api_key, env_keys):
            verbose_proxy_logger.warning(
                f"Invalid API key (fingerprint {_key_fingerprint(original_api_key)})"
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        verbose_proxy_logger.debug(
            f"API key authenticated via env var (fingerprint {_key_fingerprint(original_api_key)})"
        )
        auth_result = UserAPIKeyAuth(api_key=original_api_key, valid=True)
        return await _apply_ssh(api_key, ssh_mode, auth_result, db)

    # --- 3. Dev mode (explicit opt-in only) ---
    if _is_dev_mode_allowed(db):
        verbose_proxy_logger.warning("UNILLM_DEV_MODE=true — allowing unauthenticated request")
        auth_result = UserAPIKeyAuth(api_key=original_api_key, valid=True)
        return await _apply_ssh(api_key, ssh_mode, auth_result, db)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )


async def _apply_ssh(
    api_key: str,
    ssh_mode: str,
    auth_result: UserAPIKeyAuth,
    db: Session,
) -> UserAPIKeyAuth:
    """Run SSH verification on top of an already-authenticated key."""
    if ssh_mode == SSH_MODE_NONE:
        return auth_result

    ssh_result = verify_api_key_ssh(api_key, ssh_mode, db=db)

    if ssh_mode == SSH_MODE_ENFORCE and not ssh_result.verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ssh_result.error or "SSH key verification failed",
        )

    auth_result.ssh_verified = ssh_result.verified
    auth_result.ssh_username = ssh_result.username
    auth_result.ssh_key_name = ssh_result.key_name
    auth_result.ssh_warning = ssh_result.warning
    if ssh_result.username:
        auth_result.user_id = ssh_result.username
    if ssh_result.verified and ssh_result.key_name and ssh_result.username:
        try:
            from unillm.db.crud import touch_ssh_key_by_name
            touch_ssh_key_by_name(db, ssh_result.key_name, ssh_result.username)
        except Exception:
            pass
    return auth_result


def enforce_model_access(auth: UserAPIKeyAuth, requested_model: str):
    """Call this in endpoints after the model name is known."""
    if not _check_model_access(auth.allowed_models, requested_model):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key does not have access to model '{requested_model}'",
        )
