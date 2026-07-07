"""
Management REST API routes for UniLLM.

Endpoints for user, project, API key, and SSH key management.
All management endpoints require JWT authentication via POST /api/auth/login.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import bcrypt as _bcrypt
import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from unillm.config import get_jwt_secret
from unillm.db import get_db
from unillm.db import crud
from unillm.db.models import APIKey, Project, User

router = APIRouter(prefix="/api", tags=["management"])

# JWT configuration — the signing secret is resolved (and validated) in unillm.config.
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = 24

# Precomputed bcrypt hash of a random string, used to keep login timing constant when
# the username does not exist (mitigates username enumeration via response timing).
_DUMMY_PASSWORD_HASH = _bcrypt.hashpw(b"unillm-timing-equalizer", _bcrypt.gensalt()).decode()

GlobalRole = Literal["user", "admin", "viewer"]
ProjectRole = Literal["viewer", "developer", "admin"]

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    username: str
    name: Optional[str] = None
    email: Optional[str]
    global_role: str
    active: bool = True
    password_login_disabled: bool = False
    created_at: datetime


_USERNAME_PATTERN = r"^[a-zA-Z0-9_.-]{2,32}$"
_MIN_PASSWORD_LEN = 8


class CreateUserRequest(BaseModel):
    username: str = Field(..., pattern=_USERNAME_PATTERN)
    name: Optional[str] = None
    password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=72)
    email: Optional[str] = None
    global_role: GlobalRole = "user"


class CreateUserResponse(BaseModel):
    user: UserResponse
    api_key: Optional[str]  # plaintext, shown once; None for viewer accounts


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime


class CreateProjectRequest(BaseModel):
    name: str
    description: Optional[str] = None


class APIKeyResponse(BaseModel):
    id: int
    name: str
    key_prefix: str
    allowed_models: list
    active: bool
    created_at: datetime
    last_used_at: Optional[datetime]


class CreateAPIKeyRequest(BaseModel):
    name: str
    allowed_models: Optional[List[str]] = None  # None → ["all"]


class CreateAPIKeyResponse(BaseModel):
    key: APIKeyResponse
    api_key: str  # plaintext, shown once


class SSHKeyResponse(BaseModel):
    id: int
    key_name: str
    public_key: str
    created_at: datetime
    last_used_at: Optional[datetime] = None


class AddSSHKeyRequest(BaseModel):
    key_name: str
    public_key: str


class RequestLogResponse(BaseModel):
    id: int
    request_id: Optional[str]
    created_at: datetime
    project_id: Optional[int]
    project_name: Optional[str]
    api_key_name: Optional[str]
    api_key_prefix: Optional[str]
    ssh_username: Optional[str]
    ip_address: Optional[str]
    model: str
    backend_model: Optional[str]
    model_type: Optional[str]
    stream: bool
    labels: Optional[Dict[str, Any]]
    status_code: Optional[int]
    error_message: Optional[str]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: Optional[float]
    latency_ms: Optional[int]


class AuditLogResponse(BaseModel):
    id: int
    created_at: datetime
    user_id: Optional[int]
    username: Optional[str]
    action: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    ip_address: Optional[str]
    user_agent: Optional[str]
    severity: str
    detail: Optional[Dict[str, Any]]


class PaginatedRequestLogs(BaseModel):
    total: int
    items: List[RequestLogResponse]


class PaginatedAuditLogs(BaseModel):
    total: int
    items: List[AuditLogResponse]


class ModelPricingResponse(BaseModel):
    id: int
    model_name: str
    input_per_1m: float
    output_per_1m: float
    currency: str
    notes: Optional[str]
    updated_at: datetime


class UpsertModelPricingRequest(BaseModel):
    input_per_1m: float
    output_per_1m: float
    currency: str = "USD"
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> Optional[str]:
    """
    Resolve the client IP. Behind a reverse proxy request.client.host is the proxy,
    so honor X-Forwarded-For only when explicitly opted in (UNILLM_TRUST_PROXY_HEADERS=true),
    taking the first (client) hop. XFF is trivially spoofable when not fronted by a
    trusted proxy, hence the opt-in.
    """
    import os
    if os.getenv("UNILLM_TRUST_PROXY_HEADERS", "").lower() == "true":
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        # Malformed/legacy hash in the DB — treat as a failed check, never a 500.
        return False


def _create_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "role": user.global_role,
        "tv": user.token_version or 0,
        "exp": datetime.now(timezone.utc) + timedelta(hours=_JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=_JWT_ALGORITHM)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(credentials.credentials, get_jwt_secret(), algorithms=[_JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    # A password change/reset bumps token_version, invalidating older tokens.
    if int(payload.get("tv", 0)) != (user.token_version or 0):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been invalidated")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def _project_response(p) -> ProjectResponse:
    return ProjectResponse(id=p.id, name=p.name, description=p.description, created_at=p.created_at)


def _key_response(k) -> APIKeyResponse:
    return APIKeyResponse(
        id=k.id, name=k.name, key_prefix=k.key_prefix,
        allowed_models=k.allowed_models, active=k.active,
        created_at=k.created_at, last_used_at=k.last_used_at,
    )


def _user_response(u) -> UserResponse:
    return UserResponse(id=u.id, username=u.username, name=u.name, email=u.email,
                        global_role=u.global_role, active=u.active,
                        password_login_disabled=u.password_login_disabled,
                        created_at=u.created_at)


def _write_audit_log(**kwargs):
    """Run an audit write on its own session (background tasks outlive the request session)."""
    from unillm.db.database import SessionLocal
    db = SessionLocal()
    try:
        crud.create_audit_log(db=db, **kwargs)
    finally:
        db.close()


def _audit(
    background_tasks: BackgroundTasks,
    db: Session,
    action: str,
    request: Request,
    severity: str = "info",
    user: Optional[User] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
):
    """Schedule an audit log write as a background task (on a fresh DB session)."""
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    background_tasks.add_task(
        _write_audit_log,
        action=action,
        severity=severity,
        user_id=user.id if user else None,
        username=user.username if user else None,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip,
        user_agent=ua,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = crud.get_user_by_username(db, req.username)
    # Always run a bcrypt verification (against a dummy hash when the user is unknown)
    # so the response time does not reveal whether the username exists.
    password_ok = _verify_password(req.password, user.hashed_password if user else _DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        _audit(background_tasks, db, "login_failure", request, severity="warning",
               detail={"username": req.username})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.active:
        _audit(background_tasks, db, "login_failure", request, severity="warning",
               detail={"username": req.username, "reason": "account_disabled"})
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been disabled")
    if user.password_login_disabled:
        _audit(background_tasks, db, "login_failure", request, severity="warning",
               detail={"username": req.username, "reason": "password_login_disabled"})
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password login is disabled for this account")
    _audit(background_tasks, db, "login_success", request, user=user)
    return TokenResponse(access_token=_create_token(user))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UpdateMeRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=72)


@router.get("/users/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return _user_response(current_user)


@router.put("/users/me", response_model=UserResponse)
def update_me(
    req: UpdateMeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    current_user.hashed_password = hash_password(req.new_password)
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()
    db.refresh(current_user)
    _audit(background_tasks, db, "password_changed", request, user=current_user,
           resource_type="user", resource_id=str(current_user.id))
    return _user_response(current_user)


@router.get("/users", response_model=List[UserResponse])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [_user_response(u) for u in crud.list_users(db)]


@router.post("/users", response_model=CreateUserResponse, status_code=201)
def create_user(req: CreateUserRequest, request: Request, background_tasks: BackgroundTasks,
                admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if crud.get_user_by_username(db, req.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    user, plaintext_key = crud.create_user(
        db=db,
        username=req.username,
        name=req.name or None,
        hashed_password=hash_password(req.password),
        email=req.email or None,
        global_role=req.global_role,
    )
    _audit(background_tasks, db, "user_created", request, user=admin,
           resource_type="user", resource_id=str(user.id),
           detail={"username": user.username, "role": user.global_role})
    return CreateUserResponse(user=_user_response(user), api_key=plaintext_key)


class AdminUpdateUserRequest(BaseModel):
    name: Optional[str] = None
    global_role: Optional[GlobalRole] = None
    new_password: Optional[str] = Field(None, min_length=_MIN_PASSWORD_LEN, max_length=72)
    password_login_disabled: Optional[bool] = None
    active: Optional[bool] = None


@router.put("/users/{user_id}", response_model=UserResponse)
def admin_update_user(user_id: int, req: AdminUpdateUserRequest, request: Request,
                      background_tasks: BackgroundTasks,
                      admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admins cannot edit their own account here")
    target = crud.get_user_by_id_any(db, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Guard against locking out the last admin by demotion or deactivation.
    demoting = req.global_role is not None and req.global_role != "admin" and target.global_role == "admin"
    deactivating = req.active is False and target.active
    if (demoting or deactivating) and crud.count_active_admins(db, exclude_user_id=target.id) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Cannot remove the last remaining active admin")

    changes = {}
    invalidate_sessions = False
    if req.name is not None:
        target.name = req.name or None
        changes["name"] = req.name
    if req.global_role is not None:
        target.global_role = req.global_role
        changes["global_role"] = req.global_role
        invalidate_sessions = True
    if req.new_password is not None:
        target.hashed_password = hash_password(req.new_password)
        changes["password_reset"] = True
        invalidate_sessions = True
    if req.password_login_disabled is not None:
        target.password_login_disabled = req.password_login_disabled
        changes["password_login_disabled"] = req.password_login_disabled
    if req.active is not None:
        target.active = req.active
        changes["active"] = req.active
        invalidate_sessions = True
    if invalidate_sessions:
        target.token_version = (target.token_version or 0) + 1
    db.commit()
    db.refresh(target)
    _audit(background_tasks, db, "user_updated", request, user=admin,
           resource_type="user", resource_id=str(user_id), detail=changes)
    return _user_response(target)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects", response_model=List[ProjectResponse])
def list_projects(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [_project_response(p) for p in crud.get_projects_for_user(db, current_user.id, is_admin=current_user.global_role == "admin")]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if not role and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this project")
    from unillm.db.models import Project
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _project_response(p)


@router.post("/projects", response_model=ProjectResponse, status_code=201)
def create_project(req: CreateProjectRequest, request: Request, background_tasks: BackgroundTasks,
                   admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    project = crud.create_project(db, name=req.name, description=req.description, creator_id=admin.id)
    _audit(background_tasks, db, "project_created", request, user=admin,
           resource_type="project", resource_id=str(project.id),
           detail={"name": project.name})
    return _project_response(project)


# ---------------------------------------------------------------------------
# Project Members
# ---------------------------------------------------------------------------

class ProjectMemberResponse(BaseModel):
    user_id: int
    username: str
    email: Optional[str]
    role: str


class AddMemberRequest(BaseModel):
    user_id: int
    role: ProjectRole = "viewer"


class UpdateMemberRoleRequest(BaseModel):
    role: ProjectRole


def _member_response(access, user) -> ProjectMemberResponse:
    return ProjectMemberResponse(
        user_id=user.id, username=user.username, email=user.email, role=access.role
    )


def _require_project_admin(db, current_user, project_id):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")


@router.get("/projects/{project_id}/members", response_model=List[ProjectMemberResponse])
def list_members(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if not role and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this project")
    return [_member_response(a, u) for a, u in crud.list_project_members(db, project_id)]


@router.post("/projects/{project_id}/members", response_model=ProjectMemberResponse, status_code=201)
def add_member(
    project_id: int,
    req: AddMemberRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_project_admin(db, current_user, project_id)
    existing = crud.get_user_project_role(db, req.user_id, project_id)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")
    target = crud.get_user_by_id(db, req.user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    access = crud.add_user_to_project(db, user_id=req.user_id, project_id=project_id, role=req.role)
    _audit(background_tasks, db, "project_member_added", request, user=current_user,
           resource_type="project", resource_id=str(project_id),
           detail={"user_id": req.user_id, "role": req.role})
    return _member_response(access, target)


@router.put("/projects/{project_id}/members/{user_id}", response_model=ProjectMemberResponse)
def update_member_role(
    project_id: int,
    user_id: int,
    req: UpdateMemberRoleRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_project_admin(db, current_user, project_id)
    access = crud.update_member_role(db, project_id=project_id, user_id=user_id, role=req.role)
    if not access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    target = crud.get_user_by_id(db, user_id)
    _audit(background_tasks, db, "project_member_role_updated", request, user=current_user,
           resource_type="project", resource_id=str(project_id),
           detail={"user_id": user_id, "role": req.role})
    return _member_response(access, target)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_member(
    project_id: int,
    user_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_project_admin(db, current_user, project_id)
    if not crud.remove_project_member(db, project_id=project_id, user_id=user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    _audit(background_tasks, db, "project_member_removed", request, user=current_user,
           resource_type="project", resource_id=str(project_id),
           detail={"user_id": user_id})


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/keys", response_model=List[APIKeyResponse])
def list_api_keys(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if not role and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this project")
    if role == "viewer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viewers cannot access API keys")
    return [_key_response(k) for k in crud.get_api_keys_for_project(db, project_id)]


@router.post("/projects/{project_id}/keys", response_model=CreateAPIKeyResponse, status_code=201)
def create_api_key(
    project_id: int,
    req: CreateAPIKeyRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")
    key_obj, plaintext_key = crud.create_api_key(
        db, project_id=project_id, name=req.name, allowed_models=req.allowed_models,
    )
    _audit(background_tasks, db, "api_key_created", request, user=current_user,
           resource_type="api_key", resource_id=str(key_obj.id),
           detail={"name": key_obj.name, "project_id": project_id, "allowed_models": key_obj.allowed_models})
    return CreateAPIKeyResponse(key=_key_response(key_obj), api_key=plaintext_key)


@router.get("/keys/{key_id}/reveal")
def reveal_api_key(
    key_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    key = db.query(APIKey).filter(APIKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    # Revealing plaintext is the most sensitive read in the system: restrict to
    # project admins (or global admins), and always record it in the audit trail.
    role = crud.get_user_project_role(db, current_user.id, key.project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")
    if not key.key_ciphertext:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plaintext not available for this key")
    _audit(background_tasks, db, "api_key_revealed", request, severity="warning", user=current_user,
           resource_type="api_key", resource_id=str(key_id),
           detail={"name": key.name, "project_id": key.project_id})
    return {"api_key": crud.decrypt_api_key(key.key_ciphertext)}


@router.delete("/keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    key = db.query(APIKey).filter(APIKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    role = crud.get_user_project_role(db, current_user.id, key.project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")
    crud.revoke_api_key(db, key_id=key_id, project_id=key.project_id)
    _audit(background_tasks, db, "api_key_revoked", request, user=current_user,
           resource_type="api_key", resource_id=str(key_id),
           detail={"name": key.name, "project_id": key.project_id})


# ---------------------------------------------------------------------------
# SSH Keys
# ---------------------------------------------------------------------------

@router.get("/ssh-keys", response_model=List[SSHKeyResponse])
def list_ssh_keys(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    keys = crud.get_user_ssh_keys(db, current_user.id)
    return [SSHKeyResponse(id=k.id, key_name=k.key_name, public_key=k.public_key,
                           created_at=k.created_at, last_used_at=k.last_used_at)
            for k in keys]


@router.post("/ssh-keys", response_model=SSHKeyResponse, status_code=201)
def add_ssh_key(req: AddSSHKeyRequest, request: Request, background_tasks: BackgroundTasks,
                current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        key = crud.add_ssh_key(db, user_id=current_user.id, username=current_user.username, key_name=req.key_name, public_key=req.public_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    _audit(background_tasks, db, "ssh_key_added", request, user=current_user,
           resource_type="ssh_key", resource_id=str(key.id),
           detail={"key_name": key.key_name})
    return SSHKeyResponse(id=key.id, key_name=key.key_name, public_key=key.public_key,
                          created_at=key.created_at, last_used_at=key.last_used_at)


SSH_VALIDATE_CHALLENGE = "sk-12345678"


class ValidateSSHKeyRequest(BaseModel):
    signed_key: str


class ValidateSSHKeyResponse(BaseModel):
    key_name: str
    message: str


@router.post("/ssh-keys/validate", response_model=ValidateSSHKeyResponse)
def validate_ssh_key(req: ValidateSSHKeyRequest,
                     current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from unillm.proxy.ssh_auth import parse_ssh_api_key, verify_ssh_signature
    keys = crud.get_user_ssh_keys(db, current_user.id)

    original, key_name, sig_b64 = parse_ssh_api_key(req.signed_key.strip())
    if not key_name or not sig_b64:
        raise HTTPException(status_code=400, detail=f"Expected format: {SSH_VALIDATE_CHALLENGE}||key-name||base64-signature")
    if original != SSH_VALIDATE_CHALLENGE:
        raise HTTPException(status_code=400, detail=f"Challenge must be exactly '{SSH_VALIDATE_CHALLENGE}'")
    matched = next((k for k in keys if k.key_name == key_name), None)
    if not matched:
        raise HTTPException(status_code=400, detail=f"No SSH key named '{key_name}' on your account")
    if not verify_ssh_signature(original, sig_b64, matched.public_key):
        raise HTTPException(status_code=400, detail="Signature verification failed — check your key name and private key")
    crud.touch_ssh_key(db, matched.id)
    return ValidateSSHKeyResponse(key_name=key_name, message=f"'{key_name}' validated successfully!")


class UpdateSSHKeyRequest(BaseModel):
    key_name: Optional[str] = None
    public_key: Optional[str] = None


@router.put("/ssh-keys/{key_id}", response_model=SSHKeyResponse)
def update_ssh_key(key_id: int, req: UpdateSSHKeyRequest, request: Request, background_tasks: BackgroundTasks,
                   current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        key = crud.update_ssh_key(db, key_id=key_id, user_id=current_user.id, username=current_user.username,
                                  key_name=req.key_name, public_key=req.public_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSH key not found")
    _audit(background_tasks, db, "ssh_key_updated", request, user=current_user,
           resource_type="ssh_key", resource_id=str(key_id), detail={"key_name": key.key_name})
    return SSHKeyResponse(id=key.id, key_name=key.key_name, public_key=key.public_key,
                          created_at=key.created_at, last_used_at=key.last_used_at)


@router.delete("/ssh-keys/{key_id}", status_code=204)
def delete_ssh_key(key_id: int, request: Request, background_tasks: BackgroundTasks,
                   current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not crud.delete_ssh_key(db, key_id=key_id, user_id=current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSH key not found")
    _audit(background_tasks, db, "ssh_key_deleted", request, user=current_user,
           resource_type="ssh_key", resource_id=str(key_id))


# ---------------------------------------------------------------------------
# Model Pricing
# ---------------------------------------------------------------------------

def _pricing_response(p) -> ModelPricingResponse:
    return ModelPricingResponse(
        id=p.id, model_name=p.model_name,
        input_per_1m=p.input_per_1m, output_per_1m=p.output_per_1m,
        currency=p.currency, notes=p.notes, updated_at=p.updated_at,
    )


@router.get("/pricing", response_model=List[ModelPricingResponse])
def list_pricing(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [_pricing_response(p) for p in crud.list_model_pricing(db)]


@router.put("/pricing/{model_name}", response_model=ModelPricingResponse)
def upsert_pricing(
    model_name: str,
    req: UpsertModelPricingRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    pricing = crud.upsert_model_pricing(
        db, model_name=model_name,
        input_per_1m=req.input_per_1m, output_per_1m=req.output_per_1m,
        currency=req.currency, notes=req.notes,
    )
    _audit(background_tasks, db, "pricing_updated", request, user=admin,
           resource_type="model_pricing", resource_id=model_name,
           detail={"input_per_1m": req.input_per_1m, "output_per_1m": req.output_per_1m})
    return _pricing_response(pricing)


@router.delete("/pricing/{model_name}", status_code=204)
def delete_pricing(
    model_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not crud.delete_model_pricing(db, model_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pricing not found")
    _audit(background_tasks, db, "pricing_deleted", request, user=admin,
           resource_type="model_pricing", resource_id=model_name)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@router.get("/models")
def list_models(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from unillm.proxy.proxy_server import model_list as configured_models
    return crud.get_models_summary(db, configured_models=configured_models)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def _resolve_project_filter(db, current_user, requested_ids: List[int]) -> Optional[List[int]]:
    """Return the effective project id list to filter by, or None (admin, no filter)."""
    if current_user.global_role == "admin":
        return requested_ids or None
    accessible = [p.id for p in crud.get_projects_for_user(db, current_user.id)]
    if requested_ids:
        return [i for i in requested_ids if i in accessible]
    return accessible


@router.get("/logs/requests", response_model=PaginatedRequestLogs)
def get_request_logs(
    project_ids: List[int] = Query(default=[]),
    ssh_username: Optional[str] = None,
    model: Optional[str] = None,
    status_code: Optional[int] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filter_ids = _resolve_project_filter(db, current_user, project_ids)

    rows, total = crud.query_request_logs(
        db, allowed_project_ids=filter_ids,
        ssh_username=ssh_username,
        model=model, status_code=status_code,
        from_date=from_date, to_date=to_date, limit=limit, offset=offset,
    )
    pids = {r.project_id for r in rows if r.project_id is not None}
    project_names: dict[int, str] = {}
    if pids:
        projects = db.query(Project).filter(Project.id.in_(pids)).all()
        project_names = {p.id: p.name for p in projects}
    items = [RequestLogResponse(
        id=r.id, request_id=r.request_id, created_at=r.created_at,
        project_id=r.project_id, project_name=project_names.get(r.project_id) if r.project_id else None,
        api_key_name=r.api_key_name, api_key_prefix=r.api_key_prefix,
        ssh_username=r.ssh_username, ip_address=r.ip_address,
        model=r.model, backend_model=r.backend_model, model_type=r.model_type,
        stream=r.stream, labels=r.labels,
        status_code=r.status_code, error_message=r.error_message,
        prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
        total_tokens=r.total_tokens, cost_usd=r.cost_usd, latency_ms=r.latency_ms,
    ) for r in rows]
    return PaginatedRequestLogs(total=total, items=items)


@router.get("/logs/audit", response_model=PaginatedAuditLogs)
def get_audit_logs(
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    severity: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows, total = crud.query_audit_logs(
        db, action=action, user_id=user_id, severity=severity,
        from_date=from_date, to_date=to_date, limit=limit, offset=offset,
    )
    items = [AuditLogResponse(
        id=r.id, created_at=r.created_at, user_id=r.user_id, username=r.username,
        action=r.action, resource_type=r.resource_type, resource_id=r.resource_id,
        ip_address=r.ip_address, user_agent=r.user_agent,
        severity=r.severity, detail=r.detail,
    ) for r in rows]
    return PaginatedAuditLogs(total=total, items=items)


@router.get("/logs/stats")
def get_stats(
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    project_ids: List[int] = Query(default=[]),
    ssh_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filter_ids = _resolve_project_filter(db, current_user, project_ids)
    return crud.get_request_stats(db, from_date=from_date, to_date=to_date, allowed_project_ids=filter_ids, ssh_username=ssh_username)
