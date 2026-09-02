import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet
from sqlalchemy import func
from sqlalchemy.orm import Session

from unillm.config import get_fernet_key, recoverable_keys_allowed
from unillm.db.models import APIKey, AuditLog, ModelPricing, Project, RequestLog, SSHKey, User, UserProjectAccess
from unillm.types import SSHKeyInfo


def _utcnow() -> datetime:
    """
    Naive UTC now. Replaces the deprecated datetime.utcnow() while staying naive to
    match the (naive) DateTime columns and their datetime.utcnow model defaults.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _fernet() -> Fernet:
    return Fernet(get_fernet_key())


def encrypt_api_key(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


def _generate_api_key() -> str:
    return "sk-" + secrets.token_urlsafe(36)


def _maybe_encrypt_api_key(plaintext: str, recoverable: bool) -> Optional[str]:
    """
    Ciphertext for later reveal, or None.

    Storing nothing is the default and the safe case: a key with no ciphertext
    cannot be read back out of the database by anyone, ever. Both gates must be
    open — the deployment has to permit recoverable keys, and this particular key
    has to have asked for it.
    """
    if not recoverable or not recoverable_keys_allowed():
        return None
    return encrypt_api_key(plaintext)


def _unique_project_name(db: Session, base_name: str) -> str:
    """Project names are unique; suffix with -2, -3, ... on collision."""
    name = base_name
    counter = 2
    while db.query(Project).filter(Project.name == name).first() is not None:
        name = f"{base_name}-{counter}"
        counter += 1
    return name


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
    api_key.last_used_at = _utcnow()
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


def _validate_key_name(key_name: str, username: str) -> None:
    import re
    prefix = f"{username}--"
    if not key_name.startswith(prefix):
        raise ValueError(f"Key name must start with '{prefix}'")
    suffix = key_name[len(prefix):]
    if not suffix:
        raise ValueError("Key name suffix cannot be empty")
    if not re.match(r'^[a-zA-Z0-9-]+$', suffix):
        raise ValueError("Key name suffix must contain only letters, numbers, and hyphens")
    if len(suffix) > 20:
        raise ValueError("Key name suffix must be 20 characters or fewer")


_SSH_KEY_TYPES = {
    'ssh-rsa', 'ssh-ed25519', 'ssh-dss',
    'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
    'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com',
}


def _validate_ssh_public_key(public_key: str) -> None:
    import base64 as _b64
    parts = public_key.strip().split()
    if len(parts) < 2:
        raise ValueError("Invalid SSH public key format — expected '<type> <base64-data>'")
    if parts[0] not in _SSH_KEY_TYPES:
        raise ValueError(f"Unsupported key type '{parts[0]}'")
    try:
        data = _b64.b64decode(parts[1])
        if len(data) < 8:
            raise ValueError("Key data too short")
    except Exception:
        raise ValueError("Invalid SSH public key — bad base64 data")


def add_ssh_key(db: Session, user_id: int, username: str, key_name: str, public_key: str) -> SSHKey:
    _validate_key_name(key_name, username)
    _validate_ssh_public_key(public_key)
    if db.query(SSHKey).filter(SSHKey.user_id == user_id).count() >= 3:
        raise ValueError("Maximum of 3 SSH keys allowed per user")
    if db.query(SSHKey).filter(SSHKey.user_id == user_id, SSHKey.key_name == key_name).first():
        raise ValueError(f"Key name '{key_name}' is already in use")
    key = SSHKey(user_id=user_id, key_name=key_name, public_key=public_key)
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


def update_ssh_key(db: Session, key_id: int, user_id: int, username: str,
                   key_name: Optional[str] = None, public_key: Optional[str] = None) -> Optional[SSHKey]:
    key = db.query(SSHKey).filter(SSHKey.id == key_id, SSHKey.user_id == user_id).first()
    if not key:
        return None
    if key_name is not None:
        _validate_key_name(key_name, username)
        if db.query(SSHKey).filter(SSHKey.user_id == user_id, SSHKey.key_name == key_name, SSHKey.id != key_id).first():
            raise ValueError(f"Key name '{key_name}' is already in use")
        key.key_name = key_name
    if public_key is not None:
        _validate_ssh_public_key(public_key)
        key.public_key = public_key
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


def touch_ssh_key(db: Session, key_id: int) -> None:
    db.query(SSHKey).filter(SSHKey.id == key_id).update({"last_used_at": _utcnow()})
    db.commit()


def touch_ssh_key_by_name(db: Session, key_name: str, username: str) -> None:
    from unillm.db.models import User as UserModel
    user = db.query(UserModel).filter(UserModel.username == username).first()
    if not user:
        return
    db.query(SSHKey).filter(SSHKey.user_id == user.id, SSHKey.key_name == key_name).update(
        {"last_used_at": _utcnow()}
    )
    db.commit()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    return db.query(User).filter(User.id == user_id, User.active == True).first()


def get_user_by_id_any(db: Session, user_id: int) -> Optional[User]:
    """Like get_user_by_id but includes disabled users. For admin operations."""
    return db.query(User).filter(User.id == user_id).first()


def list_users(db: Session) -> List[User]:
    return db.query(User).all()


def count_active_admins(db: Session, exclude_user_id: Optional[int] = None) -> int:
    """Number of active admins, optionally excluding one user (to test a would-be change)."""
    q = db.query(func.count(User.id)).filter(User.global_role == "admin", User.active == True)
    if exclude_user_id is not None:
        q = q.filter(User.id != exclude_user_id)
    return q.scalar() or 0


def get_project_owner(db: Session, project_id: int) -> Optional[User]:
    """Return the user whose personal project is this project_id, if any."""
    return db.query(User).filter(User.personal_project_id == project_id).first()


def create_user(
    db: Session,
    username: str,
    hashed_password: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    global_role: str = "user",
) -> Tuple[User, Optional[str]]:
    """
    Create a user. Developers (admin/user) get a personal project + API key.
    Viewers get neither — they'll be added to projects explicitly.
    Returns (user, plaintext_api_key or None).
    """
    if global_role == "viewer":
        user = User(username=username, name=name, email=email, hashed_password=hashed_password, global_role=global_role)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user, None

    # Create personal project first
    project = Project(name=_unique_project_name(db, f"{username}-personal"),
                      description=f"Personal project for {username}")
    db.add(project)
    db.flush()  # get project.id without committing

    # Create user linked to personal project
    user = User(
        username=username,
        name=name,
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
        # The personal key is printed once at creation; it is not stored in a
        # recoverable form, so a lost one is replaced rather than revealed.
        key_ciphertext=_maybe_encrypt_api_key(plaintext_key, recoverable=False),
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


def get_project_by_name(db: Session, name: str) -> Optional[Project]:
    return db.query(Project).filter(Project.name == name).first()


def get_projects_for_user(db: Session, user_id: int, is_admin: bool = False,
                          include_archived: bool = False) -> List[Project]:
    q = db.query(Project)
    if not include_archived:
        q = q.filter(Project.archived == False)
    if is_admin:
        return q.order_by(Project.created_at.desc()).all()
    return (
        q.join(UserProjectAccess, UserProjectAccess.project_id == Project.id)
        .filter(UserProjectAccess.user_id == user_id)
        .order_by(Project.created_at.desc())
        .all()
    )


def update_project(db: Session, project_id: int, **changes) -> Optional[Project]:
    """Apply the given field changes (name / description / archived) to a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return None
    for field, value in changes.items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return project


