"""
An SSH key name identifies one account.

Signature verification resolves a key by name alone, so two accounts must never
hold the same name. Per-user uniqueness was not enough: usernames may contain
hyphens and '--' separates the username from the suffix, so 'alice--x--1' was a
legal name for both 'alice' and 'alice--x'.
"""

import pytest

from unillm.db import crud
from unillm.db.database import SessionLocal, engine, init_db
from unillm.db.models import Base, SSHKey
from unillm.proxy.api_routes import hash_password


ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ1YQlG1z8dCYhQjPqFVzYyEsFvDkCJDDNcuVEIRVJ0O test"


@pytest.fixture(scope="module", autouse=True)
def db_setup():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _user(db, username):
    user, _ = crud.create_user(db, username=username, hashed_password=hash_password("password123"))
    return user


def test_a_suffix_cannot_contain_the_separator(db):
    alice = _user(db, "alice")
    with pytest.raises(ValueError, match="cannot contain"):
        crud.add_ssh_key(db, user_id=alice.id, username="alice",
                         key_name="alice--x--1", public_key=ED25519)


def test_a_name_another_account_already_holds_is_refused(db):
    """
    The suffix rule stops new names from colliding, but rows written before it
    exists can still sit there. The uniqueness check looks across all accounts, so
    the legitimate owner of that name is told it is taken rather than quietly
    becoming the second holder of it.
    """
    squatter = _user(db, "squatter")
    bob = _user(db, "bob")
    db.add(SSHKey(user_id=squatter.id, key_name="bob--laptop", public_key=ED25519))
    db.commit()

    with pytest.raises(ValueError, match="already in use"):
        crud.add_ssh_key(db, user_id=bob.id, username="bob",
                         key_name="bob--laptop", public_key=ED25519)

    own = crud.add_ssh_key(db, user_id=bob.id, username="bob",
                           key_name="bob--desktop", public_key=ED25519)
    with pytest.raises(ValueError, match="already in use"):
        crud.update_ssh_key(db, key_id=own.id, user_id=bob.id, username="bob",
                            key_name="bob--laptop")


def test_an_ordinary_name_is_still_accepted(db):
    carol = _user(db, "carol")
    key = crud.add_ssh_key(db, user_id=carol.id, username="carol",
                           key_name="carol--laptop", public_key=ED25519)
    assert key.key_name == "carol--laptop"
    renamed = crud.update_ssh_key(db, key_id=key.id, user_id=carol.id, username="carol",
                                  key_name="carol--desktop")
    assert renamed.key_name == "carol--desktop"


def test_a_duplicate_already_in_the_table_is_reported_not_silently_preferred(db, caplog):
    dave = _user(db, "dave")
    erin = _user(db, "erin")
    # Bypass the checks the way rows predating them would have.
    db.add(SSHKey(user_id=dave.id, key_name="shared--name", public_key=ED25519))
    db.add(SSHKey(user_id=erin.id, key_name="shared--name", public_key=ED25519))
    db.commit()

    import logging
    with caplog.at_level(logging.ERROR):
        keys = crud.get_all_ssh_keys(db)
    assert keys["shared--name"].username == "dave"
    assert "more than one account" in caplog.text
