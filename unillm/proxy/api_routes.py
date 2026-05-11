"""
Management REST API routes for UniLLM.

Endpoints for user, project, API key, and SSH key management.
All management endpoints require JWT authentication via POST /api/auth/login.
"""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import bcrypt as _bcrypt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from unillm.db import get_db
from unillm.db import crud
from unillm.db.models import APIKey, User

router = APIRouter(prefix="/api", tags=["management"])

# JWT configuration — set UNILLM_JWT_SECRET in production
_JWT_SECRET = os.getenv("UNILLM_JWT_SECRET", "change-me-in-production")
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = 24

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
    email: Optional[str]
    global_role: str
    created_at: datetime


class CreateUserRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None
    global_role: str = "user"


class CreateUserResponse(BaseModel):
    user: UserResponse
    api_key: str  # plaintext, shown once


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


class AddSSHKeyRequest(BaseModel):
    key_name: str
    public_key: str


class RequestLogResponse(BaseModel):
    id: int
    request_id: Optional[str]
    created_at: datetime
    project_id: Optional[int]
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

def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


def _create_token(user_id: int, role: str) -> str:
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=_JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(credentials.credentials, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
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
    return UserResponse(id=u.id, username=u.username, email=u.email,
                        global_role=u.global_role, created_at=u.created_at)


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
    """Schedule an audit log write as a background task."""
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    background_tasks.add_task(
        crud.create_audit_log,
        db=db,
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
    if not user or not _verify_password(req.password, user.hashed_password):
        _audit(background_tasks, db, "login_failure", request, severity="warning",
               detail={"username": req.username})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    _audit(background_tasks, db, "login_success", request, user=user)
    return TokenResponse(access_token=_create_token(user.id, user.global_role))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
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
        hashed_password=hash_password(req.password),
        email=req.email,
        global_role=req.global_role,
    )
    _audit(background_tasks, db, "user_created", request, user=admin,
           resource_type="user", resource_id=str(user.id),
           detail={"username": user.username, "role": user.global_role})
    return CreateUserResponse(user=_user_response(user), api_key=plaintext_key)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects", response_model=List[ProjectResponse])
def list_projects(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [_project_response(p) for p in crud.get_projects_for_user(db, current_user.id)]


@router.post("/projects", response_model=ProjectResponse, status_code=201)
def create_project(req: CreateProjectRequest, request: Request, background_tasks: BackgroundTasks,
                   admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    project = crud.create_project(db, name=req.name, description=req.description)
    _audit(background_tasks, db, "project_created", request, user=admin,
           resource_type="project", resource_id=str(project.id),
           detail={"name": project.name})
    return _project_response(project)


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
    return [SSHKeyResponse(id=k.id, key_name=k.key_name, public_key=k.public_key, created_at=k.created_at)
            for k in keys]


@router.post("/ssh-keys", response_model=SSHKeyResponse, status_code=201)
def add_ssh_key(req: AddSSHKeyRequest, request: Request, background_tasks: BackgroundTasks,
                current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        key = crud.add_ssh_key(db, user_id=current_user.id, key_name=req.key_name, public_key=req.public_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    _audit(background_tasks, db, "ssh_key_added", request, user=current_user,
           resource_type="ssh_key", resource_id=str(key.id),
           detail={"key_name": key.key_name})
    return SSHKeyResponse(id=key.id, key_name=key.key_name, public_key=key.public_key, created_at=key.created_at)


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
# Logs
# ---------------------------------------------------------------------------

@router.get("/logs/requests", response_model=PaginatedRequestLogs)
def get_request_logs(
    project_id: Optional[int] = None,
    model: Optional[str] = None,
    status_code: Optional[int] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Non-admins can only see their own project logs
    if current_user.global_role != "admin":
        if project_id is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Specify a project_id you have access to")
        role = crud.get_user_project_role(db, current_user.id, project_id)
        if not role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this project")

    rows, total = crud.query_request_logs(
        db, project_id=project_id, model=model, status_code=status_code,
        from_date=from_date, to_date=to_date, limit=limit, offset=offset,
    )
    items = [RequestLogResponse(
        id=r.id, request_id=r.request_id, created_at=r.created_at,
        project_id=r.project_id, api_key_name=r.api_key_name, api_key_prefix=r.api_key_prefix,
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
    limit: int = 50,
    offset: int = 0,
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
    project_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.global_role != "admin":
        if project_id is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Specify a project_id you have access to")
        role = crud.get_user_project_role(db, current_user.id, project_id)
        if not role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this project")

    return crud.get_request_stats(db, from_date=from_date, to_date=to_date, project_id=project_id)