def get_project_counts(db: Session, project_ids: List[int]) -> Dict[int, Dict[str, int]]:
    """Member and active-key counts per project, for list displays."""
    counts: Dict[int, Dict[str, int]] = {pid: {"members": 0, "keys": 0} for pid in project_ids}
    if not project_ids:
        return counts
    member_rows = (
        db.query(UserProjectAccess.project_id, func.count(UserProjectAccess.user_id))
        .filter(UserProjectAccess.project_id.in_(project_ids))
        .group_by(UserProjectAccess.project_id)
        .all()
    )
    for pid, n in member_rows:
        counts[pid]["members"] = n
    key_rows = (
        db.query(APIKey.project_id, func.count(APIKey.id))
        .filter(APIKey.project_id.in_(project_ids), APIKey.active == True)
        .group_by(APIKey.project_id)
        .all()
    )
    for pid, n in key_rows:
        counts[pid]["keys"] = n
    return counts


def create_project(db: Session, name: str, description: Optional[str] = None, creator_id: Optional[int] = None) -> Project:
    project = Project(name=name, description=description)
    db.add(project)
    db.flush()  # get project.id before commit
    if creator_id is not None:
        access = UserProjectAccess(user_id=creator_id, project_id=project.id, role="admin")
        db.add(access)
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


