"""Add soft deletion for user-visible conversations.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加会话软删除时间，并让时间线索引只覆盖用户可见会话。"""

    op.execute(
        """
        ALTER TABLE conversations ADD COLUMN deleted_at timestamptz;
        DROP INDEX ix_conversations_user_timeline;
        CREATE INDEX ix_conversations_user_timeline
          ON conversations(user_id,status,last_message_at DESC,id DESC)
          WHERE deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    """移除会话软删除能力；已有软删除记录会重新可见。"""

    op.execute(
        """
        DROP INDEX ix_conversations_user_timeline;
        CREATE INDEX ix_conversations_user_timeline
          ON conversations(user_id,status,last_message_at DESC,id DESC);
        ALTER TABLE conversations DROP COLUMN deleted_at;
        """
    )
