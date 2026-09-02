"""
The signer must not require a passphrase on the command line.

Anything in argv is readable by every user on the machine through `ps` and is
written to shell history, so an encrypted key's passphrase has to reach the
signer some other way: a prompt, or an environment variable.
"""

import argparse
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from unillm.client import ssh_signer


PASSPHRASE = "correct horse"


@pytest.fixture
def encrypted_key(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / "id_ed25519"
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.BestAvailableEncryption(PASSPHRASE.encode()),
    ))
    return str(path)


def _parsed(argv):
    parser = ssh_signer._build_parser()
    return parser, parser.parse_args(argv)


def test_password_flag_with_no_value_asks_at_the_terminal(monkeypatch):
    monkeypatch.setattr(ssh_signer.getpass, "getpass", lambda *a, **k: PASSPHRASE)
    parser, args = _parsed(["sk-test", "--password"])
    assert ssh_signer._resolve_password(args, parser) == PASSPHRASE


def test_password_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MY_KEY_PASS", PASSPHRASE)
    parser, args = _parsed(["sk-test", "--password-env", "MY_KEY_PASS"])
    assert ssh_signer._resolve_password(args, parser) == PASSPHRASE


def test_an_unset_password_variable_is_an_error_not_an_empty_password(monkeypatch):
    monkeypatch.delenv("MY_KEY_PASS", raising=False)
    parser, args = _parsed(["sk-test", "--password-env", "MY_KEY_PASS"])
    with pytest.raises(SystemExit):
        ssh_signer._resolve_password(args, parser)


def test_a_literal_password_still_works_but_warns(capsys):
    parser, args = _parsed(["sk-test", "--password", PASSPHRASE])
    assert ssh_signer._resolve_password(args, parser) == PASSPHRASE
    assert "visible in 'ps'" in capsys.readouterr().err


def test_no_password_flag_means_no_password_and_no_prompt(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("should not prompt when --password was not given")
    monkeypatch.setattr(ssh_signer.getpass, "getpass", refuse)
    parser, args = _parsed(["sk-test"])
    assert ssh_signer._resolve_password(args, parser) is None


def test_an_encrypted_key_is_unlocked_by_prompting(monkeypatch, encrypted_key):
    monkeypatch.setattr(ssh_signer.getpass, "getpass", lambda *a, **k: PASSPHRASE)
    signed = ssh_signer.sign_api_key(
        "sk-test", key_name="alice--1", private_key_path=encrypted_key,
        prompt_for_password=True,
    )
    assert signed.full_key.startswith("sk-test||alice--1||")


def test_a_missing_key_file_reports_that_rather_than_asking_for_a_password(monkeypatch, tmp_path):
    def refuse(*a, **k):
        raise AssertionError("a missing file is not a locked file")
    monkeypatch.setattr(ssh_signer.getpass, "getpass", refuse)
    with pytest.raises(ValueError, match="Private key not found"):
        ssh_signer.sign_api_key(
            "sk-test", key_name="alice--1",
            private_key_path=str(tmp_path / "absent"), prompt_for_password=True,
        )
