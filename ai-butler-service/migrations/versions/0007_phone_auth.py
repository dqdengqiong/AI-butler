"""Add phone-first identity and verification challenges.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加手机号隐私字段与登录挑战；存量账号由独立显式命令清理。"""

    op.execute(
        """
        ALTER TABLE users
          ADD COLUMN phone_ciphertext text,
          ADD COLUMN phone_hash char(64);
        CREATE UNIQUE INDEX uq_users_phone_hash ON users(phone_hash) WHERE phone_hash IS NOT NULL;

        CREATE TABLE phone_verification_challenges (
          id uuid PRIMARY KEY,
          phone_hash char(64) NOT NULL,
          code_hash char(64) NOT NULL,
          device_id varchar(128) NOT NULL,
          request_key_hash char(64) NOT NULL UNIQUE,
          provider_message_id varchar(255),
          status varchar(16) NOT NULL,
          attempt_count smallint NOT NULL DEFAULT 0,
          expires_at timestamptz NOT NULL,
          sent_at timestamptz,
          consumed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_phone_verification_status
            CHECK (status IN ('PENDING','SENT','FAILED','CONSUMED','LOCKED','EXPIRED')),
          CONSTRAINT ck_phone_verification_attempts CHECK (attempt_count BETWEEN 0 AND 5),
          CONSTRAINT ck_phone_verification_expiry CHECK (expires_at > created_at)
        );
        CREATE INDEX ix_phone_challenges_phone_created
          ON phone_verification_challenges(phone_hash,created_at DESC);
        CREATE INDEX ix_phone_challenges_device_created
          ON phone_verification_challenges(device_id,created_at DESC);
        GRANT SELECT,INSERT,UPDATE,DELETE ON phone_verification_challenges
          TO butler_app, butler_test;
        """
    )


def downgrade() -> None:
    """移除手机号登录结构；不会恢复已由显式命令删除的用户数据。"""

    op.execute(
        """
        DROP TABLE phone_verification_challenges;
        DROP INDEX uq_users_phone_hash;
        ALTER TABLE users DROP COLUMN phone_hash, DROP COLUMN phone_ciphertext;
        """
    )
