from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from ai_butler.api.app import create_app
from ai_butler.config import Settings

pytestmark = pytest.mark.integration


def _login_payload(code: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "login_code": code,
        "provider": "WECHAT_MOCK",
        "device_id": f"device-{code}",
        "consent": {
            "terms_version": "2026-08-01",
            "privacy_version": "2026-08-01",
            "accepted_at": datetime.now(UTC).isoformat(),
        },
    }


async def _login(client: httpx.AsyncClient, code: str) -> tuple[dict[str, str], UUID]:
    response = await client.post(
        "/v1/auth/wechat/login",
        headers={"Idempotency-Key": f"login-{code}"},
        json=_login_payload(code),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, UUID(payload["user"]["id"])


@pytest.mark.asyncio
async def test_multi_conversation_catalog_archive_resume_and_global_run(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_database_url="postgresql+psycopg://butler_test:butler_test@127.0.0.1:5432/butler_test",
        migration_database_url="postgresql+psycopg://butler_migrator:butler_migrator@127.0.0.1:5432/butler_test",
        object_storage_local_path=tmp_path,
        public_base_url="http://test",
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, user_id = await _login(client, f"multi-{suffix}")

        catalog = (await client.get("/v1/agent-definitions")).json()["items"]
        assert [(item["code"], item["availability"]) for item in catalog] == [
            ("CIVIL_SERVICE_EXAM", "AVAILABLE"),
            ("IELTS", "COMING_SOON"),
            ("JOB_SEARCH", "COMING_SOON"),
        ]
        assert len(catalog[0]["starter_prompts"]) == 3

        initial = (await client.get("/v1/conversations", headers=headers)).json()["items"][0]
        client_conversation_id = uuid4()
        created = await client.post(
            "/v1/conversations",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_conversation_id": str(client_conversation_id),
                "specialist_code": "CIVIL_SERVICE_EXAM",
            },
        )
        assert created.status_code == 201, created.text
        specialist = created.json()
        assert specialist["specialist"]["code"] == "CIVIL_SERVICE_EXAM"
        specialist_id = specialist["id"]
        duplicate = await client.post(
            "/v1/conversations",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_conversation_id": str(client_conversation_id),
                "specialist_code": "CIVIL_SERVICE_EXAM",
            },
        )
        assert duplicate.json()["id"] == specialist_id
        unavailable = await client.post(
            "/v1/conversations",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_conversation_id": str(uuid4()),
                "specialist_code": "IELTS",
            },
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["error"]["code"] == "AGENT_NOT_AVAILABLE"

        timeline = (await client.get("/v1/conversations", headers=headers)).json()["items"]
        assert timeline[0]["id"] == specialist_id
        assert timeline[0]["status"] == "CURRENT"
        assert (
            next(item for item in timeline if item["id"] == initial["id"])["status"] == "ARCHIVED"
        )
        messages = (
            await client.get(f"/v1/conversations/{specialist_id}/messages", headers=headers)
        ).json()["items"]
        assert messages[0]["role"] == "ASSISTANT"
        assert "考公助理" in messages[0]["content"]

        first_request = {
            "schema_version": "1.0",
            "client_message_id": f"message-{suffix}",
            "content": "你好",
            "attachments": [],
            "selection": None,
        }
        first = await client.post(
            f"/v1/conversations/{specialist_id}/messages",
            headers=headers,
            json=first_request,
        )
        assert first.status_code == 202, first.text
        first_run = first.json()["run"]["id"]
        repeated = await client.post(
            f"/v1/conversations/{specialist_id}/messages",
            headers=headers,
            json=first_request,
        )
        assert repeated.json()["run"]["id"] == first_run
        changed = await client.post(
            f"/v1/conversations/{specialist_id}/messages",
            headers=headers,
            json={**first_request, "content": "不同内容"},
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        async with app.state.database.connect() as connection:
            selected = (
                await connection.execute(
                    text(
                        "SELECT ua.user_id,ad.code FROM agent_runs r "
                        "JOIN user_agents ua ON ua.id=r.selected_user_agent_id "
                        "JOIN agent_definitions ad ON ad.id=ua.agent_definition_id WHERE r.id=:id"
                    ),
                    {"id": UUID(first_run)},
                )
            ).one()
        assert selected == (user_id, "CIVIL_SERVICE_EXAM")

        blocked = await client.post(
            "/v1/conversations",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_conversation_id": str(uuid4()),
                "specialist_code": None,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "GLOBAL_RUN_IN_PROGRESS"
        blocked_restore = await client.post(
            f"/v1/conversations/{initial['id']}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"blocked-{suffix}",
                "content": "这条消息不能跨会话并发发送",
                "attachments": [],
                "selection": None,
            },
        )
        assert blocked_restore.status_code == 409
        assert blocked_restore.json()["error"]["code"] == "GLOBAL_RUN_IN_PROGRESS"
        await client.post(
            f"/v1/agent-runs/{first_run}/cancel",
            headers={**headers, "Idempotency-Key": f"cancel-{suffix}"},
        )

        normal = await client.post(
            "/v1/conversations",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_conversation_id": str(uuid4()),
                "specialist_code": None,
            },
        )
        assert normal.status_code == 201
        normal_id = normal.json()["id"]
        first_page = (await client.get("/v1/conversations?limit=1", headers=headers)).json()
        assert first_page["items"][0]["id"] == normal_id
        assert first_page["has_more"] is True
        second_page = (
            await client.get(
                "/v1/conversations",
                headers=headers,
                params={"limit": 1, "cursor": first_page["next_cursor"]},
            )
        ).json()
        assert second_page["items"][0]["id"] != normal_id
        resumed = await client.post(
            f"/v1/conversations/{specialist_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"resume-{suffix}",
                "content": "继续考公话题",
                "attachments": [],
                "selection": None,
            },
        )
        assert resumed.status_code == 202, resumed.text
        switched = (await client.get("/v1/conversations", headers=headers)).json()["items"]
        assert switched[0]["id"] == specialist_id
        assert switched[0]["title"] == "考公 · 你好"
        assert next(item for item in switched if item["id"] == normal_id)["status"] == "ARCHIVED"
        await client.post(
            f"/v1/agent-runs/{resumed.json()['run']['id']}/cancel",
            headers={**headers, "Idempotency-Key": f"cancel-resumed-{suffix}"},
        )

        other_headers, _ = await _login(client, f"other-{suffix}")
        assert (
            await client.get(f"/v1/conversations/{specialist_id}", headers=other_headers)
        ).status_code == 404
        assert (
            await client.get(f"/v1/conversations/{specialist_id}/messages", headers=other_headers)
        ).status_code == 404
