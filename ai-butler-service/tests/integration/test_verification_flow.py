from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from ai_butler.api.app import create_app
from ai_butler.config import Settings

pytestmark = pytest.mark.integration


def _login_payload(code: str, device: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "login_code": code,
        "provider": "WECHAT_MOCK",
        "device_id": device,
        "consent": {
            "terms_version": "2026-08-01",
            "privacy_version": "2026-08-01",
            "accepted_at": datetime.now(UTC).isoformat(),
        },
    }


@pytest.mark.asyncio
async def test_complete_mock_login_plan_approval_task_file_and_governance_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        app_database_url="postgresql+psycopg://butler_test:butler_test@127.0.0.1:5432/butler_test",
        migration_database_url="postgresql+psycopg://butler_migrator:butler_migrator@127.0.0.1:5432/butler_test",
        object_storage_local_path=tmp_path,
        public_base_url="http://test",
        sse_poll_interval_ms=10,
        sse_heartbeat_seconds=1,
        context_window_tokens=50,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        suffix = uuid4().hex
        login = await client.post(
            "/v1/auth/wechat/login",
            headers={"Idempotency-Key": f"login-{suffix}"},
            json=_login_payload(f"flow-{suffix}", f"device-{suffix}"),
        )
        assert login.status_code == 200, login.text
        auth = login.json()
        headers = {"Authorization": f"Bearer {auth['access_token']}"}
        conversation_id = (await client.get("/v1/conversations", headers=headers)).json()["items"][
            0
        ]["id"]

        assert (await client.get("/v1/me")).status_code == 401
        invalid_body = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"schema_version": "1.0"},
        )
        assert invalid_body.status_code == 422
        assert (await client.get("/v1/me", headers=headers)).status_code == 200
        assert (await client.patch("/v1/me", headers=headers, json={})).status_code == 200
        avatar_rejected = await client.patch(
            "/v1/me", headers=headers, json={"avatar_file_id": str(uuid4())}
        )
        assert avatar_rejected.status_code == 409
        too_wide = await client.get(
            "/v1/tasks",
            headers=headers,
            params={
                "date_from": date.today().isoformat(),
                "date_to": (date.today() + timedelta(days=100)).isoformat(),
            },
        )
        assert too_wide.status_code == 400
        renamed = await client.patch(
            "/v1/me", headers=headers, json={"nickname": "验证用户", "timezone": "Asia/Shanghai"}
        )
        assert renamed.json()["nickname"] == "验证用户"
        profile = await client.get("/v1/me/profile", headers=headers)
        profile_version = profile.json()["profile_version"]
        assert (
            await client.put(
                "/v1/me/profile",
                headers=headers,
                json={
                    "schema_version": "1.0",
                    "expected_version": profile_version,
                    "education_level": "本科",
                    "major": "计算机",
                    "region_code": "CN-44",
                    "current_level": "BEGINNER",
                    "existing_material_file_ids": [],
                },
            )
        ).status_code == 200
        availability = await client.get("/v1/me/availability", headers=headers)
        assert (
            await client.put(
                "/v1/me/availability",
                headers=headers,
                json={
                    "schema_version": "1.0",
                    "expected_version": availability.json()["version"],
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
                },
            )
        ).status_code == 200
        preferences = (await client.get("/v1/me/preferences", headers=headers)).json()
        changed_preferences = await client.patch(
            "/v1/me/preferences",
            headers=headers,
            json={
                "expected_version": preferences["version"],
                "task_reminder": {
                    "enabled": True,
                    "channels": ["IN_APP"],
                    "advance_minutes": 10,
                },
            },
        )
        assert changed_preferences.status_code == 200

        assert (
            await client.get(f"/v1/conversations/{conversation_id}", headers=headers)
        ).status_code == 200
        first = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"initial-{suffix}",
                "content": "我要准备公务员省考，请帮我制定计划",
                "attachments": [],
                "selection": None,
            },
        )
        assert first.status_code == 202, first.text
        run_id = first.json()["run"]["id"]
        waiting = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        for _ in range(10):
            if waiting["status"] != "QUEUED":
                break
            assert await app.state.butler.worker_poll_once(uuid4()) is True
            waiting = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        assert waiting["status"] == "AWAITING_INPUT"
        messages = (
            await client.get(f"/v1/conversations/{conversation_id}/messages", headers=headers)
        ).json()["items"]
        selection_card = next(
            card
            for message in messages
            for card in message.get("cards", {}).get("cards", [])
            if card["card_type"] == "SelectionCard"
        )

        invalid_selection = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"invalid-{suffix}",
                "content": "",
                "attachments": [],
                "selection": {
                    "card_id": selection_card["card_id"],
                    "action_id": "submit-selection",
                    "selected_option_ids": ["not-an-option"],
                },
            },
        )
        assert invalid_selection.status_code == 400

        resumed = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"resume-{suffix}",
                "content": "",
                "attachments": [],
                "selection": {
                    "card_id": selection_card["card_id"],
                    "action_id": "submit-selection",
                    "selected_option_ids": ["weekday-daily-60"],
                },
            },
        )
        assert resumed.status_code == 202, resumed.text
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        confirmation_run = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        assert confirmation_run["status"] == "AWAITING_INPUT"
        confirmation_messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        confirmation_card = next(
            card
            for message in reversed(confirmation_messages)
            for card in message.get("cards", {}).get("cards", [])
            if card["card_type"] == "SelectionCard"
            and card["payload"].get("phase") == "CONFIRM_AVAILABILITY"
        )
        assert confirmation_card["payload"]["interpretation"]["weekly_minutes"] == 300
        revise = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"revise-availability-{suffix}",
                "content": "",
                "attachments": [],
                "selection": {
                    "card_id": confirmation_card["card_id"],
                    "action_id": "submit-selection",
                    "selected_option_ids": ["revise-availability"],
                },
            },
        )
        assert revise.status_code == 202, revise.text
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        natural_language = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"natural-availability-{suffix}",
                "content": "每天 1 个小时，周末不学习",
                "attachments": [],
                "selection": None,
            },
        )
        assert natural_language.status_code == 202, natural_language.text
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        natural_messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        natural_confirmation = next(
            card
            for message in reversed(natural_messages)
            for card in message.get("cards", {}).get("cards", [])
            if card.get("payload", {}).get("phase") == "CONFIRM_AVAILABILITY"
            and not card["payload"].get("submitted")
        )
        assert natural_confirmation["payload"]["interpretation"]["weekly_minutes"] == 300
        confirmed = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"confirm-availability-{suffix}",
                "content": "",
                "attachments": [],
                "selection": {
                    "card_id": natural_confirmation["card_id"],
                    "action_id": "submit-selection",
                    "selected_option_ids": ["confirm-availability"],
                },
            },
        )
        assert confirmed.status_code == 202, confirmed.text
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        draft_run = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        assert draft_run["status"] == "AWAITING_APPROVAL"
        approval_id = draft_run["next_action"]["approval_id"]
        messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        plan_card = next(
            card
            for message in messages
            for card in message.get("cards", {}).get("cards", [])
            if card["card_type"] == "PlanCard"
        )
        assert plan_card["payload"]["weekly_minutes"] == 300
        assert "claim_ids" not in plan_card["payload"]
        source_card = next(
            card
            for message in messages
            for card in message.get("cards", {}).get("cards", [])
            if card["card_type"] == "SourceCard"
        )
        citation_id = source_card["entity_refs"]["citation_ids"][0]
        citation = await client.get(f"/v1/citations/{citation_id}", headers=headers)
        assert citation.status_code == 200
        assert citation.json()["source_type"] == "KNOWLEDGE"
        assert citation.json()["access"]["type"] == "UNAVAILABLE"
        assert (await client.get(f"/v1/claims/{uuid4()}", headers=headers)).status_code == 404

        decision = await client.post(
            f"/v1/approvals/{approval_id}/decisions",
            headers=headers,
            json={
                "schema_version": "1.0",
                "approval_id": approval_id,
                "expected_approval_version": 1,
                "action": "APPROVE",
                "feedback": None,
            },
        )
        assert decision.status_code == 200, decision.text
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        completed_run = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        assert completed_run["status"] == "SUCCEEDED"
        ticket = (
            await client.post(f"/v1/agent-runs/{run_id}/stream-ticket", headers=headers)
        ).json()
        events = await app.state.butler.list_events(UUID(auth["user"]["id"]), UUID(run_id), 0)
        assert ticket["last_sequence"] == events[-1]["sequence"]
        from ai_butler.api.routers import v1

        monkeypatch.setattr(v1.asyncio, "sleep", AsyncMock())
        streamed = await client.get(
            f"/v1/agent-runs/{run_id}/events",
            params={"ticket": ticket["ticket"], "after": 0},
        )
        assert "event: run.completed" in streamed.text
        invalid_stream = await client.get(
            f"/v1/agent-runs/{run_id}/events", params={"ticket": "invalid"}
        )
        assert invalid_stream.status_code == 401

        goals = (await client.get("/v1/goals", headers=headers)).json()["items"]
        plans = (await client.get("/v1/plans", headers=headers)).json()["items"]
        assert goals and plans
        plan_id = plans[0]["id"]
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
        assert all(date.fromisoformat(task["scheduled_date"]).isoweekday() <= 5 for task in tasks)
        task_id = tasks[0]["id"]
        assert (await client.get(f"/v1/tasks/{task_id}", headers=headers)).status_code == 200
        execution_payload = {
            "schema_version": "1.0",
            "client_execution_id": f"execution-{suffix}",
            "result": "COMPLETED",
            "duration_minutes": 32,
            "feedback": "已完成",
            "outcome_data": {"score": 80},
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        executed = await client.post(
            f"/v1/tasks/{task_id}/executions", headers=headers, json=execution_payload
        )
        assert executed.status_code == 201
        duplicate_execution = await client.post(
            f"/v1/tasks/{task_id}/executions", headers=headers, json=execution_payload
        )
        assert duplicate_execution.json()["execution"]["id"] == executed.json()["execution"]["id"]
        assert (await client.get("/v1/dashboard", headers=headers)).status_code == 200

        web_question = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"web-{suffix}",
                "content": "请联网查询最新公务员考试资料",
                "attachments": [],
                "selection": None,
            },
        )
        web_run_id = web_question.json()["run"]["id"]
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        assert (await client.get(f"/v1/agent-runs/{web_run_id}", headers=headers)).json()[
            "status"
        ] == "SUCCEEDED"
        web_messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        web_source_card = next(
            card
            for message in web_messages
            if message.get("agent_run_id") == web_run_id
            for card in message.get("cards", {}).get("cards", [])
            if card["card_type"] == "SourceCard"
        )
        web_citation_id = web_source_card["entity_refs"]["citation_ids"][0]
        web_answer = next(
            message["content"]
            for message in web_messages
            if message.get("agent_run_id") == web_run_id and message["role"] == "ASSISTANT"
        )
        assert "[1]" in web_answer

        adjustment = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"adjust-{suffix}",
                "content": "请调整公务员计划，每周 3 小时",
                "attachments": [],
                "selection": None,
            },
        )
        adjustment_run = adjustment.json()["run"]["id"]
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        adjustment_messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        adjustment_confirmation = next(
            card
            for message in adjustment_messages
            if message.get("agent_run_id") == adjustment_run
            for card in message.get("cards", {}).get("cards", [])
            if card.get("payload", {}).get("phase") == "CONFIRM_AVAILABILITY"
        )
        assert adjustment_confirmation["payload"]["interpretation"]["weekly_minutes"] == 180
        confirmed_adjustment = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"confirm-adjustment-{suffix}",
                "content": "",
                "attachments": [],
                "selection": {
                    "card_id": adjustment_confirmation["card_id"],
                    "action_id": "submit-selection",
                    "selected_option_ids": ["confirm-availability"],
                },
            },
        )
        assert confirmed_adjustment.status_code == 202
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        adjustment_state = (
            await client.get(f"/v1/agent-runs/{adjustment_run}", headers=headers)
        ).json()
        adjustment_approval = adjustment_state["next_action"]["approval_id"]
        edited = await client.post(
            f"/v1/approvals/{adjustment_approval}/decisions",
            headers=headers,
            json={
                "schema_version": "1.0",
                "approval_id": adjustment_approval,
                "expected_approval_version": 1,
                "action": "EDIT",
                "feedback": "工作日负荷再降低一些",
            },
        )
        assert edited.json()["status"] == "EDITED"
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        regenerated = (await client.get(f"/v1/agent-runs/{adjustment_run}", headers=headers)).json()
        assert regenerated["next_action"]["approval_version"] == 2
        refreshed_messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        regenerated_card = next(
            card
            for message in refreshed_messages
            for card in message.get("cards", {}).get("cards", [])
            if card.get("entity_refs", {}).get("approval_id") == adjustment_approval
        )
        assert regenerated_card["entity_refs"]["approval_version"] == 2
        assert regenerated_card["entity_refs"]["approval_status"] == "PENDING"
        assert regenerated_card["payload"]["weekly_minutes"] == 150
        stale = await client.post(
            f"/v1/approvals/{adjustment_approval}/decisions",
            headers=headers,
            json={
                "schema_version": "1.0",
                "approval_id": adjustment_approval,
                "expected_approval_version": 1,
                "action": "APPROVE",
                "feedback": None,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["details"]["current_approval_version"] == 2
        rejected = await client.post(
            f"/v1/approvals/{adjustment_approval}/decisions",
            headers=headers,
            json={
                "schema_version": "1.0",
                "approval_id": adjustment_approval,
                "expected_approval_version": 2,
                "action": "REJECT",
                "feedback": None,
            },
        )
        assert rejected.json()["status"] == "REJECTED"
        assert await app.state.butler.worker_poll_once(uuid4()) is True

        content = b"private verification material"
        digest = hashlib.sha256(content).hexdigest()
        intent = await client.post(
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
        assert intent.status_code == 201
        file_id = intent.json()["file"]["id"]
        upload_url = intent.json()["upload"]["url"].removeprefix("http://test")
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
            await app.state.butler.scheduler_poll_once()
            listed_files = (await client.get("/v1/files", headers=headers)).json()["items"]
            if any(
                item["id"] == file_id and item["knowledge_status"] == "READY"
                for item in listed_files
            ):
                break
        uploaded_file = next(item for item in listed_files if item["id"] == file_id)
        assert uploaded_file["knowledge_status"] == "READY"
        assert (await client.get(f"/v1/files/{file_id}", headers=headers)).status_code == 200
        download = (
            (await client.get(f"/v1/files/{file_id}/download-url", headers=headers))
            .json()["url"]
            .removeprefix("http://test")
        )
        assert (await client.get(f"/v1/files/{file_id}/content?ticket=invalid")).status_code == 401
        assert (await client.get(download)).content == content

        private_question = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"private-{suffix}",
                "content": "请根据附件总结",
                "attachments": [{"file_id": file_id, "position": 0}],
                "selection": None,
            },
        )
        private_run_id = private_question.json()["run"]["id"]
        assert await app.state.butler.worker_poll_once(uuid4()) is True
        private_messages = (
            await client.get(
                f"/v1/conversations/{conversation_id}/messages?limit=100", headers=headers
            )
        ).json()["items"]
        private_source_card = next(
            card
            for message in private_messages
            if message.get("agent_run_id") == private_run_id
            for card in message.get("cards", {}).get("cards", [])
            if card["card_type"] == "SourceCard"
        )
        private_citation_id = private_source_card["entity_refs"]["citation_ids"][0]
        private_citation = (
            await client.get(f"/v1/citations/{private_citation_id}", headers=headers)
        ).json()
        assert private_citation["source_type"] == "PRIVATE_FILE"
        assert private_citation["access"]["type"] == "SIGNED_FILE"
        assert (await client.delete(f"/v1/files/{file_id}", headers=headers)).status_code == 202

        cancel_run = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"cancel-{suffix}",
                "content": "你好",
                "attachments": [],
                "selection": None,
            },
        )
        cancel_id = cancel_run.json()["run"]["id"]
        cancelled = await client.post(
            f"/v1/agent-runs/{cancel_id}/cancel",
            headers={**headers, "Idempotency-Key": f"cancel-key-{suffix}"},
        )
        assert cancelled.json()["status"] == "CANCELLED"

        retry_run = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={
                "schema_version": "1.0",
                "client_message_id": f"retry-{suffix}",
                "content": "你好",
                "attachments": [],
                "selection": None,
            },
        )
        retry_id = retry_run.json()["run"]["id"]
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
        await app.state.butler.worker_poll_once(uuid4())

        other = await client.post(
            "/v1/auth/wechat/login",
            headers={"Idempotency-Key": f"other-{suffix}"},
            json=_login_payload(f"other-{suffix}", f"other-device-{suffix}"),
        )
        other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
        assert (await client.get(f"/v1/plans/{plan_id}", headers=other_headers)).status_code == 404
        assert (
            await client.get(f"/v1/citations/{web_citation_id}", headers=other_headers)
        ).status_code == 404
        assert (
            await client.get(f"/v1/citations/{private_citation_id}", headers=other_headers)
        ).status_code == 404
        deleted = await client.request(
            "DELETE",
            "/v1/me",
            headers={**other_headers, "Idempotency-Key": f"delete-{suffix}"},
            json={"schema_version": "1.0", "confirmation": "DELETE_MY_ACCOUNT"},
        )
        assert deleted.status_code == 202
        while await app.state.butler.scheduler_poll_once():
            pass

        refreshed = await client.post(
            "/v1/auth/refresh",
            json={
                "schema_version": "1.0",
                "refresh_token": auth["refresh_token"],
                "device_id": f"device-{suffix}",
            },
        )
        assert refreshed.status_code == 200
        reused = await client.post(
            "/v1/auth/refresh",
            json={
                "schema_version": "1.0",
                "refresh_token": auth["refresh_token"],
                "device_id": f"device-{suffix}",
            },
        )
        assert reused.status_code == 401

        logout_headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
        logout = await client.post(
            "/v1/auth/logout",
            headers=logout_headers,
            json={"schema_version": "1.0", "refresh_token": refreshed.json()["refresh_token"]},
        )
        assert logout.status_code in {204, 401}

    await app.state.database.close()
