from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from ai_butler.api.app import create_app
from ai_butler.config import Settings

pytestmark = pytest.mark.integration


def _phone() -> str:
    return f"139{uuid4().int % 100_000_000:08d}"


def _login_payload(phone: str, device_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "phone": phone,
        "device_id": device_id,
        "consent": {
            "terms_version": "2026-08-01",
            "privacy_version": "2026-08-01",
            "accepted_at": datetime.now(UTC).isoformat(),
        },
    }


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_database_url": (
            "postgresql+psycopg://butler_test:butler_test@127.0.0.1:5432/butler_test"
        ),
        "migration_database_url": (
            "postgresql+psycopg://butler_migrator:butler_migrator@127.0.0.1:5432/butler_test"
        ),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def test_phone_login_without_verification_is_idempotent_account_identity() -> None:
    app = create_app(_settings(sms_verification_enabled=False))
    phone = _phone()
    device = f"device-{uuid4()}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        config = await client.get("/v1/auth/config")
        assert config.json()["sms_verification_enabled"] is False
        first = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json=_login_payload(phone, device),
        )
        second = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json=_login_payload(f"+86{phone}", device),
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["user"]["id"] == second.json()["user"]["id"]

        disabled_send = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json={"schema_version": "1.0", "phone": phone, "device_id": device},
        )
        assert disabled_send.status_code == 409
        assert disabled_send.json()["error"]["code"] == "SMS_VERIFICATION_DISABLED"

        invalid_phone = "1380013800x"
        invalid = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json=_login_payload(invalid_phone, device),
        )
        assert invalid.status_code == 422
        assert invalid_phone not in invalid.text


async def test_concurrent_first_login_and_deleted_phone_tombstone() -> None:
    app = create_app(_settings(sms_verification_enabled=False))
    phone = _phone()
    device = f"device-{uuid4()}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first, second = await asyncio.gather(
            client.post(
                "/v1/auth/phone/login",
                headers={"Idempotency-Key": f"login-{uuid4()}"},
                json=_login_payload(phone, device),
            ),
            client.post(
                "/v1/auth/phone/login",
                headers={"Idempotency-Key": f"login-{uuid4()}"},
                json=_login_payload(phone, device),
            ),
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["user"]["id"] == second.json()["user"]["id"]

        deleted = await client.request(
            "DELETE",
            "/v1/me",
            headers={
                "Authorization": f"Bearer {first.json()['access_token']}",
                "Idempotency-Key": f"delete-{uuid4()}",
            },
            json={"schema_version": "1.0", "confirmation": "DELETE_MY_ACCOUNT"},
        )
        assert deleted.status_code == 202, deleted.text
        blocked = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json=_login_payload(phone, device),
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "ACCOUNT_UNAVAILABLE"


