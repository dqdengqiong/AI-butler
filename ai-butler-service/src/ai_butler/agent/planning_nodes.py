"""紧凑 Planner 模型建议、规范化与确定性 Review。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterable
from datetime import date, timedelta
from itertools import pairwise
from time import monotonic
from uuid import UUID

from pydantic import ValidationError

from ai_butler.adapters.llm import LLM, ModelError, ModelRequest, ModelResponse, ModelTask
from ai_butler.adapters.model_routing import OutputMode, ThinkingMode
from ai_butler.agent.contracts import (
    PlanDraftV1,
    PlannerResultV1,
    PlannerSuggestionV2,
    PlanScopeV1,
    PlanStageV1,
    PlanTaskTemplateV1,
)
from ai_butler.agent.model_errors import model_boundary_error
from ai_butler.agent.plan_scope import stage_windows
from ai_butler.domain.errors import ButlerError


class PlannerNode:
    """根据已确认目标、时间和验证 Claim 生成计划建议，不执行任何写操作。"""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    async def plan(
        self,
        *,
        objective: str,
        weekly_minutes: int,
        availability: dict[str, object],
        verified_claims: tuple[dict[str, object], ...],
        plan_scope: PlanScopeV1,
        existing_plan: dict[str, object] | None,
        run_id: UUID,
    ) -> PlannerResultV1:
        """在 55 秒总预算内生成紧凑建议，并由代码补齐所有业务结构。"""

        windows = stage_windows(plan_scope)
        output_schema = PlannerSuggestionV2.model_json_schema(mode="validation")
        prompt: dict[str, object] = {
            "instruction": (
                "根据已确认范围生成紧凑的公务员备考内容建议。不得创建正式任务、预测成功率、"
                "伪造政策事实、修改日期或修改其他计划。只返回符合 output_schema 的 JSON。"
            ),
            "rules": [
                "每个 stage_key 必须与 stage_windows 中对应项完全一致且顺序相同。",
                "每阶段只能建议 1 至 2 个任务模板。",
                "每阶段任务的 days_per_week × expected_minutes 总和不得超过 weekly_minutes。",
                "具体政策事实只能引用 verified_claims 中已有 claim_key。",
                "日期、顺序、稳定 key 与容量由服务端生成，输出中不得添加这些字段。",
            ],
            "schema_version": "2.0",
            "objective": plan_scope.objective_summary or objective,
            "weekly_minutes": weekly_minutes,
            "availability": availability,
            "confirmed_scope": {
                "start_date": plan_scope.start_date.isoformat(),
                "target_date": plan_scope.target_date.isoformat(),
                "period_source": plan_scope.period_source,
            },
            "stage_windows": [
                {"stage_key": key, "start_date": start.isoformat(), "end_date": end.isoformat()}
                for key, start, end in windows
            ],
            "verified_claims": verified_claims,
            "existing_plan": existing_plan,
            "output_schema": output_schema,
            "example": {
                "schema_version": "2.0",
                "status": "READY",
                "title": "公务员备考计划",
                "objective_summary": "按已确认周期完成基础、强化与复盘。",
                "assumptions": [],
                "stages": [
                    {
                        "stage_key": stage_key,
                        "name": f"阶段 {index}",
                        "objective": "完成本阶段训练并建立复盘记录。",
                        "task_templates": [
                            {
                                "title": "训练与复盘",
                                "description": "完成练习并记录错误原因。",
                                "days_per_week": 3,
                                "expected_minutes": max(1, weekly_minutes // 3),
                                "priority": 2,
                                "claim_keys": [],
                            }
                        ],
                    }
                    for index, (stage_key, _start, _end) in enumerate(windows, 1)
                ],
                "question": None,
                "adjustment_options": [],
                "warnings": [],
            },
        }
        started = monotonic()
        try:
            async with asyncio.timeout(55):
                response = await self._generate(
                    "planner-v2", prompt, run_id=run_id, timeout_ms=45_000
                )
                parsed = self._parse(response.content)
                if parsed is None:
                    remaining_ms = max(100, min(10_000, int((55 - (monotonic() - started)) * 1000)))
                    response = await self._generate(
                        "planner-v2-repair",
                        {
                            "instruction": (
                                "只修复为原 PlannerSuggestionV2 Schema 的 JSON，不增加事实。"
                            ),
                            "invalid_output": response.content[:4000],
                            "original_request": prompt,
                        },
                        run_id=run_id,
                        model_profile=response.model_profile,
                        attempt_offset=response.attempt,
                        timeout_ms=remaining_ms,
                    )
                    parsed = self._parse(response.content)
        except TimeoutError as exc:
            raise ButlerError(
                "PLANNER_MODEL_UNAVAILABLE", "计划生成超时，本次未创建计划", 503, True
            ) from exc
        except ModelError as exc:
            raise model_boundary_error(exc, "PLANNER", "计划生成") from exc
        if parsed is None:
            raise ButlerError("PLANNER_MODEL_INVALID", "计划生成结果不符合约束", 502)
        return self._normalize(parsed, plan_scope, weekly_minutes, windows)

    async def _generate(
        self,
        prompt_version: str,
        prompt: dict[str, object],
        *,
        run_id: UUID,
        model_profile: str | None = None,
        attempt_offset: int = 0,
        timeout_ms: int,
    ) -> ModelResponse:
        return await self._llm.generate(
            ModelRequest.user(
                ModelTask.PLANNER,
                prompt_version,
                json.dumps(prompt, ensure_ascii=False),
                schema_version="2.0",
                run_id=run_id,
                model_profile=model_profile,
                attempt_offset=attempt_offset,
                output_mode=OutputMode.JSON,
                thinking=ThinkingMode.DISABLED,
                max_output_tokens=4096,
                timeout_ms=timeout_ms,
            )
        )

    @staticmethod
    def _parse(content: str) -> PlannerSuggestionV2 | None:
        try:
            return PlannerSuggestionV2.model_validate_json(content)
        except (ValidationError, ValueError):
            return None

    @staticmethod
    def _normalize(
        suggestion: PlannerSuggestionV2,
        scope: PlanScopeV1,
        weekly_minutes: int,
        windows: tuple[tuple[str, date, date], ...],
    ) -> PlannerResultV1:
        """把内容建议转换成既有 PlannerResultV1，模型不控制日期或稳定键。"""

        if suggestion.status != "READY":
            return PlannerResultV1(
                status=suggestion.status,
                question=suggestion.question,
                adjustment_options=suggestion.adjustment_options,
                warnings=suggestion.warnings,
            )
        by_key = {stage.stage_key: stage for stage in suggestion.stages}
        if len(by_key) != len(windows) or tuple(by_key) != tuple(item[0] for item in windows):
            raise ButlerError("PLANNER_MODEL_INVALID", "计划阶段建议与确认周期不匹配", 502)
        stages: list[PlanStageV1] = []
        for sequence, (stage_key, start, end) in enumerate(windows, 1):
            suggested_stage = by_key[stage_key]
            templates = tuple(
                PlanTaskTemplateV1(
                    template_key=f"{stage_key}_task_{template_index}",
                    title=template.title,
                    description=template.description,
                    frequency={"days_per_week": template.days_per_week},
                    expected_minutes=template.expected_minutes,
                    priority=template.priority,
                    claim_keys=template.claim_keys,
                )
                for template_index, template in enumerate(suggested_stage.task_templates, start=1)
            )
            duration_days = (end - start).days + 1
            stages.append(
                PlanStageV1(
                    stage_key=stage_key,
                    name=suggested_stage.name,
                    objective=suggested_stage.objective,
                    sequence=sequence,
                    start_date=start,
                    end_date=end,
                    allocated_minutes=max(1, weekly_minutes * duration_days // 7),
                    task_templates=templates,
                )
            )
        return PlannerResultV1(
            status="READY",
            plan=PlanDraftV1(
                title=suggestion.title or "公务员备考计划",
                objective_summary=suggestion.objective_summary or scope.objective_summary,
                start_date=scope.start_date,
                end_date=scope.target_date,
                weekly_minutes=weekly_minutes,
                assumptions=suggestion.assumptions,
                stages=tuple(stages),
            ),
            warnings=suggestion.warnings,
        )


class DeterministicPlanReview:
    """对 Planner 草稿执行不可由 Prompt 代替的日期、负荷和引用校验。"""

    @staticmethod
    def validate(
        result: PlannerResultV1,
        *,
        available_weekly_minutes: int,
        allowed_claim_keys: Iterable[str],
        expected_start_date: date | None = None,
        expected_end_date: date | None = None,
    ) -> None:
        """验证 READY 草稿；失败时拒绝持久化任何计划业务事实。"""

        if result.status != "READY" or result.plan is None:
            return
        plan = result.plan
        if expected_start_date is not None and plan.start_date != expected_start_date:
            raise ButlerError("PLAN_SCOPE_RANGE_INVALID", "计划开始日期与确认范围不一致", 422)
        if expected_end_date is not None and plan.end_date != expected_end_date:
            raise ButlerError("PLAN_SCOPE_RANGE_INVALID", "计划结束日期与确认范围不一致", 422)
        capacity = max(1, int(available_weekly_minutes * 0.85))
        if plan.weekly_minutes > capacity:
            raise ButlerError("PLAN_WEEKLY_LOAD_EXCEEDED", "计划负荷超过可用时间的 85%", 422)
        stages = sorted(plan.stages, key=lambda item: item.sequence)
        if not 2 <= len(stages) <= 4:
            raise ButlerError("PLAN_STAGE_COUNT_INVALID", "计划阶段数量必须为 2 至 4", 422)
        if [item.sequence for item in stages] != list(range(1, len(stages) + 1)):
            raise ButlerError("PLAN_STAGE_SEQUENCE_INVALID", "计划阶段顺序无效", 422)
        if stages[0].start_date != plan.start_date or stages[-1].end_date != plan.end_date:
            raise ButlerError("PLAN_STAGE_RANGE_INVALID", "计划阶段没有覆盖完整周期", 422)
        for previous, current in pairwise(stages):
            if current.start_date != previous.end_date + timedelta(days=1):
                raise ButlerError("PLAN_STAGE_RANGE_INVALID", "计划阶段日期不连续", 422)
        plan_weeks = max(1, math.ceil(((plan.end_date - plan.start_date).days + 1) / 7))
        if sum(stage.allocated_minutes for stage in stages) > plan.weekly_minutes * plan_weeks:
            raise ButlerError("PLAN_STAGE_LOAD_EXCEEDED", "计划阶段总负荷超过计划容量", 422)
        template_keys: set[str] = set()
        allowed = set(allowed_claim_keys)
        for stage in stages:
            if not 1 <= len(stage.task_templates) <= 2:
                raise ButlerError(
                    "PLAN_TEMPLATE_COUNT_INVALID", "每阶段只能包含 1 至 2 个模板", 422
                )
            stage_weekly_load = 0
            for template in stage.task_templates:
                if template.template_key in template_keys:
                    raise ButlerError("PLAN_TEMPLATE_DUPLICATE", "计划任务模板标识重复", 422)
                template_keys.add(template.template_key)
                if not set(template.claim_keys) <= allowed:
                    raise ButlerError("PLAN_CLAIM_REFERENCE_INVALID", "计划引用了未知事实", 422)
                frequency = template.frequency.get("days_per_week", 1)
                if isinstance(frequency, bool) or not isinstance(frequency, int):
                    raise ButlerError("PLAN_TEMPLATE_FREQUENCY_INVALID", "任务模板频率无效", 422)
                if not 1 <= frequency <= 7:
                    raise ButlerError("PLAN_TEMPLATE_FREQUENCY_INVALID", "任务模板频率无效", 422)
                days_per_week = frequency
                stage_weekly_load += template.expected_minutes * days_per_week
            if stage_weekly_load > plan.weekly_minutes:
                raise ButlerError("PLAN_TEMPLATE_LOAD_EXCEEDED", "任务模板周负荷超过计划容量", 422)
