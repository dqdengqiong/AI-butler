from __future__ import annotations

import json
import tomllib
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ai_butler.adapters.llm import (
    FakeLLM,
    ModelAuthenticationError,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelTask,
    ModelTimeoutError,
)
from ai_butler.adapters.model_routing import ModelRoutingConfig
from ai_butler.agent.availability import AvailabilityInterpretationV1
from ai_butler.agent.contracts import PlannerResultV1, PlanScopeV1
from ai_butler.agent.model_nodes import (
    ExecutorNode,
    FeedbackAdjustNode,
    IntentRouterNode,
    ResponseNode,
)
from ai_butler.agent.planning_nodes import DeterministicPlanReview, PlannerNode
from ai_butler.domain.errors import ButlerError


class _StaticLLM:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return ModelResponse(
            provider="test",
            model="test-model",
            model_profile="test-profile",
            content=output,
            prompt_version=request.prompt_version,
            attempt=request.attempt_offset + 1,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = await self.generate(request)
        yield ModelStreamEvent(delta=response.content)
        yield ModelStreamEvent(response=response)


def _plan_scope(start: date = date(2026, 8, 18)) -> PlanScopeV1:
    return PlanScopeV1(
        objective_summary="准备省考",
        availability=AvailabilityInterpretationV1(
            status="COMPLETE",
            weekly_minutes=300,
            summary="每周共 5 小时",
        ),
        start_date=start,
        target_date=start + timedelta(days=27),
        period_source="QUICK_WEEKS",
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("你好", "GENERAL_CHAT"),
        ("百度的网址是什么", "GENERAL_CHAT"),
        ("行测是什么", "CIVIL_QA"),
        ("帮我制定国考计划", "PLAN_CREATE"),
        ("调整一下当前计划", "PLAN_ADJUST"),
        ("今天的学习任务太多了", "TASK_FEEDBACK"),
        ("请记住我习惯晚上学习", "MEMORY"),
        ("给我保证收益的股票建议", "UNSUPPORTED"),
        ("", "CLARIFY"),
    ],
)
async def test_intent_router_covers_all_business_intents(content: str, expected: str) -> None:
    result = await IntentRouterNode(FakeLLM()).route(
        content,
        recent_messages=(),
        published_summaries=(),
        active_plan_titles=(),
        attachment_count=0,
        run_id=uuid4(),
    )

    assert result.intent == expected


async def test_intent_router_repairs_once_then_clarifies_low_confidence() -> None:
    low_confidence = json.dumps(
        {
            "schema_version": "1.0",
            "intent": "PLAN_CREATE",
            "confidence": 0.4,
            "context_needs": ["PLAN_REQUIREMENTS"],
            "clarifying_question": None,
        }
    )
    llm = _StaticLLM(["not-json", low_confidence])

    result = await IntentRouterNode(llm).route(
        "安排一下",
        recent_messages=(),
        published_summaries=(),
        active_plan_titles=(),
        attachment_count=0,
        run_id=uuid4(),
    )

    assert result.intent == "CLARIFY"
    assert len(llm.requests) == 2


async def test_intent_router_keeps_explicit_site_question_out_of_prior_civil_context() -> None:
    llm = _StaticLLM(
        [
            json.dumps(
                {
                    "schema_version": "1.0",
                    "intent": "CIVIL_QA",
                    "confidence": 0.95,
                    "context_needs": ["PUBLIC_KNOWLEDGE"],
                    "clarifying_question": None,
                }
            )
        ]
    )

    result = await IntentRouterNode(llm).route(
        "百度的网址是什么",
        recent_messages=("此前讨论的是行测复习。",),
        published_summaries=(),
        active_plan_titles=("公考计划",),
        attachment_count=0,
        run_id=uuid4(),
    )

    assert result.intent == "GENERAL_CHAT"
    assert "PUBLIC_KNOWLEDGE" not in result.context_needs


async def test_planner_and_executor_fake_results_pass_deterministic_review() -> None:
    today = date(2026, 8, 18)
    planner = await PlannerNode(FakeLLM()).plan(
        objective="准备省考",
        weekly_minutes=255,
        availability={"weekly_minutes": 300, "windows": []},
        verified_claims=(),
        plan_scope=_plan_scope(today),
        existing_plan=None,
        run_id=uuid4(),
    )
    DeterministicPlanReview.validate(
        planner,
        available_weekly_minutes=300,
        allowed_claim_keys=(),
        expected_start_date=today,
        expected_end_date=today + timedelta(days=27),
    )
    assert planner.plan is not None
    templates = tuple(
        {
            "stage_key": stage.stage_key,
            "template_key": template.template_key,
            "title": template.title,
            "expected_minutes": template.expected_minutes,
            "priority": template.priority,
        }
        for stage in planner.plan.stages[:1]
        for template in stage.task_templates
    )
    tasks = await ExecutorNode(FakeLLM()).schedule(
        revision_id=uuid4(),
        templates=templates,
        availability={"weekly_minutes": 300, "windows": []},
        current_date=today,
        run_id=uuid4(),
    )

    assert tasks.task_drafts
    assert all(
        today <= item.scheduled_date <= today + timedelta(days=6) for item in tasks.task_drafts
    )


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_retryable"),
    [
        (ModelTimeoutError("timeout"), "PLANNER_MODEL_UNAVAILABLE", True),
        (ModelAuthenticationError("auth"), "PLANNER_MODEL_INVALID", False),
    ],
)
async def test_planner_maps_provider_errors_to_stable_domain_errors(
    error: Exception, expected_code: str, expected_retryable: bool
) -> None:
    with pytest.raises(ButlerError) as caught:
        await PlannerNode(_StaticLLM([error])).plan(
            objective="准备省考",
            weekly_minutes=255,
            availability={"weekly_minutes": 300, "windows": []},
            verified_claims=(),
            plan_scope=_plan_scope(),
            existing_plan=None,
            run_id=uuid4(),
        )

    assert caught.value.code == expected_code
    assert caught.value.retryable is expected_retryable