async def test_enabled_verification_consumes_code_and_links_wechat_to_phone() -> None:
    app = create_app(_settings(sms_verification_enabled=True))
    phone = _phone()
    device = f"device-{uuid4()}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        challenge = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json={"schema_version": "1.0", "phone": phone, "device_id": device},
        )
        assert challenge.status_code == 202, challenge.text
        payload = {
            **_login_payload(phone, device),
            "verification_challenge_id": challenge.json()["challenge_id"],
            "verification_code": "123456",
        }
        wrong = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json={**payload, "verification_code": "000000"},
        )
        assert wrong.status_code == 401
        login = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json=payload,
        )
        assert login.status_code == 200, login.text
        replay = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json=payload,
        )
        assert replay.status_code == 401

        wechat_login_code = f"wechat-{uuid4()}"
        wechat = await client.post(
            "/v1/auth/wechat/login",
            headers={"Idempotency-Key": f"wechat-{uuid4()}"},
            json={
                "schema_version": "1.0",
                "login_code": wechat_login_code,
                "phone_code": f"phone:{phone}",
                "provider": "WECHAT_MOCK",
                "device_id": device,
                "consent": _login_payload(phone, device)["consent"],
            },
        )
        assert wechat.status_code == 200, wechat.text
        assert wechat.json()["user"]["id"] == login.json()["user"]["id"]

        repeated_wechat = await client.post(
            "/v1/auth/wechat/login",
            headers={"Idempotency-Key": f"wechat-{uuid4()}"},
            json={
                "schema_version": "1.0",
                "login_code": wechat_login_code,
                "phone_code": f"phone:{phone}",
                "provider": "WECHAT_MOCK",
                "device_id": device,
                "consent": _login_payload(phone, device)["consent"],
            },
        )
        assert repeated_wechat.status_code == 200
        assert repeated_wechat.json()["user"]["id"] == login.json()["user"]["id"]

        conflict_phone = _phone()
        conflict = await client.post(
            "/v1/auth/wechat/login",
            headers={"Idempotency-Key": f"wechat-{uuid4()}"},
            json={
                "schema_version": "1.0",
                "login_code": wechat_login_code,
                "phone_code": f"phone:{conflict_phone}",
                "provider": "WECHAT_MOCK",
                "device_id": device,
                "consent": _login_payload(phone, device)["consent"],
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "PHONE_IDENTITY_CONFLICT"


async def test_verification_cooldown_attempt_lock_and_expiry() -> None:
    app = create_app(
        _settings(
            sms_verification_enabled=True,
            sms_max_attempts=2,
            sms_phone_hourly_limit=3,
            sms_device_hourly_limit=3,
        )
    )
    device = f"device-{uuid4()}"
    phone = _phone()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        challenge = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json={"schema_version": "1.0", "phone": phone, "device_id": device},
        )
        assert challenge.status_code == 202
        cooldown = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json={"schema_version": "1.0", "phone": phone, "device_id": device},
        )
        assert cooldown.status_code == 429

        payload = {
            **_login_payload(phone, device),
            "verification_challenge_id": challenge.json()["challenge_id"],
            "verification_code": "000000",
        }
        for _ in range(2):
            wrong = await client.post(
                "/v1/auth/phone/login",
                headers={"Idempotency-Key": f"login-{uuid4()}"},
                json=payload,
            )
            assert wrong.status_code == 401
        locked = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json={**payload, "verification_code": "123456"},
        )
        assert locked.status_code == 401

        expired_phone = _phone()
        expired = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json={"schema_version": "1.0", "phone": expired_phone, "device_id": device},
        )
        assert expired.status_code == 202
        async with app.state.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE phone_verification_challenges "
                    "SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 hour' "
                    "WHERE id=:id"
                ),
                {"id": expired.json()["challenge_id"]},
            )
        expired_login = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json={
                **_login_payload(expired_phone, device),
                "verification_challenge_id": expired.json()["challenge_id"],
                "verification_code": "123456",
            },
        )
        assert expired_login.status_code == 401
        assert expired_login.json()["error"]["code"] == "SMS_CODE_EXPIRED"


async def test_verification_idempotency_hourly_limit_and_required_fields() -> None:
    app = create_app(
        _settings(
            sms_verification_enabled=True,
            sms_resend_seconds=10,
            sms_phone_hourly_limit=2,
            sms_device_hourly_limit=10,
        )
    )
    phone = _phone()
    device = f"device-{uuid4()}"
    request_key = f"code-{uuid4()}"
    request = {"schema_version": "1.0", "phone": phone, "device_id": device}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": request_key},
            json=request,
        )
        assert first.status_code == 202
        repeated = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": request_key},
            json=request,
        )
        assert repeated.status_code == 202
        assert repeated.json()["challenge_id"] == first.json()["challenge_id"]

        reused = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": request_key},
            json={**request, "device_id": f"other-{device}"},
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        async with app.state.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE phone_verification_challenges "
                    "SET created_at=now()-interval '11 seconds' WHERE id=:id"
                ),
                {"id": first.json()["challenge_id"]},
            )

        second = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json=request,
        )
        assert second.status_code == 202
        limited = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json=request,
        )
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "SMS_RATE_LIMITED"

        missing_code = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json=_login_payload(phone, device),
        )
        assert missing_code.status_code == 422
        assert missing_code.json()["error"]["code"] == "SMS_CODE_REQUIRED"

        wrong_device = await client.post(
            "/v1/auth/phone/login",
            headers={"Idempotency-Key": f"login-{uuid4()}"},
            json={
                **_login_payload(phone, f"wrong-{device}"),
                "verification_challenge_id": first.json()["challenge_id"],
                "verification_code": "123456",
            },
        )
        assert wrong_device.status_code == 401
        assert wrong_device.json()["error"]["code"] == "SMS_CODE_INVALID"


async def test_sms_provider_failure_is_safely_persisted() -> None:
    class FailingSmsProvider:
        async def send_login_code(self, *_args: object) -> str:
            raise RuntimeError("provider secret must not escape")

    app = create_app(_settings(sms_verification_enabled=True))
    app.state.butler.sms_provider = FailingSmsProvider()
    phone = _phone()
    device = f"device-{uuid4()}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/auth/phone/verification-codes",
            headers={"Idempotency-Key": f"code-{uuid4()}"},
            json={"schema_version": "1.0", "phone": phone, "device_id": device},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SMS_SEND_FAILED"
        assert "provider secret" not in response.text

    async with app.state.database.connect() as connection:
        status = await connection.scalar(
            text(
                "SELECT status FROM phone_verification_challenges "
                "WHERE device_id=:device_id ORDER BY created_at DESC LIMIT 1"
            ),
            {"device_id": device},
        )
    assert status == "FAILED"
