"""
SSH Key Authentication Module for UniLLM

Provides signature verification for API keys signed with SSH private keys.

API Key Format: original-api-key||key-name||base64-signature

The signature is created by signing the original API key with an SSH private key.
Verification is done using the registered public key.

Configuration via environment variables:
  UNILLM_SSH_KEYS="key_name1:public_key_value1:username1,key_name2:public_key_value2:username2"

Or via .env file with the same format.
"""

import base64
import os
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ec, ed25519
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature

from unillm._logging import verbose_proxy_logger
from unillm.types import SSHKeyInfo, SSHVerificationResult


# SSH key modes
SSH_MODE_NONE = "none"
SSH_MODE_WARNING = "warning"
SSH_MODE_ENFORCE = "enforce"

# Global registered SSH keys cache
_ssh_keys_cache: Optional[Dict[str, SSHKeyInfo]] = None


def get_ssh_mode(general_settings: Dict) -> str:
    """Get the SSH verification mode from general settings."""
    mode = general_settings.get("ssh_required", SSH_MODE_NONE)
    if mode not in (SSH_MODE_NONE, SSH_MODE_WARNING, SSH_MODE_ENFORCE):
        verbose_proxy_logger.warning(
            f"Invalid ssh_required value: {mode}. Using 'none'."
        )
        return SSH_MODE_NONE
    return mode


def load_ssh_keys() -> Dict[str, SSHKeyInfo]:
    """
    Load SSH public keys from environment variable.
    
    Format: UNILLM_SSH_KEYS="key_name1:public_key_value1:username1,key_name2:public_key_value2:username2"
    
    Returns:
        Dictionary mapping key_name to SSHKeyInfo
    """
    global _ssh_keys_cache
    
    if _ssh_keys_cache is not None:
        return _ssh_keys_cache
    
    _ssh_keys_cache = {}
    
    ssh_keys_str = os.getenv("UNILLM_SSH_KEYS", "")
    if not ssh_keys_str:
        verbose_proxy_logger.debug("No SSH keys configured (UNILLM_SSH_KEYS not set)")
        return _ssh_keys_cache
    
    # Parse key entries (comma-separated)
    key_entries = ssh_keys_str.split(",")
    
    for entry in key_entries:
        entry = entry.strip()
        if not entry:
            continue
        
        # Parse key_name:public_key:username
        parts = entry.split(":", 2)
        if len(parts) != 3:
            verbose_proxy_logger.warning(
                f"Invalid SSH key entry format: {entry[:50]}... Expected 'key_name:public_key:username'"
            )
            continue
        
        key_name, public_key, username = parts
        key_name = key_name.strip()
        public_key = public_key.strip()
        username = username.strip()
        
        if not all([key_name, public_key, username]):
            verbose_proxy_logger.warning(
                f"Empty values in SSH key entry: key_name={key_name}, username={username}"
            )
            continue
        
        _ssh_keys_cache[key_name] = SSHKeyInfo(
            key_name=key_name,
            public_key=public_key,
            username=username,
        )
        verbose_proxy_logger.info(f"Registered SSH key: {key_name} for user: {username}")
    
    verbose_proxy_logger.info(f"Loaded {len(_ssh_keys_cache)} SSH public keys")
    return _ssh_keys_cache


def clear_ssh_keys_cache():
    """Clear the SSH keys cache (useful for testing or reloading)."""
    global _ssh_keys_cache
    _ssh_keys_cache = None


