"""
An SSH signature must say what it is for.

The old format signed the API key's raw bytes, so any tool that will sign an
opaque blob with the same key could be used to mint a UniLLM signature. Signatures
now cover a protocol-specific prefix. Keys already in people's config files keep
working, so the server accepts both forms.
"""

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from unillm.client import ssh_signer
from unillm.proxy import ssh_auth


API_KEY = "sk-test-key"


@pytest.fixture
def keypair(tmp_path):
    private = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / "id_ed25519"
    path.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return private, str(path), public


def _sig(private, message: bytes) -> str:
    return base64.b64encode(private.sign(message)).decode()


def test_the_signer_covers_the_protocol_prefix_by_default(keypair):
    private, path, public = keypair
    signed = ssh_signer.sign_api_key(API_KEY, key_name="alice--1", private_key_path=path)
    expected = _sig(private, ssh_auth.SSH_SIGNATURE_DOMAIN + API_KEY.encode())
    assert signed.signature_b64 == expected
    assert ssh_auth.verify_ssh_signature(API_KEY, signed.signature_b64, public)


def test_signatures_already_in_the_wild_still_verify(keypair):
    private, _, public = keypair
    assert ssh_auth.verify_ssh_signature(API_KEY, _sig(private, API_KEY.encode()), public)


def test_the_legacy_flag_reproduces_the_old_bytes(keypair):
    private, path, public = keypair
    signed = ssh_signer.sign_api_key(API_KEY, key_name="alice--1", private_key_path=path,
                                     legacy_signature=True)
    assert signed.signature_b64 == _sig(private, API_KEY.encode())
    assert ssh_auth.verify_ssh_signature(API_KEY, signed.signature_b64, public)


def test_a_signature_over_something_else_is_still_rejected(keypair):
    private, _, public = keypair
    for message in (b"", b"some-other-protocol\x00" + API_KEY.encode(),
                    ssh_auth.SSH_SIGNATURE_DOMAIN + b"sk-a-different-key"):
        assert not ssh_auth.verify_ssh_signature(API_KEY, _sig(private, message), public)


def test_the_signer_and_the_server_agree_on_the_prefix():
    assert ssh_signer.SIGNATURE_DOMAIN == ssh_auth.SSH_SIGNATURE_DOMAIN
