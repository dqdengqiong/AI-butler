from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from ai_butler.api.app import create_app
from ai_butler.application.butler.memory import LongTermMemoryService
from ai_butler.config import Settings

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_long_term_memory_and_private_vectors_are_deleted_across_stores(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_database_url="postgresql+psycopg://butler_test:butler_test@127.0.0.1:5432/butler_test",
        migration_database_url=(
            "postgresql+psycopg://butler_migrator:butler_migrator@127.0.0.1:5432/butler_test"
        ),
        langgraph_database_url=(
            "postgresql://butler_test:butler_test@127.0.0.1:5432/butler_langgraph_test"
        ),
        langgraph_migration_database_url=(
            "postgresql://butler_migrator:butler_migrator@127.0.0.1:5432/butler_langgraph_test"
        ),
        object_storage_local_path=tmp_path,
        public_base_url="http://test",
    )
    app = create_app(settings)
    suffix = uuid4().hex
    payload = {
        "schema_version": "1.0",
        "login_code": f"memory-{suffix}",
        "phone_code": f"phone-memory-{suffix}",
        "provider": "WECHAT_MOCK",
        "device_id": f"memory-device-{suffix}",
        "consent": {
            "terms_version": "2026-08-01",
            "privacy_version": "2026-08-01",
            "accepted_at": datetime.now(UTC).isoformat(),
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        login = await client.post(
            "/v1/auth/wechat/login",
            headers={"Idempotency-Key": f"memory-login-{suffix}"},
            json=payload,
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        user_id = UUID((await client.get("/v1/me", headers=headers)).json()["id"])

        content = b"synthetic private memory deletion fixture"
        digest = hashlib.sha256(content).hexdigest()
        intent = await client.post(
            "/v1/files/upload-intents",
            headers={**headers, "Idempotency-Key": f"memory-upload-{suffix}"},
            json={
                "schema_version": "1.0",
                "purpose": "CHAT_ATTACHMENT",
                "filename": "memory-fixture.txt",
                "declared_mime_type": "text/plain",
                "size_bytes": len(content),
                "sha256": digest,
            },
        )
        assert intent.status_code == 201
        file_id = intent.json()["file"]["id"]
        upload_url = intent.json()["upload"]["url"].removeprefix("http://test")
        assert (await client.put(upload_url, content=content)).status_code == 204
        completed = await client.post(
            f"/v1/files/{file_id}/complete",
            headers={**headers, "Idempotency-Key": f"memory-complete-{suffix}"},
            json={"schema_version": "1.0", "sha256": digest},
        )
        assert completed.status_code == 202
        for _ in range(100):
            await app.state.butler.scheduler_poll_once()
            files = (await client.get("/v1/files", headers=headers)).json()["items"]
            if any(item["id"] == file_id and item["knowledge_status"] == "READY" for item in files):
                break
        assert any(item["id"] == file_id and item["knowledge_status"] == "READY" for item in files)

    memory = LongTermMemoryService(app.state.butler._context)
    assert await memory.remember(user_id, "我通常早上学习", user_requested=True)
    assert await memory.remember(user_id, "我的银行卡是 62220000", user_requested=True) is False
    assert await memory.search(user_id, "学习时间") == ("我通常早上学习",)
    await memory.forget(user_id, "我通常早上学习", "USER_REQUESTED")
    assert await memory.search(user_id, "学习时间") == ()
    assert await memory.remember(user_id, "我通常早上学习", user_requested=True) is False
    assert await memory.remember(user_id, "我喜欢晚间复习", user_requested=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        deleted = await client.request(
            "DELETE",
            "/v1/me",
            headers={**headers, "Idempotency-Key": f"memory-delete-{suffix}"},
            json={"schema_version": "1.0", "confirmation": "DELETE_MY_ACCOUNT"},
        )
        assert deleted.status_code == 202
        for _ in range(100):
            if not await app.state.butler.scheduler_poll_once():
                break
        assert (await client.get("/v1/me", headers=headers)).status_code == 401

    async with app.state.database.connect() as connection:
        status = (
            await connection.execute(
                text("SELECT status FROM users WHERE id=:id"),
                {"id": user_id},
            )
        ).scalar_one()
        documents = (
            await connection.execute(
                text("SELECT count(*) FROM knowledge_documents WHERE owner_user_id=:id"),
                {"id": user_id},
            )
        ).scalar_one()
    assert status == "DELETED"
    assert documents == 0
    assert await memory.search(user_id, "晚间复习") == ()
    vector = await app.state.butler.embedding_provider.embed("synthetic private memory")
    assert await app.state.butler.vector_store.search(user_id, vector, 8) == ()
    has_files = await asyncio.to_thread(lambda: any(path.is_file() for path in tmp_path.rglob("*")))
    assert not has_files
    await app.state.database.close()
