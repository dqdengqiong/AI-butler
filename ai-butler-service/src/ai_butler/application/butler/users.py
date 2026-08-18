from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text

from ai_butler.api.schemas import (
    AvailabilityRequest,
    PreferencesRequest,
    ProfileRequest,
    UpdateMeRequest,
)
from ai_butler.domain.errors import ButlerError, conflict, not_found

from .bootstrap import BootstrapService
from .context import ButlerContext
from .shared import (
    _json,
    _row,
)
from .support import validate_availability_overlap


class UserService:
    def __init__(self, context: ButlerContext, bootstrap: BootstrapService) -> None:
        self.database = context.database
        self._get_user = bootstrap._get_user
        self._validate_availability_overlap = validate_availability_overlap

    async def get_me(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            return await self._get_user(connection, user_id)

    async def update_me(self, user_id: UUID, request: UpdateMeRequest) -> dict[str, object]:
        values = request.model_dump(exclude_unset=True)
        if not values:
            return await self.get_me(user_id)
        allowed = {"nickname", "locale", "timezone"}
        assignments = [f"{key}=:{key}" for key in values if key in allowed]
        if request.avatar_file_id is not None:
            raise ButlerError("AVATAR_NOT_READY", "头像文件尚未完成验证", 409)
        async with self.database.transaction() as connection:
            if assignments:
                await connection.execute(
                    text(
                        f"UPDATE users SET {','.join(assignments)},updated_at=now() "  # noqa: S608
                        "WHERE id=:user_id AND status='ACTIVE'"
                    ),
                    {**{key: values[key] for key in values if key in allowed}, "user_id": user_id},
                )
            return await self._get_user(connection, user_id)

    async def get_profile(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            row = _row(
                await connection.execute(
                    text("SELECT * FROM user_profiles WHERE user_id=:user_id"),
                    {"user_id": user_id},
                )
            )
            if row is None:
                raise not_found()
            return row

    async def put_profile(self, user_id: UUID, request: ProfileRequest) -> dict[str, object]:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                text(
                    "UPDATE user_profiles SET education_level=:education_level,major=:major,"
                    "region_code=:region_code,current_level=:current_level,"
                    "existing_materials=CAST(:materials AS jsonb),profile_version=profile_version+1,updated_at=now() "
                    "WHERE user_id=:user_id AND profile_version=:expected_version RETURNING *"
                ),
                {
                    "user_id": user_id,
                    "expected_version": request.expected_version,
                    "education_level": request.education_level,
                    "major": request.major,
                    "region_code": request.region_code,
                    "current_level": request.current_level,
                    "materials": _json([str(item) for item in request.existing_material_file_ids]),
                },
            )
            row = _row(result)
            if row is None:
                raise conflict("RESOURCE_VERSION_CONFLICT", "画像版本已更新，请刷新后重试")
            return row

    async def get_availability(self, user_id: UUID) -> dict[str, object]:
        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT * FROM study_availability WHERE user_id=:user_id "
                            "ORDER BY day_of_week NULLS FIRST,start_time NULLS FIRST"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            profile = _row(
                await connection.execute(
                    text("SELECT profile_version FROM user_profiles WHERE user_id=:user_id"),
                    {"user_id": user_id},
                )
            )
            return {
                "version": profile["profile_version"] if profile else 1,
                "windows": [dict(row) for row in rows],
            }

    async def put_availability(
        self, user_id: UUID, request: AvailabilityRequest
    ) -> dict[str, object]:
        self._validate_availability_overlap(request)
        async with self.database.transaction() as connection:
            locked = _row(
                await connection.execute(
                    text(
                        "SELECT profile_version FROM user_profiles WHERE user_id=:user_id FOR UPDATE"
                    ),
                    {"user_id": user_id},
                )
            )
            if locked is None or locked["profile_version"] != request.expected_version:
                raise conflict("RESOURCE_VERSION_CONFLICT", "学习时间版本已更新，请刷新后重试")
            await connection.execute(
                text("DELETE FROM study_availability WHERE user_id=:user_id"),
                {"user_id": user_id},
            )
            for window in request.windows:
                await connection.execute(
                    text(
                        "INSERT INTO study_availability(id,user_id,day_of_week,start_time,end_time,"
                        "available_minutes,effective_from,effective_to) "
                        "VALUES(:id,:user_id,:day,:start,:end,:minutes,:effective_from,:effective_to)"
                    ),
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "day": window.day_of_week,
                        "start": window.start_time,
                        "end": window.end_time,
                        "minutes": window.available_minutes,
                        "effective_from": window.effective_from,
                        "effective_to": window.effective_to,
                    },
                )
            await connection.execute(
                text(
                    "UPDATE user_profiles SET profile_version=profile_version+1,updated_at=now() "
                    "WHERE user_id=:user_id"
                ),
                {"user_id": user_id},
            )
        return await self.get_availability(user_id)

    async def get_preferences(self, user_id: UUID) -> dict[str, object]:
        profile = await self.get_profile(user_id)
        reminder = profile["notification_preferences"] or {
            "enabled": True,
            "channels": ["IN_APP"],
            "advance_minutes": 15,
        }
        return {
            "version": profile["profile_version"],
            "task_reminder": reminder,
            "plan_change_confirmation_required": True,
            "read_only_policies": ["plan_change_confirmation_required"],
        }

    async def patch_preferences(
        self, user_id: UUID, request: PreferencesRequest
    ) -> dict[str, object]:
        async with self.database.transaction() as connection:
            updated = await connection.execute(
                text(
                    "UPDATE user_profiles SET notification_preferences=CAST(:settings AS jsonb),"
                    "profile_version=profile_version+1,updated_at=now() "
                    "WHERE user_id=:user_id AND profile_version=:version RETURNING user_id"
                ),
                {
                    "settings": request.task_reminder.model_dump_json(),
                    "user_id": user_id,
                    "version": request.expected_version,
                },
            )
            if updated.first() is None:
                raise conflict("RESOURCE_VERSION_CONFLICT", "设置版本已更新，请刷新后重试")
        return await self.get_preferences(user_id)

    async def delete_account(self, user_id: UUID) -> dict[str, object]:
        now = datetime.now(UTC)
        async with self.database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE users SET status='DELETING',phone_ciphertext=NULL,updated_at=:now "
                    "WHERE id=:user_id AND status='ACTIVE'"
                ),
                {"user_id": user_id, "now": now},
            )
            await connection.execute(
                text(
                    "UPDATE auth_sessions SET status='REVOKED',revoked_at=:now "
                    "WHERE user_id=:user_id AND status='ACTIVE'"
                ),
                {"user_id": user_id, "now": now},
            )
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='CANCEL_REQUESTED',cancel_requested_at=:now "
                    "WHERE user_id=:user_id AND status IN ('QUEUED','RUNNING','AWAITING_INPUT','AWAITING_APPROVAL')"
                ),
                {"user_id": user_id, "now": now},
            )
            await connection.execute(
                text(
                    "INSERT INTO account_deletion_jobs(id,user_id,status,current_step) "
                    "VALUES(:id,:user_id,'PENDING','CANCEL_WORK') "
                    "ON CONFLICT(user_id) DO NOTHING"
                ),
                {"id": uuid4(), "user_id": user_id},
            )
        return {"status": "DELETING", "accepted_at": now}

    async def list_agent_definitions(self) -> dict[str, object]:
        """返回用户可见的专业入口目录，不暴露内部定义或用户 Agent ID。"""

        async with self.database.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT code,name,COALESCE(description,'') AS description,catalog_status,"
                            "catalog_metadata FROM agent_definitions WHERE catalog_status <> 'HIDDEN' "
                            "ORDER BY display_order,code"
                        )
                    )
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            metadata = row["catalog_metadata"] if isinstance(row["catalog_metadata"], dict) else {}
            items.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "description": row["description"],
                    "icon": str(metadata.get("icon", "AI")),
                    "availability": row["catalog_status"],
                    "welcome_message": str(metadata.get("welcome_message", "")),
                    "starter_prompts": metadata.get("starter_prompts", []),
                }
            )
        return {"items": items}
