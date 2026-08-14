"""Add user-visible conversations and the specialist shortcut catalog.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """前滚为多会话、全局单 run 和公开 Agent 目录建立事实约束。"""

    op.execute(
        """
        ALTER TABLE agent_definitions
          ADD COLUMN catalog_status varchar(16) NOT NULL DEFAULT 'HIDDEN',
          ADD COLUMN display_order smallint NOT NULL DEFAULT 0,
          ADD COLUMN catalog_metadata jsonb NOT NULL DEFAULT '{}',
          ADD CONSTRAINT ck_agent_definitions_catalog_status
            CHECK(catalog_status IN ('AVAILABLE','COMING_SOON','HIDDEN')),
          ADD CONSTRAINT ck_agent_definitions_available_active
            CHECK(catalog_status <> 'AVAILABLE' OR status = 'ACTIVE');
        CREATE INDEX ix_agent_definitions_catalog
          ON agent_definitions(catalog_status,display_order,code);

        UPDATE agent_definitions
        SET catalog_status='AVAILABLE', display_order=10,
            name='考公', description='规划备考与任务复盘',
            catalog_metadata='{
              "icon":"公",
              "welcome_message":"我是你的考公助理，可以帮你制定备考计划、安排每日任务、整理资料并复盘错题。告诉我你的目标考试和可投入时间，我们从最重要的一步开始。",
              "starter_prompts":[
                {"label":"制定备考计划","content":"根据我的目标考试和可投入时间，帮我制定备考计划"},
                {"label":"安排今日任务","content":"结合我的备考进度，帮我安排今天的学习任务"},
                {"label":"复盘错题","content":"帮我整理并复盘最近的错题，找出薄弱环节"}
              ]
            }'::jsonb
        WHERE code='CIVIL_SERVICE_EXAM' AND status='ACTIVE';
        INSERT INTO agent_definitions(
          id,code,version,name,description,graph_name,status,catalog_status,
          display_order,catalog_metadata
        ) VALUES
          ('dc5e3a6e-8042-5df2-ad2d-f2f49d410bb2','IELTS',1,'考雅思',
           '听说读写专项提升','unavailable','DRAFT','COMING_SOON',20,
           '{"icon":"雅","welcome_message":"","starter_prompts":[]}'::jsonb),
          ('fdc17590-121d-5b09-89be-282746bcd5fb','JOB_SEARCH',1,'求职',
           '简历、面试与投递规划','unavailable','DRAFT','COMING_SOON',30,
           '{"icon":"职","welcome_message":"","starter_prompts":[]}'::jsonb)
        ON CONFLICT(id) DO NOTHING;

        ALTER TABLE conversations
          ADD COLUMN client_conversation_id uuid,
          ADD COLUMN title varchar(200),
          ADD COLUMN status varchar(16) NOT NULL DEFAULT 'CURRENT',
          ADD COLUMN specialist_user_agent_id uuid REFERENCES user_agents(id),
          ADD COLUMN archived_at timestamptz,
          ADD CONSTRAINT ck_conversations_status CHECK(status IN ('CURRENT','ARCHIVED')),
          ADD CONSTRAINT ck_conversations_archived_at
            CHECK((status='CURRENT' AND archived_at IS NULL) OR status='ARCHIVED');

        UPDATE conversations AS c
        SET client_conversation_id = c.id,
            title = COALESCE(
              (
                SELECT NULLIF(left(regexp_replace(m.content, E'\\s+', ' ', 'g'), 24), '')
                FROM messages AS m
                WHERE m.conversation_id=c.id AND m.role='USER'
                ORDER BY m.created_at,m.id LIMIT 1
              ),
              '新的对话'
            );
        ALTER TABLE conversations
          ALTER COLUMN client_conversation_id SET NOT NULL,
          ALTER COLUMN title SET NOT NULL;
        ALTER TABLE conversations DROP CONSTRAINT conversations_user_id_user_agent_id_key;
        CREATE UNIQUE INDEX uq_conversations_client_id
          ON conversations(user_id,client_conversation_id);
        CREATE UNIQUE INDEX uq_conversations_current_user
          ON conversations(user_id) WHERE status='CURRENT';
        CREATE INDEX ix_conversations_user_timeline
          ON conversations(user_id,status,last_message_at DESC,id DESC);

        ALTER TABLE messages ADD COLUMN client_request_hash char(64);
        UPDATE messages
        SET client_message_id = COALESCE(client_message_id, 'migrated:' || id::text),
            client_request_hash = md5(content) || md5(content)
        WHERE role='USER';
        ALTER TABLE messages DROP CONSTRAINT messages_user_id_client_message_id_key;
        CREATE UNIQUE INDEX uq_messages_conversation_client_id
          ON messages(conversation_id,client_message_id)
          WHERE client_message_id IS NOT NULL;
        ALTER TABLE messages ADD CONSTRAINT ck_messages_user_idempotency
          CHECK(
            role <> 'USER'
            OR (client_message_id IS NOT NULL AND client_request_hash IS NOT NULL)
          );

        ALTER TABLE agent_runs
          ADD COLUMN selected_user_agent_id uuid REFERENCES user_agents(id);
        CREATE UNIQUE INDEX uq_user_active_run ON agent_runs(user_id)
          WHERE status IN (
            'QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL',
            'FAILED_RETRYABLE','CANCEL_REQUESTED'
          );
        """
    )


def downgrade() -> None:
    """仅为未承载多会话数据的验证环境恢复单聊天结构。"""

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM conversations GROUP BY user_id HAVING COUNT(*) > 1
          ) THEN
            RAISE EXCEPTION 'cannot downgrade while users have multiple conversations';
          END IF;
        END $$;

        DROP INDEX uq_user_active_run;
        ALTER TABLE agent_runs DROP COLUMN selected_user_agent_id;

        ALTER TABLE messages DROP CONSTRAINT ck_messages_user_idempotency;
        DROP INDEX uq_messages_conversation_client_id;
        ALTER TABLE messages ADD CONSTRAINT messages_user_id_client_message_id_key
          UNIQUE(user_id,client_message_id);
        ALTER TABLE messages DROP COLUMN client_request_hash;

        DROP INDEX ix_conversations_user_timeline;
        DROP INDEX uq_conversations_current_user;
        DROP INDEX uq_conversations_client_id;
        ALTER TABLE conversations
          ADD CONSTRAINT conversations_user_id_user_agent_id_key UNIQUE(user_id,user_agent_id),
          DROP CONSTRAINT ck_conversations_archived_at,
          DROP CONSTRAINT ck_conversations_status,
          DROP COLUMN archived_at,
          DROP COLUMN specialist_user_agent_id,
          DROP COLUMN status,
          DROP COLUMN title,
          DROP COLUMN client_conversation_id;

        DROP INDEX ix_agent_definitions_catalog;
        ALTER TABLE agent_definitions
          DROP CONSTRAINT ck_agent_definitions_available_active,
          DROP CONSTRAINT ck_agent_definitions_catalog_status,
          DROP COLUMN catalog_metadata,
          DROP COLUMN display_order,
          DROP COLUMN catalog_status;
        """
    )
