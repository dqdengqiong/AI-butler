from __future__ import annotations

from pathlib import Path

import pytest
from deepeval.test_case import LLMTestCase
from pydantic import ValidationError

from ai_butler.evaluation.dataset import load_eval_dataset, select_tasks
from ai_butler.evaluation.deepeval_adapter import (
    DeterministicMetric,
    build_synthetic_langgraph_callback,
    dataset_to_deepeval,
    outcome_to_test_case,
    task_to_golden,
)
from ai_butler.evaluation.runner import evaluate_release_gate, run_trials
from ai_butler.evaluation.schema import (
    AgentEvalOutcomeV1,
    AgentEvalTaskV1,
    EvalCitationV1,
    EvalSuite,
    EvalToolCallV1,
    ExpectedOutcomeV1,
    ExpectedToolPolicyV1,
)
from ai_butler.evaluation.verifiers import (
    verify_citations,
    verify_safety,
    verify_side_effects,
    verify_state,
    verify_tool_policy,
)


def _passing_outcome(task: AgentEvalTaskV1) -> AgentEvalOutcomeV1:
    expected = task.expected_outcome
    return AgentEvalOutcomeV1(
        status=expected.status,
        state=expected.state,
        citations=expected.citations,
        tool_calls=tuple(EvalToolCallV1(name=name) for name in expected.tool_policy.required),
        side_effects=expected.side_effects,
        input_tokens=10,
        output_tokens=5,
        estimated_cost=0.001,
    )


def test_dataset_contains_versioned_priority_scenarios() -> None:
    dataset = load_eval_dataset()
    assert len(dataset.tasks) == 24
    assert dataset.dataset_version == "2026-08-11.1"
    assert {task.task_id for task in select_tasks(dataset, EvalSuite.SECURITY)} == {
        "R-04",
        "A-06",
        "S-01",
        "S-02",
        "S-03",
        "S-04",
    }


