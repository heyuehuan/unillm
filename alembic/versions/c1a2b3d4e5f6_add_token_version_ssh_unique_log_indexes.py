"""add token_version, ssh key uniqueness, request_log indexes

Revision ID: c1a2b3d4e5f6
Revises: 7890f75152ae
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1a2b3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "7890f75152ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
        )

    with op.batch_alter_table("ssh_keys") as batch_op:
        batch_op.create_unique_constraint("uq_ssh_keys_user_key_name", ["user_id", "key_name"])

    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.create_index("ix_request_logs_created_at", ["created_at"])
        batch_op.create_index("ix_request_logs_project_id", ["project_id"])
        batch_op.create_index("ix_request_logs_model", ["model"])


def downgrade() -> None:
    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.drop_index("ix_request_logs_model")
        batch_op.drop_index("ix_request_logs_project_id")
        batch_op.drop_index("ix_request_logs_created_at")

    with op.batch_alter_table("ssh_keys") as batch_op:
        batch_op.drop_constraint("uq_ssh_keys_user_key_name", type_="unique")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("token_version")
