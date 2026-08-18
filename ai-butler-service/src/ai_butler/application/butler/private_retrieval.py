"""私有知识召回与 PostgreSQL 所有权复核。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ai_butler.adapters.search import SearchResult

from .context import ButlerContext


class PrivateEvidenceRetriever:
    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.embedding_provider = context.embedding_provider
        self.vector_store = context.vector_store
        self.settings = context.settings

    async def _retrieve_private_evidence(
        self,
        user_id: UUID,
        query: str,
        allowed_file_ids: tuple[UUID, ...],
    ) -> tuple[SearchResult, ...]:
        """只召回 PostgreSQL 已授权且就绪的私有文档，并复核每个命中。

        模型可以建议使用私有知识，但不能证明用户确实拥有可检索资料。向量调用前
        必须先解析业务事实：没有合格文档时返回空证据，让计划或联网分支继续执行；
        不能让空 Qdrant collection、历史向量维度或暂不可用的向量服务阻断请求。
        """

        if allowed_file_ids:
            document_query = text(
                "SELECT kd.id FROM knowledge_documents kd "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kd.owner_user_id=:user_id AND kd.stored_file_id=ANY(:file_ids) "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN'"
            )
            document_parameters: dict[str, object] = {
                "user_id": user_id,
                "file_ids": list(allowed_file_ids),
            }
        else:
            document_query = text(
                "SELECT kd.id FROM knowledge_documents kd "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kd.owner_user_id=:user_id "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN'"
            )
            document_parameters = {"user_id": user_id}
        async with self.database.connect() as connection:
            document_rows = (await connection.execute(document_query, document_parameters)).all()
        document_ids = tuple(UUID(str(row[0])) for row in document_rows)
        if not document_ids:
            return ()

        vector = await self.embedding_provider.embed(query)
        hits = await self.vector_store.search(
            user_id,
            vector,
            self.settings.search_candidate_results,
            document_ids,
        )
        if not hits:
            return ()
        hit_by_chunk = {hit.chunk_id: hit for hit in hits}
        chunk_ids = tuple(hit_by_chunk)
        parameters: dict[str, object] = {"user_id": user_id, "chunk_ids": list(chunk_ids)}
        if allowed_file_ids:
            parameters["file_ids"] = list(allowed_file_ids)
            query_text = text(
                "SELECT kc.id,kc.content,kd.id AS document_id,kd.title,kd.stored_file_id "
                "FROM knowledge_chunks kc "
                "JOIN knowledge_documents kd ON kd.id=kc.document_id "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kc.id=ANY(:chunk_ids) AND kd.owner_user_id=:user_id "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN' "
                "AND kd.stored_file_id=ANY(:file_ids)"
            )
        else:
            query_text = text(
                "SELECT kc.id,kc.content,kd.id AS document_id,kd.title,kd.stored_file_id "
                "FROM knowledge_chunks kc "
                "JOIN knowledge_documents kd ON kd.id=kc.document_id "
                "JOIN stored_files sf ON sf.id=kd.stored_file_id "
                "WHERE kc.id=ANY(:chunk_ids) AND kd.owner_user_id=:user_id "
                "AND kd.visibility='PRIVATE' AND kd.ingestion_status='READY' "
                "AND sf.upload_status='VERIFIED' AND sf.scan_status='CLEAN'"
            )
        async with self.database.connect() as connection:
            chunk_rows = (await connection.execute(query_text, parameters)).mappings().all()
        by_id = {
            UUID(str(row["id"])): row
            for row in chunk_rows
            if hit_by_chunk[UUID(str(row["id"]))].document_id == UUID(str(row["document_id"]))
        }
        return tuple(
            SearchResult(
                evidence_ref=f"private-{chunk_id}",
                title=str(by_id[chunk_id]["title"]),
                source_organization="我的资料",
                content=str(by_id[chunk_id]["content"]),
                score=hit_by_chunk[chunk_id].score,
                url=None,
                source_type="PRIVATE_FILE",
                knowledge_chunk_id=chunk_id,
                document_id=UUID(str(by_id[chunk_id]["document_id"])),
            )
            for chunk_id in chunk_ids
            if chunk_id in by_id
        )