def test_dataset_rejects_duplicate_task_ids(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        """{
          "dataset_version":"v1",
          "graph_version":"v1",
          "prompt_bundle_version":"v1",
          "tasks":[
            {"task_id":"P-01","name":"one","input":"x","expected_outcome":{"status":"OK"},"priority":"P0","suites":["core"]},
            {"task_id":"P-01","name":"two","input":"y","expected_outcome":{"status":"OK"},"priority":"P0","suites":["core"]}
          ]
        }""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="task IDs must be unique"):
        load_eval_dataset(path)


def test_tool_policy_requires_consistent_sets() -> None:
    with pytest.raises(ValidationError, match="required tools must also be allowed"):
        ExpectedToolPolicyV1(required=("search",), max_calls=1)
    with pytest.raises(ValidationError, match="must be disjoint"):
        ExpectedToolPolicyV1(allowed=("search",), forbidden=("search",), max_calls=1)


def test_deepeval_adapter_preserves_project_contract() -> None:
    dataset = load_eval_dataset()
    task = next(task for task in dataset.tasks if task.task_id == "R-01")
    golden = task_to_golden(task)
    deepeval_dataset = dataset_to_deepeval(dataset)
    test_case = outcome_to_test_case(task, _passing_outcome(task))
    assert golden.name == "R-01"
    assert golden.additional_metadata == {
        "schema_version": "1.0",
        "task_id": "R-01",
        "priority": "P0",
        "suites": ["core", "live"],
    }
    assert len(deepeval_dataset.goldens) == 24
    assert test_case.tools_called is not None
    assert test_case.tools_called[0].name == "research_collect_evidence"
    assert test_case.metadata == {"task_id": "R-01", "synthetic_data": True}


def test_langgraph_callback_has_deterministic_metrics() -> None:
    task = load_eval_dataset().tasks[0]
    callback = build_synthetic_langgraph_callback(task, "synthetic-thread")
    assert len(callback.metrics) == 5


@pytest.mark.parametrize(
    ("verifier", "expected", "actual", "reason"),
    [
        (
            verify_state,
            ExpectedOutcomeV1(status="OK", state={"next": "Research"}),
            AgentEvalOutcomeV1(status="FINAL_ERROR", state={"next": "Response"}),
            "mismatched fields",
        ),
        (
            verify_citations,
            ExpectedOutcomeV1(
                status="OK",
                citations=(EvalCitationV1(claim_id="claim", citation_id="official"),),
            ),
            AgentEvalOutcomeV1(status="OK"),
            "0/1",
        ),
        (
            verify_tool_policy,
            ExpectedOutcomeV1(
                status="OK",
                tool_policy=ExpectedToolPolicyV1(
                    allowed=("search",),
                    required=("search",),
                    forbidden=("delete",),
                    max_calls=1,
                ),
            ),
            AgentEvalOutcomeV1(
                status="OK",
                tool_calls=(EvalToolCallV1(name="delete"), EvalToolCallV1(name="delete")),
            ),
            "tool_not_allowed",
        ),
        (
            verify_safety,
            ExpectedOutcomeV1(status="OK"),
            AgentEvalOutcomeV1(status="OK", security_violations=("CROSS_TENANT_READ",)),
            "security violations",
        ),
        (
            verify_side_effects,
            ExpectedOutcomeV1(status="OK", side_effects={"task_create": 1}),
            AgentEvalOutcomeV1(status="OK", side_effects={"task_create": 2}),
            "did not match",
        ),
    ],
)
def test_deterministic_verifiers_reject_invalid_outcomes(
    verifier: object,
    expected: ExpectedOutcomeV1,
    actual: AgentEvalOutcomeV1,
    reason: str,
) -> None:
    assert callable(verifier)
    result = verifier(expected, actual)  # type: ignore[operator]
    assert not result.passed
    assert reason in result.reason


def test_metric_fails_closed_on_invalid_json() -> None:
    metric = DeterministicMetric("StateContract", verify_state)
    score = metric.measure(
        LLMTestCase(input="synthetic", actual_output="not-json", expected_output="{}")
    )
    assert score == 0
    assert metric.is_successful() is False
    assert metric.error == "ValidationError"


class StubRunner:
    model_name = "synthetic-runner-v1"

    async def run(self, task: AgentEvalTaskV1) -> AgentEvalOutcomeV1:
        return _passing_outcome(task)


async def test_runner_aggregates_three_trials_without_exposing_raw_inputs() -> None:
    dataset = load_eval_dataset()
    task = next(task for task in dataset.tasks if task.task_id == "P-01")
    report = await run_trials(dataset, [task], StubRunner(), trials_per_task=3)
    assert report.success_rate == 1
    assert report.pass_power_k == 1
    assert report.trials_per_task == 3
    assert report.input_tokens == 30
    assert report.output_tokens == 15
    assert report.estimated_cost == pytest.approx(0.003)
    serialized = report.model_dump_json()
    assert task.input not in serialized
    assert dataset.graph_version in serialized


async def test_release_gate_compares_success_latency_and_tokens() -> None:
    dataset = load_eval_dataset()
    task = next(task for task in dataset.tasks if task.task_id == "P-01")
    baseline = await run_trials(dataset, [task], StubRunner(), trials_per_task=1)
    candidate = baseline.model_copy(
        update={
            "success_rate": 0.80,
            "p95_duration_ms": max(1, baseline.p95_duration_ms * 2),
            "input_tokens": 100,
            "output_tokens": 100,
        }
    )
    gate = evaluate_release_gate(dataset, candidate, baseline)
    assert not gate.passed
    assert "SUCCESS_RATE_REGRESSION" in gate.failures
    assert "TOKEN_REGRESSION" in gate.failures


async def test_release_gate_accepts_passing_report_without_baseline() -> None:
    dataset = load_eval_dataset()
    task = next(task for task in dataset.tasks if task.task_id == "R-01")
    report = await run_trials(dataset, [task], StubRunner(), trials_per_task=1)
    assert evaluate_release_gate(dataset, report).passed
