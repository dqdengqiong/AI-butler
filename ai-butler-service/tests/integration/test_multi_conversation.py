from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from ai_butler.api.app import create_app
from ai_butler.application.butler import _encode_cursor
from ai_butler.config import Settings
from ai_butler.domain.errors import ButlerError

pytestmark = pytest.mark.integration


def _login_payload(code: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "login_code": code,
        "phone_code": f"phone-code-{code}",
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


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model_routing_enabled=False,
        app_database_url=(
            "postgresql+psycopg://butler_test:butler_test@127.0.0.1:5432/butler_test"
        ),
        migration_database_url=(
            "postgresql+psycopg://butler_migrator:butler_migrator@127.0.0.1:5432/butler_test"
        ),
        object_storage_local_path=tmp_path,
        public_base_url="http://test",
    )


def _message(client_message_id: str, content: str, **extra: object) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "client_message_id": client_message_id,
        "content": content,
        "attachments": [],
        "selection": None,
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
async def test_unified_message_entry_routes_topics_and_is_user_idempotent(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, _ = await _login(client, f"route-{suffix}")

        catalog = (await client.get("/v1/agent-definitions")).json()["items"]
        assert [(item["code"], item["availability"]) for item in catalog] == [
            ("CIVIL_SERVICE_EXAM", "AVAILABLE"),
            ("IELTS", "COMING_SOON"),
            ("JOB_SEARCH", "COMING_SOON"),
        ]
        # 登录产生的内部空工作区不应出现在历史，手动创建能力也不再公开。
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
                f"/v1/conversations/{first_conversation}/messages?limit=1",
                headers=headers,
            )
        ).json()
        assert message_page["has_more"] is True
        assert message_page["next_cursor"]
        older_message_page = (
            await client.get(
                f"/v1/conversations/{first_conversation}/messages",
                headers=headers,
                params={"limit": 1, "cursor": message_page["next_cursor"]},
            )
        ).json()
        assert len(older_message_page["items"]) == 1
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
        assert changed_payload["conversation_id"] != first_conversation

        repeated = await client.post("/v1/messages", headers=headers, json=changed_request)
        assert repeated.status_code == 202
        assert repeated.json()["run"]["id"] == changed_payload["run"]["id"]
        assert repeated.json()["conversation_id"] == changed_payload["conversation_id"]
        reused = await client.post(
            "/v1/messages",
            headers=headers,
            json={**changed_request, "content": "相同幂等键的其他内容"},
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
        assert all(item["last_message"] is not None for item in timeline)
        first_page = (await client.get("/v1/conversations?limit=1", headers=headers)).json()
        assert first_page["has_more"] is True
        second_page = (
            await client.get(
                "/v1/conversations",
                headers=headers,
                params={"limit": 1, "cursor": first_page["next_cursor"]},
            )
        ).json()
        assert second_page["items"][0]["id"] == first_conversation
        invalid_cursor = await client.get(
            "/v1/conversations", headers=headers, params={"cursor": "not-a-cursor"}
        )
        assert invalid_cursor.status_code == 400
        invalid_typed_cursor = await client.get(
            "/v1/conversations",
            headers=headers,
            params={"cursor": _encode_cursor("x", "not-a-time", "not-a-uuid")},
        )
        assert invalid_typed_cursor.status_code == 400
        assert (
            await client.delete(
                f"/v1/conversations/{uuid4()}",
                headers={**headers, "Idempotency-Key": f"delete-missing-{suffix}"},
            )
        ).status_code == 404

        current_delete = await client.delete(
            f"/v1/conversations/{changed_payload['conversation_id']}",
            headers={**headers, "Idempotency-Key": f"delete-current-{suffix}"},
        )
        assert current_delete.status_code == 409
        delete_headers = {**headers, "Idempotency-Key": f"delete-history-{suffix}"}
        assert (
            await client.delete(f"/v1/conversations/{first_conversation}", headers=delete_headers)
        ).status_code == 204
        assert (
            await client.delete(f"/v1/conversations/{first_conversation}", headers=delete_headers)
        ).status_code == 204
        assert (
            await client.get(f"/v1/conversations/{first_conversation}", headers=headers)
        ).status_code == 404


@pytest.mark.asyncio
async def test_specialist_welcome_is_ephemeral_and_suspended_work_is_resumed(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, user_id = await _login(client, f"specialist-{suffix}")

        specialist = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"specialist-{suffix}",
                "制定四周备考计划",
                specialist_code="CIVIL_SERVICE_EXAM",
            ),
        )
        assert specialist.status_code == 202, specialist.text
        specialist_payload = specialist.json()
        specialist_id = specialist_payload["conversation_id"]
        assert specialist_payload["transition"]["kind"] == "CREATED"
        timeline = (
            await client.get(f"/v1/conversations/{specialist_id}/messages", headers=headers)
        ).json()["items"]
        assert [item["role"] for item in timeline] == ["USER", "ASSISTANT"]
        assert not any("欢迎" in item["content"] for item in timeline)
        unavailable = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"unavailable-{suffix}",
                "开始雅思计划",
                specialist_code="IELTS",
            ),
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["error"]["code"] == "AGENT_NOT_AVAILABLE"
        same_specialist = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"same-specialist-{suffix}",
                "继续",
                specialist_code="CIVIL_SERVICE_EXAM",
            ),
        )
        assert same_specialist.status_code == 409
        assert same_specialist.json()["error"]["code"] == "CONVERSATION_BUSY"

        # 模拟一个持久化的待回复流程；它可以在另一个会话创建后继续挂起。
        async with app.state.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='AWAITING_INPUT' WHERE id=:run_id "
                    "AND user_id=:user_id"
                ),
                {"run_id": UUID(specialist_payload["run"]["id"]), "user_id": user_id},
            )
            await connection.execute(
                text("UPDATE messages SET status='COMPLETED' WHERE id=:message_id"),
                {"message_id": UUID(specialist_payload["assistant_message"]["id"])},
            )

        generic = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"generic-{suffix}",
                "开始处理另一个普通事项",
                context_policy="ARCHIVE_AND_START",
            ),
        )
        assert generic.status_code == 202, generic.text
        await _cancel(client, headers, generic.json()["run"]["id"], suffix)

        resumed = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"resume-{suffix}",
                "继续刚才的备考安排",
                specialist_code="CIVIL_SERVICE_EXAM",
            ),
        )
        assert resumed.status_code == 202, resumed.text
        assert resumed.json()["conversation_id"] == specialist_id
        assert resumed.json()["transition"]["kind"] == "RESUMED"
        assert resumed.json()["run"]["execution_mode"] == "INPUT_RESUME"