def parse_ssh_api_key(api_key: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Parse an API key that may contain SSH signature.
    
    Format: original-api-key||key-name||base64-signature
    
    Args:
        api_key: The full API key string
        
    Returns:
        Tuple of (original_api_key, key_name, signature_b64)
        If no SSH signature present, key_name and signature_b64 will be None
    """
    if "||" not in api_key:
        return api_key, None, None
    
    parts = api_key.split("||")
    if len(parts) != 3:
        verbose_proxy_logger.debug(
            f"API key has || but not 3 parts (got {len(parts)}), treating as regular key"
        )
        return api_key, None, None
    
    original_key, key_name, signature_b64 = parts
    return original_key.strip(), key_name.strip(), signature_b64.strip()


def _load_public_key(public_key_str: str):
    """
    Load a public key from string (supports OpenSSH format).
    
    Args:
        public_key_str: Public key in OpenSSH format (ssh-rsa ..., ssh-ed25519 ..., etc.)
        
    Returns:
        Loaded public key object
    """
    # Try OpenSSH format first
    try:
        public_key = serialization.load_ssh_public_key(
            public_key_str.encode(),
            backend=default_backend()
        )
        return public_key
    except Exception as e:
        verbose_proxy_logger.debug(f"Failed to load as OpenSSH key: {e}")
    
    # Try PEM format
    try:
        public_key = serialization.load_pem_public_key(
            public_key_str.encode(),
            backend=default_backend()
        )
        return public_key
    except Exception as e:
        verbose_proxy_logger.debug(f"Failed to load as PEM key: {e}")
    
    raise ValueError(f"Unable to load public key - unsupported format")


def verify_ssh_signature(
    original_api_key: str,
    signature_b64: str,
    public_key_str: str,
) -> bool:
    """
    Verify that the signature was created by signing the original API key
    with the corresponding private key.
    
    Args:
        original_api_key: The original API key that was signed
        signature_b64: Base64-encoded signature
        public_key_str: Public key in OpenSSH or PEM format
        
    Returns:
        True if signature is valid, False otherwise
    """
    try:
        # Decode signature
        signature = base64.b64decode(signature_b64)
        
        # Load public key
        public_key = _load_public_key(public_key_str)
        
        # Verify signature based on key type
        message = original_api_key.encode()
        
        if isinstance(public_key, rsa.RSAPublicKey):
            # RSA verification
            public_key.verify(
                signature,
                message,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            return True
        
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            # ECDSA verification
            public_key.verify(
                signature,
                message,
                ec.ECDSA(hashes.SHA256())
            )
            return True
        
        elif isinstance(public_key, ed25519.Ed25519PublicKey):
            # Ed25519 verification
            public_key.verify(signature, message)
            return True
        
        else:
            verbose_proxy_logger.warning(f"Unsupported key type: {type(public_key)}")
            return False
            
    except InvalidSignature:
        verbose_proxy_logger.debug("SSH signature verification failed: invalid signature")
        return False
    except Exception as e:
        verbose_proxy_logger.warning(f"SSH signature verification error: {e}")
        return False


def verify_api_key_ssh(
    api_key: str,
    ssh_mode: str,
) -> SSHVerificationResult:
    """
    Verify an API key's SSH signature.
    
    Args:
        api_key: The full API key (may contain SSH signature)
        ssh_mode: The SSH verification mode (none, warning, enforce)
        
    Returns:
        SSHVerificationResult with verification status and details
    """
    # If SSH is disabled, return default result
    if ssh_mode == SSH_MODE_NONE:
        return SSHVerificationResult(verified=True)
    
    # Parse the API key
    original_key, key_name, signature_b64 = parse_ssh_api_key(api_key)
    
    # If no SSH signature provided
    if key_name is None or signature_b64 is None:
        verbose_proxy_logger.debug(
            f"SSH verification: No signature provided in API key"
        )
        if ssh_mode == SSH_MODE_ENFORCE:
            return SSHVerificationResult(
                verified=False,
                error="SSH key signature required. API key format: original-api-key||key-name||signature"
            )
        else:  # warning mode
            return SSHVerificationResult(
                verified=False,
                warning="No SSH key signature provided. Consider signing your API key for enhanced security."
            )
    
    # Load registered SSH keys
    ssh_keys = load_ssh_keys()
    
    # Check if key_name is registered
    if key_name not in ssh_keys:
        # Detailed backend logging - includes key_name for debugging
        verbose_proxy_logger.warning(
            f"SSH verification failed: key-name '{key_name}' is not registered. "
            f"Registered keys: {list(ssh_keys.keys()) if ssh_keys else 'none'}"
        )
        # Generic user-facing error - don't expose whether key exists
        generic_error = "SSH signature verification failed. Please check your credentials."
        generic_warning = "SSH signature could not be verified. Please check your credentials."
        if ssh_mode == SSH_MODE_ENFORCE:
            return SSHVerificationResult(
                verified=False,
                key_name=key_name,
                error=generic_error
            )
        else:  # warning mode
            return SSHVerificationResult(
                verified=False,
                key_name=key_name,
                warning=generic_warning
            )
    
    # Get the registered key info
    key_info = ssh_keys[key_name]
    
    # Verify the signature
    if verify_ssh_signature(original_key, signature_b64, key_info.public_key):
        verbose_proxy_logger.info(
            f"SSH signature verified for key '{key_name}', user: {key_info.username}"
        )
        return SSHVerificationResult(
            verified=True,
            username=key_info.username,
            key_name=key_name,
        )
    else:
        # Detailed backend logging - signature mismatch for registered key
        verbose_proxy_logger.warning(
            f"SSH verification failed: key-name '{key_name}' is registered but signature is invalid. "
            f"Expected user: {key_info.username}. Possible key mismatch or tampering."
        )
        # Generic user-facing error - don't reveal that key was recognized
        generic_error = "SSH signature verification failed. Please check your credentials."
        generic_warning = "SSH signature could not be verified. Please check your credentials."
        if ssh_mode == SSH_MODE_ENFORCE:
            return SSHVerificationResult(
                verified=False,
                key_name=key_name,
                error=generic_error
            )
        else:  # warning mode
            return SSHVerificationResult(
                verified=False,
                key_name=key_name,
                warning=generic_warning
            )


def get_original_api_key(api_key: str) -> str:
    """
    Extract the original API key from a potentially SSH-signed key.
    
    Args:
        api_key: The full API key (may contain SSH signature)
        
    Returns:
        The original API key without SSH signature parts
    """
    original_key, _, _ = parse_ssh_api_key(api_key)
    return original_key
