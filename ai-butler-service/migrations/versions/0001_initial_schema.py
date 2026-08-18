"""Create the disposable verification baseline schema.

Revision ID: 0001
Revises: None

The project intentionally resets all verification data when this baseline changes.  There is no
upgrade path from the former 0001-0009 chain; production/staging resets are rejected by the reset
command.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """一次性建立验证版最终结构、约束、索引和最小权限。"""

    op.execute(
        """
        CREATE TABLE users (
          id uuid PRIMARY KEY,
          status varchar(16) NOT NULL DEFAULT 'ACTIVE'
            CHECK(status IN ('ACTIVE','SUSPENDED','DELETING','DELETED')),
          nickname varchar(64), locale varchar(16) NOT NULL DEFAULT 'zh-CN',
          timezone varchar(64) NOT NULL DEFAULT 'Asia/Shanghai',
          phone_ciphertext text, phone_hash char(64),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz
        );
        CREATE UNIQUE INDEX uq_users_phone_hash ON users(phone_hash) WHERE phone_hash IS NOT NULL;

        CREATE TABLE user_identities (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          provider varchar(32) NOT NULL, provider_subject varchar(255) NOT NULL,
          union_subject varchar(255), created_at timestamptz NOT NULL DEFAULT now(),
          last_login_at timestamptz, UNIQUE(provider,provider_subject)
        );
        CREATE INDEX ix_user_identities_user ON user_identities(user_id);
        CREATE TABLE auth_sessions (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          refresh_token_hash char(64) NOT NULL UNIQUE, device_id varchar(128),
          status varchar(16) NOT NULL CHECK(status IN ('ACTIVE','REVOKED','EXPIRED')),
          expires_at timestamptz NOT NULL, last_used_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
          CHECK(expires_at>created_at)
        );
        CREATE INDEX ix_auth_sessions_user_status ON auth_sessions(user_id,status,expires_at);
        CREATE TABLE phone_verification_challenges (
          id uuid PRIMARY KEY, phone_hash char(64) NOT NULL, code_hash char(64) NOT NULL,
          device_id varchar(128) NOT NULL, request_key_hash char(64) NOT NULL UNIQUE,
          provider_message_id varchar(255), status varchar(16) NOT NULL
            CHECK(status IN ('PENDING','SENT','FAILED','CONSUMED','LOCKED','EXPIRED')),
          attempt_count smallint NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 5),
          expires_at timestamptz NOT NULL, sent_at timestamptz, consumed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), CHECK(expires_at>created_at)
        );
        CREATE INDEX ix_phone_challenges_phone_created
          ON phone_verification_challenges(phone_hash,created_at DESC);
        CREATE INDEX ix_phone_challenges_device_created
          ON phone_verification_challenges(device_id,created_at DESC);
        CREATE TABLE user_profiles (
          user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          education_level varchar(32), major varchar(128), region_code varchar(32),
          current_level varchar(32), existing_materials jsonb NOT NULL DEFAULT '[]',
          notification_preferences jsonb NOT NULL DEFAULT '{}',
          profile_version integer NOT NULL DEFAULT 1 CHECK(profile_version>0),
          availability_version integer NOT NULL DEFAULT 1 CHECK(availability_version>0),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE study_availability (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          day_of_week smallint, start_time time, end_time time,
          available_minutes smallint NOT NULL CHECK(available_minutes BETWEEN 1 AND 1440),
          effective_from date NOT NULL, effective_to date,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK(day_of_week IS NULL OR day_of_week BETWEEN 1 AND 7),
          CHECK((start_time IS NULL)=(end_time IS NULL)),
          CHECK(start_time IS NULL OR end_time>start_time),
          CHECK(effective_to IS NULL OR effective_to>=effective_from)
        );

        CREATE TABLE agent_definitions (
          id uuid PRIMARY KEY, code varchar(64) NOT NULL, version integer NOT NULL CHECK(version>0),
          name varchar(128) NOT NULL, description text, graph_name varchar(128) NOT NULL,
          status varchar(16) NOT NULL CHECK(status IN ('DRAFT','ACTIVE','RETIRED')),
          catalog_status varchar(16) NOT NULL DEFAULT 'HIDDEN'
            CHECK(catalog_status IN ('AVAILABLE','COMING_SOON','HIDDEN')),
          display_order smallint NOT NULL DEFAULT 0, catalog_metadata jsonb NOT NULL DEFAULT '{}',
          default_config jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(code,version),
          CHECK(catalog_status<>'AVAILABLE' OR status='ACTIVE')
        );
        CREATE UNIQUE INDEX uq_agent_definition_active ON agent_definitions(code) WHERE status='ACTIVE';
        CREATE INDEX ix_agent_definitions_catalog ON agent_definitions(catalog_status,display_order,code);
        CREATE TABLE user_agents (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          agent_definition_id uuid NOT NULL REFERENCES agent_definitions(id),
          status varchar(16) NOT NULL CHECK(status IN ('ACTIVE','PAUSED','COMPLETED')),
          config jsonb NOT NULL DEFAULT '{}', activated_at timestamptz, completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(user_id,agent_definition_id)
        );

        CREATE TABLE conversations (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          user_agent_id uuid NOT NULL REFERENCES user_agents(id), client_conversation_id uuid NOT NULL,
          title varchar(200) NOT NULL, status varchar(16) NOT NULL DEFAULT 'CURRENT'
            CHECK(status IN ('CURRENT','ARCHIVED')),
          specialist_user_agent_id uuid REFERENCES user_agents(id), archive_reason varchar(32)
            CHECK(archive_reason IS NULL OR archive_reason IN
              ('TOPIC_SWITCH','SPECIALIST_SWITCH','HISTORY_RESUME','WORKFLOW_EXIT')),
          archived_at timestamptz, deleted_at timestamptz, active_segment_id uuid,
          latest_handoff_summary_id uuid, context_version integer NOT NULL DEFAULT 1 CHECK(context_version>0),
          last_message_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,client_conversation_id),
          CHECK((status='CURRENT' AND archived_at IS NULL) OR status='ARCHIVED')
        );
        CREATE UNIQUE INDEX uq_conversations_current_user ON conversations(user_id)
          WHERE status='CURRENT' AND deleted_at IS NULL;
        CREATE INDEX ix_conversations_user_timeline
          ON conversations(user_id,status,last_message_at DESC,id DESC) WHERE deleted_at IS NULL;
        CREATE TABLE conversation_segments (
          id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          sequence integer NOT NULL CHECK(sequence>0), thread_id varchar(128) NOT NULL UNIQUE,
          status varchar(16) NOT NULL CHECK(status IN ('ACTIVE','ARCHIVING','ARCHIVED')),
          estimated_context_tokens integer NOT NULL DEFAULT 0 CHECK(estimated_context_tokens>=0),
          start_message_id uuid, end_message_id uuid, final_summary_id uuid,
          archive_reason varchar(32), created_at timestamptz NOT NULL DEFAULT now(), archived_at timestamptz,
          UNIQUE(conversation_id,sequence)
        );
        CREATE UNIQUE INDEX uq_conversation_active_segment ON conversation_segments(conversation_id)
          WHERE status='ACTIVE';
        CREATE TABLE conversation_summaries (
          id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          segment_id uuid REFERENCES conversation_segments(id) ON DELETE CASCADE,
          summary_type varchar(24) NOT NULL CHECK(summary_type IN
            ('INCREMENTAL','SEGMENT_FINAL','CUMULATIVE_HANDOFF')),
          version integer NOT NULL CHECK(version>0), summary_data jsonb NOT NULL,
          source_from_message_id uuid, source_through_message_id uuid, source_hash char(64) NOT NULL UNIQUE,
          prompt_version varchar(32) NOT NULL, token_count integer NOT NULL DEFAULT 0 CHECK(token_count>=0),
          status varchar(16) NOT NULL DEFAULT 'PUBLISHED'
            CHECK(status IN ('GENERATING','PUBLISHED','FAILED','SUPERSEDED')),
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(conversation_id,summary_type,version)
        );
        ALTER TABLE conversations ADD CONSTRAINT fk_conversations_active_segment
          FOREIGN KEY(active_segment_id) REFERENCES conversation_segments(id);
        ALTER TABLE conversations ADD CONSTRAINT fk_conversations_handoff_summary
          FOREIGN KEY(latest_handoff_summary_id) REFERENCES conversation_summaries(id);
        ALTER TABLE conversation_segments ADD CONSTRAINT fk_segments_final_summary
          FOREIGN KEY(final_summary_id) REFERENCES conversation_summaries(id);

        CREATE TABLE messages (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          segment_id uuid NOT NULL REFERENCES conversation_segments(id), agent_run_id uuid,
          client_message_id varchar(128), client_request_hash char(64),
          role varchar(20) NOT NULL CHECK(role IN ('USER','ASSISTANT','SYSTEM_EVENT')),
          status varchar(20) NOT NULL CHECK(status IN ('PENDING','STREAMING','COMPLETED','FAILED','CANCELLED')),
          content text NOT NULL DEFAULT '', structured_content jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK(role<>'USER' OR (client_message_id IS NOT NULL AND client_request_hash IS NOT NULL))
        );
        CREATE UNIQUE INDEX uq_messages_user_client_id ON messages(user_id,client_message_id)
          WHERE client_message_id IS NOT NULL;
        CREATE INDEX ix_messages_timeline ON messages(conversation_id,created_at DESC,id DESC);
        CREATE INDEX ix_messages_segment ON messages(segment_id,created_at,id);

        CREATE TABLE agent_runs (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          segment_id uuid NOT NULL REFERENCES conversation_segments(id),
          selected_user_agent_id uuid REFERENCES user_agents(id),
          trigger_message_id uuid NOT NULL REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED,
          pending_message_id uuid REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED,
          pending_response_message_id uuid REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED,
          status varchar(24) NOT NULL CHECK(status IN
            ('QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL','SUCCEEDED',
             'FAILED_RETRYABLE','FAILED_FINAL','CANCEL_REQUESTED','CANCELLED')),
          pending_action varchar(24) NOT NULL DEFAULT 'START'
            CHECK(pending_action IN ('NONE','START','INPUT_RESUME','APPROVAL_RESUME','RETRY')),
          pending_action_key varchar(160), last_applied_action_key varchar(160),
          graph_version varchar(32) NOT NULL DEFAULT 'butler-graph-v2',
          prompt_bundle_version varchar(32) NOT NULL DEFAULT 'butler-prompts-v2',
          capability_registry_version varchar(32) NOT NULL DEFAULT '1.0',
          capability_registry_fingerprint char(64) NOT NULL,
          model_provider varchar(64), model_name varchar(256), last_node varchar(64),
          attempt integer NOT NULL DEFAULT 0 CHECK(attempt>=0),
          last_event_sequence integer NOT NULL DEFAULT 0 CHECK(last_event_sequence>=0),
          input_summary varchar(500), output_data jsonb, warning_data jsonb NOT NULL DEFAULT '[]',
          error_code varchar(64), error_detail jsonb, input_tokens integer NOT NULL DEFAULT 0,
          output_tokens integer NOT NULL DEFAULT 0, trace_id varchar(128) NOT NULL UNIQUE,
          worker_id uuid, lease_expires_at timestamptz, heartbeat_at timestamptz,
          cancel_requested_at timestamptz, started_at timestamptz, completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK(input_tokens>=0 AND output_tokens>=0),
          CHECK(pending_action='NONE' OR pending_action_key IS NOT NULL)
        );
        ALTER TABLE messages ADD CONSTRAINT fk_messages_run FOREIGN KEY(agent_run_id)
          REFERENCES agent_runs(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;
        CREATE UNIQUE INDEX uq_conversation_active_run ON agent_runs(conversation_id) WHERE status IN
          ('QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL','FAILED_RETRYABLE','CANCEL_REQUESTED');
        CREATE UNIQUE INDEX uq_user_executing_run ON agent_runs(user_id)
          WHERE status IN ('QUEUED','RUNNING','CANCEL_REQUESTED');
        CREATE INDEX ix_agent_runs_queue ON agent_runs(status,lease_expires_at,created_at);
        CREATE TABLE agent_run_events (
          id bigserial PRIMARY KEY, agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          sequence integer NOT NULL CHECK(sequence>0), event_type varchar(40) NOT NULL,
          attempt smallint NOT NULL DEFAULT 0 CHECK(attempt>=0), payload jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(agent_run_id,sequence)
        );
        CREATE INDEX ix_run_events_replay ON agent_run_events(agent_run_id,sequence);
        CREATE INDEX ix_run_events_cleanup ON agent_run_events(created_at);
        CREATE TABLE agent_trace_spans (
          id uuid PRIMARY KEY, agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, trace_id varchar(128) NOT NULL,
          span_id varchar(32) NOT NULL, parent_span_id varchar(32), attempt smallint NOT NULL CHECK(attempt>=0),
          span_kind varchar(32) NOT NULL, node_name varchar(64), work_item_id varchar(128),
          capability_name varchar(128), capability_version varchar(32), registry_fingerprint char(64) NOT NULL,
          risk_level varchar(16), gate_decision varchar(16), status varchar(24) NOT NULL,
          error_code varchar(64), retry_count smallint NOT NULL DEFAULT 0 CHECK(retry_count>=0),
          input_hash char(64), output_hash char(64), trust_level varchar(32), result_items integer,
          truncated boolean NOT NULL DEFAULT false, input_tokens integer NOT NULL DEFAULT 0,
          output_tokens integer NOT NULL DEFAULT 0, started_at timestamptz NOT NULL DEFAULT now(),
          ended_at timestamptz, duration_ms integer, created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(trace_id,span_id)
        );

        CREATE TABLE goals (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          goal_type varchar(64) NOT NULL, title varchar(200) NOT NULL, target_date date,
          status varchar(16) NOT NULL CHECK(status IN ('DRAFT','ACTIVE','COMPLETED','CANCELLED')),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE plans (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE, title varchar(200) NOT NULL,
          status varchar(16) NOT NULL CHECK(status IN ('DRAFT','ACTIVE','COMPLETED','CANCELLED')),
          current_revision_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE plan_revisions (
          id uuid PRIMARY KEY, plan_id uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          agent_run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
          revision integer NOT NULL CHECK(revision>0),
          status varchar(24) NOT NULL CHECK(status IN
            ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED','SUPERSEDED')),
          objective_summary text NOT NULL, start_date date NOT NULL, end_date date NOT NULL,
          weekly_minutes integer NOT NULL CHECK(weekly_minutes>0), change_reason text,
          content jsonb NOT NULL DEFAULT '{}', approved_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(plan_id,revision), CHECK(end_date>=start_date)
        );
        ALTER TABLE plans ADD CONSTRAINT fk_plans_current_revision
          FOREIGN KEY(current_revision_id) REFERENCES plan_revisions(id);
        CREATE TABLE plan_stages (
          id uuid PRIMARY KEY, plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id) ON DELETE CASCADE,
          sequence integer NOT NULL CHECK(sequence>0), title varchar(200) NOT NULL, objective text NOT NULL,
          start_date date NOT NULL, end_date date NOT NULL, UNIQUE(plan_revision_id,sequence)
        );
        CREATE TABLE plan_task_templates (
          id uuid PRIMARY KEY, plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id) ON DELETE CASCADE,
          stage_id uuid REFERENCES plan_stages(id) ON DELETE CASCADE, sequence integer NOT NULL,
          template_key varchar(128) NOT NULL, title varchar(200) NOT NULL,
          expected_minutes integer NOT NULL CHECK(expected_minutes>0), schedule_rule jsonb NOT NULL DEFAULT '{}',
          UNIQUE(plan_revision_id,template_key)
        );
        CREATE TABLE approval_decisions (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          approval_version integer NOT NULL DEFAULT 1 CHECK(approval_version>0),
          status varchar(16) NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','APPROVED','EDITED','REJECTED','CANCELLED')),
          action varchar(16), feedback text, decided_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX uq_run_pending_approval ON approval_decisions(agent_run_id)
          WHERE status='PENDING';
        CREATE TABLE approval_decision_items (
          id uuid PRIMARY KEY, approval_id uuid NOT NULL REFERENCES approval_decisions(id) ON DELETE CASCADE,
          plan_id uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id) ON DELETE CASCADE,
          expected_current_revision_id uuid, work_item_id varchar(128) NOT NULL,
          UNIQUE(approval_id,plan_revision_id), UNIQUE(approval_id,work_item_id)
        );
        CREATE TABLE tasks (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          plan_id uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
          plan_revision_id uuid NOT NULL REFERENCES plan_revisions(id), task_key varchar(160) NOT NULL,
          title varchar(200) NOT NULL, scheduled_date date NOT NULL, due_at timestamptz,
          expected_minutes integer NOT NULL CHECK(expected_minutes>0), priority smallint NOT NULL DEFAULT 3,
          status varchar(16) NOT NULL DEFAULT 'TODO'
            CHECK(status IN ('TODO','DOING','DONE','SKIPPED','CANCELLED')),
          cancellation_reason varchar(32), completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(plan_revision_id,task_key)
        );
        CREATE INDEX ix_tasks_user_date ON tasks(user_id,scheduled_date,status);
        CREATE TABLE task_executions (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          client_execution_id varchar(128) NOT NULL, result varchar(16) NOT NULL,
          duration_minutes integer, feedback text, outcome_data jsonb NOT NULL DEFAULT '{}',
          occurred_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(user_id,client_execution_id)
        );

        CREATE TABLE stored_files (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          purpose varchar(32) NOT NULL, original_filename varchar(255) NOT NULL,
          object_key varchar(512) NOT NULL UNIQUE, mime_type varchar(128) NOT NULL,
          size_bytes bigint NOT NULL CHECK(size_bytes>=0), sha256 char(64) NOT NULL,
          upload_status varchar(16) NOT NULL, scan_status varchar(16) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          deleted_at timestamptz
        );
        CREATE TABLE message_attachments (
          id uuid PRIMARY KEY, message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          stored_file_id uuid NOT NULL REFERENCES stored_files(id),
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          position smallint NOT NULL CHECK(position>=0), created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(message_id,stored_file_id), UNIQUE(message_id,position)
        );
        CREATE TABLE knowledge_documents (
          id uuid PRIMARY KEY, owner_user_id uuid REFERENCES users(id) ON DELETE CASCADE,
          stored_file_id uuid REFERENCES stored_files(id) ON DELETE CASCADE,
          visibility varchar(16) NOT NULL CHECK(visibility IN ('PUBLIC','PRIVATE')),
          domain varchar(64) NOT NULL, title varchar(300) NOT NULL, source_url text,
          source_organization varchar(200), source_level varchar(16) NOT NULL,
          object_key varchar(512), mime_type varchar(128), sha256 char(64) NOT NULL,
          document_version integer NOT NULL DEFAULT 1 CHECK(document_version>0),
          published_at timestamptz, retrieved_at timestamptz NOT NULL, valid_from timestamptz,
          valid_to timestamptz, ingestion_status varchar(24) NOT NULL, error_code varchar(64),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX uq_knowledge_documents_stored_file ON knowledge_documents(stored_file_id)
          WHERE stored_file_id IS NOT NULL;
        CREATE TABLE knowledge_chunks (
          id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
          chunk_index integer NOT NULL, heading_path text, content text NOT NULL,
          token_count integer NOT NULL CHECK(token_count>=0), content_hash char(64) NOT NULL,
          embedding_model varchar(128) NOT NULL, qdrant_collection varchar(128) NOT NULL,
          qdrant_point_id uuid NOT NULL, vector_status varchar(16) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(document_id,chunk_index), UNIQUE(qdrant_collection,qdrant_point_id)
        );
        CREATE TABLE claims (
          id uuid PRIMARY KEY, agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          plan_revision_id uuid REFERENCES plan_revisions(id) ON DELETE CASCADE,
          claim_key varchar(128) NOT NULL, claim_text text NOT NULL,
          claim_type varchar(24) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(agent_run_id,claim_key)
        );
        CREATE TABLE citations (
          id uuid PRIMARY KEY, claim_id uuid NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
          knowledge_chunk_id uuid REFERENCES knowledge_chunks(id) ON DELETE SET NULL,
          source_type varchar(24) NOT NULL CHECK(source_type IN ('WEB','PRIVATE_FILE','KNOWLEDGE')),
          source_url_snapshot text, source_title_snapshot varchar(300) NOT NULL,
          source_organization_snapshot varchar(200), source_domain_snapshot varchar(255),
          published_at_snapshot timestamptz, retrieved_at_snapshot timestamptz NOT NULL,
          evidence_excerpt varchar(1000), relation varchar(16) NOT NULL,
          relevance_score numeric(5,4), source_rank smallint NOT NULL CHECK(source_rank>0),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK((source_type='WEB' AND source_url_snapshot IS NOT NULL) OR source_type IN ('PRIVATE_FILE','KNOWLEDGE'))
        );
        CREATE INDEX ix_citations_claim_rank ON citations(claim_id,source_rank,id);

        CREATE TABLE memory_policy_state (
          user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1 CHECK(version>0),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE memory_extraction_jobs (
          id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          status varchar(16) NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','RUNNING','RETRY','SUCCEEDED','DEAD')),
          attempt_count smallint NOT NULL DEFAULT 0, worker_id uuid, lease_expires_at timestamptz,
          next_attempt_at timestamptz, error_code varchar(64),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(message_id)
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
          channel varchar(16) NOT NULL, scheduled_at timestamptz NOT NULL,
          payload jsonb NOT NULL DEFAULT '{}', status varchar(16) NOT NULL
            CHECK(status IN ('PENDING','RUNNING','RETRY','SENT','DEAD','CANCELLED')),
          attempt_count smallint NOT NULL DEFAULT 0, max_attempts smallint NOT NULL DEFAULT 4,
          next_attempt_at timestamptz, provider_message_id varchar(255),
          idempotency_key varchar(255) NOT NULL UNIQUE, last_error_code varchar(64), sent_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE account_deletion_jobs (
          id uuid PRIMARY KEY, user_id uuid NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
          status varchar(16) NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','RUNNING','RETRY','SUCCEEDED','DEAD')),
          current_step varchar(32) NOT NULL DEFAULT 'CANCEL_WORK', attempt_count smallint NOT NULL DEFAULT 0,
          next_attempt_at timestamptz, lease_expires_at timestamptz, error_code varchar(64),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE model_invocations (
          id uuid PRIMARY KEY, request_id varchar(64), run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
          task varchar(64) NOT NULL, provider varchar(64) NOT NULL, model varchar(256) NOT NULL,
          prompt_version varchar(64) NOT NULL, schema_version varchar(32), attempt smallint NOT NULL,
          route_role varchar(16) NOT NULL CHECK(route_role IN ('PRIMARY','FALLBACK','SHADOW')),
          status varchar(16) NOT NULL CHECK(status IN ('SUCCEEDED','FAILED')),
          input_tokens integer NOT NULL DEFAULT 0, cached_input_tokens integer NOT NULL DEFAULT 0,
          output_tokens integer NOT NULL DEFAULT 0, duration_ms integer NOT NULL,
          error_class varchar(64), created_at timestamptz NOT NULL DEFAULT now(),
          CHECK(attempt BETWEEN 1 AND 3),
          CHECK(input_tokens>=0 AND cached_input_tokens>=0 AND output_tokens>=0 AND duration_ms>=0)
        );
        CREATE INDEX ix_model_invocations_created ON model_invocations(created_at DESC);
        CREATE INDEX ix_model_invocations_run ON model_invocations(run_id) WHERE run_id IS NOT NULL;
        CREATE VIEW model_invocation_metrics_hourly AS
          SELECT date_trunc('hour',created_at) AS bucket,task,provider,model,
                 count(*) AS invocation_count,sum(input_tokens) AS input_tokens,
                 sum(cached_input_tokens) AS cached_input_tokens,sum(output_tokens) AS output_tokens,
                 percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms) AS p50_duration_ms,
                 percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_duration_ms,
                 avg((status='SUCCEEDED')::int) AS success_rate,
                 avg((prompt_version LIKE '%-repair')::int) AS schema_repair_rate,
                 avg((route_role='FALLBACK')::int) AS fallback_rate
            FROM model_invocations GROUP BY date_trunc('hour',created_at),task,provider,model;

        GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO butler_app,butler_test;
        GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO butler_app,butler_test;
        GRANT SELECT ON model_invocation_metrics_hourly TO butler_app,butler_test;
        """
    )


def downgrade() -> None:
    """验证数据可丢弃；回滚直接清空 public schema。"""

    op.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