@pytest.mark.asyncio
async def test_running_topic_switch_requires_confirmation_and_cancels_atomically(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, _ = await _login(client, f"running-{suffix}")
        first = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(f"running-first-{suffix}", "制定公务员备考计划"),
        )
        assert first.status_code == 202
        first_payload = first.json()

        switch_request = _message(f"running-switch-{suffix}", "帮我修改求职简历")
        blocked = await client.post("/v1/messages", headers=headers, json=switch_request)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "TOPIC_SWITCH_CONFIRMATION_REQUIRED"

        confirmed = await client.post(
            "/v1/messages",
            headers=headers,
            json={
                **switch_request,
                "context_policy": "ARCHIVE_AND_START",
                "execution_policy": "CANCEL_OTHER",
            },
        )
        assert confirmed.status_code == 202, confirmed.text
        assert confirmed.json()["transition"]["kind"] == "CREATED"
        assert confirmed.json()["conversation_id"] != first_payload["conversation_id"]
        assert (
            await client.get(f"/v1/agent-runs/{first_payload['run']['id']}", headers=headers)
        ).json()["status"] == "CANCELLED"

        # 被取消 Worker 的迟到失败不能覆盖终态。
        await app.state.butler._fail_run(
            UUID(first_payload["run"]["id"]),
            ButlerError("LATE_WORKER_FAILURE", "旧任务失败", 500, True),
        )
        assert (
            await client.get(f"/v1/agent-runs/{first_payload['run']['id']}", headers=headers)
        ).json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_history_send_resumes_owned_conversation_and_rejects_cross_user(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, _ = await _login(client, f"history-{suffix}")
        first = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(f"history-first-{suffix}", "公务员备考"),
        )
        first_payload = first.json()
        await _cancel(client, headers, first_payload["run"]["id"], suffix)
        second = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"history-second-{suffix}",
                "明确开始新的生活话题",
                context_policy="ARCHIVE_AND_START",
            ),
        )
        blocked = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"history-blocked-{suffix}",
                "继续旧话题",
                target_conversation_id=first_payload["conversation_id"],
                context_policy="CONTINUE_CURRENT",
            ),
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "OTHER_CONVERSATION_RUNNING"
        await _cancel(client, headers, second.json()["run"]["id"], suffix)

        resumed = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"history-resume-{suffix}",
                "继续这个计划",
                target_conversation_id=first_payload["conversation_id"],
                context_policy="CONTINUE_CURRENT",
            ),
        )
        assert resumed.status_code == 202, resumed.text
        assert resumed.json()["conversation_id"] == first_payload["conversation_id"]
        assert resumed.json()["transition"]["kind"] == "RESUMED"

        other_headers, _ = await _login(client, f"history-other-{suffix}")
        forbidden = await client.post(
            "/v1/messages",
            headers=other_headers,
            json=_message(
                f"history-forbidden-{suffix}",
                "越权续聊",
                target_conversation_id=first_payload["conversation_id"],
                context_policy="CONTINUE_CURRENT",
            ),
        )
        assert forbidden.status_code == 404