async def test_planner_prompt_uses_dynamic_scope_schema_and_fixed_budgets() -> None:
    suggestion = {
        "schema_version": "2.0",
        "status": "READY",
        "title": "省考计划",
        "objective_summary": "准备省考",
        "assumptions": [],
        "stages": [
            {
                "stage_key": key,
                "name": f"阶段 {index}",
                "objective": "完成阶段训练",
                "task_templates": [
                    {
                        "title": "练习",
                        "description": "练习并复盘",
                        "days_per_week": 3,
                        "expected_minutes": 60,
                        "priority": 2,
                        "claim_keys": [],
                    }
                ],
            }
            for index, key in enumerate(("stage_1", "stage_2"), 1)
        ],
        "question": None,
        "adjustment_options": [],
        "warnings": [],
    }
    llm = _StaticLLM([json.dumps(suggestion, ensure_ascii=False)])

    await PlannerNode(llm).plan(
        objective="准备省考",
        weekly_minutes=255,
        availability={"weekly_minutes": 300, "windows": []},
        verified_claims=(),
        plan_scope=_plan_scope(),
        existing_plan=None,
        run_id=uuid4(),
    )

    request = llm.requests[0]
    prompt = json.loads(request.user_input)
    assert request.prompt_version == "planner-v2"
    assert request.schema_version == "2.0"
    assert request.timeout_ms == 45_000
    assert request.max_output_tokens == 4096
    assert prompt["confirmed_scope"]["target_date"] == "2026-09-14"
    assert "output_schema" in prompt
    assert "四周" not in request.user_input


async def test_planner_repairs_only_parse_failure_with_remaining_format_budget() -> None:
    llm = _StaticLLM(["not-json", "still-not-json"])

    with pytest.raises(ButlerError) as caught:
        await PlannerNode(llm).plan(
            objective="准备省考",
            weekly_minutes=255,
            availability={"weekly_minutes": 300, "windows": []},
            verified_claims=(),
            plan_scope=_plan_scope(),
            existing_plan=None,
            run_id=uuid4(),
        )

    assert caught.value.code == "PLANNER_MODEL_INVALID"
    assert [request.prompt_version for request in llm.requests] == [
        "planner-v2",
        "planner-v2-repair",
    ]
    assert llm.requests[0].timeout_ms == 45_000
    assert 0 < (llm.requests[1].timeout_ms or 0) <= 10_000


async def test_executor_timeout_is_retryable_domain_error() -> None:
    with pytest.raises(ButlerError) as caught:
        await ExecutorNode(_StaticLLM([ModelTimeoutError("timeout")])).schedule(
            revision_id=uuid4(),
            templates=(),
            availability={"weekly_minutes": 300, "windows": []},
            current_date=date(2026, 8, 18),
            run_id=uuid4(),
        )

    assert caught.value.code == "EXECUTOR_MODEL_UNAVAILABLE"
    assert caught.value.retryable is True


def test_plan_review_rejects_load_over_85_percent() -> None:
    result = PlannerResultV1.model_validate(
        {
            "status": "READY",
            "plan": {
                "title": "计划",
                "objective_summary": "目标",
                "start_date": "2026-08-18",
                "end_date": "2026-08-24",
                "weekly_minutes": 86,
                "stages": [
                    {
                        "stage_key": "week",
                        "name": "第一周",
                        "objective": "完成训练",
                        "sequence": 1,
                        "start_date": "2026-08-18",
                        "end_date": "2026-08-24",
                        "allocated_minutes": 86,
                        "task_templates": [
                            {
                                "template_key": "practice",
                                "title": "训练",
                                "frequency": {"days_per_week": 1},
                                "expected_minutes": 30,
                                "priority": 2,
                            }
                        ],
                    }
                ],
            },
        }
    )

    with pytest.raises(ButlerError, match="85%"):
        DeterministicPlanReview.validate(
            result,
            available_weekly_minutes=100,
            allowed_claim_keys=(),
        )


