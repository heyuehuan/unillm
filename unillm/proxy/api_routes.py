"""
Management REST API routes for UniLLM.

Endpoints for user, project, API key, and SSH key management.
All management endpoints require JWT authentication via POST /api/auth/login.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import bcrypt as _bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from unillm.config import get_jwt_secret, recoverable_keys_allowed
from unillm.db import get_db
from unillm.db import crud
from unillm.db.models import APIKey, Project, User
from unillm.proxy import forwarded
from unillm.proxy import ratelimit
from unillm.proxy import server_settings

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

# Ceilings on free-text fields. Pydantic will accept a string of any size
# otherwise, so a single request could carry megabytes into a column sized for a
# name — costing memory to parse, disk to store, and rendering time in the console
# forever after. The numbers are generous enough that no real value hits them.
_MAX_NAME_LEN = 128          # display names, project names, key names
_MAX_EMAIL_LEN = 254         # the longest address SMTP permits
_MAX_DESCRIPTION_LEN = 2048
# Long enough for an RSA-4096 authorized_keys line with a comment, which is the
# largest key OpenSSH realistically produces.
_MAX_PUBLIC_KEY_LEN = 8192
# An API key, a key name, and a base64 signature joined by '||'.
_MAX_SIGNED_KEY_LEN = 8192
# Passwords are capped at 72 when set (bcrypt ignores anything beyond that), but a
# login has to accept whatever the caller typed before it can reject it.
_MAX_PASSWORD_LEN = 1024
# No deployment serves anywhere near this many distinct models.
_MAX_MODEL_LIST_LEN = 512


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=_MAX_NAME_LEN)
    password: str = Field(..., max_length=_MAX_PASSWORD_LEN)


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
    name: Optional[str] = Field(None, max_length=_MAX_NAME_LEN)
    password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=72)
    email: Optional[str] = Field(None, max_length=_MAX_EMAIL_LEN)
    global_role: GlobalRole = "user"


class CreateUserResponse(BaseModel):
    user: UserResponse
    api_key: Optional[str]  # plaintext, shown once; None for viewer accounts


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    archived: bool = False
    created_at: datetime
    # A personal project belongs to one account and takes no members. The console
    # needs to know so it can leave out membership controls that would only 400.
    personal: bool = False
    # Only populated on list responses (for the project cards)
    member_count: Optional[int] = None
    key_count: Optional[int] = None


class CreateProjectRequest(BaseModel):
    name: str = Field(..., max_length=_MAX_NAME_LEN)
    description: Optional[str] = Field(None, max_length=_MAX_DESCRIPTION_LEN)


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=_MAX_NAME_LEN)
    description: Optional[str] = Field(None, max_length=_MAX_DESCRIPTION_LEN)
    archived: Optional[bool] = None


class APIKeyResponse(BaseModel):
    id: int
    name: str
    key_prefix: str
    allowed_models: list
    active: bool
    created_at: datetime
    last_used_at: Optional[datetime]
    # True when this key can be revealed later. Lets the console show the Reveal
    # button only where it will work, instead of offering it and then 404ing.
    recoverable: bool = False


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(..., max_length=_MAX_NAME_LEN)
    allowed_models: Optional[List[str]] = Field(None, max_length=_MAX_MODEL_LIST_LEN)  # None → ["all"]
    # Opt in to storing an encrypted copy so a project admin can read this key back
    # later. Off by default: a key nobody can read back cannot leak from the
    # database, and the plaintext is right there in this call's response.
    recoverable: bool = False


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
    key_name: str = Field(..., max_length=_MAX_NAME_LEN)
    public_key: str = Field(..., max_length=_MAX_PUBLIC_KEY_LEN)


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


class ServerSettingResponse(BaseModel):
    """A setting's effective value plus enough context to explain where it came from."""
    key: str
    value: Any
    source: Literal["database", "config", "default"]
    default: Any
    config_value: Optional[Any] = None
    description: str
    unit: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


class UpdateServerSettingRequest(BaseModel):
    value: Any


