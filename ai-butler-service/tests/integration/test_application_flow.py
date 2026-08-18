from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
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
        qdrant_collection="ai_butler_knowledge_test",
        object_storage_local_path=tmp_path,
        public_base_url="http://test",
        sse_poll_interval_ms=10,
    )


def _login_payload(suffix: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "login_code": f"flow-{suffix}",
        "phone_code": f"phone-flow-{suffix}",
        "provider": "WECHAT_MOCK",
        "device_id": f"flow-device-{suffix}",
        "consent": {
            "terms_version": "2026-08-01",
            "privacy_version": "2026-08-01",
            "accepted_at": datetime.now(UTC).isoformat(),
        },
    }


async def _login(
    client: httpx.AsyncClient, suffix: str
) -> tuple[dict[str, str], dict[str, object]]:
    response = await client.post(
        "/v1/auth/wechat/login",
        headers={"Idempotency-Key": f"flow-login-{suffix}"},
        json=_login_payload(suffix),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, payload


async def _finish(
    app: object, client: httpx.AsyncClient, headers: dict[str, str], run_id: str
) -> dict[str, object]:
    for _ in range(100):
        run = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        if run["status"] not in {"QUEUED", "RUNNING"}:
            assert run["status"] == "SUCCEEDED", run
            return run
        await app.state.butler.worker_poll_once(uuid4())  # type: ignore[attr-defined]
    raise AssertionError("run did not finish")


@pytest.mark.asyncio
async def test_profile_confirmed_plan_task_file_and_account_governance_flow(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, auth = await _login(client, suffix)
        assert (await client.get("/v1/me")).status_code == 401
        assert (await client.get("/v1/me", headers=headers)).status_code == 200
        assert (await client.patch("/v1/me", headers=headers, json={})).status_code == 200
        assert (
            await client.patch("/v1/me", headers=headers, json={"avatar_file_id": str(uuid4())})
        ).status_code == 409
        renamed = await client.patch(
            "/v1/me",
            headers=headers,
            json={"nickname": "验证用户", "timezone": "Asia/Shanghai"},
        )
        assert renamed.json()["nickname"] == "验证用户"

        profile = (await client.get("/v1/me/profile", headers=headers)).json()
        profile_update = {
            "schema_version": "1.0",
            "expected_version": profile["profile_version"],
            "education_level": "本科",
            "major": "计算机",
            "region_code": "CN-44",
            "current_level": "BEGINNER",
            "existing_material_file_ids": [],
        }
        assert (
            await client.put("/v1/me/profile", headers=headers, json=profile_update)
        ).status_code == 200
        assert (
            await client.put("/v1/me/profile", headers=headers, json=profile_update)
        ).status_code == 409

        availability = (await client.get("/v1/me/availability", headers=headers)).json()
        availability_update = {
            "schema_version": "1.0",
            "expected_version": availability["version"],
            "windows": [
                {
                    "day_of_week": 1,
                    "start_time": "20:00:00",
                    "end_time": "21:00:00",
                    "available_minutes": 60,
                    "effective_from": date.today().isoformat(),
                    "effective_to": None,
                }
            ],
        }
        assert (
            await client.put("/v1/me/availability", headers=headers, json=availability_update)
        ).status_code == 200
        assert (
            await client.put("/v1/me/availability", headers=headers, json=availability_update)
        ).status_code == 409
        preferences = (await client.get("/v1/me/preferences", headers=headers)).json()
        preference_update = {
            "expected_version": preferences["version"],
            "task_reminder": {
                "enabled": True,
                "channels": ["IN_APP"],
                "advance_minutes": 10,
            },
        }
        assert (
            await client.patch("/v1/me/preferences", headers=headers, json=preference_update)
        ).status_code == 200
        assert (
            await client.patch("/v1/me/preferences", headers=headers, json=preference_update)
        ).status_code == 409

        preview = await client.post(
            "/v1/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"governance-preview-{suffix}",
                "content": "帮我制定2027年广东省考计划，准备4周，工作日每天1小时，周末每天2小时",
                "attachments": [],
            },
        )
        assert preview.status_code == 202, preview.text
        conversation_id = preview.json()["conversation_id"]
        await _finish(app, client, headers, preview.json()["run"]["id"])
        messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        preview_message = next(
            message
            for message in messages
            if any(card["card_type"] == "PlanPreviewCard" for card in message["cards"]["cards"])
        )
        preview_card = next(
            card
            for card in preview_message["cards"]["cards"]
            if card["card_type"] == "PlanPreviewCard"
        )
        confirmed = await client.post(
            f"/v1/plan-previews/{preview_message['id']}/confirm",
            headers={**headers, "Idempotency-Key": f"governance-confirm-{suffix}"},
            json={
                "schema_version": "1.0",
                "expected_preview_hash": preview_card["payload"]["preview_hash"],
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        plan_id = confirmed.json()["plan_id"]

        goals = (await client.get("/v1/goals", headers=headers)).json()["items"]
        plans = (await client.get("/v1/plans", headers=headers)).json()["items"]
        assert goals and plans
        plan = (await client.get(f"/v1/plans/{plan_id}", headers=headers)).json()
        revisions = (await client.get(f"/v1/plans/{plan_id}/revisions", headers=headers)).json()[
            "items"
        ]
        assert (
            await client.get(f"/v1/plans/{plan_id}/revisions/{revisions[0]['id']}", headers=headers)
        ).status_code == 200
        assert plan["current_revision"]["status"] == "APPROVED"
        tasks = (await client.get("/v1/tasks", headers=headers)).json()["items"]
        assert tasks
        assert (
            await client.get(
                "/v1/tasks",
                headers=headers,
                params={
                    "date_from": date.today().isoformat(),
                    "date_to": (date.today() + timedelta(days=100)).isoformat(),
                },
            )
        ).status_code == 400
        task_id = tasks[0]["id"]
        assert (await client.get(f"/v1/tasks/{task_id}", headers=headers)).status_code == 200
        execution = {
            "schema_version": "1.0",
            "client_execution_id": f"execution-{suffix}",
            "result": "COMPLETED",
            "duration_minutes": 32,
            "feedback": "已完成",
            "outcome_data": {"score": 80},
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        first_execution = await client.post(
            f"/v1/tasks/{task_id}/executions", headers=headers, json=execution
        )
        repeated_execution = await client.post(
            f"/v1/tasks/{task_id}/executions", headers=headers, json=execution
        )
        assert first_execution.status_code == 201
        assert (
            repeated_execution.json()["execution"]["id"]
            == first_execution.json()["execution"]["id"]
        )
        assert (await client.get("/v1/dashboard", headers=headers)).status_code == 200

        content = b"private verification material"
        digest = hashlib.sha256(content).hexdigest()
        upload_intent = await client.post(
            "/v1/files/upload-intents",
            headers={**headers, "Idempotency-Key": f"upload-{suffix}"},
            json={
                "schema_version": "1.0",
                "purpose": "CHAT_ATTACHMENT",
                "filename": "material.txt",
                "declared_mime_type": "text/plain",
                "size_bytes": len(content),
                "sha256": digest,
            },
        )
        assert upload_intent.status_code == 201
        file_id = upload_intent.json()["file"]["id"]
        upload_url = upload_intent.json()["upload"]["url"].removeprefix("http://test")
        assert (
            await client.put(f"/v1/files/{file_id}/content?ticket=invalid", content=content)
        ).status_code == 401
        assert (await client.put(upload_url, content=content)).status_code == 204
        assert (
            await client.post(
                f"/v1/files/{file_id}/complete",
                headers={**headers, "Idempotency-Key": f"complete-{suffix}"},
                json={"schema_version": "1.0", "sha256": digest},
            )
        ).status_code == 202
        listed_files: list[dict[str, object]] = []
        for _ in range(100):
            await app.state.butler.scheduler_poll_once()  # type: ignore[attr-defined]
            listed_files = (await client.get("/v1/files", headers=headers)).json()["items"]
            if any(
                item["id"] == file_id and item["knowledge_status"] == "READY"
                for item in listed_files
            ):
                break
        assert (
            next(item for item in listed_files if item["id"] == file_id)["knowledge_status"]
            == "READY"
        )
        download_url = (
            await client.get(f"/v1/files/{file_id}/download-url", headers=headers)
        ).json()["url"]
        assert (await client.get(download_url.removeprefix("http://test"))).content == content

        private_question = await client.post(
            "/v1/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"private-{suffix}",
                "target_conversation_id": conversation_id,
                "context_policy": "CONTINUE_CURRENT",
                "content": "请根据附件总结",
                "attachments": [{"file_id": file_id, "position": 0}],
            },
        )
        await _finish(app, client, headers, private_question.json()["run"]["id"])
        private_messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        private_card = next(
            card
            for message in private_messages
            if message.get("agent_run_id") == private_question.json()["run"]["id"]
            for card in message["cards"]["cards"]
            if card["card_type"] == "SourceCard"
        )
        citation_id = private_card["entity_refs"]["citation_ids"][0]
        citation = (await client.get(f"/v1/citations/{citation_id}", headers=headers)).json()
        assert citation["source_type"] == "PRIVATE_FILE"
        assert citation["access"]["type"] == "SIGNED_FILE"
        assert (await client.delete(f"/v1/files/{file_id}", headers=headers)).status_code == 202

        cancellable = await client.post(
            "/v1/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"cancel-{suffix}",
                "target_conversation_id": conversation_id,
                "context_policy": "CONTINUE_CURRENT",
                "content": "你好",
                "attachments": [],
            },
        )
        cancelled = await client.post(
            f"/v1/agent-runs/{cancellable.json()['run']['id']}/cancel",
            headers={**headers, "Idempotency-Key": f"cancel-run-{suffix}"},
        )
        assert cancelled.json()["status"] == "CANCELLED"

        retryable = await client.post(
            "/v1/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"retry-{suffix}",
                "target_conversation_id": conversation_id,
                "context_policy": "CONTINUE_CURRENT",
                "content": "你好",
                "attachments": [],
            },
        )
        retry_id = retryable.json()["run"]["id"]
        async with app.state.database.transaction() as connection:
            await connection.execute(
                text("UPDATE agent_runs SET status='FAILED_RETRYABLE' WHERE id=:id"),
                {"id": UUID(retry_id)},
            )
        retried = await client.post(
            f"/v1/agent-runs/{retry_id}/retry",
            headers=headers,
            json={"schema_version": "1.0", "expected_attempt": 0},
        )
        assert retried.json()["attempt"] == 1
        await _finish(app, client, headers, retry_id)

        other_headers, _ = await _login(client, f"other-{suffix}")
        assert (await client.get(f"/v1/plans/{plan_id}", headers=other_headers)).status_code == 404
        assert (
            await client.get(f"/v1/citations/{citation_id}", headers=other_headers)
        ).status_code == 404
        deleted = await client.request(
            "DELETE",
            "/v1/me",
            headers={**other_headers, "Idempotency-Key": f"delete-account-{suffix}"},
            json={"schema_version": "1.0", "confirmation": "DELETE_MY_ACCOUNT"},
        )
        assert deleted.status_code == 202
        while await app.state.butler.scheduler_poll_once():  # type: ignore[attr-defined]
            pass

        refreshed = await client.post(
            "/v1/auth/refresh",
            json={
                "schema_version": "1.0",
                "refresh_token": auth["refresh_token"],
                "device_id": f"flow-device-{suffix}",
            },
        )
        assert refreshed.status_code == 200
        assert (
            await client.post(
                "/v1/auth/refresh",
                json={
                    "schema_version": "1.0",
                    "refresh_token": auth["refresh_token"],
                    "device_id": f"flow-device-{suffix}",
                },
            )
        ).status_code == 401
        logout = await client.post(
            "/v1/auth/logout",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
            json={
                "schema_version": "1.0",
                "refresh_token": refreshed.json()["refresh_token"],
            },
        )
        assert logout.status_code in {204, 401}
