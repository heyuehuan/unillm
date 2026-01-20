"""
SSH Key Signer for UniLLM Client

Provides utilities to sign API keys with SSH private keys for
enhanced authentication with UniLLM proxy.

Usage:
    from unillm.client import sign_api_key
    
    # Simple usage with defaults (uses system username and auto-detected SSH key)
    signed_key = sign_api_key("sk-your-api-key")
    
    # Custom key name and path
    signed_key = sign_api_key(
        "sk-your-api-key",
        key_name="my-custom-name",
        private_key_path="~/.ssh/id_ed25519"
    )
    
    # Use the signed key in requests
    print(signed_key.full_key)  # "sk-your-api-key||username||base64signature"
"""

import base64
import getpass
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ec, ed25519
from cryptography.hazmat.backends import default_backend


# Common SSH key file locations (in order of preference)
COMMON_SSH_KEY_PATHS = [
    "~/.ssh/id_ed25519",      # Ed25519 (recommended, most modern)
    "~/.ssh/id_ecdsa",        # ECDSA
    "~/.ssh/id_rsa",          # RSA
    "~/.ssh/id_dsa",          # DSA (legacy, not recommended)
    "~/.ssh/identity",        # SSH1 (very old)
]


@dataclass
class SSHKeyFile:
    """Information about a discovered SSH key file."""
    path: Path
    key_type: str
    modified_time: float
    
    @property
    def age_description(self) -> str:
        """Human-readable age of the key file."""
        import time
        age_seconds = time.time() - self.modified_time
        if age_seconds < 60:
            return "just now"
        elif age_seconds < 3600:
            return f"{int(age_seconds / 60)} minutes ago"
        elif age_seconds < 86400:
            return f"{int(age_seconds / 3600)} hours ago"
        else:
            return f"{int(age_seconds / 86400)} days ago"


@dataclass
class SignedAPIKey:
    """Result of signing an API key."""
    original_key: str
    key_name: str
    signature_b64: str
    private_key_path: str
    
    @property
    def full_key(self) -> str:
        """The full signed API key in the format: original||key_name||signature"""
        return f"{self.original_key}||{self.key_name}||{self.signature_b64}"
    
    def __str__(self) -> str:
        return self.full_key


def get_default_key_name() -> str:
    """
    Get the default key name based on system username.
    
    Returns:
        The current system username
    """
    return getpass.getuser()


def find_ssh_private_keys(search_paths: Optional[List[str]] = None) -> List[SSHKeyFile]:
    """
    Find SSH private keys in common locations.
    
    Args:
        search_paths: Optional list of paths to search. If None, uses COMMON_SSH_KEY_PATHS.
        
    Returns:
        List of SSHKeyFile objects, sorted by modification time (newest first)
    """
    if search_paths is None:
        search_paths = COMMON_SSH_KEY_PATHS
    
    found_keys: List[SSHKeyFile] = []
    
    for path_str in search_paths:
        path = Path(os.path.expanduser(path_str))
        
        if not path.exists():
            continue
        
        if not path.is_file():
            continue
        
        # Check if it's a private key (not .pub file)
        if path.suffix == ".pub":
            continue
        
        # Try to determine key type from filename
        key_type = _detect_key_type_from_path(path)
        
        # Get modification time
        try:
            stat_info = path.stat()
            modified_time = stat_info.st_mtime
        except OSError:
            continue
        
        found_keys.append(SSHKeyFile(
            path=path,
            key_type=key_type,
            modified_time=modified_time,
        ))
    
    # Sort by modification time (newest first)
    found_keys.sort(key=lambda k: k.modified_time, reverse=True)
    
    return found_keys


def _detect_key_type_from_path(path: Path) -> str:
    """Detect key type from file path/name."""
    name = path.name.lower()
    if "ed25519" in name:
        return "ed25519"
    elif "ecdsa" in name:
        return "ecdsa"
    elif "rsa" in name:
        return "rsa"
    elif "dsa" in name:
        return "dsa"
    else:
        return "unknown"


def _load_private_key(private_key_path: str, password: Optional[bytes] = None):
    """
    Load a private key from file.
    
    Args:
        private_key_path: Path to the private key file
        password: Optional password for encrypted keys
        
    Returns:
        Loaded private key object
    """
    path = Path(os.path.expanduser(private_key_path))
    
    if not path.exists():
        raise FileNotFoundError(f"Private key not found: {path}")
    
    with open(path, "rb") as f:
        key_data = f.read()
    
    # Try to load as OpenSSH format first
    try:
        private_key = serialization.load_ssh_private_key(
            key_data,
            password=password,
            backend=default_backend()
        )
        return private_key
    except Exception:
        pass
    
    # Try PEM format
    try:
        private_key = serialization.load_pem_private_key(
            key_data,
            password=password,
            backend=default_backend()
        )
        return private_key
    except Exception:
        pass
    
    raise ValueError(f"Unable to load private key from {path}. Unsupported format or wrong password.")


def _sign_message(private_key, message: bytes) -> bytes:
    """
    Sign a message with a private key.
    
    Args:
        private_key: The private key object
        message: The message to sign
        
    Returns:
        The signature bytes
    """
    if isinstance(private_key, rsa.RSAPrivateKey):
        # RSA signing
        signature = private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256()
        )
    elif isinstance(private_key, ec.EllipticCurvePrivateKey):
        # ECDSA signing
        signature = private_key.sign(
            message,
            ec.ECDSA(hashes.SHA256())
        )
    elif isinstance(private_key, ed25519.Ed25519PrivateKey):
        # Ed25519 signing
        signature = private_key.sign(message)
    else:
        raise ValueError(f"Unsupported private key type: {type(private_key)}")
    
    return signature


