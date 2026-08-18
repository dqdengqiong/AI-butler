from __future__ import annotations

import json

import pytest

from ai_butler.adapters.fake_llm_responses import (
    fake_availability_response,
    fake_executor_response,
    fake_feedback_response,
    fake_intent_response,
    fake_planner_response,
    fake_research_response,
)


def test_fake_research_and_availability_fail_closed_on_invalid_prompts() -> None:
    assert json.loads(fake_research_response("not-json"))["segments"] == []
    availability = json.loads(fake_availability_response("missing marker"))
    assert availability["status"] == "NEEDS_CLARIFICATION"


@pytest.mark.parametrize(
    ("value", "intent"),
    [
        ("", "CLARIFY"),
        ("帮我安排今天", "DAILY_PLANNING"),
        ("复盘计划进度", "PLAN_REVIEW"),
        ("任务太难了", "TASK_FEEDBACK"),
        ("省考怎么准备", "CIVIL_QA"),
        ("请给我股票买卖建议", "UNSUPPORTED"),
    ],
)
def test_fake_intent_covers_direct_routing_branches(value: str, intent: str) -> None:
    result = json.loads(fake_intent_response(json.dumps({"user_input": value})))
    assert result["intent"] == intent


def test_fake_intent_recovers_from_invalid_payload_and_detects_private_context() -> None:
    assert json.loads(fake_intent_response("not-json"))["intent"] == "CLARIFY"
    result = json.loads(
        fake_intent_response(json.dumps({"user_input": "分析我的资料", "attachment_count": 1}))
    )
    assert "PRIVATE_KNOWLEDGE" in result["context_needs"]


def test_fake_feedback_covers_replan_response_and_clarification() -> None:
    assert json.loads(fake_feedback_response("not-json"))["action"] == "CLARIFY"
    assert json.loads(fake_feedback_response('{"user_input":"任务太多"}'))["action"] == "REPLAN"
    assert json.loads(fake_feedback_response('{"user_input":"已完成"}'))["action"] == "RESPOND"


def test_fake_planner_and_executor_have_safe_invalid_input_fallbacks() -> None:
    planner = json.loads(fake_planner_response("not-json"))
    assert len(planner["stages"]) == 2
    executor = json.loads(fake_executor_response("not-json"))
    assert executor["task_drafts"] == []
    mixed = json.loads(
        fake_executor_response(
            json.dumps(
                {
                    "current_date": "2026-08-19",
                    "templates": [None, {"title": "复盘", "expected_minutes": 60}],
                    "availability": {"windows": [{"day_of_week": 3, "available_minutes": 30}]},
                }
            )
        )
    )
    assert mixed["task_drafts"][0]["expected_minutes"] == 25
