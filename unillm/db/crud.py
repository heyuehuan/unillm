import hashlib
import os
import secrets
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from unillm.db.models import APIKey, Project, RequestLog, SSHKey, User, UserProjectAccess
from unillm.types import SSHKeyInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _generate_api_key() -> str:
    return "sk-" + secrets.token_urlsafe(36)


# ---------------------------------------------------------------------------
# API Key auth
# ---------------------------------------------------------------------------

def get_api_key_by_value(db: Session, raw_key: str) -> Optional[APIKey]:
    key_hash = _hash_key(raw_key)
    return db.query(APIKey).filter(
        APIKey.key_hash == key_hash,
        APIKey.active == True,
    ).first()


def touch_api_key(db: Session, api_key: APIKey) -> None:
    api_key.last_used_at = datetime.utcnow()
    db.commit()


# ---------------------------------------------------------------------------
# SSH Keys (for request verification)
# ---------------------------------------------------------------------------

def get_all_ssh_keys(db: Session) -> Dict[str, SSHKeyInfo]:
    """Load all registered SSH keys for signature verification."""
    keys = db.query(SSHKey).join(SSHKey.user).filter(User.active == True).all()
    result = {}
    for key in keys:
        result[key.key_name] = SSHKeyInfo(
            key_name=key.key_name,
            public_key=key.public_key,
            username=key.user.username,
        )
    return result


def get_user_ssh_keys(db: Session, user_id: int) -> List[SSHKey]:
    return db.query(SSHKey).filter(SSHKey.user_id == user_id).all()


def add_ssh_key(db: Session, user_id: int, key_name: str, public_key: str) -> SSHKey:
    existing = db.query(SSHKey).filter(SSHKey.user_id == user_id).count()
    if existing >= 3:
        raise ValueError("Maximum of 3 SSH keys allowed per user")
    key = SSHKey(user_id=user_id, key_name=key_name, public_key=public_key)
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


def delete_ssh_key(db: Session, key_id: int, user_id: int) -> bool:
    key = db.query(SSHKey).filter(SSHKey.id == key_id, SSHKey.user_id == user_id).first()
    if not key:
        return False
    db.delete(key)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username, User.active == True).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    return db.query(User).filter(User.id == user_id, User.active == True).first()


def list_users(db: Session) -> List[User]:
    return db.query(User).filter(User.active == True).all()


def create_user(
    db: Session,
    username: str,
    hashed_password: str,
    email: Optional[str] = None,
    global_role: str = "user",
) -> Tuple[User, str]:
    """
    Create a user with a personal project and one auto-generated API key.
    Returns (user, plaintext_api_key) — plaintext shown once, not stored.
    """
    # Create personal project first
    project = Project(name=f"{username}-personal", description=f"Personal project for {username}")
    db.add(project)
    db.flush()  # get project.id without committing

    # Create user linked to personal project
    user = User(
        username=username,
        email=email,
        hashed_password=hashed_password,
        global_role=global_role,
        personal_project_id=project.id,
    )
    db.add(user)
    db.flush()

    # Add user as admin of their personal project
    access = UserProjectAccess(user_id=user.id, project_id=project.id, role="admin")
    db.add(access)

    # Generate personal API key (unrestricted)
    plaintext_key = _generate_api_key()
    api_key = APIKey(
        project_id=project.id,
        name="default",
        key_hash=_hash_key(plaintext_key),
        key_prefix=plaintext_key[:8],
        allowed_models=["all"],
    )
    db.add(api_key)
    db.commit()
    db.refresh(user)
    return user, plaintext_key


def deactivate_user(db: Session, user_id: int) -> bool:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    user.active = False
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def get_project_by_id(db: Session, project_id: int) -> Optional[Project]:
    return db.query(Project).filter(Project.id == project_id).first()


def get_projects_for_user(db: Session, user_id: int) -> List[Project]:
    return (
        db.query(Project)
        .join(UserProjectAccess, UserProjectAccess.project_id == Project.id)
        .filter(UserProjectAccess.user_id == user_id)
        .all()
    )


def create_project(db: Session, name: str, description: Optional[str] = None) -> Project:
    project = Project(name=name, description=description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def add_user_to_project(db: Session, user_id: int, project_id: int, role: str = "viewer") -> UserProjectAccess:
    access = UserProjectAccess(user_id=user_id, project_id=project_id, role=role)
    db.add(access)
    db.commit()
    return access


def get_user_project_role(db: Session, user_id: int, project_id: int) -> Optional[str]:
    access = db.query(UserProjectAccess).filter(
        UserProjectAccess.user_id == user_id,
        UserProjectAccess.project_id == project_id,
    ).first()
    return access.role if access else None


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

def create_api_key(
    db: Session,
    project_id: int,
    name: str,
    allowed_models: Optional[List[str]] = None,
) -> Tuple[APIKey, str]:
    """
    Create an API key for a project.
    Returns (api_key, plaintext_key) — plaintext shown once, not stored.
    allowed_models: ["all"] = unrestricted, [] = no access, ["model-a"] = specific
    """
    if allowed_models is None:
        allowed_models = ["all"]

    plaintext_key = _generate_api_key()
    api_key = APIKey(
        project_id=project_id,
        name=name,
        key_hash=_hash_key(plaintext_key),
        key_prefix=plaintext_key[:8],
        allowed_models=allowed_models,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return api_key, plaintext_key


def get_api_keys_for_project(db: Session, project_id: int) -> List[APIKey]:
    return db.query(APIKey).filter(
        APIKey.project_id == project_id,
        APIKey.active == True,
    ).all()


def revoke_api_key(db: Session, key_id: int, project_id: int) -> bool:
    key = db.query(APIKey).filter(APIKey.id == key_id, APIKey.project_id == project_id).first()
    if not key:
        return False
    key.active = False
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Request Logs
# ---------------------------------------------------------------------------

def create_request_log(
    db: Session,
    model: str,
    project_id: Optional[int] = None,
    api_key_name: Optional[str] = None,
    ssh_username: Optional[str] = None,
    labels: Optional[dict] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: Optional[int] = None,
    status_code: Optional[int] = None,
) -> RequestLog:
    log = RequestLog(
        project_id=project_id,
        api_key_name=api_key_name,
        ssh_username=ssh_username,
        model=model,
        labels=labels,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        status_code=status_code,
    )
    db.add(log)
    db.commit()
    return log


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def seed_admin_if_needed(db: Session) -> Optional[str]:
    """
    Create the first admin user from env vars if no admin exists yet.

    Reads UNILLM_ADMIN_USERNAME and UNILLM_ADMIN_PASSWORD.
    Returns the plaintext API key if an admin was created, None otherwise.
    """
    username = os.getenv("UNILLM_ADMIN_USERNAME", "").strip()
    password = os.getenv("UNILLM_ADMIN_PASSWORD", "").strip()
    if not username or not password:
        return None

    existing_admin = db.query(User).filter(User.global_role == "admin", User.active == True).first()
    if existing_admin:
        return None

    from unillm.proxy.api_routes import hash_password
    _, plaintext_key = create_user(
        db=db,
        username=username,
        hashed_password=hash_password(password),
        global_role="admin",
    )
    return plaintext_key