def sign_api_key(
    api_key: str,
    key_name: Optional[str] = None,
    private_key_path: Optional[str] = None,
    password: Optional[str] = None,
    verbose: bool = False,
) -> SignedAPIKey:
    """
    Sign an API key with an SSH private key.
    
    Args:
        api_key: The original API key to sign
        key_name: Optional key name to include. Defaults to system username.
        private_key_path: Optional path to private key. If None, searches common locations.
        password: Optional password for encrypted private keys
        verbose: If True, print information about key discovery
        
    Returns:
        SignedAPIKey object with the signed key
        
    Raises:
        FileNotFoundError: If no private key is found
        ValueError: If the key cannot be loaded or signing fails
        
    Example:
        >>> signed = sign_api_key("sk-my-api-key")
        >>> print(signed.full_key)
        'sk-my-api-key||johndoe||base64encodedSignature...'
    """
    # Determine key name
    if key_name is None:
        key_name = get_default_key_name()
        if verbose:
            print(f"Using default key name (system username): {key_name}")
    
    # Find or validate private key path
    if private_key_path is None:
        # Search for SSH keys
        found_keys = find_ssh_private_keys()
        
        if not found_keys:
            raise FileNotFoundError(
                "No SSH private keys found in common locations. "
                f"Searched: {', '.join(COMMON_SSH_KEY_PATHS)}. "
                "Please specify private_key_path explicitly."
            )
        
        # Use the newest key
        selected_key = found_keys[0]
        private_key_path = str(selected_key.path)
        
        if verbose:
            print(f"Found {len(found_keys)} SSH key(s):")
            for key in found_keys:
                marker = " (selected)" if key == selected_key else ""
                print(f"  - {key.path} [{key.key_type}] modified {key.age_description}{marker}")
            print(f"Using: {private_key_path}")
    else:
        # Expand user path
        private_key_path = os.path.expanduser(private_key_path)
        if verbose:
            print(f"Using specified private key: {private_key_path}")
    
    # Convert password to bytes if provided
    password_bytes = password.encode() if password else None
    
    # Load the private key
    try:
        private_key = _load_private_key(private_key_path, password_bytes)
    except Exception as e:
        raise ValueError(f"Failed to load private key: {e}")
    
    # Sign the API key
    try:
        signature = _sign_message(private_key, api_key.encode())
    except Exception as e:
        raise ValueError(f"Failed to sign API key: {e}")
    
    # Encode signature as base64
    signature_b64 = base64.b64encode(signature).decode()
    
    return SignedAPIKey(
        original_key=api_key,
        key_name=key_name,
        signature_b64=signature_b64,
        private_key_path=private_key_path,
    )


def main():
    """Command-line interface for signing API keys."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Sign an API key with your SSH private key for UniLLM authentication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sign with defaults (uses system username and auto-detected SSH key)
  python -m unillm.client.ssh_signer sk-your-api-key
  
  # Specify custom key name
  python -m unillm.client.ssh_signer sk-your-api-key --key-name mykey
  
  # Specify custom private key path
  python -m unillm.client.ssh_signer sk-your-api-key --private-key ~/.ssh/my_key
  
  # List available SSH keys without signing
  python -m unillm.client.ssh_signer --list-keys
"""
    )
    
    parser.add_argument(
        "api_key",
        nargs="?",
        help="The API key to sign"
    )
    parser.add_argument(
        "--key-name", "-n",
        help="Key name to use in the signed key (default: system username)"
    )
    parser.add_argument(
        "--private-key", "-k",
        help="Path to SSH private key (default: auto-detect from common locations)"
    )
    parser.add_argument(
        "--password", "-p",
        help="Password for encrypted private key"
    )
    parser.add_argument(
        "--list-keys", "-l",
        action="store_true",
        help="List available SSH keys and exit"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show verbose output"
    )
    parser.add_argument(
        "--output", "-o",
        choices=["full", "signature", "json"],
        default="full",
        help="Output format (default: full)"
    )
    
    args = parser.parse_args()
    
    # Handle --list-keys
    if args.list_keys:
        found_keys = find_ssh_private_keys()
        if not found_keys:
            print("No SSH private keys found in common locations:")
            for path in COMMON_SSH_KEY_PATHS:
                print(f"  - {path}")
        else:
            print(f"Found {len(found_keys)} SSH private key(s):")
            for i, key in enumerate(found_keys):
                newest = " (newest)" if i == 0 else ""
                print(f"  {key.path}")
                print(f"    Type: {key.key_type}, Modified: {key.age_description}{newest}")
        return
    
    # Require api_key if not listing keys
    if not args.api_key:
        parser.error("api_key is required unless --list-keys is specified")
    
    try:
        signed = sign_api_key(
            api_key=args.api_key,
            key_name=args.key_name,
            private_key_path=args.private_key,
            password=args.password,
            verbose=args.verbose,
        )
        
        if args.output == "full":
            print(signed.full_key)
        elif args.output == "signature":
            print(signed.signature_b64)
        elif args.output == "json":
            import json
            print(json.dumps({
                "original_key": signed.original_key,
                "key_name": signed.key_name,
                "signature": signed.signature_b64,
                "private_key_path": signed.private_key_path,
                "full_key": signed.full_key,
            }, indent=2))
            
    except FileNotFoundError as e:
        print(f"Error: {e}", file=__import__("sys").stderr)
        exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=__import__("sys").stderr)
        exit(1)


if __name__ == "__main__":
    main()
