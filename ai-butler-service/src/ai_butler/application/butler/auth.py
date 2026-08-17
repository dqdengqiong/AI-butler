from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_butler.adapters.auth import AuthIdentity
from ai_butler.api.schemas import (
    PhoneLoginRequest,
    PhoneVerificationCodeRequest,
)
from ai_butler.domain.errors import ButlerError, conflict
from ai_butler.phone import normalize_mainland_phone, phone_lookup_hash
from ai_butler.security import issue_refresh_token, refresh_token_session_id, token_hmac

from .bootstrap import BootstrapService
from .context import ButlerContext
from .shared import (
    _row,
)
from .support import ResponseFactory


class AuthService:
    def __init__(
        self, context: ButlerContext, bootstrap: BootstrapService, responses: ResponseFactory
    ) -> None:
        self.database = context.database
        self.settings = context.settings
        self.sms_provider = context.sms_provider
        self.phone_cipher = context.phone_cipher
        self._ensure_agent_definitions = bootstrap._ensure_agent_definitions
        self._ensure_user_workspace = bootstrap._ensure_user_workspace
        self._get_user = bootstrap._get_user
        self._token_response = responses._token_response

    def auth_config(self) -> dict[str, object]:
        """返回不含供应商和密钥信息的公开登录能力配置。"""

        return {
            "sms_verification_enabled": self.settings.sms_verification_enabled,
            "sms_code_length": self.settings.sms_code_length,
            "sms_code_ttl_seconds": self.settings.sms_code_ttl_seconds,
            "sms_resend_seconds": self.settings.sms_resend_seconds,
        }

    async def send_phone_verification_code(
        self,
        request: PhoneVerificationCodeRequest,
        idempotency_key: str,
    ) -> dict[str, object]:
        """创建验证码挑战并在事务外调用短信供应商。

        PENDING 挑战也计入限流，避免供应商慢请求期间并发绕过。供应商失败后
        挑战转为 FAILED，绝不会被登录流程接受。
        """

        if not self.settings.sms_verification_enabled:
            raise ButlerError("SMS_VERIFICATION_DISABLED", "短信验证码功能未开启", 409)
        phone = normalize_mainland_phone(request.phone)
        phone_hash = phone_lookup_hash(phone, self.settings.phone_lookup_secret)
        request_key_hash = token_hmac(idempotency_key, self.settings.sms_code_secret)
        now = datetime.now(UTC)
        challenge_id = uuid4()
        code = self.settings.sms_mock_code
        if len(code) != self.settings.sms_code_length or not code.isdigit():
            raise RuntimeError("invalid mock sms code configuration")
        code_hash = token_hmac(f"{challenge_id}:{code}", self.settings.sms_code_secret)

        async with self.database.transaction() as connection:
            duplicate = _row(
                await connection.execute(
                    text(
                        "SELECT id,phone_hash,device_id,status,expires_at FROM "
                        "phone_verification_challenges WHERE request_key_hash=:request_key_hash"
                    ),
                    {"request_key_hash": request_key_hash},
                )
            )
            if duplicate:
                if (
                    duplicate["phone_hash"] != phone_hash
                    or duplicate["device_id"] != request.device_id
                ):
                    raise conflict("IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他验证码请求")
                return {
                    "challenge_id": duplicate["id"],
                    "expires_in": max(0, int((duplicate["expires_at"] - now).total_seconds())),
                    "resend_after": self.settings.sms_resend_seconds,
                }
            recent = _row(
                await connection.execute(
                    text(
                        "SELECT "
                        "COUNT(*) FILTER (WHERE phone_hash=:phone_hash) AS phone_count,"
                        "COUNT(*) FILTER (WHERE device_id=:device_id) AS device_count,"
                        "MAX(created_at) FILTER (WHERE phone_hash=:phone_hash) AS last_phone_send "
                        "FROM phone_verification_challenges WHERE created_at>=:hour_ago"
                    ),
                    {
                        "phone_hash": phone_hash,
                        "device_id": request.device_id,
                        "hour_ago": now - timedelta(hours=1),
                    },
                )
            )
            if recent and (
                int(recent["phone_count"] or 0) >= self.settings.sms_phone_hourly_limit
                or int(recent["device_count"] or 0) >= self.settings.sms_device_hourly_limit
            ):
                raise ButlerError("SMS_RATE_LIMITED", "验证码请求过于频繁，请稍后再试", 429, True)
            if recent and recent.get("last_phone_send") is not None:
                elapsed = (now - recent["last_phone_send"]).total_seconds()
                if elapsed < self.settings.sms_resend_seconds:
                    raise ButlerError(
                        "SMS_RATE_LIMITED",
                        "验证码请求过于频繁，请稍后再试",
                        429,
                        True,
                        {"retry_after": int(self.settings.sms_resend_seconds - elapsed) + 1},
                    )
            await connection.execute(
                text(
                    "INSERT INTO phone_verification_challenges("
                    "id,phone_hash,code_hash,device_id,request_key_hash,status,expires_at) "
                    "VALUES(:id,:phone_hash,:code_hash,:device_id,:request_key_hash,'PENDING',:expires_at)"
                ),
                {
                    "id": challenge_id,
                    "phone_hash": phone_hash,
                    "code_hash": code_hash,
                    "device_id": request.device_id,
                    "request_key_hash": request_key_hash,
                    "expires_at": now + timedelta(seconds=self.settings.sms_code_ttl_seconds),
                },
            )

        try:
            provider_message_id = await self.sms_provider.send_login_code(phone, code, challenge_id)
        except Exception as exc:
            async with self.database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE phone_verification_challenges SET status='FAILED' "
                        "WHERE id=:id AND status='PENDING'"
                    ),
                    {"id": challenge_id},
                )
            raise ButlerError("SMS_SEND_FAILED", "验证码发送失败，请稍后重试", 503, True) from exc
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE phone_verification_challenges SET status='SENT',sent_at=:now,"
                    "provider_message_id=:provider_message_id WHERE id=:id AND status='PENDING'"
                ),
                {
                    "id": challenge_id,
                    "now": now,
                    "provider_message_id": provider_message_id,
                },
            )
        return {
            "challenge_id": challenge_id,
            "expires_in": self.settings.sms_code_ttl_seconds,
            "resend_after": self.settings.sms_resend_seconds,
        }

    async def phone_login(self, request: PhoneLoginRequest) -> dict[str, object]:
        """按手机号唯一账号键登录，并在开启时原子消费验证码。"""

        phone = normalize_mainland_phone(request.phone)
        now = datetime.now(UTC)
        verification_error: ButlerError | None = None
        async with self.database.transaction() as connection:
            if self.settings.sms_verification_enabled:
                verification_error = await self._consume_phone_challenge(
                    connection, request, phone, now
                )
            if verification_error is None:
                return await self._login_by_phone(connection, phone, request.device_id, now)
        # 错误尝试和过期状态必须先提交，不能因抛出领域错误而随事务回滚。
        raise verification_error

    async def wechat_login(
        self,
        identity: AuthIdentity,
        phone_value: str,
        device_id: str,
    ) -> dict[str, object]:
        """按微信已授权手机号登录，并把微信稳定主体绑定到同一账号。"""

        phone = normalize_mainland_phone(phone_value)
        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            return await self._login_by_phone(connection, phone, device_id, now, identity)

    async def _login_by_phone(
        self,
        connection: AsyncConnection,
        phone: str,
        device_id: str,
        now: datetime,
        identity: AuthIdentity | None = None,
    ) -> dict[str, object]:
        """在一个短事务中解析手机号账号、绑定身份并签发刷新会话。"""

        await self._ensure_agent_definitions(connection)
        phone_hash = phone_lookup_hash(phone, self.settings.phone_lookup_secret)
        # 空结果无法通过 FOR UPDATE 锁定；事务级 advisory lock 保证同一手机号
        # 的并发首次登录串行，唯一索引则作为最终数据库防线。
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:phone_hash,0))"),
            {"phone_hash": phone_hash},
        )
        existing = _row(
            await connection.execute(
                text("SELECT * FROM users WHERE phone_hash=:phone_hash FOR UPDATE"),
                {"phone_hash": phone_hash},
            )
        )
        identity_row = None
        if identity is not None:
            identity_row = _row(
                await connection.execute(
                    text(
                        "SELECT i.*,u.phone_hash,u.status FROM user_identities i "
                        "JOIN users u ON u.id=i.user_id WHERE i.provider=:provider "
                        "AND i.provider_subject=:subject FOR UPDATE"
                    ),
                    {"provider": identity.provider, "subject": identity.subject},
                )
            )
            if identity_row and identity_row["phone_hash"] != phone_hash:
                raise conflict("PHONE_IDENTITY_CONFLICT", "微信身份已绑定其他手机号")

        is_new = existing is None and identity_row is None
        if identity_row is not None:
            user_id = UUID(str(identity_row["user_id"]))
            existing = identity_row
        elif existing is not None:
            user_id = UUID(str(existing["id"]))
        else:
            user_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO users(id,status,nickname,phone_ciphertext,phone_hash,locale,timezone) "
                    "VALUES(:id,'ACTIVE','用户',:phone_ciphertext,:phone_hash,'zh-CN','Asia/Shanghai')"
                ),
                {
                    "id": user_id,
                    "phone_ciphertext": self.phone_cipher.encrypt(phone),
                    "phone_hash": phone_hash,
                },
            )
            await connection.execute(
                text("INSERT INTO user_profiles(user_id) VALUES(:user_id)"),
                {"user_id": user_id},
            )
            await connection.execute(
                text("INSERT INTO memory_policy_state(user_id) VALUES(:user_id)"),
                {"user_id": user_id},
            )

        if existing is not None and existing["status"] != "ACTIVE":
            raise ButlerError("ACCOUNT_UNAVAILABLE", "账号当前不可登录", 403)
        if identity is not None:
            if identity_row is None:
                await connection.execute(
                    text(
                        "INSERT INTO user_identities("
                        "id,user_id,provider,provider_subject,union_subject,last_login_at) "
                        "VALUES(:id,:user_id,:provider,:subject,:union_subject,:now)"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "provider": identity.provider,
                        "subject": identity.subject,
                        "union_subject": identity.union_subject,
                        "now": now,
                    },
                )
            else:
                await connection.execute(
                    text(
                        "UPDATE user_identities SET last_login_at=:now,"
                        "union_subject=COALESCE(:union_subject,union_subject) "
                        "WHERE provider=:provider AND provider_subject=:subject"
                    ),
                    {
                        "now": now,
                        "union_subject": identity.union_subject,
                        "provider": identity.provider,
                        "subject": identity.subject,
                    },
                )
        await self._ensure_user_workspace(connection, user_id)
        session_id = uuid4()
        refresh_token = issue_refresh_token(session_id)
        await connection.execute(
            text(
                "INSERT INTO auth_sessions(id,user_id,refresh_token_hash,device_id,status,expires_at) "
                "VALUES(:id,:user_id,:token_hash,:device_id,'ACTIVE',:expires_at)"
            ),
            {
                "id": session_id,
                "user_id": user_id,
                "token_hash": token_hmac(refresh_token, self.settings.auth_refresh_token_secret),
                "device_id": device_id,
                "expires_at": now + timedelta(seconds=self.settings.auth_refresh_token_seconds),
            },
        )
        user = await self._get_user(connection, user_id)
        return self._token_response(user, session_id, refresh_token, is_new)

    async def _consume_phone_challenge(
        self,
        connection: AsyncConnection,
        request: PhoneLoginRequest,
        phone: str,
        now: datetime,
    ) -> ButlerError | None:
        """锁定并一次性消费挑战，避免并发重放产生多个会话。"""

        if request.verification_challenge_id is None or request.verification_code is None:
            return ButlerError("SMS_CODE_REQUIRED", "请输入短信验证码", 422)
        phone_hash = phone_lookup_hash(phone, self.settings.phone_lookup_secret)
        challenge = _row(
            await connection.execute(
                text(
                    "SELECT * FROM phone_verification_challenges WHERE id=:id "
                    "AND phone_hash=:phone_hash AND device_id=:device_id FOR UPDATE"
                ),
                {
                    "id": request.verification_challenge_id,
                    "phone_hash": phone_hash,
                    "device_id": request.device_id,
                },
            )
        )
        if challenge is None or challenge["status"] != "SENT":
            return ButlerError("SMS_CODE_INVALID", "验证码无效或已使用", 401)
        if challenge["expires_at"] <= now:
            await connection.execute(
                text("UPDATE phone_verification_challenges SET status='EXPIRED' WHERE id=:id"),
                {"id": challenge["id"]},
            )
            return ButlerError("SMS_CODE_EXPIRED", "验证码已过期，请重新获取", 401)
        supplied_hash = token_hmac(
            f"{challenge['id']}:{request.verification_code}", self.settings.sms_code_secret
        )
        if not hmac.compare_digest(str(challenge["code_hash"]), supplied_hash):
            attempts = int(challenge["attempt_count"]) + 1
            status_value = "LOCKED" if attempts >= self.settings.sms_max_attempts else "SENT"
            await connection.execute(
                text(
                    "UPDATE phone_verification_challenges SET attempt_count=:attempts,status=:status "
                    "WHERE id=:id"
                ),
                {"attempts": attempts, "status": status_value, "id": challenge["id"]},
            )
            return ButlerError("SMS_CODE_INVALID", "验证码无效或已使用", 401)
        await connection.execute(
            text(
                "UPDATE phone_verification_challenges SET status='CONSUMED',consumed_at=:now "
                "WHERE id=:id"
            ),
            {"id": challenge["id"], "now": now},
        )
        return None

    async def refresh(self, refresh_token: str, device_id: str) -> dict[str, object]:
        """原子轮换刷新令牌；旧令牌复用会撤销当前会话。"""

        session_id = refresh_token_session_id(refresh_token)
        supplied_hash = token_hmac(refresh_token, self.settings.auth_refresh_token_secret)
        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            session = _row(
                await connection.execute(
                    text("SELECT * FROM auth_sessions WHERE id=:id FOR UPDATE"),
                    {"id": session_id},
                )
            )
            if session is None or session["status"] != "ACTIVE":
                raise ButlerError("INVALID_REFRESH_TOKEN", "登录状态已失效", 401)
            if session["device_id"] not in (None, device_id):
                raise ButlerError("INVALID_REFRESH_TOKEN", "登录状态已失效", 401)
            if session["expires_at"] <= now:
                await connection.execute(
                    text("UPDATE auth_sessions SET status='EXPIRED' WHERE id=:id"),
                    {"id": session_id},
                )
                raise ButlerError("REFRESH_TOKEN_EXPIRED", "登录状态已过期", 401)
            if session["refresh_token_hash"] != supplied_hash:
                await connection.execute(
                    text("UPDATE auth_sessions SET status='REVOKED',revoked_at=:now WHERE id=:id"),
                    {"id": session_id, "now": now},
                )
                raise ButlerError("REFRESH_TOKEN_REUSED", "检测到旧令牌复用，请重新登录", 401)
            rotated = issue_refresh_token(session_id)
            await connection.execute(
                text(
                    "UPDATE auth_sessions SET refresh_token_hash=:token_hash,last_used_at=:now "
                    "WHERE id=:id"
                ),
                {
                    "id": session_id,
                    "token_hash": token_hmac(rotated, self.settings.auth_refresh_token_secret),
                    "now": now,
                },
            )
            user = await self._get_user(connection, UUID(str(session["user_id"])))
        return self._token_response(user, session_id, rotated, False)

    async def logout(self, user_id: UUID, refresh_token: str) -> None:
        session_id = refresh_token_session_id(refresh_token)
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE auth_sessions SET status='REVOKED',revoked_at=now() "
                    "WHERE id=:id AND user_id=:user_id AND status='ACTIVE'"
                ),
                {"id": session_id, "user_id": user_id},
            )
