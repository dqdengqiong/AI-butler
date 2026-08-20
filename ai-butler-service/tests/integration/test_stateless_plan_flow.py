from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text

from ai_butler.api.app import create_app
from ai_butler.application.butler.shared import _content_hash, _json
from ai_butler.config import Settings

pytestmark = pytest.mark.integration


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model_routing_enabled=False,
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


async def _login(client: httpx.AsyncClient, suffix: str) -> tuple[dict[str, str], UUID]:
    response = await client.post(
        "/v1/auth/wechat/login",
        headers={"Idempotency-Key": f"login-{suffix}"},
        json={
            "schema_version": "1.0",
            "login_code": f"login-{suffix}",
            "phone_code": f"phone-{suffix}",
            "provider": "WECHAT_MOCK",
            "device_id": f"device-{suffix}",
            "consent": {
                "terms_version": "2026-08-01",
                "privacy_version": "2026-08-01",
                "accepted_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, UUID(payload["user"]["id"])


async def _finish(
    app: object, client: httpx.AsyncClient, headers: dict[str, str], run_id: str
) -> None:
    for _ in range(100):
        run = (await client.get(f"/v1/agent-runs/{run_id}", headers=headers)).json()
        if run["status"] not in {"QUEUED", "RUNNING"}:
            assert run["status"] == "SUCCEEDED", run
            return
        await app.state.butler.worker_poll_once(uuid4())  # type: ignore[attr-defined]
    raise AssertionError("run did not finish")


async def _send(
    app: object,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    content: str,
    suffix: str,
    conversation_id: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "schema_version": "1.0",
            "client_message_id": f"message-{suffix}-{uuid4().hex}",
            "target_conversation_id": conversation_id,
            "context_policy": "CONTINUE_CURRENT" if conversation_id else "AUTO",
            "content": content,
            "attachments": [],
        },
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    await _finish(app, client, headers, payload["run"]["id"])
    messages = (
        await client.get(
            f"/v1/conversations/{payload['conversation_id']}/messages?limit=100", headers=headers
        )
    ).json()["items"]
    return payload, messages


def _preview(messages: list[dict[str, object]]) -> tuple[dict[str, object], dict[str, object]]:
    for message in reversed(messages):
        cards = message["cards"]["cards"]  # type: ignore[index]
        for card in cards:
            if card["card_type"] == "PlanPreviewCard" and card["payload"]["status"] == "READY":
                return message, card
    raise AssertionError("missing ready preview")


async def _confirm(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    message: dict[str, object],
    card: dict[str, object],
    key: str,
) -> dict[str, object]:
    response = await client.post(
        f"/v1/plan-previews/{message['id']}/confirm",
        headers={**headers, "Idempotency-Key": key},
        json={
            "schema_version": "1.0",
            "expected_preview_hash": card["payload"]["preview_hash"],  # type: ignore[index]
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _replace_preview_payload(app: object, message_id: str, payload: dict[str, object]) -> str:
    unsigned = {
        key: value for key, value in payload.items() if key not in {"status", "preview_hash"}
    }
    preview_hash = _content_hash(unsigned)
    payload["preview_hash"] = preview_hash
    async with app.state.butler.database.transaction() as connection:  # type: ignore[attr-defined]
        structured = (
            await connection.execute(
                text("SELECT structured_content FROM messages WHERE id=:id FOR UPDATE"),
                {"id": message_id},
            )
        ).scalar_one()
        cards = structured["cards"]
        preview = next(card for card in cards if card["card_type"] == "PlanPreviewCard")
        preview["payload"] = payload
        await connection.execute(
            text("UPDATE messages SET structured_content=CAST(:value AS jsonb) WHERE id=:id"),
            {"value": _json(structured), "id": message_id},
        )
    return preview_hash


@pytest.mark.asyncio
async def test_natural_language_clarification_preview_and_confirmation(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, user_id = await _login(client, suffix)
        first, messages = await _send(app, client, headers, "帮我制定省考计划", suffix)
        assert "为了生成计划预览" in messages[-1]["content"]
        assert not any(
            card["card_type"] == "ClientActionCard"
            for message in messages
            for card in message["cards"]["cards"]  # type: ignore[index]
        )
        async with app.state.butler.database.connect() as connection:  # type: ignore[attr-defined]
            count = await connection.execute(
                text("SELECT count(*) FROM plans WHERE user_id=:user"), {"user": user_id}
            )
            assert int(count.scalar_one()) == 0

        _, messages = await _send(
            app,
            client,
            headers,
            "2027年广东省考，准备12周，工作日每天1小时，周末每天2小时",
            suffix,
            str(first["conversation_id"]),
        )
        preview_message, preview_card = _preview(messages)
        daily_availability = preview_card["payload"]["daily_availability"]
        assert len(daily_availability) == 7
        assert sum(item["available_minutes"] for item in daily_availability) == 540
        assert all(item["source"] == "EXPLICIT_RULE" for item in daily_availability)
        async with app.state.butler.database.connect() as connection:  # type: ignore[attr-defined]
            for table in ("goals", "plans", "plan_revisions", "tasks", "notification_jobs"):
                result = await connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE user_id=:user"),  # noqa: S608
                    {"user": user_id},
                )
                assert int(result.scalar_one()) == 0

        confirmed = await _confirm(
            client, headers, preview_message, preview_card, f"confirm-{suffix}"
        )
        repeated = await _confirm(
            client, headers, preview_message, preview_card, f"confirm-{suffix}"
        )
        assert repeated == confirmed
        async with app.state.butler.database.connect() as connection:  # type: ignore[attr-defined]
            task_dates = (
                (
                    await connection.execute(
                        text(
                            "SELECT scheduled_date FROM tasks WHERE plan_id=:plan "
                            "ORDER BY scheduled_date"
                        ),
                        {"plan": confirmed["plan_id"]},
                    )
                )
                .scalars()
                .all()
            )
            watermark = (
                await connection.execute(
                    text(
                        "SELECT materialized_through FROM plan_schedule_watermarks "
                        "WHERE plan_id=:plan"
                    ),
                    {"plan": confirmed["plan_id"]},
                )
            ).scalar_one()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        assert (
            task_dates and min(task_dates) >= today and max(task_dates) <= today + timedelta(days=6)
        )
        assert watermark == today + timedelta(days=6)

        confirmed_with_new_key = await _confirm(
            client, headers, preview_message, preview_card, f"confirm-again-{suffix}"
        )
        assert confirmed_with_new_key == confirmed
        reused_key = await client.post(
            f"/v1/plan-previews/{preview_message['id']}/confirm",
            headers={**headers, "Idempotency-Key": f"confirm-{suffix}"},
            json={"schema_version": "1.0", "expected_preview_hash": "0" * 64},
        )
        assert reused_key.status_code == 409
        assert reused_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        _, adjusted_messages = await _send(
            app,
            client,
            headers,
            "调整我的计划：2027年广东省考，准备10周，每天可学习2小时",
            f"{suffix}-adjust",
            str(first["conversation_id"]),
        )
        adjusted_message, adjusted_card = _preview(adjusted_messages)
        assert adjusted_card["payload"]["operation"] == "ADJUST"  # type: ignore[index]
        adjusted = await _confirm(
            client,
            headers,
            adjusted_message,
            adjusted_card,
            f"confirm-adjust-{suffix}",
        )
        assert adjusted["plan_id"] == confirmed["plan_id"]
        assert adjusted["revision_id"] != confirmed["revision_id"]


@pytest.mark.asyncio
async def test_create_allows_similar_plans_and_delete_is_hidden_and_idempotent(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, _ = await _login(client, suffix)
        plan_ids: list[str] = []
        for index in range(2):
            _, messages = await _send(
                app,
                client,
                headers,
                "帮我制定2027年广东省考计划，准备8周，工作日每天1小时，周末每天2小时",
                f"{suffix}-{index}",
            )
            message, card = _preview(messages)
            confirmed = await _confirm(client, headers, message, card, f"confirm-{suffix}-{index}")
            plan_ids.append(str(confirmed["plan_id"]))
        assert len((await client.get("/v1/plans", headers=headers)).json()["items"]) == 2

        other_headers, _ = await _login(client, f"{suffix}-other")
        cross_user = await client.delete(
            f"/v1/plans/{plan_ids[0]}",
            headers={**other_headers, "Idempotency-Key": f"cross-delete-{suffix}"},
        )
        assert cross_user.status_code == 404

        delete_headers = {**headers, "Idempotency-Key": f"delete-{suffix}"}
        endpoint = f"/v1/plans/{plan_ids[0]}"
        assert (await client.delete(endpoint, headers=delete_headers)).status_code == 204
        assert (await client.delete(endpoint, headers=delete_headers)).status_code == 204
        assert (
            await client.delete(
                endpoint,
                headers={**headers, "Idempotency-Key": f"delete-again-{suffix}"},
            )
        ).status_code == 204
        assert (await client.get(endpoint, headers=headers)).status_code == 404
        assert len((await client.get("/v1/plans", headers=headers)).json()["items"]) == 1
        reused = await client.delete(f"/v1/plans/{plan_ids[1]}", headers=delete_headers)
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        tasks = (await client.get("/v1/tasks", headers=headers)).json()["items"]
        assert all(str(item["plan_id"]) != plan_ids[0] for item in tasks)


@pytest.mark.asyncio
async def test_scheduler_rolls_only_the_window_end_and_is_retry_idempotent(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, _ = await _login(client, suffix)
        _, messages = await _send(
            app,
            client,
            headers,
            "帮我制定2027年广东省考计划，准备12周，每天可学习2小时",
            suffix,
        )
        message, card = _preview(messages)
        confirmed = await _confirm(client, headers, message, card, f"confirm-{suffix}")
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        endpoint_date = today + timedelta(days=6)
        async with app.state.butler.database.transaction() as connection:  # type: ignore[attr-defined]
            await connection.execute(
                text("DELETE FROM tasks WHERE plan_id=:plan AND scheduled_date=:date"),
                {"plan": confirmed["plan_id"], "date": endpoint_date},
            )
            await connection.execute(
                text(
                    "UPDATE plan_schedule_watermarks SET materialized_through=:through "
                    "WHERE plan_id=:plan"
                ),
                {"through": endpoint_date - timedelta(days=1), "plan": confirmed["plan_id"]},
            )
        assert await app.state.butler.scheduler_poll_once()  # type: ignore[attr-defined]
        assert not await app.state.butler._scheduler._materialize_one_plan_window()  # type: ignore[attr-defined]
        async with app.state.butler.database.connect() as connection:  # type: ignore[attr-defined]
            watermark = (
                await connection.execute(
                    text(
                        "SELECT materialized_through FROM plan_schedule_watermarks "
                        "WHERE plan_id=:plan"
                    ),
                    {"plan": confirmed["plan_id"]},
                )
            ).scalar_one()
            duplicate_keys = int(
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM (SELECT task_key FROM tasks WHERE plan_id=:plan "
                            "GROUP BY task_key HAVING count(*)>1) duplicates"
                        ),
                        {"plan": confirmed["plan_id"]},
                    )
                ).scalar_one()
            )
        assert watermark == endpoint_date
        assert duplicate_keys == 0


@pytest.mark.asyncio
async def test_preview_confirmation_rejects_unowned_tampered_and_invalid_snapshots(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        suffix = uuid4().hex
        headers, _ = await _login(client, suffix)
        other_headers, _ = await _login(client, f"{suffix}-other")
        _, messages = await _send(
            app,
            client,
            headers,
            "帮我制定2027年广东省考计划，准备8周，每天可学习2小时",
            suffix,
        )
        message, card = _preview(messages)
        message_id = str(message["id"])
        original = deepcopy(card["payload"])
        endpoint = f"/v1/plan-previews/{message_id}/confirm"

        unowned = await client.post(
            endpoint,
            headers={**other_headers, "Idempotency-Key": f"unowned-{suffix}"},
            json={"schema_version": "1.0", "expected_preview_hash": original["preview_hash"]},
        )
        assert unowned.status_code == 404

        wrong_hash = await client.post(
            endpoint,
            headers={**headers, "Idempotency-Key": f"wrong-hash-{suffix}"},
            json={"schema_version": "1.0", "expected_preview_hash": "0" * 64},
        )
        assert wrong_hash.status_code == 409

        cases: list[tuple[str, dict[str, object], int, str]] = []
        superseded = deepcopy(original)
        superseded["status"] = "SUPERSEDED"
        cases.append(("superseded", superseded, 409, "PLAN_PREVIEW_NOT_CONFIRMABLE"))
        expired = deepcopy(original)
        expired["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        cases.append(("expired", expired, 409, "PLAN_PREVIEW_EXPIRED"))
        missing_plan = deepcopy(original)
        missing_plan["plan"] = None
        cases.append(("missing-plan", missing_plan, 422, "PLAN_PREVIEW_INVALID"))
        missing_stages = deepcopy(original)
        missing_stages["plan"]["stages"] = []  # type: ignore[index]
        cases.append(("missing-stages", missing_stages, 422, "PLAN_PREVIEW_INVALID"))
        stale_date = deepcopy(original)
        stale_date["plan"]["start_date"] = "2020-01-01"  # type: ignore[index]
        cases.append(("stale-date", stale_date, 409, "PLAN_PREVIEW_DATE_CONFLICT"))
        excessive_load = deepcopy(original)
        excessive_load["plan"]["weekly_minutes"] = 9999  # type: ignore[index]
        cases.append(("excessive-load", excessive_load, 422, "PLAN_PREVIEW_LOAD_INVALID"))
        excessive_tasks = deepcopy(original)
        excessive_tasks["plan"]["tasks"][0]["expected_minutes"] = 9999  # type: ignore[index]
        cases.append(("excessive-tasks", excessive_tasks, 422, "PLAN_PREVIEW_LOAD_INVALID"))

        for label, payload, status, code in cases:
            preview_hash = await _replace_preview_payload(app, message_id, payload)
            response = await client.post(
                endpoint,
                headers={**headers, "Idempotency-Key": f"{label}-{suffix}"},
                json={"schema_version": "1.0", "expected_preview_hash": preview_hash},
            )
            assert response.status_code == status, response.text
            assert response.json()["error"]["code"] == code
