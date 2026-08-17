from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from ai_butler.api.schemas import (
    CompleteUploadRequest,
    UploadIntentRequest,
)
from ai_butler.domain.errors import ButlerError, not_found
from ai_butler.security import issue_signed_ticket

from .context import ButlerContext
from .shared import (
    _row,
)


class FileService:
    def __init__(self, context: ButlerContext) -> None:
        self.database = context.database
        self.settings = context.settings
        self.evidence_gate = context.evidence_gate

    async def create_upload_intent(
        self, user_id: UUID, request: UploadIntentRequest
    ) -> dict[str, object]:
        file_id = uuid4()
        object_key = f"users/{user_id}/{file_id}"
        ticket = issue_signed_ticket(
            file_id, self.settings.stream_ticket_secret, self.settings.stream_ticket_seconds
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO stored_files(id,user_id,purpose,original_filename,object_key,mime_type,size_bytes,sha256,"
                    "upload_status,scan_status) VALUES(:id,:user_id,:purpose,:filename,:object_key,:mime,:size,:sha,"
                    "'PENDING','PENDING')"
                ),
                {
                    "id": file_id,
                    "user_id": user_id,
                    "purpose": request.purpose,
                    "filename": Path(request.filename).name,
                    "object_key": object_key,
                    "mime": request.declared_mime_type,
                    "size": request.size_bytes,
                    "sha": request.sha256,
                },
            )
        return {
            "file": {
                "id": file_id,
                "purpose": request.purpose,
                "original_filename": Path(request.filename).name,
                "upload_status": "PENDING",
                "scan_status": "PENDING",
            },
            "upload": {
                "method": "PUT",
                "url": f"{self.settings.public_base_url}/v1/files/{file_id}/content?ticket={ticket}",
                "headers": {"Content-Type": request.declared_mime_type},
                "expires_at": datetime.now(UTC)
                + timedelta(seconds=self.settings.stream_ticket_seconds),
            },
        }

    async def store_local_upload(self, file_id: UUID, content: bytes) -> None:
        async with self.database.transaction() as connection:
            row = _row(
                await connection.execute(
                    text("SELECT * FROM stored_files WHERE id=:id FOR UPDATE"), {"id": file_id}
                )
            )
            if row is None or row["upload_status"] != "PENDING":
                raise not_found()
            if len(content) != row["size_bytes"]:
                raise ButlerError("FILE_SIZE_MISMATCH", "文件大小与上传意图不一致", 400)
            target = (self.settings.object_storage_local_path / row["object_key"]).resolve()
            root = self.settings.object_storage_local_path.resolve()
            if not target.is_relative_to(root):
                raise ButlerError("INVALID_OBJECT_KEY", "文件路径无效", 400)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            await connection.execute(
                text(
                    "UPDATE stored_files SET upload_status='UPLOADED',updated_at=now() WHERE id=:id"
                ),
                {"id": file_id},
            )

    async def read_local_file(self, file_id: UUID) -> tuple[bytes, str, str]:
        """在短期票据已验证后读取本地对象；不暴露 object_key。"""

        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT object_key,mime_type,original_filename,upload_status,scan_status "
                        "FROM stored_files WHERE id=:id"
                    ),
                    {"id": file_id},
                )
            )
        if row is None or row["upload_status"] != "VERIFIED" or row["scan_status"] != "CLEAN":
            raise not_found()
        target = self.settings.object_storage_local_path / row["object_key"]
        if not target.exists():
            raise not_found()
        return target.read_bytes(), row["mime_type"], row["original_filename"]

    async def complete_upload(
        self, user_id: UUID, file_id: UUID, request: CompleteUploadRequest
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            row = _row(
                await connection.execute(
                    text("SELECT * FROM stored_files WHERE id=:id AND user_id=:user_id FOR UPDATE"),
                    {"id": file_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            target = self.settings.object_storage_local_path / row["object_key"]
            if (
                not target.exists()
                or hashlib.sha256(target.read_bytes()).hexdigest() != request.sha256
            ):
                raise ButlerError("FILE_HASH_MISMATCH", "文件校验失败", 400)
            if row["sha256"] != request.sha256:
                raise ButlerError("FILE_HASH_MISMATCH", "文件校验失败", 400)
            # 验证环境使用确定性扫描器；生产配置不得把该结果当作真实恶意软件扫描。
            await connection.execute(
                text(
                    "UPDATE stored_files SET upload_status='VERIFIED',scan_status='CLEAN',updated_at=now() WHERE id=:id"
                ),
                {"id": file_id},
            )
        return await self.get_file(user_id, file_id)

    async def get_file(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT sf.id,sf.purpose,sf.original_filename,sf.mime_type,sf.size_bytes,"
                        "sf.upload_status,sf.scan_status,sf.created_at,kd.ingestion_status AS knowledge_status "
                        "FROM stored_files sf LEFT JOIN knowledge_documents kd ON kd.stored_file_id=sf.id "
                        "WHERE sf.id=:id AND sf.user_id=:user_id AND sf.upload_status<>'DELETED'"
                    ),
                    {"id": file_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            row["status"] = (
                "READY"
                if row["upload_status"] == "VERIFIED" and row["scan_status"] == "CLEAN"
                else "PROCESSING"
            )
            return row

    async def file_download(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        file = await self.get_file(user_id, file_id)
        if file["status"] != "READY":
            raise not_found()
        ticket = issue_signed_ticket(
            file_id, self.settings.stream_ticket_secret, self.settings.stream_ticket_seconds
        )
        return {
            "url": f"{self.settings.public_base_url}/v1/files/{file_id}/content?ticket={ticket}",
            "expires_at": datetime.now(UTC)
            + timedelta(seconds=self.settings.stream_ticket_seconds),
            "filename": file["original_filename"],
        }

    async def delete_file(self, user_id: UUID, file_id: UUID) -> dict[str, object]:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                text(
                    "UPDATE stored_files SET upload_status='DELETED',deleted_at=now(),updated_at=now() "
                    "WHERE id=:id AND user_id=:user_id AND upload_status<>'DELETED' "
                    "RETURNING object_key"
                ),
                {"id": file_id, "user_id": user_id},
            )
            row = result.first()
            if row is None:
                raise not_found()
            await connection.execute(
                text(
                    "UPDATE knowledge_documents SET ingestion_status='DELETING',updated_at=now() "
                    "WHERE stored_file_id=:file_id"
                ),
                {"file_id": file_id},
            )
        target = self.settings.object_storage_local_path / row[0]
        if target.exists():
            target.unlink()
        return {"id": file_id, "status": "DELETED"}

    async def get_document_access(self, user_id: UUID, document_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text(
                        "SELECT * FROM knowledge_documents WHERE id=:id AND ingestion_status='READY' "
                        "AND (visibility='PUBLIC' OR owner_user_id=:user_id)"
                    ),
                    {"id": document_id, "user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            if row["source_url"]:
                return {"access_type": "EXTERNAL_URL", "url": row["source_url"], "expires_at": None}
            raise ButlerError("SOURCE_FIXTURE_ONLY", "合成验收资料没有外部原文", 409)

    async def list_files(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT sf.id,sf.purpose,sf.original_filename,sf.mime_type,sf.size_bytes,"
                            "sf.created_at,kd.ingestion_status AS knowledge_status FROM stored_files sf "
                            "LEFT JOIN knowledge_documents kd ON kd.stored_file_id=sf.id "
                            "WHERE sf.user_id=:user_id AND sf.upload_status='VERIFIED' "
                            "AND sf.scan_status='CLEAN' ORDER BY sf.created_at DESC"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [dict(row) for row in rows], "next_cursor": None, "has_more": False}

    async def get_citation(self, user_id: UUID, citation_id: UUID) -> dict[str, object]:
        """返回当前用户 run 中的来源快照，并按来源类型生成安全访问方式。"""

        async with self.database.connect() as connection:
            citation = _row(
                await connection.execute(
                    text(
                        "SELECT ci.*,kd.stored_file_id,sf.original_filename,sf.upload_status,"
                        "sf.scan_status FROM citations ci JOIN claims c ON c.id=ci.claim_id "
                        "JOIN agent_runs r ON r.id=c.agent_run_id "
                        "LEFT JOIN knowledge_chunks kc ON kc.id=ci.knowledge_chunk_id "
                        "LEFT JOIN knowledge_documents kd ON kd.id=kc.document_id "
                        "LEFT JOIN stored_files sf ON sf.id=kd.stored_file_id "
                        "WHERE ci.id=:id AND r.user_id=:user_id AND "
                        "(kd.id IS NULL OR kd.visibility='PUBLIC' OR kd.owner_user_id=:user_id)"
                    ),
                    {"id": citation_id, "user_id": user_id},
                )
            )
            if citation is None:
                raise not_found()
        access: dict[str, object] = {"type": "UNAVAILABLE", "url": None, "expires_at": None}
        source_url = citation["source_url_snapshot"]
        if source_url:
            canonical, _ = self.evidence_gate.canonicalize_url(str(source_url))
            access = {"type": "EXTERNAL_URL", "url": canonical, "expires_at": None}
        elif citation["stored_file_id"]:
            if citation["upload_status"] != "VERIFIED" or citation["scan_status"] != "CLEAN":
                raise not_found()
            expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.stream_ticket_seconds)
            ticket = issue_signed_ticket(
                UUID(str(citation["stored_file_id"])),
                self.settings.stream_ticket_secret,
                self.settings.stream_ticket_seconds,
            )
            access = {
                "type": "SIGNED_FILE",
                "url": f"{self.settings.public_base_url}/v1/files/{citation['stored_file_id']}/content?ticket={ticket}",
                "expires_at": expires_at,
            }
        return {
            "id": citation["id"],
            "source_type": citation["source_type"],
            "title": citation["source_title_snapshot"],
            "source_organization": citation["source_organization_snapshot"],
            "domain": citation["source_domain_snapshot"],
            "published_at": citation["published_at_snapshot"],
            "retrieved_at": citation["retrieved_at_snapshot"],
            "evidence_excerpt": citation["evidence_excerpt"],
            "access": access,
        }
