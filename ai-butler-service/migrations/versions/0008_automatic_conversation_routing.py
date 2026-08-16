"""Add automatic conversation routing constraints.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许跨会话挂起交互，仅限制真正执行中的用户级 run。"""

    op.execute(
        """
        DROP INDEX uq_user_active_run;
        CREATE UNIQUE INDEX uq_user_executing_run ON agent_runs(user_id)
          WHERE status IN ('QUEUED','RUNNING','CANCEL_REQUESTED');

        DROP INDEX uq_messages_conversation_client_id;
        CREATE UNIQUE INDEX uq_messages_user_client_id ON messages(user_id,client_message_id)
          WHERE client_message_id IS NOT NULL;

        ALTER TABLE conversations ADD COLUMN archive_reason varchar(32);
        ALTER TABLE conversations ADD CONSTRAINT ck_conversations_archive_reason CHECK (
          archive_reason IS NULL OR archive_reason IN (
            'TOPIC_SWITCH','SPECIALIST_SWITCH','HISTORY_RESUME','WORKFLOW_EXIT'
          )
        );
        """
    )


def downgrade() -> None:
    """恢复用户全局非终态 run 约束与会话内消息幂等范围。"""

    op.execute(
        """
        ALTER TABLE conversations DROP CONSTRAINT ck_conversations_archive_reason;
        ALTER TABLE conversations DROP COLUMN archive_reason;

        DROP INDEX uq_messages_user_client_id;
        CREATE UNIQUE INDEX uq_messages_conversation_client_id
          ON messages(conversation_id,client_message_id)
          WHERE client_message_id IS NOT NULL;

        DROP INDEX uq_user_executing_run;
        CREATE UNIQUE INDEX uq_user_active_run ON agent_runs(user_id)
          WHERE status IN (
            'QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL',
            'FAILED_RETRYABLE','CANCEL_REQUESTED'
          );
        """
    )
