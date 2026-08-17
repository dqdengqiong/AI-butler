from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.domain.errors import not_found

from .shared import (
    BUTLER_DEFINITION_ID,
    CIVIL_DEFINITION_ID,
    IELTS_DEFINITION_ID,
    JOB_SEARCH_DEFINITION_ID,
    PUBLIC_CHUNK_ID,
    PUBLIC_SOURCE_ID,
    _json,
    _row,
)


class BootstrapService:
    def __init__(self) -> None:
        pass

    async def _ensure_agent_definitions(self, connection: AsyncConnection) -> None:
        catalog: tuple[tuple[Any, ...], ...] = (
            (
                BUTLER_DEFINITION_ID,
                "BUTLER",
                "AI 大管家",
                "butler_graph_v1",
                "HIDDEN",
                0,
                {},
            ),
            (
                CIVIL_DEFINITION_ID,
                "CIVIL_SERVICE_EXAM",
                "考公",
                "civil_service_v1",
                "AVAILABLE",
                10,
                {
                    "icon": "公",
                    "welcome_message": "我是你的考公助理，可以帮你制定备考计划、安排每日任务、整理资料并复盘错题。告诉我你的目标考试和可投入时间，我们从最重要的一步开始。",
                    "starter_prompts": [
                        {
                            "label": "制定备考计划",
                            "content": "根据我的目标考试和可投入时间，帮我制定备考计划",
                        },
                        {
                            "label": "安排今日任务",
                            "content": "结合我的备考进度，帮我安排今天的学习任务",
                        },
                        {
                            "label": "复盘错题",
                            "content": "帮我整理并复盘最近的错题，找出薄弱环节",
                        },
                    ],
                },
            ),
            (
                IELTS_DEFINITION_ID,
                "IELTS",
                "考雅思",
                "unavailable",
                "COMING_SOON",
                20,
                {"icon": "雅", "welcome_message": "", "starter_prompts": []},
            ),
            (
                JOB_SEARCH_DEFINITION_ID,
                "JOB_SEARCH",
                "求职",
                "unavailable",
                "COMING_SOON",
                30,
                {"icon": "职", "welcome_message": "", "starter_prompts": []},
            ),
        )
        for definition_id, code, name, graph, catalog_status, order, metadata in catalog:
            execution_status = "ACTIVE" if catalog_status != "COMING_SOON" else "DRAFT"
            await connection.execute(
                text(
                    "INSERT INTO agent_definitions(id,code,version,name,description,graph_name,status,"
                    "catalog_status,display_order,catalog_metadata) "
                    "VALUES(:id,:code,1,:name,:description,:graph,:status,:catalog_status,:display_order,"
                    "CAST(:metadata AS jsonb)) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,"
                    "description=EXCLUDED.description,catalog_status=EXCLUDED.catalog_status,"
                    "display_order=EXCLUDED.display_order,catalog_metadata=EXCLUDED.catalog_metadata"
                ),
                {
                    "id": definition_id,
                    "code": code,
                    "name": name,
                    "description": {
                        "CIVIL_SERVICE_EXAM": "规划备考与任务复盘",
                        "IELTS": "听说读写专项提升",
                        "JOB_SEARCH": "简历、面试与投递规划",
                    }.get(code, ""),
                    "graph": graph,
                    "status": execution_status,
                    "catalog_status": catalog_status,
                    "display_order": order,
                    "metadata": _json(metadata),
                },
            )

    async def _ensure_user_workspace(self, connection: AsyncConnection, user_id: UUID) -> None:
        butler_user_agent = uuid5(user_id, "BUTLER")
        civil_user_agent = uuid5(user_id, "CIVIL_SERVICE_EXAM")
        for user_agent_id, definition_id in (
            (butler_user_agent, BUTLER_DEFINITION_ID),
            (civil_user_agent, CIVIL_DEFINITION_ID),
        ):
            await connection.execute(
                text(
                    "INSERT INTO user_agents(id,user_id,agent_definition_id,status,activated_at) "
                    "VALUES(:id,:user_id,:definition,'ACTIVE',now()) ON CONFLICT(user_id,agent_definition_id) DO NOTHING"
                ),
                {"id": user_agent_id, "user_id": user_id, "definition": definition_id},
            )
        conversation_id = uuid5(user_id, "MAIN_CONVERSATION")
        segment_id = uuid5(user_id, "MAIN_CONVERSATION:1")
        await connection.execute(
            text(
                "INSERT INTO conversations(id,user_id,user_agent_id,client_conversation_id,title,status) "
                "VALUES(:id,:user_id,:agent,:id,'新的对话','CURRENT') "
                "ON CONFLICT(user_id,client_conversation_id) DO NOTHING"
            ),
            {"id": conversation_id, "user_id": user_id, "agent": butler_user_agent},
        )
        await connection.execute(
            text(
                "INSERT INTO conversation_segments(id,conversation_id,user_id,sequence,thread_id,status) "
                "VALUES(:id,:conversation,:user_id,1,:thread,'ACTIVE') ON CONFLICT(id) DO NOTHING"
            ),
            {
                "id": segment_id,
                "conversation": conversation_id,
                "user_id": user_id,
                "thread": f"thread-{segment_id}",
            },
        )
        await connection.execute(
            text(
                "UPDATE conversations SET active_segment_id=COALESCE(active_segment_id,:segment) WHERE id=:id"
            ),
            {"segment": segment_id, "id": conversation_id},
        )

    async def _ensure_synthetic_source(self, connection: AsyncConnection) -> None:
        digest = hashlib.sha256(b"synthetic civil service verification source").hexdigest()
        await connection.execute(
            text(
                "INSERT INTO knowledge_documents(id,visibility,domain,title,source_organization,source_level,sha256,"
                "retrieved_at,ingestion_status) VALUES(:id,'PUBLIC','CIVIL_SERVICE_EXAM',"
                "'合成公考验收资料（非真实公告）','AI Butler Test Fixtures','GENERAL',:sha,now(),'READY') "
                "ON CONFLICT(id) DO NOTHING"
            ),
            {"id": PUBLIC_SOURCE_ID, "sha": digest},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_chunks(id,document_id,chunk_index,content,token_count,content_hash,embedding_model,"
                "qdrant_collection,qdrant_point_id,vector_status) VALUES(:id,:document,0,"
                "'合成验收资料：训练计划包含行测与申论模块。',20,:hash,'fake-embedding-v1',"
                "'ai_butler_knowledge',:point,'READY') ON CONFLICT(id) DO NOTHING"
            ),
            {
                "id": PUBLIC_CHUNK_ID,
                "document": PUBLIC_SOURCE_ID,
                "hash": digest,
                "point": PUBLIC_CHUNK_ID,
            },
        )

    async def _get_user(self, connection: AsyncConnection, user_id: UUID) -> dict[str, object]:
        row = _row(
            await connection.execute(
                text(
                    "SELECT id,nickname,locale,timezone,status,created_at FROM users WHERE id=:id"
                ),
                {"id": user_id},
            )
        )
        if row is None:
            raise not_found()
        row["avatar_url"] = None
        return row