class UpsertModelPricingRequest(BaseModel):
    input_per_1m: float
    output_per_1m: float
    currency: str = Field("USD", max_length=8)
    notes: Optional[str] = Field(None, max_length=_MAX_DESCRIPTION_LEN)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> Optional[str]:
    """
    Resolve the client IP, honoring X-Forwarded-For only behind a trusted proxy.

    See unillm.proxy.forwarded for why the entry is counted from the right: the
    leading entries of that header are written by the client, so reading them
    would let a caller pick its own rate-limit bucket and audit-log identity.
    """
    return forwarded.client_ip(request)


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


def _project_response(p, counts: Optional[Dict[str, int]] = None,
                      personal: Optional[bool] = None) -> ProjectResponse:
    return ProjectResponse(
        id=p.id, name=p.name, description=p.description,
        archived=p.archived, created_at=p.created_at,
        personal=personal if personal is not None else False,
        member_count=counts["members"] if counts else None,
        key_count=counts["keys"] if counts else None,
    )


def _key_response(k) -> APIKeyResponse:
    return APIKeyResponse(
        id=k.id, name=k.name, key_prefix=k.key_prefix,
        allowed_models=k.allowed_models, active=k.active,
        created_at=k.created_at, last_used_at=k.last_used_at,
        # Derived rather than stored: holding a decryptable copy is exactly what
        # "recoverable" means, so the two can never drift apart. The deployment
        # switch is applied too, so turning it off hides Reveal everywhere at once.
        recoverable=bool(k.key_ciphertext) and recoverable_keys_allowed(),
    )


def _user_response(u) -> UserResponse:
    return UserResponse(id=u.id, username=u.username, name=u.name, email=u.email,
                        global_role=u.global_role, active=u.active,
                        password_login_disabled=u.password_login_disabled,
                        created_at=u.created_at)