@pytest.mark.asyncio
async def test_deterministic_continue_current_specialist_and_missing_current_recovery(
    tmp_path: Path,
) -> None:
    """覆盖无需模型切分的延续规则和旧数据缺少 CURRENT 时的安全恢复。"""

    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, user_id = await _login(client, f"continue-{suffix}")
        first = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(f"continue-first-{suffix}", "制定公务员备考计划"),
        )
        assert first.status_code == 202
        current_id = first.json()["conversation_id"]
        await _cancel(client, headers, first.json()["run"]["id"], suffix)

        follow_up = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(f"continue-follow-{suffix}", "继续刚才这个计划"),
        )
        assert follow_up.status_code == 202
        assert follow_up.json()["conversation_id"] == current_id
        assert follow_up.json()["transition"]["kind"] == "CONTINUED"
        await _cancel(client, headers, follow_up.json()["run"]["id"], suffix)

        targeted_current = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"continue-target-{suffix}",
                "继续当前",
                target_conversation_id=current_id,
                context_policy="CONTINUE_CURRENT",
            ),
        )
        assert targeted_current.status_code == 202
        assert targeted_current.json()["transition"]["kind"] == "CONTINUED"
        await _cancel(client, headers, targeted_current.json()["run"]["id"], suffix)

        specialist = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"continue-specialist-{suffix}",
                "进入考公助理",
                specialist_code="CIVIL_SERVICE_EXAM",
            ),
        )
        assert specialist.status_code == 202
        specialist_id = specialist.json()["conversation_id"]
        await _cancel(client, headers, specialist.json()["run"]["id"], suffix)
        same_specialist = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(
                f"continue-same-specialist-{suffix}",
                "继续专业计划",
                specialist_code="CIVIL_SERVICE_EXAM",
            ),
        )
        assert same_specialist.status_code == 202
        assert same_specialist.json()["conversation_id"] == specialist_id
        assert same_specialist.json()["transition"]["kind"] == "CONTINUED"
        await _cancel(client, headers, same_specialist.json()["run"]["id"], suffix)

        async with app.state.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE conversations SET status='ARCHIVED',archived_at=now(),"
                    "archive_reason='WORKFLOW_EXIT' WHERE user_id=:user_id AND status='CURRENT'"
                ),
                {"user_id": user_id},
            )
        recovered = await client.post(
            "/v1/messages",
            headers=headers,
            json=_message(f"continue-recover-{suffix}", "恢复一个普通话题"),
        )
        assert recovered.status_code == 202, recovered.text
        assert recovered.json()["transition"]["kind"] == "CREATED"
        assert recovered.json()["conversation_id"] != specialist_id
