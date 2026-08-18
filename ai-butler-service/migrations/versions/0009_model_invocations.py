"""Add provider-neutral model invocation metadata.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_invocations (
          id uuid PRIMARY KEY,
          request_id varchar(64),
          run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
          task varchar(64) NOT NULL,
          provider varchar(64) NOT NULL,
          model varchar(256) NOT NULL,
          prompt_version varchar(64) NOT NULL,
          schema_version varchar(32),
          attempt smallint NOT NULL,
          route_role varchar(16) NOT NULL,
          status varchar(16) NOT NULL,
          input_tokens integer NOT NULL DEFAULT 0,
          cached_input_tokens integer NOT NULL DEFAULT 0,
          output_tokens integer NOT NULL DEFAULT 0,
          duration_ms integer NOT NULL,
          error_class varchar(64),
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_model_invocations_attempt CHECK (attempt BETWEEN 1 AND 3),
          CONSTRAINT ck_model_invocations_role CHECK (
            route_role IN ('PRIMARY','FALLBACK','SHADOW')
          ),
          CONSTRAINT ck_model_invocations_status CHECK (status IN ('SUCCEEDED','FAILED')),
          CONSTRAINT ck_model_invocations_usage CHECK (
            input_tokens >= 0 AND cached_input_tokens >= 0 AND output_tokens >= 0
            AND duration_ms >= 0
          )
        );
        CREATE INDEX ix_model_invocations_created ON model_invocations(created_at DESC);
        CREATE INDEX ix_model_invocations_task_model
          ON model_invocations(task,provider,model,created_at DESC);
        CREATE INDEX ix_model_invocations_run ON model_invocations(run_id)
          WHERE run_id IS NOT NULL;
        CREATE VIEW model_invocation_metrics_hourly AS
          SELECT date_trunc('hour',created_at) AS bucket,
                 task,provider,model,
                 count(*) AS invocation_count,
                 sum(input_tokens) AS input_tokens,
                 sum(cached_input_tokens) AS cached_input_tokens,
                 sum(output_tokens) AS output_tokens,
                 percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms) AS p50_duration_ms,
                 percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_duration_ms,
                 avg((status = 'SUCCEEDED')::int) AS success_rate,
                 avg((prompt_version LIKE '%-repair')::int) AS schema_repair_rate,
                 avg((route_role = 'FALLBACK')::int) AS fallback_rate
            FROM model_invocations
           GROUP BY date_trunc('hour',created_at),task,provider,model;
        GRANT SELECT,INSERT,UPDATE,DELETE ON model_invocations TO butler_app, butler_test;
        GRANT SELECT ON model_invocation_metrics_hourly TO butler_app, butler_test;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW model_invocation_metrics_hourly; DROP TABLE model_invocations")
