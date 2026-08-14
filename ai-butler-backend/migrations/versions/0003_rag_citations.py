"""Replace source review records with RAG citation provenance.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """前滚删除审核事实，并为历史可重放的引用补齐来源快照。"""

    op.execute(
        """
        ALTER TABLE knowledge_documents
          ADD COLUMN stored_file_id uuid REFERENCES stored_files(id) ON DELETE CASCADE;
        CREATE UNIQUE INDEX uq_knowledge_documents_stored_file
          ON knowledge_documents(stored_file_id) WHERE stored_file_id IS NOT NULL;

        ALTER TABLE citations
          ADD COLUMN source_type varchar(24),
          ADD COLUMN source_title_snapshot varchar(300),
          ADD COLUMN source_organization_snapshot varchar(200),
          ADD COLUMN source_domain_snapshot varchar(255),
          ADD COLUMN published_at_snapshot timestamptz,
          ADD COLUMN retrieved_at_snapshot timestamptz,
          ADD COLUMN source_rank smallint NOT NULL DEFAULT 1;

        UPDATE citations AS ci
        SET source_type = CASE
              WHEN kd.id IS NOT NULL THEN 'KNOWLEDGE'
              ELSE 'WEB'
            END,
            source_title_snapshot = COALESCE(kd.title, '引用来源'),
            source_organization_snapshot = kd.source_organization,
            source_domain_snapshot = COALESCE(
              NULLIF(substring(COALESCE(ci.source_url_snapshot, kd.source_url) from
                '^https?://([^/]+)'), ''),
              kd.source_organization
            ),
            published_at_snapshot = kd.published_at,
            retrieved_at_snapshot = COALESCE(kd.retrieved_at, ci.created_at)
        FROM knowledge_chunks AS kc
        FULL JOIN knowledge_documents AS kd ON kd.id = kc.document_id
        WHERE kc.id = ci.knowledge_chunk_id
           OR (ci.knowledge_chunk_id IS NULL AND kd.id IS NULL);

        UPDATE citations
        SET source_type = COALESCE(source_type, 'WEB'),
            source_title_snapshot = COALESCE(source_title_snapshot, '引用来源'),
            source_domain_snapshot = COALESCE(
              source_domain_snapshot,
              substring(source_url_snapshot from '^https?://([^/]+)')
            ),
            retrieved_at_snapshot = COALESCE(retrieved_at_snapshot, created_at);

        ALTER TABLE citations
          ALTER COLUMN source_type SET NOT NULL,
          ALTER COLUMN source_title_snapshot SET NOT NULL,
          ALTER COLUMN retrieved_at_snapshot SET NOT NULL,
          ADD CONSTRAINT ck_citations_source_type
            CHECK (source_type IN ('WEB','PRIVATE_FILE','KNOWLEDGE')),
          ADD CONSTRAINT ck_citations_source_rank CHECK (source_rank > 0);
        CREATE INDEX ix_citations_claim_rank ON citations(claim_id,source_rank,id);
        ALTER TABLE citations DROP CONSTRAINT citations_check;
        ALTER TABLE citations ADD CONSTRAINT ck_citations_source_reference
          CHECK (
            (source_type = 'WEB' AND source_url_snapshot IS NOT NULL)
            OR source_type IN ('PRIVATE_FILE','KNOWLEDGE')
          );
        ALTER TABLE citations DROP CONSTRAINT citations_knowledge_chunk_id_fkey;
        ALTER TABLE citations ADD CONSTRAINT citations_knowledge_chunk_id_fkey
          FOREIGN KEY(knowledge_chunk_id) REFERENCES knowledge_chunks(id) ON DELETE SET NULL;

        DROP TABLE review_records;
        ALTER TABLE claims
          DROP COLUMN requires_citation,
          DROP COLUMN verification_status;
        """
    )


def downgrade() -> None:
    """仅为未承载生产数据的验证环境恢复旧审核结构。"""

    op.execute(
        """
        ALTER TABLE claims
          ADD COLUMN requires_citation boolean NOT NULL DEFAULT true,
          ADD COLUMN verification_status varchar(24) NOT NULL DEFAULT 'SUPPORTED';
        CREATE TABLE review_records (
          id uuid PRIMARY KEY, agent_run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          plan_revision_id uuid REFERENCES plan_revisions(id) ON DELETE CASCADE,
          reviewer_type varchar(24) NOT NULL, status varchar(16) NOT NULL, severity varchar(16) NOT NULL,
          issues jsonb NOT NULL DEFAULT '[]', prompt_version varchar(32), model_name varchar(128),
          created_at timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT,INSERT,UPDATE,DELETE ON review_records TO butler_app, butler_test;

        DROP INDEX ix_citations_claim_rank;
        DELETE FROM citations
          WHERE knowledge_chunk_id IS NULL AND source_url_snapshot IS NULL;
        ALTER TABLE citations DROP CONSTRAINT ck_citations_source_reference;
        ALTER TABLE citations ADD CONSTRAINT citations_check
          CHECK (knowledge_chunk_id IS NOT NULL OR source_url_snapshot IS NOT NULL);
        ALTER TABLE citations DROP CONSTRAINT citations_knowledge_chunk_id_fkey;
        ALTER TABLE citations ADD CONSTRAINT citations_knowledge_chunk_id_fkey
          FOREIGN KEY(knowledge_chunk_id) REFERENCES knowledge_chunks(id);
        ALTER TABLE citations
          DROP CONSTRAINT ck_citations_source_rank,
          DROP CONSTRAINT ck_citations_source_type,
          DROP COLUMN source_rank,
          DROP COLUMN retrieved_at_snapshot,
          DROP COLUMN published_at_snapshot,
          DROP COLUMN source_domain_snapshot,
          DROP COLUMN source_organization_snapshot,
          DROP COLUMN source_title_snapshot,
          DROP COLUMN source_type;
        DROP INDEX uq_knowledge_documents_stored_file;
        ALTER TABLE knowledge_documents DROP COLUMN stored_file_id;
        """
    )
