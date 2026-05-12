from datetime import datetime
from typing import List, Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from unillm.db.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    global_role: Mapped[str] = mapped_column(String, default="user")  # user | admin
    personal_project_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_login_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ssh_keys: Mapped[List["SSHKey"]] = relationship("SSHKey", back_populates="user", cascade="all, delete-orphan")
    project_access: Mapped[List["UserProjectAccess"]] = relationship("UserProjectAccess", back_populates="user", cascade="all, delete-orphan")
    personal_project: Mapped[Optional["Project"]] = relationship("Project", foreign_keys=[personal_project_id])


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    api_keys: Mapped[List["APIKey"]] = relationship("APIKey", back_populates="project", cascade="all, delete-orphan")
    user_access: Mapped[List["UserProjectAccess"]] = relationship("UserProjectAccess", back_populates="project", cascade="all, delete-orphan")


class UserProjectAccess(Base):
    __tablename__ = "user_project_access"
    __table_args__ = (UniqueConstraint("user_id", "project_id"),)

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, default="viewer")  # viewer | admin

    user: Mapped["User"] = relationship("User", back_populates="project_access")
    project: Mapped["Project"] = relationship("Project", back_populates="user_access")


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)  # first 8 chars for display
    key_ciphertext: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # Fernet-encrypted plaintext
    # ["all"] = unrestricted, [] = no access, ["model-a", "model-b"] = specific models
    allowed_models: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: ["all"])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="api_keys")


class SSHKey(Base):
    __tablename__ = "ssh_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    key_name: Mapped[str] = mapped_column(String, nullable=False)
    public_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="ssh_keys")


class ModelPricing(Base):
    """Per-model-alias pricing for cost calculation in request logs."""
    __tablename__ = "model_pricing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_name: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    input_per_1m: Mapped[float] = mapped_column(Float, nullable=False)   # USD per 1M input tokens
    output_per_1m: Mapped[float] = mapped_column(Float, nullable=False)  # USD per 1M output tokens
    currency: Mapped[str] = mapped_column(String, default="USD")
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Who made the request (plain int ref — no FK so logs survive user deletion)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True)
    api_key_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    api_key_prefix: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ssh_username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # What was requested
    model: Mapped[str] = mapped_column(String, nullable=False)
    backend_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    model_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    labels: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Result
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Tokens & cost
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Performance
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class AuditLog(Base):
    """Append-only security audit trail. Never updated or deleted."""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Who performed the action (plain int ref — no FK so audit trail survives user deletion)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # denormalized
    # What happened
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Request context
    ip_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Severity and detail
    severity: Mapped[str] = mapped_column(String, default="info")  # info | warning | critical
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
