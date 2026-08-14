from __future__ import annotations

import hashlib
from uuid import UUID

import psycopg

from ai_butler.application.butler import PUBLIC_CHUNK_ID, PUBLIC_SOURCE_ID
from ai_butler.config import get_settings

DEBUG_USERS = (
    UUID("00000000-0000-4000-8000-000000000001"),
    UUID("00000000-0000-4000-8000-000000000002"),
)


def main() -> None:
    database_url = get_settings().migration_database_url.replace("+psycopg", "")
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, status) VALUES (%s, 'ACTIVE') ON CONFLICT (id) DO NOTHING",
            [(user_id,) for user_id in DEBUG_USERS],
        )
        digest = hashlib.sha256(b"synthetic civil service verification source").hexdigest()
        cursor.execute(
            "INSERT INTO knowledge_documents("
            "id,visibility,domain,title,source_organization,source_level,"
            "sha256,retrieved_at,ingestion_status) VALUES(%s,'PUBLIC','CIVIL_SERVICE_EXAM',"
            "'合成公考验收资料（非真实公告）','AI Butler Test Fixtures','GENERAL',"
            "%s,now(),'READY') "
            "ON CONFLICT(id) DO NOTHING",
            (PUBLIC_SOURCE_ID, digest),
        )
        cursor.execute(
            "INSERT INTO knowledge_chunks("
            "id,document_id,chunk_index,content,token_count,content_hash,"
            "embedding_model,qdrant_collection,qdrant_point_id,vector_status) VALUES(%s,%s,0,"
            "'合成验收资料：训练计划包含行测与申论模块。',20,%s,'fake-embedding-v1',"
            "'ai_butler_knowledge',%s,'READY') ON CONFLICT(id) DO NOTHING",
            (PUBLIC_CHUNK_ID, PUBLIC_SOURCE_ID, digest, PUBLIC_CHUNK_ID),
        )
        connection.commit()
    print("seeded two synthetic users and one clearly-labelled public knowledge fixture")


if __name__ == "__main__":
    main()
