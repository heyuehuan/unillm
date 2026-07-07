"""
Central resolution of security-sensitive secrets.

Keeping this in one place avoids two problems the codebase used to have:

  1. A hardcoded default JWT secret ("change-me-in-production") that let anyone
     forge admin tokens if the env var was left unset.
  2. The API-key encryption key being silently derived from that same default.

Both the JWT signing secret and the data-encryption key are resolved here so the
proxy and the CRUD layer agree on the value within a single process.
"""

import base64
import hashlib
import os
import secrets as _secrets
from typing import Optional

from unillm._logging import verbose_proxy_logger

# The old insecure default. Treated as "unset" so we never sign or encrypt with it.
_INSECURE_DEFAULT = "change-me-in-production"

_jwt_secret_cache: Optional[str] = None


def get_jwt_secret() -> str:
    """
    Return the JWT signing secret.

    If UNILLM_JWT_SECRET is unset (or left at the insecure default) we generate a
    strong random ephemeral secret and warn loudly, rather than falling back to a
    well-known constant. Ephemeral secrets invalidate all sessions on restart and
    make previously-encrypted API keys unrecoverable — which is the point: it fails
    safe instead of silently accepting forged tokens.
    """
    global _jwt_secret_cache
    if _jwt_secret_cache is None:
        secret = os.getenv("UNILLM_JWT_SECRET", "").strip()
        if not secret or secret == _INSECURE_DEFAULT:
            secret = _secrets.token_urlsafe(48)
            verbose_proxy_logger.warning(
                "UNILLM_JWT_SECRET is not set (or is the insecure default). A random "
                "ephemeral secret was generated: existing sessions are invalid, tokens "
                "will not survive a restart, and API keys encrypted under a previous "
                "secret cannot be revealed. Set a strong, stable UNILLM_JWT_SECRET in "
                "production."
            )
        _jwt_secret_cache = secret
    return _jwt_secret_cache


def get_fernet_key() -> bytes:
    """
    Return a urlsafe-base64 Fernet key for API-key-at-rest encryption.

    Prefers a dedicated UNILLM_ENCRYPTION_KEY (a real Fernet key). Falls back to a
    key derived from the JWT secret with domain separation so the two uses are never
    the literally-identical key material.
    """
    key = os.getenv("UNILLM_ENCRYPTION_KEY", "").strip()
    if key:
        return key.encode()
    raw = hashlib.sha256(b"unillm-fernet-v1:" + get_jwt_secret().encode()).digest()
    return base64.urlsafe_b64encode(raw)


def reset_caches() -> None:
    """Clear cached secrets (test helper)."""
    global _jwt_secret_cache
    _jwt_secret_cache = None