def _audit(
    db: Session,
    action: str,
    request: Request,
    severity: str = "info",
    user: Optional[User] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
):
    """
    Append a row to the audit trail, synchronously, on the request's own session.

    This used to be scheduled as a FastAPI background task. Background tasks are
    attached to the response the endpoint returns, so any audit followed by a
    `raise HTTPException` was silently discarded — which meant every failure event,
    including every failed login, was missing from the trail. A security audit log
    that drops exactly the events worth auditing is worse than none, so the write
    now happens inline.

    Callers commit their own changes before auditing, so the commit here only ever
    persists the audit row. Failing to record an audit entry deliberately fails the
    request rather than passing silently.
    """
    crud.create_audit_log(
        db=db,
        action=action,
        severity=severity,
        user_id=user.id if user else None,
        username=user.username if user else None,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _login_key(username: str) -> str:
    """Normalized key for the per-username failure counter."""
    return (username or "").strip().lower()


@router.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    # Throttle before doing any work. Checking first means a blocked caller cannot
    # spend the server's bcrypt CPU, and the decision does not depend on whether the
    # username exists — so this adds no enumeration signal.
    ip = _client_ip(request)
    user_key = _login_key(req.username)
    pair_key = ratelimit.login_pair_key(ip, user_key)
    # Decide against every budget before spending any of them, so a request rejected
    # by one limiter does not leave a hit recorded on another.
    retry_after = ratelimit.login_attempt_limiter.check(pair_key)
    if retry_after is None:
        retry_after = ratelimit.login_ip_limiter.check(ip)
    if retry_after is None:
        retry_after = ratelimit.login_user_limiter.check(user_key)
    if retry_after is None:
        ratelimit.login_attempt_limiter.record(pair_key)
        ratelimit.login_ip_limiter.record(ip)
    if retry_after is not None:
        # Sampled, not per attempt: see login_audit_limiter. A rejected caller can
        # retry without limit, so a row per rejection would turn a throttled flood
        # into unbounded writes to the audit table — the cost the throttle exists to
        # avoid. One row per key per window shows that throttling happened.
        if ratelimit.login_audit_limiter.hit(pair_key or ip or user_key) is None:
            _audit(db, "login_rate_limited", request, severity="warning",
                   detail={"username": req.username})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please wait and try again.",
            headers={"Retry-After": str(retry_after)},
        )

    user = crud.get_user_by_username(db, req.username)
    # Always run a bcrypt verification (against a dummy hash when the user is unknown)
    # so the response time does not reveal whether the username exists.
    password_ok = _verify_password(req.password, user.hashed_password if user else _DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        ratelimit.login_user_limiter.record(user_key)
        _audit(db, "login_failure", request, severity="warning",
               detail={"username": req.username})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.active:
        _audit(db, "login_failure", request, severity="warning",
               detail={"username": req.username, "reason": "account_disabled"})
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been disabled")
    if user.password_login_disabled:
        _audit(db, "login_failure", request, severity="warning",
               detail={"username": req.username, "reason": "password_login_disabled"})
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password login is disabled for this account")
    # Genuine owner proved themselves — drop the failure history so a burst of
    # typos does not keep throttling them. The per-IP ceiling is deliberately not
    # cleared: it bounds work per address, and letting one valid credential reset it
    # would hand an attacker with any account an unlimited bcrypt budget.
    ratelimit.login_user_limiter.reset(user_key)
    ratelimit.login_attempt_limiter.reset(pair_key)
    _audit(db, "login_success", request, user=user)
    return TokenResponse(access_token=_create_token(user))


# ---------------------------------------------------------------------------
# Server configuration
# ---------------------------------------------------------------------------

class ServerConfigResponse(BaseModel):
    """Deployment switches the console needs in order to render honestly."""
    recoverable_keys_allowed: bool
    logprobs_max_bytes: int


@router.get("/config", response_model=ServerConfigResponse)
def get_server_config(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Report deployment-wide toggles. Without this the console would offer a
    "let me reveal this later" checkbox on a deployment that forbids it, and the
    create call would fail after the user had filled the form in.

    The logprobs cap is here rather than admin-only because every caller needs it:
    it decides how many tokens of logprobs a request can ask for before being
    rejected, so a non-admin sizing a batch job has to be able to read it.
    """
    return ServerConfigResponse(
        recoverable_keys_allowed=recoverable_keys_allowed(),
        logprobs_max_bytes=server_settings.get_setting(db, server_settings.LOGPROBS_MAX_BYTES),
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UpdateMeRequest(BaseModel):
    # Profile fields — no password confirmation required.
    name: Optional[str] = Field(None, max_length=_MAX_NAME_LEN)
    email: Optional[str] = Field(None, max_length=_MAX_EMAIL_LEN)
    # Password change — both must be provided together.
    current_password: Optional[str] = Field(None, max_length=_MAX_PASSWORD_LEN)
    new_password: Optional[str] = Field(None, min_length=_MIN_PASSWORD_LEN, max_length=72)


class UpdateMeResponse(BaseModel):
    user: UserResponse
    # Fresh token signed with the bumped token_version — the caller's current token
    # is invalidated by a password change, so without this every password change
    # immediately logged the user out. None when only profile fields changed.
    access_token: Optional[str] = None
    token_type: str = "bearer"


@router.get("/users/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return _user_response(current_user)


@router.put("/users/me", response_model=UpdateMeResponse)
def update_me(
    req: UpdateMeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    changes = {}
    new_token: Optional[str] = None

    if req.new_password is not None:
        if not req.current_password or not _verify_password(req.current_password, current_user.hashed_password):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
        current_user.hashed_password = hash_password(req.new_password)
        current_user.token_version = (current_user.token_version or 0) + 1
        changes["password_changed"] = True

    if "name" in req.model_fields_set and (req.name or None) != current_user.name:
        current_user.name = req.name or None
        changes["name"] = req.name
    if "email" in req.model_fields_set and (req.email or None) != current_user.email:
        # Throttled: a duplicate address comes back as a 409, which would otherwise
        # let any signed-in user test addresses one at a time and learn who holds an
        # account here. See profile_email_limiter for why the limit sits here rather
        # than in the error message.
        retry_after = ratelimit.profile_email_limiter.hit(str(current_user.id))
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many email changes. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        current_user.email = req.email or None
        changes["email"] = req.email

    if changes:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This email is already in use")
        db.refresh(current_user)
        action = "password_changed" if changes.get("password_changed") else "profile_updated"
        _audit(db, action, request, user=current_user,
               resource_type="user", resource_id=str(current_user.id),
               detail={k: v for k, v in changes.items() if k != "password_changed"} or None)
        if changes.get("password_changed"):
            new_token = _create_token(current_user)
    return UpdateMeResponse(user=_user_response(current_user), access_token=new_token)


@router.get("/users", response_model=List[UserResponse])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [_user_response(u) for u in crud.list_users(db)]


@router.post("/users", response_model=CreateUserResponse, status_code=201)
def create_user(req: CreateUserRequest, request: Request,                admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if crud.get_user_by_username(db, req.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    try:
        user, plaintext_key = crud.create_user(
            db=db,
            username=req.username,
            name=req.name or None,
            hashed_password=hash_password(req.password),
            email=req.email or None,
            global_role=req.global_role,
        )
    except IntegrityError:
        # Lost a check-then-insert race (or duplicate email) — 409, not a 500.
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or email already exists")
    _audit(db, "user_created", request, user=admin,
           resource_type="user", resource_id=str(user.id),
           detail={"username": user.username, "role": user.global_role})
    return CreateUserResponse(user=_user_response(user), api_key=plaintext_key)


class AdminUpdateUserRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=_MAX_NAME_LEN)
    global_role: Optional[GlobalRole] = None
    new_password: Optional[str] = Field(None, min_length=_MIN_PASSWORD_LEN, max_length=72)
    password_login_disabled: Optional[bool] = None
    active: Optional[bool] = None


class AdminUpdateUserResponse(UserResponse):
    # Set only when this update promoted a viewer, which provisions the personal
    # project and first key the account would have been given at creation. Shown
    # once, exactly like the key from creating a user.
    api_key: Optional[str] = None


@router.put("/users/{user_id}", response_model=AdminUpdateUserResponse)
def admin_update_user(user_id: int, req: AdminUpdateUserRequest, request: Request,
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

    # A present-but-unchanged field is not a change: clients (the admin UI included)
    # send the full form, and only real changes may invalidate the target's sessions
    # or appear in the audit detail.
    changes = {}
    invalidate_sessions = False
    if req.name is not None and (req.name or None) != target.name:
        target.name = req.name or None
        changes["name"] = req.name
    if req.global_role is not None and req.global_role != target.global_role:
        target.global_role = req.global_role
        changes["global_role"] = req.global_role
        invalidate_sessions = True
    if req.new_password is not None:
        target.hashed_password = hash_password(req.new_password)
        changes["password_reset"] = True
        invalidate_sessions = True
    if req.password_login_disabled is not None and req.password_login_disabled != target.password_login_disabled:
        target.password_login_disabled = req.password_login_disabled
        changes["password_login_disabled"] = req.password_login_disabled
    if req.active is not None and req.active != target.active:
        target.active = req.active
        changes["active"] = req.active
        invalidate_sessions = True
    if invalidate_sessions:
        target.token_version = (target.token_version or 0) + 1
    db.commit()
    db.refresh(target)

    # A viewer is created with no personal project, so promoting one to a developer
    # role has to provision it now — otherwise the account can hold no keys of its
    # own and every "create a key" path has nowhere to put them.
    plaintext_key = None
    if target.global_role != "viewer":
        plaintext_key = crud.ensure_personal_project(db, target)
        if plaintext_key:
            changes["personal_project_created"] = True

    _audit(db, "user_updated", request, user=admin,
           resource_type="user", resource_id=str(user_id), detail=changes)
    return AdminUpdateUserResponse(**_user_response(target).model_dump(),
                                   api_key=plaintext_key)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects", response_model=List[ProjectResponse])
def list_projects(
    include_archived: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    projects = crud.get_projects_for_user(
        db, current_user.id,
        is_admin=current_user.global_role == "admin",
        include_archived=include_archived,
    )
    counts = crud.get_project_counts(db, [p.id for p in projects])
    personal_ids = crud.personal_project_ids(db, [p.id for p in projects])
    return [_project_response(p, counts.get(p.id), p.id in personal_ids) for p in projects]


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
    return _project_response(p, personal=crud.is_personal_project(db, project_id))


@router.post("/projects", response_model=ProjectResponse, status_code=201)
def create_project(req: CreateProjectRequest, request: Request,                   admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if crud.get_project_by_name(db, req.name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A project with this name already exists")
    try:
        project = crud.create_project(db, name=req.name, description=req.description, creator_id=admin.id)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A project with this name already exists")
    _audit(db, "project_created", request, user=admin,
           resource_type="project", resource_id=str(project.id),
           detail={"name": project.name})
    return _project_response(project)


@router.put("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int,
    req: UpdateProjectRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_project_admin(db, current_user, project_id)
    project = crud.get_project_by_id(db, project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    changes = {}
    if req.name is not None and req.name != project.name:
        existing = crud.get_project_by_name(db, req.name)
        if existing and existing.id != project_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A project with this name already exists")
        changes["name"] = req.name
    if "description" in req.model_fields_set and req.description != project.description:
        changes["description"] = req.description
    if req.archived is not None and req.archived != project.archived:
        changes["archived"] = req.archived

    if changes:
        try:
            project = crud.update_project(db, project_id, **changes)
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A project with this name already exists")
        action = "project_archived" if changes.get("archived") else (
            "project_unarchived" if changes.get("archived") is False else "project_updated")
        _audit(db, action, request, user=current_user,
               resource_type="project", resource_id=str(project_id), detail=changes)
    return _project_response(project)


# ---------------------------------------------------------------------------
# Project Members
# ---------------------------------------------------------------------------

class ProjectMemberResponse(BaseModel):
    user_id: int
    username: str
    email: Optional[str]
    role: str
    # Disabled accounts keep their membership; the UI flags them rather than
    # showing them as ordinary members.
    active: bool = True


class MemberCandidateResponse(BaseModel):
    # Username only. Picking someone to add needs an identifier, not their contact
    # details, and this list is readable by project admins rather than global ones.
    id: int
    username: str


class AddMemberRequest(BaseModel):
    user_id: int
    role: ProjectRole = "viewer"


class UpdateMemberRoleRequest(BaseModel):
    role: ProjectRole


def _member_response(access, user) -> ProjectMemberResponse:
    return ProjectMemberResponse(
        user_id=user.id, username=user.username, email=user.email,
        role=access.role, active=user.active,
    )


def _require_project_admin(db, current_user, project_id):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")


def _reject_self_membership_change(current_user, user_id, action):
    """
    An admin may not change or remove their own membership row.

    Demoting or removing yourself takes away the very right you used to do it,
    and on a project with a single admin that leaves nobody who can manage
    members. It has to come from another admin.
    """
    if current_user.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"You cannot {action} your own membership — ask another project admin",
        )


def _reject_personal_project(db, project_id):
    """
    Membership does not apply to a personal project.

    Every non-viewer account is created owning one, and is its admin. That made
    project-admin a right everybody held, so any user could reach the endpoints
    below on their own project — and the candidate list, which answers "who else
    could join", was the whole active-user directory including email addresses.
    GET /users deliberately restricts exactly that to global admins.

    A personal project is standalone: it belongs to one account and is not a place
    other people are invited into. Refusing membership operations on it removes the
    pivot, and stops an owner removing themselves from the project holding their
    own API keys.
    """
    if crud.is_personal_project(db, project_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This is a personal project and cannot have members. "
                   "Ask an administrator for a shared project.",
        )


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


@router.get("/projects/{project_id}/member-candidates", response_model=List[MemberCandidateResponse])
def list_member_candidates(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Users who can still be added to this project.

    Project admins need this list to add members, but GET /users is global-admin
    only — without its own endpoint the add-member form is empty for them.
    """
    _require_project_admin(db, current_user, project_id)
    if not crud.get_project_by_id(db, project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    _reject_personal_project(db, project_id)
    return [MemberCandidateResponse(id=u.id, username=u.username)
            for u in crud.list_member_candidates(db, project_id)]


@router.post("/projects/{project_id}/members", response_model=ProjectMemberResponse, status_code=201)
def add_member(
    project_id: int,
    req: AddMemberRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_project_admin(db, current_user, project_id)
    if not crud.get_project_by_id(db, project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    _reject_personal_project(db, project_id)
    existing = crud.get_user_project_role(db, req.user_id, project_id)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")
    target = crud.get_user_by_id_any(db, req.user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not target.active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"'{target.username}' is disabled — re-enable the account before adding it")
    try:
        access = crud.add_user_to_project(db, user_id=req.user_id, project_id=project_id, role=req.role)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")
    _audit(db, "project_member_added", request, user=current_user,
           resource_type="project", resource_id=str(project_id),
           detail={"user_id": req.user_id, "role": req.role})
    return _member_response(access, target)


@router.put("/projects/{project_id}/members/{user_id}", response_model=ProjectMemberResponse)
def update_member_role(
    project_id: int,
    user_id: int,
    req: UpdateMemberRoleRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_project_admin(db, current_user, project_id)
    _reject_personal_project(db, project_id)
    _reject_self_membership_change(current_user, user_id, "change the role of")
    access = crud.update_member_role(db, project_id=project_id, user_id=user_id, role=req.role)
    if not access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    # get_user_by_id_any: the member may be a disabled account, and the role
    # change has already been committed — looking it up as active-only left the
    # response building against None (500) with the change silently applied.
    target = crud.get_user_by_id_any(db, user_id)
    _audit(db, "project_member_role_updated", request, user=current_user,
           resource_type="project", resource_id=str(project_id),
           detail={"user_id": user_id, "role": req.role})
    return _member_response(access, target)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_member(
    project_id: int,
    user_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_project_admin(db, current_user, project_id)
    _reject_personal_project(db, project_id)
    _reject_self_membership_change(current_user, user_id, "remove")
    if not crud.remove_project_member(db, project_id=project_id, user_id=user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    _audit(db, "project_member_removed", request, user=current_user,
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")
    project = crud.get_project_by_id(db, project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.archived:
        # The proxy refuses every request from an archived project's keys, so a key
        # minted here would be dead on arrival — and its owner would only find that
        # out at the first call.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This project is archived. Restore it before creating keys.",
        )
    if req.recoverable and not recoverable_keys_allowed():
        # Fail loudly. Silently creating a show-once key would leave the caller
        # believing they can retrieve it later, and they would find out only after
        # losing it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Recoverable keys are disabled on this deployment (UNILLM_RECOVERABLE_KEYS=false)",
        )
    key_obj, plaintext_key = crud.create_api_key(
        db, project_id=project_id, name=req.name, allowed_models=req.allowed_models,
        recoverable=req.recoverable,
    )
    # Recoverability is a security-relevant choice, so record which way it went.
    _audit(db, "api_key_created", request, user=current_user,
           resource_type="api_key", resource_id=str(key_obj.id),
           detail={"name": key_obj.name, "project_id": project_id,
                   "allowed_models": key_obj.allowed_models,
                   "recoverable": bool(key_obj.key_ciphertext)})
    return CreateAPIKeyResponse(key=_key_response(key_obj), api_key=plaintext_key)


class UpdateAPIKeyRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=_MAX_NAME_LEN)
    # None → leave unchanged; [] → no access
    allowed_models: Optional[List[str]] = Field(None, max_length=_MAX_MODEL_LIST_LEN)


@router.put("/keys/{key_id}", response_model=APIKeyResponse)
def update_api_key(
    key_id: int,
    req: UpdateAPIKeyRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    key = db.query(APIKey).filter(APIKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    role = crud.get_user_project_role(db, current_user.id, key.project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")
    if not key.active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot edit a revoked key")
    # The UI sends null for "all models" — normalize like key creation does.
    allowed = req.allowed_models
    if "allowed_models" in req.model_fields_set and allowed is None:
        allowed = ["all"]
    key = crud.update_api_key(db, key_id, name=req.name, allowed_models=allowed)
    _audit(db, "api_key_updated", request, user=current_user,
           resource_type="api_key", resource_id=str(key_id),
           detail={"name": key.name, "allowed_models": key.allowed_models})
    return _key_response(key)


@router.get("/keys/{key_id}/reveal")
def reveal_api_key(
    key_id: int,
    request: Request,
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
    if not key.key_ciphertext or not recoverable_keys_allowed():
        # Either the key was created show-once, or the deployment has since
        # withdrawn recoverability altogether. Same answer both ways: there is
        # nothing here to give back.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This key was created without recovery. Revoke it and create a new one.",
        )
    try:
        plaintext = crud.decrypt_api_key(key.key_ciphertext)
    except Exception:
        # Encryption key changed since this key was created (e.g. rotated
        # UNILLM_ENCRYPTION_KEY or an ephemeral JWT secret) — unrecoverable.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Key cannot be decrypted (encryption key has changed). Revoke it and create a new one.",
        )
    _audit(db, "api_key_revealed", request, severity="warning", user=current_user,
           resource_type="api_key", resource_id=str(key_id),
           detail={"name": key.name, "project_id": key.project_id})
    return {"api_key": plaintext}


@router.delete("/keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: int,
    request: Request,
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
    _audit(db, "api_key_revoked", request, user=current_user,
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
def add_ssh_key(req: AddSSHKeyRequest, request: Request,                current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        key = crud.add_ssh_key(db, user_id=current_user.id, username=current_user.username, key_name=req.key_name, public_key=req.public_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Key name '{req.key_name}' is already in use")
    _audit(db, "ssh_key_added", request, user=current_user,
           resource_type="ssh_key", resource_id=str(key.id),
           detail={"key_name": key.key_name})
    return SSHKeyResponse(id=key.id, key_name=key.key_name, public_key=key.public_key,
                          created_at=key.created_at, last_used_at=key.last_used_at)


SSH_VALIDATE_CHALLENGE = "sk-12345678"


class ValidateSSHKeyRequest(BaseModel):
    signed_key: str = Field(..., max_length=_MAX_SIGNED_KEY_LEN)


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
    key_name: Optional[str] = Field(None, max_length=_MAX_NAME_LEN)
    public_key: Optional[str] = Field(None, max_length=_MAX_PUBLIC_KEY_LEN)


@router.put("/ssh-keys/{key_id}", response_model=SSHKeyResponse)
def update_ssh_key(key_id: int, req: UpdateSSHKeyRequest, request: Request,                   current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        key = crud.update_ssh_key(db, key_id=key_id, user_id=current_user.id, username=current_user.username,
                                  key_name=req.key_name, public_key=req.public_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Key name '{req.key_name}' is already in use")
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSH key not found")
    _audit(db, "ssh_key_updated", request, user=current_user,
           resource_type="ssh_key", resource_id=str(key_id), detail={"key_name": key.key_name})
    return SSHKeyResponse(id=key.id, key_name=key.key_name, public_key=key.public_key,
                          created_at=key.created_at, last_used_at=key.last_used_at)


@router.delete("/ssh-keys/{key_id}", status_code=204)
def delete_ssh_key(key_id: int, request: Request,                   current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not crud.delete_ssh_key(db, key_id=key_id, user_id=current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSH key not found")
    _audit(db, "ssh_key_deleted", request, user=current_user,
           resource_type="ssh_key", resource_id=str(key_id))


# ---------------------------------------------------------------------------
# Server Settings
# ---------------------------------------------------------------------------

@router.get("/settings", response_model=List[ServerSettingResponse])
def list_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [ServerSettingResponse(**row) for row in server_settings.describe(db)]


@router.put("/settings/{key}", response_model=ServerSettingResponse)
def update_setting(
    key: str,
    req: UpdateServerSettingRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    definition = server_settings.SETTINGS.get(key)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown setting '{key}'")
    try:
        value = definition.validate(req.value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    crud.set_server_setting(db, key=key, value=value, updated_by=admin.username)
    server_settings.invalidate_cache()
    _audit(db, "setting_updated", request, user=admin,
           resource_type="server_setting", resource_id=key, detail={"value": value})
    return _setting_response(db, key)


@router.delete("/settings/{key}", response_model=ServerSettingResponse)
def reset_setting(
    key: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Drop the override so the setting falls back to the config file or the default."""
    if key not in server_settings.SETTINGS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown setting '{key}'")
    crud.delete_server_setting(db, key)
    server_settings.invalidate_cache()
    _audit(db, "setting_reset", request, user=admin,
           resource_type="server_setting", resource_id=key)
    return _setting_response(db, key)


def _setting_response(db: Session, key: str) -> ServerSettingResponse:
    for row in server_settings.describe(db):
        if row["key"] == key:
            return ServerSettingResponse(**row)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown setting '{key}'")


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


# {model_name:path} — model aliases may contain '/' (e.g. vLLM backends like
# "Qwen/Qwen2.5"); a plain path param never matches them (ASGI decodes %2F
# before routing).
@router.put("/pricing/{model_name:path}", response_model=ModelPricingResponse)
def upsert_pricing(
    model_name: str,
    req: UpsertModelPricingRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    pricing = crud.upsert_model_pricing(
        db, model_name=model_name,
        input_per_1m=req.input_per_1m, output_per_1m=req.output_per_1m,
        currency=req.currency, notes=req.notes,
    )
    _audit(db, "pricing_updated", request, user=admin,
           resource_type="model_pricing", resource_id=model_name,
           detail={"input_per_1m": req.input_per_1m, "output_per_1m": req.output_per_1m})
    return _pricing_response(pricing)


@router.delete("/pricing/{model_name:path}", status_code=204)
def delete_pricing(
    model_name: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not crud.delete_model_pricing(db, model_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pricing not found")
    _audit(db, "pricing_deleted", request, user=admin,
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

def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Normalize a query-param datetime to naive UTC.

    DB timestamps are stored naive-UTC; clients send tz-aware values ('...Z').
    Comparing aware against naive only "works" on SQLite by string accident and
    breaks on other backends, so convert before filtering.
    """
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _resolve_project_filter(db, current_user, requested_ids: List[int]) -> Optional[List[int]]:
    """Return the effective project id list to filter by, or None (admin, no filter)."""
    if current_user.global_role == "admin":
        return requested_ids or None
    # Archived projects count here. Archiving takes a project out of the active list
    # and stops its keys working; it does not revoke anyone's membership, and it
    # should not make a member's own past usage disappear from their logs and stats
    # while an admin can still see all of it.
    accessible = [p.id for p in crud.get_projects_for_user(
        db, current_user.id, include_archived=True)]
    if requested_ids:
        return [i for i in requested_ids if i in accessible]
    return accessible


def _resolve_ssh_scope(current_user: User, mine: bool, ssh_username: Optional[str]) -> Optional[str]:
    """
    Resolve the effective ssh_username filter.

    `mine=true` always means the authenticated user's own username. A raw
    `ssh_username` value is admin-only — non-admins may only filter by their own
    username (anything else would let them read another user's per-user stats).
    """
    if mine:
        return current_user.username
    if ssh_username is not None and current_user.global_role != "admin" \
            and ssh_username != current_user.username:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You can only filter by your own SSH username")
    return ssh_username


@router.get("/logs/requests", response_model=PaginatedRequestLogs)
def get_request_logs(
    project_ids: List[int] = Query(default=[]),
    mine: bool = False,
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
    effective_ssh = _resolve_ssh_scope(current_user, mine, ssh_username)
    # When filtering by one's own SSH identity, don't also require a project match:
    # SSH-signed requests through env-var keys have no project_id, and they are
    # still the caller's own traffic.
    if mine:
        filter_ids = [i for i in project_ids] or None
    else:
        filter_ids = _resolve_project_filter(db, current_user, project_ids)

    rows, total = crud.query_request_logs(
        db, allowed_project_ids=filter_ids,
        ssh_username=effective_ssh,
        model=model, status_code=status_code,
        from_date=_naive_utc(from_date), to_date=_naive_utc(to_date),
        limit=limit, offset=offset,
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
        from_date=_naive_utc(from_date), to_date=_naive_utc(to_date),
        limit=limit, offset=offset,
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
    mine: bool = False,
    ssh_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    effective_ssh = _resolve_ssh_scope(current_user, mine, ssh_username)
    if mine:
        filter_ids = [i for i in project_ids] or None
    else:
        filter_ids = _resolve_project_filter(db, current_user, project_ids)
    return crud.get_request_stats(db, from_date=_naive_utc(from_date), to_date=_naive_utc(to_date),
                                  allowed_project_ids=filter_ids, ssh_username=effective_ssh)