def list_project_members(db: Session, project_id: int):
    return (
        db.query(UserProjectAccess, User)
        .join(User, User.id == UserProjectAccess.user_id)
        .filter(UserProjectAccess.project_id == project_id)
        .all()
    )


def list_member_candidates(db: Session, project_id: int) -> List[User]:
    """Active users who are not yet members of the project — the "add member" pick list."""
    member_ids = db.query(UserProjectAccess.user_id).filter(
        UserProjectAccess.project_id == project_id,
    )
    return (
        db.query(User)
        .filter(User.active == True, User.id.notin_(member_ids))  # noqa: E712
        .order_by(User.username)
        .all()
    )


def update_member_role(db: Session, project_id: int, user_id: int, role: str) -> Optional[UserProjectAccess]:
    access = db.query(UserProjectAccess).filter(
        UserProjectAccess.project_id == project_id,
        UserProjectAccess.user_id == user_id,
    ).first()
    if not access:
        return None
    access.role = role
    db.commit()
    db.refresh(access)
    return access


def remove_project_member(db: Session, project_id: int, user_id: int) -> bool:
    access = db.query(UserProjectAccess).filter(
        UserProjectAccess.project_id == project_id,
        UserProjectAccess.user_id == user_id,
    ).first()
    if not access:
        return False
    db.delete(access)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

