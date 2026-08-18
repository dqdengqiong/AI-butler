from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from ai_butler.api.app import create_app
from ai_butler.application.butler import _encode_cursor
from ai_butler.config import Settings

pytestmark = pytest.mark.integration


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model_routing_enabled=False,
        app_database_url=(
            "postgresql+psycopg://butler_test:butler_test@127.0.0.1:5432/butler_test"
        ),
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


async def _login(client: httpx.AsyncClient, suffix: str) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/wechat/login",
        headers={"Idempotency-Key": f"conversation-login-{suffix}"},
        json={
            "schema_version": "1.0",
            "login_code": f"conversation-{suffix}",
            "phone_code": f"phone-conversation-{suffix}",
            "provider": "WECHAT_MOCK",
            "device_id": f"conversation-device-{suffix}",
            "consent": {
                "terms_version": "2026-08-01",
                "privacy_version": "2026-08-01",
                "accepted_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _message(client_id: str, content: str, **extra: object) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "client_message_id": client_id,
        "content": content,
        "attachments": [],
        **extra,
    }


async def _cancel(
    client: httpx.AsyncClient, headers: dict[str, str], run_id: str, suffix: str
) -> None:
    response = await client.post(
        f"/v1/agent-runs/{run_id}/cancel",
        headers={**headers, "Idempotency-Key": f"cancel-{suffix}-{run_id}"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_message_entry_conversation_routing_pagination_and_idempotency(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers = await _login(client, suffix)
        catalog = (await client.get("/v1/agent-definitions")).json()["items"]
        assert [(item["code"], item["availability"]) for item in catalog] == [
            ("CIVIL_SERVICE_EXAM", "AVAILABLE"),
            ("IELTS", "COMING_SOON"),
            ("JOB_SEARCH", "COMING_SOON"),
        ]
        assert (await client.get("/v1/conversations", headers=headers)).json()["items"] == []
        assert (await client.post("/v1/conversations", headers=headers, json={})).status_code == 405

        first_request = _message(f"first-{suffix}", "我要制定公务员行测计划")
        first = await client.post("/v1/messages", headers=headers, json=first_request)
        assert first.status_code == 202, first.text
        first_payload = first.json()
        assert first_payload["transition"]["kind"] == "CREATED"
        first_conversation = first_payload["conversation_id"]
        await _cancel(client, headers, first_payload["run"]["id"], suffix)

        message_page = (
            await client.get(
                f"/v1/conversations/{first_conversation}/messages?limit=1", headers=headers
            )
        ).json()
        assert message_page["has_more"] is True
        older_page = (
            await client.get(
                f"/v1/conversations/{first_conversation}/messages",
                headers=headers,
                params={"limit": 1, "cursor": message_page["next_cursor"]},
            )
        ).json()
        assert len(older_page["items"]) == 1
        invalid_message_cursor = await client.get(
            f"/v1/conversations/{first_conversation}/messages",
            headers=headers,
            params={"cursor": _encode_cursor("not-a-time", "not-a-uuid")},
        )
        assert invalid_message_cursor.status_code == 400
        assert (
            await client.get(f"/v1/conversations/{uuid4()}/messages", headers=headers)
        ).status_code == 404
        assert (await client.get(f"/v1/agent-runs/{uuid4()}", headers=headers)).status_code == 404

        changed_request = _message(f"changed-{suffix}", "帮我修改求职简历")
        changed = await client.post("/v1/messages", headers=headers, json=changed_request)
        assert changed.status_code == 202, changed.text
        changed_payload = changed.json()
        assert changed_payload["transition"] == {
            "kind": "CREATED",
            "archived_conversation_id": first_conversation,
        }
        repeated = await client.post("/v1/messages", headers=headers, json=changed_request)
        assert repeated.json()["run"]["id"] == changed_payload["run"]["id"]
        reused = await client.post(
            "/v1/messages",
            headers=headers,
            json={**changed_request, "content": "相同消息标识的其他内容"},
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        await _cancel(client, headers, changed_payload["run"]["id"], suffix)
        ambiguous = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(f"ambiguous-{suffix}", "另外问一下"),
        )
        assert ambiguous.status_code == 409
        assert ambiguous.json()["error"]["code"] == "TOPIC_SWITCH_CONFIRMATION_REQUIRED"

        timeline = (await client.get("/v1/conversations", headers=headers)).json()["items"]
        assert [item["status"] for item in timeline] == ["CURRENT", "ARCHIVED"]
        first_page = (await client.get("/v1/conversations?limit=1", headers=headers)).json()
        second_page = (
            await client.get(
                "/v1/conversations",
                headers=headers,
                params={"limit": 1, "cursor": first_page["next_cursor"]},
            )
        ).json()
        assert second_page["items"][0]["id"] == first_conversation
        assert (
            await client.get("/v1/conversations", headers=headers, params={"cursor": "invalid"})
        ).status_code == 400
        assert (
            await client.delete(
                f"/v1/conversations/{changed_payload['conversation_id']}",
                headers={**headers, "Idempotency-Key": f"delete-current-{suffix}"},
            )
        ).status_code == 409
        history_headers = {**headers, "Idempotency-Key": f"delete-history-{suffix}"}
        assert (
            await client.delete(f"/v1/conversations/{first_conversation}", headers=history_headers)
        ).status_code == 204
        assert (
            await client.delete(f"/v1/conversations/{first_conversation}", headers=history_headers)
        ).status_code == 204
