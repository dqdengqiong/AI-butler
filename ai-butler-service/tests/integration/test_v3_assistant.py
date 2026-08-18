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


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_database_url=(
            "postgresql+psycopg://butler_test:butler_test@127.0.0.1:5432/butler_test"
        ),
        migration_database_url=(
            "postgresql+psycopg://butler_migrator:butler_migrator@127.0.0.1:5432/butler_test"
        ),
        object_storage_local_path=tmp_path,
        public_base_url="http://test",
    )


async def _complete_run(
    app: object, client: httpx.AsyncClient, headers: dict[str, str], run_id: str
) -> dict[str, object]:
    for _ in range(100):
        run = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        if run["status"] != "QUEUED":
            return run
        assert await app.state.butler.worker_poll_once(uuid4()) is True  # type: ignore[attr-defined]
    raise AssertionError("run was not claimed")


async def test_v3_general_response_stream_and_research_are_model_driven(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        login = await client.post(
            "/v1/auth/wechat/login",
            headers={"Idempotency-Key": f"v3-login-{suffix}"},
            json={
                "schema_version": "1.0",
                "login_code": f"v3-{suffix}",
                "phone_code": f"phone-v3-{suffix}",
                "provider": "WECHAT_MOCK",
                "device_id": f"v3-device-{suffix}",
                "consent": {
                    "terms_version": "2026-08-01",
                    "privacy_version": "2026-08-01",
                    "accepted_at": datetime.now(UTC).isoformat(),
                },
            },
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        general = await client.post(
            "/v1/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"general-{suffix}",
                "content": "你好，请简单说明怎样建立稳定的学习习惯",
                "attachments": [],
                "selection": None,
            },
        )
        run_id = general.json()["run"]["id"]
        conversation_id = general.json()["conversation_id"]
        run = await _complete_run(app, client, headers, run_id)

        assert run["status"] == "SUCCEEDED"
        messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        answer = next(
            item["content"]
            for item in messages
            if item.get("agent_run_id") == run_id and item["role"] == "ASSISTANT"
        )
        assert "我目前可以协助公务员备考规划" not in answer
        events = await app.state.butler.list_events(  # type: ignore[attr-defined]
            UUID(login.json()["user"]["id"]), UUID(run_id), 0
        )
        event_names = [item["event_type"] for item in events]
        assert "message.start" in event_names
        assert "message.delta" in event_names
        assert "message.completed" in event_names

        async with app.state.butler.database.connect() as connection:  # type: ignore[attr-defined]
            versions = (
                await connection.execute(
                    text(
                        "SELECT graph_version,prompt_bundle_version FROM agent_runs "
                        "WHERE id=:run_id"
                    ),
                    {"run_id": UUID(run_id)},
                )
            ).one()
            nodes = set(
                (
                    await connection.execute(
                        text(
                            "SELECT node_name FROM agent_trace_spans "
                            "WHERE agent_run_id=:run_id AND span_kind='NODE'"
                        ),
                        {"run_id": UUID(run_id)},
                    )
                ).scalars()
            )
        assert tuple(versions) == ("butler-graph-v3", "butler-prompts-v3")
        assert {"Router", "Response"} <= nodes

        researched = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"research-{suffix}",
                "content": "请联网查询最新公务员考试资料",
                "attachments": [],
                "selection": None,
            },
        )
        research_run_id = researched.json()["run"]["id"]
        research_run = await _complete_run(app, client, headers, research_run_id)
        assert research_run["status"] == "SUCCEEDED"
        messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        assert any(
            card["card_type"] == "SourceCard"
            for message in messages
            if message.get("agent_run_id") == research_run_id
            for card in message.get("cards", {}).get("cards", [])
        )
