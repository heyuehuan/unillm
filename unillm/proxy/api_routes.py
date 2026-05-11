"""
Management REST API routes for UniLLM.

Endpoints for user, project, API key, and SSH key management.
All management endpoints require JWT authentication via POST /api/auth/login.
"""

import os
from datetime import datetime, timedelta
from typing import List, Optional

import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = crud.get_user_by_username(db, req.username)
    if not user or not _verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
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
def create_user(req: CreateUserRequest, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    if crud.get_user_by_username(db, req.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    user, plaintext_key = crud.create_user(
        db=db,
        username=req.username,
        hashed_password=hash_password(req.password),
        email=req.email,
        global_role=req.global_role,
    )
    return CreateUserResponse(user=_user_response(user), api_key=plaintext_key)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects", response_model=List[ProjectResponse])
def list_projects(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [_project_response(p) for p in crud.get_projects_for_user(db, current_user.id)]


@router.post("/projects", response_model=ProjectResponse, status_code=201)
def create_project(req: CreateProjectRequest, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    return _project_response(crud.create_project(db, name=req.name, description=req.description))


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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = crud.get_user_project_role(db, current_user.id, project_id)
    if role != "admin" and current_user.global_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project admin access required")
    key_obj, plaintext_key = crud.create_api_key(
        db, project_id=project_id, name=req.name, allowed_models=req.allowed_models,
    )
    return CreateAPIKeyResponse(key=_key_response(key_obj), api_key=plaintext_key)


@router.delete("/keys/{key_id}", status_code=204)
def revoke_api_key(
    key_id: int,
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


# ---------------------------------------------------------------------------
# SSH Keys
# ---------------------------------------------------------------------------

@router.get("/ssh-keys", response_model=List[SSHKeyResponse])
def list_ssh_keys(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    keys = crud.get_user_ssh_keys(db, current_user.id)
    return [SSHKeyResponse(id=k.id, key_name=k.key_name, public_key=k.public_key, created_at=k.created_at)
            for k in keys]


@router.post("/ssh-keys", response_model=SSHKeyResponse, status_code=201)
def add_ssh_key(req: AddSSHKeyRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        key = crud.add_ssh_key(db, user_id=current_user.id, key_name=req.key_name, public_key=req.public_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return SSHKeyResponse(id=key.id, key_name=key.key_name, public_key=key.public_key, created_at=key.created_at)


@router.delete("/ssh-keys/{key_id}", status_code=204)
def delete_ssh_key(key_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not crud.delete_ssh_key(db, key_id=key_id, user_id=current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSH key not found")
