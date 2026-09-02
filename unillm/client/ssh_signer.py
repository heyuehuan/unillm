"""
SSH Key Signer for UniLLM Client

Provides utilities to sign API keys with SSH private keys for
enhanced authentication with UniLLM proxy.

Usage:
    from unillm.client import sign_api_key

    # Simple usage with defaults (key name '<system-username>--1', auto-detected SSH key)
    signed_key = sign_api_key("sk-your-api-key")

    # Custom key name and path. UniLLM registers keys as
    # '<unillm-username>--<suffix>', so pass key_name explicitly when your
    # UniLLM username differs from your system username or you registered a
    # different suffix.
    signed_key = sign_api_key(
        "sk-your-api-key",
        key_name="myuser--laptop",
        private_key_path="~/.ssh/id_ed25519"
    )

    # Use the signed key in requests
    print(signed_key.full_key)  # "sk-your-api-key||myuser--laptop||base64signature"
"""

import base64
import getpass
import os
import stat
import sys
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
    Get the default key name: '<system-username>--1'.

    UniLLM only registers key names of the form '<unillm-username>--<suffix>',
    so a bare username could never match a registered key. The default assumes
    your UniLLM username equals your system username and you registered the
    suffix '1' (the UI's default); pass key_name explicitly otherwise.
    """
    return f"{getpass.getuser()}--1"


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


# Marker for "--password was given with no value", meaning "ask me".
_PROMPT = object()


def _resolve_password(args, parser) -> Optional[str]:
    """Work out the private key password without it ever appearing in argv."""
    if args.password_env:
        value = os.environ.get(args.password_env)
        if value is None:
            parser.error(f"environment variable {args.password_env} is not set")
        return value
    if args.password is _PROMPT:
        return getpass.getpass("Private key password: ")
    if args.password is not None:
        print("Warning: a password passed on the command line is visible in 'ps' and "
              "your shell history. Use --password with no value, or --password-env.",
              file=sys.stderr)
    return args.password


def sign_api_key(
    api_key: str,
    key_name: Optional[str] = None,
    private_key_path: Optional[str] = None,
    password: Optional[str] = None,
    verbose: bool = False,
    prompt_for_password: bool = False,
) -> SignedAPIKey:
    """
    Sign an API key with an SSH private key.
    
    Args:
        api_key: The original API key to sign
        key_name: Optional key name to include. Defaults to '<system-username>--1'
            (UniLLM key names are always '<unillm-username>--<suffix>').
        private_key_path: Optional path to private key. If None, searches common locations.
        password: Optional password for encrypted private keys
        verbose: If True, print information about key discovery
        prompt_for_password: If True, ask for the password on the terminal when the
            key turns out to be encrypted and no password was supplied
        
    Returns:
        SignedAPIKey object with the signed key
        
    Raises:
        FileNotFoundError: If no private key is found
        ValueError: If the key cannot be loaded or signing fails
        
    Example:
        >>> signed = sign_api_key("sk-my-api-key")
        >>> print(signed.full_key)
        'sk-my-api-key||johndoe--1||base64encodedSignature...'
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
        # An encrypted key with no password reaches here. Asking for it on the
        # terminal keeps the passphrase out of the process list and the shell
        # history, which is the whole reason not to pass it as an argument.
        if (prompt_for_password and password_bytes is None
                and not isinstance(e, FileNotFoundError)):
            entered = getpass.getpass(f"Password for {private_key_path}: ")
            try:
                private_key = _load_private_key(private_key_path, entered.encode() or None)
            except Exception as retry_error:
                raise ValueError(f"Failed to load private key: {retry_error}")
        else:
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


def _build_parser():
    """The command-line interface, split out so tests can parse argv directly."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Sign an API key with your SSH private key for UniLLM authentication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sign with defaults (uses system username and auto-detected SSH key)
  python -m unillm.client.ssh_signer sk-your-api-key
  
  # Specify custom key name (UniLLM key names are '<username>--<suffix>')
  python -m unillm.client.ssh_signer sk-your-api-key --key-name myuser--laptop
  
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
        help="Key name to use in the signed key, format '<username>--<suffix>' "
             "(default: '<system-username>--1')"
    )
    parser.add_argument(
        "--private-key", "-k",
        help="Path to SSH private key (default: auto-detect from common locations)"
    )
    parser.add_argument(
        "--password", "-p",
        nargs="?",
        const=_PROMPT,
        default=None,
        metavar="PASSWORD",
        help="Password for an encrypted private key. Pass the flag with no value to "
             "be prompted instead — a password given on the command line is visible "
             "to every process on the machine via 'ps' and is saved in your shell "
             "history."
    )
    parser.add_argument(
        "--password-env",
        metavar="VAR",
        help="Read the private key password from this environment variable"
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
    
    return parser


def main():
    """Command-line interface for signing API keys."""
    parser = _build_parser()
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
            password=_resolve_password(args, parser),
            verbose=args.verbose,
            prompt_for_password=True,
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
        print(f"Error: {e}", file=sys.stderr)
        exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        exit(1)


if __name__ == "__main__":
    main()
