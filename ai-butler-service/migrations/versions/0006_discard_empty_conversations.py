"""Discard empty conversations instead of archiving them.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """仅约束可见当前会话，并清理升级前遗留的空历史会话。"""

    op.execute(
        """
        DROP INDEX uq_conversations_current_user;
        CREATE UNIQUE INDEX uq_conversations_current_user
          ON conversations(user_id)
          WHERE status='CURRENT' AND deleted_at IS NULL;

        UPDATE conversations c
        SET deleted_at=now(),updated_at=now()
        WHERE c.status='ARCHIVED'
          AND c.deleted_at IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM messages m
            WHERE m.conversation_id=c.id AND m.role='USER'
          );
        """
    )


def downgrade() -> None:
    """恢复旧索引；软删除的 CURRENT 必须先转为归档以满足旧约束。"""

    op.execute(
        """
        DROP INDEX uq_conversations_current_user;
        UPDATE conversations
        SET status='ARCHIVED',archived_at=COALESCE(archived_at,deleted_at),updated_at=now()
        WHERE status='CURRENT' AND deleted_at IS NOT NULL;
        CREATE UNIQUE INDEX uq_conversations_current_user
          ON conversations(user_id) WHERE status='CURRENT';
        """
    )