def create_api_key(
    db: Session,
    project_id: int,
    name: str,
    allowed_models: Optional[List[str]] = None,
    recoverable: bool = False,
) -> Tuple[APIKey, str]:
    """
    Create an API key for a project.
    Returns (api_key, plaintext_key) — plaintext shown once.
    allowed_models: ["all"] = unrestricted, [] = no access, ["model-a"] = specific
    recoverable: store an encrypted copy so a project admin can reveal it later.
      Off by default, so the plaintext exists only in the creation response.
    """
    if allowed_models is None:
        allowed_models = ["all"]

    plaintext_key = _generate_api_key()
    api_key = APIKey(
        project_id=project_id,
        name=name,
        key_hash=_hash_key(plaintext_key),
        key_prefix=plaintext_key[:8],
        key_ciphertext=_maybe_encrypt_api_key(plaintext_key, recoverable=recoverable),
        allowed_models=allowed_models,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return api_key, plaintext_key


def get_api_keys_for_project(db: Session, project_id: int) -> List[APIKey]:
    # Includes revoked keys — the UI filters them but offers a "show revoked" view.
    return (
        db.query(APIKey)
        .filter(APIKey.project_id == project_id)
        .order_by(APIKey.active.desc(), APIKey.created_at.desc())
        .all()
    )


def update_api_key(db: Session, key_id: int, name: Optional[str] = None,
                   allowed_models: Optional[List[str]] = None) -> Optional[APIKey]:
    """Edit a key's name and/or model restriction without rotating the secret."""
    key = db.query(APIKey).filter(APIKey.id == key_id).first()
    if not key:
        return None
    if name is not None:
        key.name = name
    if allowed_models is not None:
        key.allowed_models = allowed_models
    db.commit()
    db.refresh(key)
    return key


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
    request_id: Optional[str] = None,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    api_key_name: Optional[str] = None,
    api_key_prefix: Optional[str] = None,
    ssh_username: Optional[str] = None,
    ip_address: Optional[str] = None,
    backend_model: Optional[str] = None,
    model_type: Optional[str] = None,
    stream: bool = False,
    labels: Optional[dict] = None,
    status_code: Optional[int] = None,
    error_message: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
) -> RequestLog:
    log = RequestLog(
        request_id=request_id,
        user_id=user_id,
        project_id=project_id,
        api_key_name=api_key_name,
        api_key_prefix=api_key_prefix,
        ssh_username=ssh_username,
        ip_address=ip_address,
        model=model,
        backend_model=backend_model,
        model_type=model_type,
        stream=stream,
        labels=labels,
        status_code=status_code,
        error_message=error_message,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or (prompt_tokens + completion_tokens),
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    db.add(log)
    db.commit()
    return log


# ---------------------------------------------------------------------------
# Audit Logs (append-only — no update or delete)
# ---------------------------------------------------------------------------

def create_audit_log(
    db: Session,
    action: str,
    severity: str = "info",
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    log = AuditLog(
        user_id=user_id,
        username=username,
        action=action,
        severity=severity,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=detail,
    )
    db.add(log)
    db.commit()
    return log


# ---------------------------------------------------------------------------
# Log queries
# ---------------------------------------------------------------------------

def query_request_logs(
    db: Session,
    allowed_project_ids: Optional[List[int]] = None,
    user_id: Optional[int] = None,
    ssh_username: Optional[str] = None,
    model: Optional[str] = None,
    status_code: Optional[int] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[RequestLog], int]:
    q = db.query(RequestLog)
    if allowed_project_ids is not None:
        q = q.filter(RequestLog.project_id.in_(allowed_project_ids))
    if user_id is not None:
        q = q.filter(RequestLog.user_id == user_id)
    if ssh_username is not None:
        q = q.filter(RequestLog.ssh_username == ssh_username)
    if model is not None:
        q = q.filter(RequestLog.model == model)
    if status_code is not None:
        q = q.filter(RequestLog.status_code == status_code)
    if from_date is not None:
        q = q.filter(RequestLog.created_at >= from_date)
    if to_date is not None:
        q = q.filter(RequestLog.created_at <= to_date)
    total = q.count()
    rows = q.order_by(RequestLog.created_at.desc()).offset(offset).limit(limit).all()
    return rows, total


def query_audit_logs(
    db: Session,
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    severity: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[AuditLog], int]:
    q = db.query(AuditLog)
    if action is not None:
        q = q.filter(AuditLog.action == action)
    if user_id is not None:
        q = q.filter(AuditLog.user_id == user_id)
    if severity is not None:
        q = q.filter(AuditLog.severity == severity)
    if from_date is not None:
        q = q.filter(AuditLog.created_at >= from_date)
    if to_date is not None:
        q = q.filter(AuditLog.created_at <= to_date)
    total = q.count()
    rows = q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()
    return rows, total


def get_request_stats(
    db: Session,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    allowed_project_ids: Optional[List[int]] = None,
    ssh_username: Optional[str] = None,
) -> Dict[str, Any]:
    q = db.query(RequestLog)
    if allowed_project_ids is not None:
        q = q.filter(RequestLog.project_id.in_(allowed_project_ids))
    if ssh_username is not None:
        q = q.filter(RequestLog.ssh_username == ssh_username)
    if from_date is not None:
        q = q.filter(RequestLog.created_at >= from_date)
    if to_date is not None:
        q = q.filter(RequestLog.created_at <= to_date)

    totals = q.with_entities(
        func.count(RequestLog.id),
        func.sum(RequestLog.prompt_tokens),
        func.sum(RequestLog.completion_tokens),
        func.sum(RequestLog.total_tokens),
        func.sum(RequestLog.cost_usd),
        func.avg(RequestLog.latency_ms),
    ).first()

    by_model = q.with_entities(
        RequestLog.model,
        func.count(RequestLog.id),
        func.sum(RequestLog.total_tokens),
        func.sum(RequestLog.cost_usd),
        func.avg(RequestLog.latency_ms),
    ).group_by(RequestLog.model).all()

    by_status = q.with_entities(
        RequestLog.status_code,
        func.count(RequestLog.id),
    ).group_by(RequestLog.status_code).all()

    return {
        "total_requests": totals[0] or 0,
        "total_prompt_tokens": int(totals[1] or 0),
        "total_completion_tokens": int(totals[2] or 0),
        "total_tokens": int(totals[3] or 0),
        "total_cost_usd": round(float(totals[4] or 0), 6),
        "avg_latency_ms": round(float(totals[5] or 0), 1),
        "by_model": [
            {
                "model": r[0],
                "requests": r[1],
                "total_tokens": int(r[2] or 0),
                "cost_usd": round(float(r[3] or 0), 6),
                "avg_latency_ms": round(float(r[4] or 0), 1),
            }
            for r in by_model
        ],
        "by_status": {str(r[0]): r[1] for r in by_status},
    }


# ---------------------------------------------------------------------------
# Model Pricing
# ---------------------------------------------------------------------------

def get_model_pricing(db: Session, model_name: str) -> Optional[ModelPricing]:
    return db.query(ModelPricing).filter(ModelPricing.model_name == model_name).first()


def list_model_pricing(db: Session) -> List[ModelPricing]:
    return db.query(ModelPricing).order_by(ModelPricing.model_name).all()


def upsert_model_pricing(
    db: Session,
    model_name: str,
    input_per_1m: float,
    output_per_1m: float,
    currency: str = "USD",
    notes: Optional[str] = None,
) -> ModelPricing:
    existing = get_model_pricing(db, model_name)
    if existing:
        existing.input_per_1m = input_per_1m
        existing.output_per_1m = output_per_1m
        existing.currency = currency
        existing.notes = notes
        existing.updated_at = _utcnow()
    else:
        existing = ModelPricing(
            model_name=model_name,
            input_per_1m=input_per_1m,
            output_per_1m=output_per_1m,
            currency=currency,
            notes=notes,
        )
        db.add(existing)
    db.commit()
    db.refresh(existing)
    return existing


def delete_model_pricing(db: Session, model_name: str) -> bool:
    row = get_model_pricing(db, model_name)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def get_models_summary(db: Session, configured_models: list = None) -> List[Dict[str, Any]]:
    """Return one entry per model: config + request logs + pricing merged."""
    cfg_map: Dict[str, Dict[str, Any]] = {}
    for m in (configured_models or []):
        name = m.get("model_name")
        if not name:
            continue
        params = m.get("unillm_params", m.get("litellm_params", {}))
        cfg_map[name] = {
            "model_type": params.get("model_type", "vertex-ai"),
            "backend_model": params.get("model"),
        }

    log_names = {r[0] for r in db.query(RequestLog.model).distinct().all()}
    pricing_map = {p.model_name: p for p in db.query(ModelPricing).all()}
    all_names = sorted(log_names | set(pricing_map) | set(cfg_map))

    result = []
    for name in all_names:
        # Last 10 requests for status
        recent = (
            db.query(RequestLog.status_code, RequestLog.created_at)
            .filter(RequestLog.model == name)
            .order_by(RequestLog.created_at.desc())
            .limit(10)
            .all()
        )

        last_success = next((r.created_at for r in recent if r.status_code and 200 <= r.status_code < 300), None)
        last_failure = next((r.created_at for r in recent if r.status_code and r.status_code >= 400), None)

        if not recent:
            status = "unsure"
        elif recent[0].status_code and 200 <= recent[0].status_code < 300:
            status = "healthy"
        elif recent[0].status_code and recent[0].status_code >= 400:
            status = "issues"
        else:
            status = "unsure"

        total = db.query(func.count(RequestLog.id)).filter(RequestLog.model == name).scalar() or 0

        p = pricing_map.get(name)
        cfg = cfg_map.get(name, {})
        result.append({
            "name": name,
            "model_type": cfg.get("model_type"),
            "backend_model": cfg.get("backend_model"),
            "description": p.notes if p else None,
            "status": status,
            "last_success_at": last_success.isoformat() if last_success else None,
            "last_failure_at": last_failure.isoformat() if last_failure else None,
            "total_requests": total,
            "pricing": {
                "input_per_1m": p.input_per_1m,
                "output_per_1m": p.output_per_1m,
                "currency": p.currency,
                "notes": p.notes,
            } if p else None,
        })
    return result


def compute_cost(db: Session, model_name: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    pricing = get_model_pricing(db, model_name)
    if not pricing:
        return None
    cost = (prompt_tokens / 1_000_000) * pricing.input_per_1m + \
           (completion_tokens / 1_000_000) * pricing.output_per_1m
    return round(cost, 8)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def seed_admin_if_needed(db: Session) -> Optional[str]:
    """
    Bootstrap the first admin from UNILLM_ADMIN_USERNAME / UNILLM_ADMIN_PASSWORD.

    - If the user doesn't exist yet: creates it with a personal project and API key.
    - If the user already exists: left untouched by default. Overwriting an existing
      account's password/role/active-flag on every restart is dangerous (a mistyped
      env var silently re-enables and escalates whatever account it names), so that
      behavior is opt-in via UNILLM_ADMIN_SYNC=true.

    Returns the plaintext API key only when a new user is created.
    """
    import os as _os

    username = _os.getenv("UNILLM_ADMIN_USERNAME", "").strip()
    password = _os.getenv("UNILLM_ADMIN_PASSWORD", "").strip()
    if not username or not password:
        return None

    from unillm.proxy.api_routes import hash_password

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        if _os.getenv("UNILLM_ADMIN_SYNC", "").lower() == "true":
            existing.hashed_password = hash_password(password)
            existing.global_role = "admin"
            existing.active = True
            existing.token_version = (existing.token_version or 0) + 1
            db.commit()
        return None

    _, plaintext_key = create_user(
        db=db,
        username=username,
        hashed_password=hash_password(password),
        global_role="admin",
    )
    return plaintext_key
