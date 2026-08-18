"""计划范围收集与最终确认状态机。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text

from ai_butler.agent.availability import AvailabilityInterpretationV1
from ai_butler.agent.contracts import PlanScopeDraftV1, PlanScopeV1
from ai_butler.agent.plan_scope import (
    extract_explicit_target_date,
    target_date_for_weeks,
)

from .context import ButlerContext
from .interrupts import InterruptionService
from .shared import _json, _row


class PlanScopeFlowService:
    """只从服务端卡片快照和 run 状态恢复已确认的计划范围。"""

    def __init__(self, context: ButlerContext, interrupts: InterruptionService) -> None:
        self.database = context.database
        self.interrupts = interrupts

    async def resolve(
        self,
        *,
        run_id: UUID,
        snapshot: dict[str, Any],
        objective: str,
        current_input: str,
        request_context: dict[str, Any],
        availability_candidate: AvailabilityInterpretationV1 | None,
        confirmed_availability: AvailabilityInterpretationV1 | None,
        revise_availability: bool,
    ) -> PlanScopeV1 | None:
        """返回已最终确认的 scope；其余分支原子写入下一张输入卡。"""

        state = snapshot.get("output_data")
        state = dict(state) if isinstance(state, dict) else {}
        draft = self._parse_draft(state.get("plan_scope_draft"))
        scope = self._parse_scope(state.get("plan_scope"))
        scope_confirmed = state.get("plan_scope_confirmed") is True
        phase = str(request_context.get("card_phase") or "")
        selected = self._selected_option(request_context)
        selected_id = str(selected.get("id") or "")

        # Planner 失败后的 retry 不创建新输入，也不丢弃用户已确认的范围。
        if snapshot.get("pending_action") == "RETRY" and scope is not None and scope_confirmed:
            return scope

        if phase == "CONFIRM_PLAN_SCOPE":
            if selected_id == "confirm-plan-scope" and scope is not None:
                await self._save_state_and_continue(run_id, draft, scope, confirmed=True)
                return scope
            if selected_id == "revise-plan-period" and draft is not None:
                await self._save_and_interrupt_period(run_id, draft)
                return None
            if selected_id == "revise-availability":
                await self._clear_and_interrupt_availability(run_id)
                return None
            await self._interrupt_invalid_scope(run_id, draft, scope)
            return None

        if draft is not None and scope is None:
            target_date = None
            period_source: Literal["QUICK_WEEKS", "CUSTOM_DATE"] = "CUSTOM_DATE"
            if selected_id.startswith("period-") and selected_id.endswith("-weeks"):
                try:
                    weeks = int(selected_id.removeprefix("period-").removesuffix("-weeks"))
                    target_date = target_date_for_weeks(draft.start_date, weeks)
                    period_source = "QUICK_WEEKS"
                except ValueError:
                    target_date = None
            elif current_input:
                target_date = extract_explicit_target_date(current_input, draft.start_date)
            if target_date is None:
                await self._save_and_interrupt_period(
                    run_id,
                    draft,
                    question="请输入带四位年份、晚于开始日期的目标日期",
                )
                return None
            scope = PlanScopeV1(
                **draft.model_dump(),
                target_date=target_date,
                period_source=period_source,
            )
            await self._save_and_interrupt_confirmation(run_id, draft, scope)
            return None

        if confirmed_availability is not None:
            normalized_objective = " ".join(objective.split()).strip()[:4000]
            draft = PlanScopeDraftV1(
                objective_summary=normalized_objective or "公务员备考计划",
                availability=confirmed_availability,
                start_date=datetime.now(UTC).date(),
            )
            explicit_target = extract_explicit_target_date(objective, draft.start_date)
            if explicit_target is None:
                await self._save_and_interrupt_period(run_id, draft)
                return None
            scope = PlanScopeV1(
                **draft.model_dump(),
                target_date=explicit_target,
                period_source="EXPLICIT_DATE",
            )
            await self._save_and_interrupt_confirmation(run_id, draft, scope)
            return None

        if revise_availability:
            await self._clear_and_interrupt_availability(run_id)
        elif availability_candidate is not None:
            await self._interrupt_availability_confirmation(run_id, availability_candidate)
        else:
            await self._interrupt_availability_input(run_id)
        return None

    @staticmethod
    def _selected_option(request_context: dict[str, Any]) -> dict[str, Any]:
        selected = request_context.get("selected_options")
        if isinstance(selected, list) and selected and isinstance(selected[0], dict):
            return selected[0]
        return {}

    @staticmethod
    def _parse_draft(value: object) -> PlanScopeDraftV1 | None:
        try:
            return PlanScopeDraftV1.model_validate(value)
        except (ValidationError, ValueError, TypeError):
            return None

    @staticmethod
    def _parse_scope(value: object) -> PlanScopeV1 | None:
        try:
            return PlanScopeV1.model_validate(value)
        except (ValidationError, ValueError, TypeError):
            return None

    async def _locked_run(self, connection: Any, run_id: UUID) -> dict[str, Any] | None:
        run = _row(
            await connection.execute(
                text("SELECT * FROM agent_runs WHERE id=:id FOR UPDATE"), {"id": run_id}
            )
        )
        return run if run is not None and run["status"] == "RUNNING" else None

    async def _write_state(
        self,
        connection: Any,
        run: dict[str, Any],
        draft: PlanScopeDraftV1 | None,
        scope: PlanScopeV1 | None,
        *,
        confirmed: bool,
    ) -> None:
        output = run.get("output_data")
        output = dict(output) if isinstance(output, dict) else {}
        if draft is None:
            output.pop("plan_scope_draft", None)
        else:
            output["plan_scope_draft"] = draft.model_dump(mode="json")
        if scope is None:
            output.pop("plan_scope", None)
        else:
            output["plan_scope"] = scope.model_dump(mode="json")
        output["plan_scope_confirmed"] = confirmed
        await connection.execute(
            text(
                "UPDATE agent_runs SET output_data=CAST(:output AS jsonb),updated_at=now() "
                "WHERE id=:id"
            ),
            {"id": run["id"], "output": _json(output)},
        )
        run["output_data"] = output

    async def _save_state_and_continue(
        self,
        run_id: UUID,
        draft: PlanScopeDraftV1 | None,
        scope: PlanScopeV1,
        *,
        confirmed: bool,
    ) -> None:
        async with self.database.transaction() as connection:
            run = await self._locked_run(connection, run_id)
            if run is not None:
                await self._write_state(connection, run, draft, scope, confirmed=confirmed)

    async def _save_and_interrupt_period(
        self,
        run_id: UUID,
        draft: PlanScopeDraftV1,
        *,
        question: str = "选择计划周期",
    ) -> None:
        async with self.database.transaction() as connection:
            run = await self._locked_run(connection, run_id)
            if run is None:
                return
            await self._write_state(connection, run, draft, None, confirmed=False)
            await self.interrupts._interrupt_for_plan_period(
                connection, run, draft, question=question
            )

    async def _save_and_interrupt_confirmation(
        self, run_id: UUID, draft: PlanScopeDraftV1, scope: PlanScopeV1
    ) -> None:
        async with self.database.transaction() as connection:
            run = await self._locked_run(connection, run_id)
            if run is None:
                return
            await self._write_state(connection, run, draft, scope, confirmed=False)
            await self.interrupts._interrupt_for_plan_scope_confirmation(connection, run, scope)

    async def _clear_and_interrupt_availability(self, run_id: UUID) -> None:
        async with self.database.transaction() as connection:
            run = await self._locked_run(connection, run_id)
            if run is None:
                return
            await self._write_state(connection, run, None, None, confirmed=False)
            await self.interrupts._interrupt_for_availability_clarification(
                connection, run, "好的，请重新描述你的学习时间。"
            )

    async def _interrupt_availability_confirmation(
        self, run_id: UUID, candidate: AvailabilityInterpretationV1
    ) -> None:
        async with self.database.transaction() as connection:
            run = await self._locked_run(connection, run_id)
            if run is not None:
                await self.interrupts._interrupt_for_availability_confirmation(
                    connection, run, candidate
                )

    async def _interrupt_availability_input(self, run_id: UUID) -> None:
        async with self.database.transaction() as connection:
            run = await self._locked_run(connection, run_id)
            if run is not None:
                await self.interrupts._interrupt_for_input(connection, run)

    async def _interrupt_invalid_scope(
        self,
        run_id: UUID,
        draft: PlanScopeDraftV1 | None,
        scope: PlanScopeV1 | None,
    ) -> None:
        async with self.database.transaction() as connection:
            run = await self._locked_run(connection, run_id)
            if run is None:
                return
            if scope is not None:
                await self.interrupts._interrupt_for_plan_scope_confirmation(connection, run, scope)
            elif draft is not None:
                await self.interrupts._interrupt_for_plan_period(connection, run, draft)
            else:
                await self.interrupts._interrupt_for_input(connection, run)
