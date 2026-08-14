"""Create the AI Butler verification-version business schema.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """前滚创建验证版表；业务外键均以 PostgreSQL 事实表为准。"""

    op.execute(
        """
        ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_status;
        ALTER TABLE users
          ADD COLUMN nickname varchar(64),
          ADD COLUMN locale varchar(16) NOT NULL DEFAULT 'zh-CN',
          ADD COLUMN timezone varchar(64) NOT NULL DEFAULT 'Asia/Shanghai',
          ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
          ADD COLUMN deleted_at timestamptz;
        ALTER TABLE users ADD CONSTRAINT ck_users_status
          CHECK (status IN ('ACTIVE','SUSPENDED','DELETING','DELETED'));

        CREATE TABLE user_identities (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          provider varchar(32) NOT NULL, provider_subject varchar(255) NOT NULL,
          union_subject varchar(255), created_at timestamptz NOT NULL DEFAULT now(),
          last_login_at timestamptz, UNIQUE(provider, provider_subject)
        );
        CREATE INDEX ix_user_identities_user ON user_identities(user_id);

        CREATE TABLE auth_sessions (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          refresh_token_hash char(64) NOT NULL UNIQUE, device_id varchar(128),
          status varchar(16) NOT NULL, expires_at timestamptz NOT NULL,
          last_used_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
          revoked_at timestamptz,
          CONSTRAINT ck_auth_sessions_status CHECK (status IN ('ACTIVE','REVOKED','EXPIRED')),
          CONSTRAINT ck_auth_sessions_expiry CHECK (expires_at > created_at)
        );
        CREATE INDEX ix_auth_sessions_user_status ON auth_sessions(user_id,status,expires_at);

        CREATE TABLE user_profiles (
          user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          education_level varchar(32), major varchar(128), region_code varchar(32),
          current_level varchar(32), existing_materials jsonb NOT NULL DEFAULT '[]',
          notification_preferences jsonb NOT NULL DEFAULT '{}',
          profile_version integer NOT NULL DEFAULT 1 CHECK (profile_version > 0),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE study_availability (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          day_of_week smallint, start_time time, end_time time, available_minutes smallint NOT NULL,
          effective_from date NOT NULL, effective_to date,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK (day_of_week IS NULL OR day_of_week BETWEEN 1 AND 7),
          CHECK ((start_time IS NULL) = (end_time IS NULL)),
          CHECK (start_time IS NULL OR end_time > start_time),
          CHECK (available_minutes BETWEEN 1 AND 1440),
          CHECK (effective_to IS NULL OR effective_to >= effective_from)
        );

        CREATE TABLE agent_definitions (
          id uuid PRIMARY KEY, code varchar(64) NOT NULL, version integer NOT NULL,
          name varchar(128) NOT NULL, description text, graph_name varchar(128) NOT NULL,
          status varchar(16) NOT NULL, default_config jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(code,version), CHECK(version > 0), CHECK(status IN ('DRAFT','ACTIVE','RETIRED'))
        );
        CREATE UNIQUE INDEX uq_agent_definition_active ON agent_definitions(code) WHERE status='ACTIVE';
        CREATE TABLE user_agents (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          agent_definition_id uuid NOT NULL REFERENCES agent_definitions(id), status varchar(16) NOT NULL,
          config jsonb NOT NULL DEFAULT '{}', activated_at timestamptz, completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(user_id,agent_definition_id)
        );

        CREATE TABLE conversations (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          user_agent_id uuid NOT NULL REFERENCES user_agents(id), active_segment_id uuid,
          context_version integer NOT NULL DEFAULT 1 CHECK(context_version > 0),
          last_message_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,user_agent_id)
        );
        CREATE TABLE conversation_segments (
          id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, sequence integer NOT NULL,
          thread_id varchar(128) NOT NULL UNIQUE, status varchar(16) NOT NULL,
          estimated_tokens integer NOT NULL DEFAULT 0, start_message_id uuid, end_message_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(), archived_at timestamptz,
          UNIQUE(conversation_id,sequence), CHECK(status IN ('ACTIVE','ARCHIVING','ARCHIVED'))
        );
        ALTER TABLE conversations ADD CONSTRAINT fk_conversations_active_segment
          FOREIGN KEY(active_segment_id) REFERENCES conversation_segments(id);
        CREATE TABLE conversation_summaries (
          id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          segment_id uuid REFERENCES conversation_segments(id) ON DELETE CASCADE,
          summary_type varchar(24) NOT NULL, version integer NOT NULL, content jsonb NOT NULL,
          source_message_count integer NOT NULL DEFAULT 0, token_count integer NOT NULL DEFAULT 0,
          status varchar(16) NOT NULL DEFAULT 'PUBLISHED', created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(conversation_id,segment_id,summary_type,version)
        );
        CREATE TABLE messages (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          segment_id uuid NOT NULL REFERENCES conversation_segments(id), agent_run_id uuid,
          client_message_id varchar(128), role varchar(20) NOT NULL, status varchar(20) NOT NULL,
          content text NOT NULL DEFAULT '', structured_content jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(user_id,client_message_id),
          CHECK(role IN ('USER','ASSISTANT','SYSTEM_EVENT')),
          CHECK(status IN ('PENDING','STREAMING','COMPLETED','FAILED','CANCELLED'))
        );
        CREATE INDEX ix_messages_timeline ON messages(conversation_id,created_at DESC,id DESC);

        CREATE TABLE agent_runs (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          segment_id uuid NOT NULL REFERENCES conversation_segments(id),
          request_message_id uuid NOT NULL REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED,
          response_message_id uuid NOT NULL REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED,
          status varchar(24) NOT NULL,
          pending_action varchar(24) NOT NULL DEFAULT 'START', pending_action_key varchar(128) NOT NULL,
          attempt integer NOT NULL DEFAULT 0, last_event_sequence integer NOT NULL DEFAULT 0,
          worker_id uuid, lease_expires_at timestamptz, heartbeat_at timestamptz,
          cancel_requested_at timestamptz, error_code varchar(64), input_summary varchar(500),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        ALTER TABLE messages ADD CONSTRAINT fk_messages_run FOREIGN KEY(agent_run_id)
          REFERENCES agent_runs(id) DEFERRABLE INITIALLY DEFERRED;
        CREATE UNIQUE INDEX uq_conversation_active_run ON agent_runs(conversation_id)
          WHERE status IN ('QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL','FAILED_RETRYABLE','CANCEL_REQUESTED');
        CREATE INDEX ix_agent_runs_queue ON agent_runs(status,created_at);
        CREATE TABLE agent_run_events (
          id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, sequence integer NOT NULL,
          event_type varchar(40) NOT NULL, attempt integer NOT NULL DEFAULT 0,
          payload jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,sequence)
        );
        CREATE INDEX ix_run_events_replay ON agent_run_events(run_id,sequence);
        CREATE TABLE agent_trace_spans (
          id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          parent_span_id uuid, span_type varchar(32) NOT NULL, name varchar(128) NOT NULL,
          status varchar(16) NOT NULL, metadata jsonb NOT NULL DEFAULT '{}',
          started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz
        );

        CREATE TABLE goals (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          goal_type varchar(64) NOT NULL, title varchar(200) NOT NULL, target_date date,
          status varchar(16) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE plans (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE, title varchar(200) NOT NULL,
          status varchar(16) NOT NULL, current_revision_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE plan_revisions (
          id uuid PRIMARY KEY, plan_id uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, agent_run_id uuid REFERENCES agent_runs(id),
          revision integer NOT NULL, status varchar(16) NOT NULL, objective_summary text NOT NULL,
          start_date date NOT NULL, end_date date NOT NULL, weekly_minutes integer NOT NULL,
          change_reason text, content jsonb NOT NULL DEFAULT '{}', approved_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(plan_id,revision)
        );
        ALTER TABLE plans ADD CONSTRAINT fk_plans_current_revision FOREIGN KEY(current_revision_id) REFERENCES plan_revisions(id);
        CREATE TABLE plan_stages (
          id uuid PRIMARY KEY, plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id) ON DELETE CASCADE,
          sequence integer NOT NULL, title varchar(200) NOT NULL, objective text NOT NULL,
          start_date date NOT NULL, end_date date NOT NULL, UNIQUE(plan_revision_id,sequence)
        );
        CREATE TABLE plan_task_templates (
          id uuid PRIMARY KEY, plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id) ON DELETE CASCADE,
          stage_id uuid REFERENCES plan_stages(id) ON DELETE CASCADE, sequence integer NOT NULL,
          title varchar(200) NOT NULL, expected_minutes integer NOT NULL, schedule_rule jsonb NOT NULL DEFAULT '{}'
        );
        CREATE TABLE approval_decisions (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          approval_version integer NOT NULL DEFAULT 1, status varchar(16) NOT NULL DEFAULT 'PENDING',
          action varchar(16), feedback text, decided_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE approval_decision_items (
          id uuid PRIMARY KEY, approval_id uuid NOT NULL REFERENCES approval_decisions(id) ON DELETE CASCADE,
          plan_id uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id) ON DELETE CASCADE,
          expected_current_revision_id uuid, UNIQUE(approval_id,plan_revision_id)
        );
        CREATE TABLE tasks (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          plan_id uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id), title varchar(200) NOT NULL,
          scheduled_date date NOT NULL, due_at timestamptz, expected_minutes integer NOT NULL,
          priority smallint NOT NULL DEFAULT 3, status varchar(16) NOT NULL DEFAULT 'TODO',
          completed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_tasks_user_date ON tasks(user_id,scheduled_date,status);
        CREATE TABLE task_executions (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, client_execution_id varchar(128) NOT NULL,
          result varchar(16) NOT NULL, duration_minutes integer, feedback text,
          outcome_data jsonb NOT NULL DEFAULT '{}', occurred_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,client_execution_id)
        );

        CREATE TABLE stored_files (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          purpose varchar(32) NOT NULL, original_filename varchar(255) NOT NULL, object_key varchar(512) NOT NULL UNIQUE,
          mime_type varchar(128) NOT NULL, size_bytes bigint NOT NULL, sha256 char(64) NOT NULL,
          upload_status varchar(16) NOT NULL, scan_status varchar(16) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz
        );
        CREATE TABLE message_attachments (
          message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          file_id uuid NOT NULL REFERENCES stored_files(id), position smallint NOT NULL,
          PRIMARY KEY(message_id,file_id), UNIQUE(message_id,position)
        );
        CREATE TABLE knowledge_documents (
          id uuid PRIMARY KEY, owner_user_id uuid REFERENCES users(id) ON DELETE CASCADE,
          visibility varchar(16) NOT NULL, domain varchar(64) NOT NULL, title varchar(300) NOT NULL,
          source_url text, source_organization varchar(200), source_level varchar(16) NOT NULL,
          object_key varchar(512), mime_type varchar(128), sha256 char(64) NOT NULL,
          document_version integer NOT NULL DEFAULT 1, published_at timestamptz, retrieved_at timestamptz NOT NULL,
          valid_from timestamptz, valid_to timestamptz, ingestion_status varchar(24) NOT NULL,
          error_code varchar(64), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE knowledge_chunks (
          id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
          chunk_index integer NOT NULL, heading_path text, content text NOT NULL, token_count integer NOT NULL,
          content_hash char(64) NOT NULL, embedding_model varchar(128) NOT NULL,
          qdrant_collection varchar(128) NOT NULL, qdrant_point_id uuid NOT NULL,
          vector_status varchar(16) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(document_id,chunk_index), UNIQUE(qdrant_collection,qdrant_point_id)
        );
        CREATE TABLE claims (
          id uuid PRIMARY KEY, agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          plan_revision_id uuid REFERENCES plan_revisions(id) ON DELETE CASCADE,
          claim_key varchar(128) NOT NULL, claim_text text NOT NULL, claim_type varchar(24) NOT NULL,
          requires_citation boolean NOT NULL, verification_status varchar(24) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(agent_run_id,claim_key)
        );
        CREATE TABLE citations (
          id uuid PRIMARY KEY, claim_id uuid NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
          knowledge_chunk_id uuid REFERENCES knowledge_chunks(id), source_url_snapshot text,
          evidence_excerpt varchar(1000), relation varchar(16) NOT NULL, relevance_score numeric(5,4),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK(knowledge_chunk_id IS NOT NULL OR source_url_snapshot IS NOT NULL)
        );
        CREATE TABLE review_records (
          id uuid PRIMARY KEY, agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          plan_revision_id uuid REFERENCES plan_revisions(id) ON DELETE CASCADE,
          reviewer_type varchar(24) NOT NULL, status varchar(16) NOT NULL, severity varchar(16) NOT NULL,
          issues jsonb NOT NULL DEFAULT '[]', prompt_version varchar(32), model_name varchar(128),
          created_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE memory_policy_state (
          user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1,
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE memory_extraction_jobs (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          status varchar(16) NOT NULL DEFAULT 'PENDING', attempt_count smallint NOT NULL DEFAULT 0,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(message_id)
        );
        CREATE TABLE memory_tombstones (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          normalized_key varchar(255) NOT NULL, reason varchar(32) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,normalized_key)
        );
        CREATE TABLE memory_audit_records (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          action varchar(32) NOT NULL, memory_key_hash char(64) NOT NULL,
          metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE notification_jobs (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          task_id uuid REFERENCES tasks(id) ON DELETE CASCADE, event_type varchar(32) NOT NULL,
          channel varchar(16) NOT NULL, scheduled_at timestamptz NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
          status varchar(16) NOT NULL, attempt_count smallint NOT NULL DEFAULT 0,
          max_attempts smallint NOT NULL DEFAULT 3, next_attempt_at timestamptz,
          provider_message_id varchar(255), idempotency_key varchar(255) NOT NULL UNIQUE,
          last_error_code varchar(64), sent_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );

        GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO butler_app, butler_test;
        """
    )


def downgrade() -> None:
    """仅用于未承载真实数据的验证环境回滚。"""

    op.execute(
        """
        DROP TABLE IF EXISTS notification_jobs, memory_audit_records, memory_tombstones,
          memory_extraction_jobs, memory_policy_state, review_records, citations, claims,
          knowledge_chunks, knowledge_documents, message_attachments, stored_files,
          task_executions, tasks, approval_decision_items, approval_decisions,
          plan_task_templates, plan_stages CASCADE;
        ALTER TABLE plans DROP CONSTRAINT IF EXISTS fk_plans_current_revision;
        DROP TABLE IF EXISTS plan_revisions, plans, goals, agent_trace_spans, agent_run_events CASCADE;
        ALTER TABLE messages DROP CONSTRAINT IF EXISTS fk_messages_run;
        DROP TABLE IF EXISTS agent_runs, messages, conversation_summaries CASCADE;
        ALTER TABLE conversations DROP CONSTRAINT IF EXISTS fk_conversations_active_segment;
        DROP TABLE IF EXISTS conversation_segments, conversations, user_agents, agent_definitions,
          study_availability, user_profiles, auth_sessions, user_identities CASCADE;
        ALTER TABLE users DROP COLUMN IF EXISTS nickname, DROP COLUMN IF EXISTS locale,
          DROP COLUMN IF EXISTS timezone, DROP COLUMN IF EXISTS updated_at, DROP COLUMN IF EXISTS deleted_at;
        ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_status;
        ALTER TABLE users ADD CONSTRAINT ck_users_status CHECK(status IN ('ACTIVE','DISABLED','DELETED'));
        """
    )