def _review_payload() -> dict[str, Any]:
    return {
        "status": "READY",
        "plan": {
            "title": "计划",
            "objective_summary": "目标",
            "start_date": "2026-08-18",
            "end_date": "2026-08-31",
            "weekly_minutes": 85,
            "stages": [
                {
                    "stage_key": f"stage_{index}",
                    "name": f"阶段 {index}",
                    "objective": "完成训练",
                    "sequence": index,
                    "start_date": start,
                    "end_date": end,
                    "allocated_minutes": 85,
                    "task_templates": [
                        {
                            "template_key": f"stage_{index}_task_1",
                            "title": "训练",
                            "frequency": {"days_per_week": 2},
                            "expected_minutes": 30,
                            "priority": 2,
                            "claim_keys": [],
                        }
                    ],
                }
                for index, (start, end) in enumerate(
                    (("2026-08-18", "2026-08-24"), ("2026-08-25", "2026-08-31")),
                    1,
                )
            ],
        },
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda payload: payload["plan"].update({"end_date": "2026-09-01"}),
            "PLAN_SCOPE_RANGE_INVALID",
        ),
        (
            lambda payload: payload["plan"]["stages"][1].update({"start_date": "2026-08-26"}),
            "PLAN_STAGE_RANGE_INVALID",
        ),
        (
            lambda payload: payload["plan"]["stages"][1]["task_templates"][0].update(
                {"template_key": "stage_1_task_1"}
            ),
            "PLAN_TEMPLATE_DUPLICATE",
        ),
        (
            lambda payload: payload["plan"]["stages"][0]["task_templates"][0].update(
                {"claim_keys": ["unknown"]}
            ),
            "PLAN_CLAIM_REFERENCE_INVALID",
        ),
        (
            lambda payload: payload["plan"]["stages"][0]["task_templates"][0].update(
                {"expected_minutes": 50, "frequency": {"days_per_week": 2}}
            ),
            "PLAN_TEMPLATE_LOAD_EXCEEDED",
        ),
    ],
)
def test_plan_review_rejects_invalid_scope_stages_templates_and_load(
    mutation: Callable[[dict[str, Any]], None], expected_code: str
) -> None:
    payload = deepcopy(_review_payload())
    mutation(payload)
    result = PlannerResultV1.model_validate(payload)

    with pytest.raises(ButlerError) as caught:
        DeterministicPlanReview.validate(
            result,
            available_weekly_minutes=100,
            allowed_claim_keys=(),
            expected_start_date=date(2026, 8, 18),
            expected_end_date=date(2026, 8, 31),
        )

    assert caught.value.code == expected_code


async def test_feedback_adjust_uses_dedicated_model_task() -> None:
    result = await FeedbackAdjustNode(FakeLLM()).analyze(
        user_input="任务太多了，帮我降低负荷",
        has_active_plan=True,
        run_id=uuid4(),
    )
    assert result.action == "REPLAN"


async def test_response_node_streams_real_response_contract() -> None:
    events = [
        event
        async for event in ResponseNode(FakeLLM()).stream(
            user_input="你好",
            published_summaries=(),
            recent_messages=(),
            memories=(),
            run_id=uuid4(),
        )
    ]
    assert "".join(event.delta for event in events) != ""
    assert events[-1].response is not None


async def test_stream_gateway_resets_partial_primary_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self, alias: str) -> None:
            self.alias = alias

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
            if self.alias == "qwen_balanced":
                yield ModelStreamEvent(delta="primary partial")
                raise ModelTimeoutError("timeout")
            response = ModelResponse(
                provider="doubao",
                model="fallback",
                model_profile=self.alias,
                content="fallback answer",
                prompt_version=request.prompt_version,
            )
            yield ModelStreamEvent(delta=response.content)
            yield ModelStreamEvent(response=response)

    monkeypatch.setattr(
        "ai_butler.adapters.llm.OpenAICompatibleLLM",
        lambda _key, _url, alias, _profile: _Client(alias),
    )
    routing_path = Path(__file__).parents[2] / "model-routing.toml"
    payload = tomllib.loads(routing_path.read_text(encoding="utf-8"))
    payload["models"]["doubao_turbo"] = {
        "provider": "doubao",
        "model": "doubao-seed-2-1-turbo-260628",
        "protocol": "openai_responses",
        "structured_output": True,
        "multimodal": True,
        "tools": True,
        "context_window_tokens": 256000,
    }
    payload["routes"]["response"]["fallbacks"] = ["doubao_turbo"]
    gateway = ModelGateway(
        ModelRoutingConfig.model_validate(payload),
        {"qwen": "key", "doubao": "key"},
    )
    events = [
        event
        async for event in gateway.stream(
            ModelRequest.user(ModelTask.RESPONSE, "response-v1", "hello")
        )
    ]

    assert [event.reset for event in events].count(True) == 1
    reset_index = next(index for index, event in enumerate(events) if event.reset)
    assert "primary" in events[0].delta
    assert "fallback" in events[reset_index + 1].delta
