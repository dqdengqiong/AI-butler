from __future__ import annotations

from collections.abc import Callable

from deepeval.dataset import EvaluationDataset, Golden
from deepeval.integrations.langchain import CallbackHandler
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase, ToolCall
from pydantic import ValidationError

from ai_butler.evaluation.schema import (
    AgentEvalDatasetV1,
    AgentEvalOutcomeV1,
    AgentEvalTaskV1,
    ExpectedOutcomeV1,
)
from ai_butler.evaluation.verifiers import (
    VerificationResult,
    verify_citations,
    verify_safety,
    verify_side_effects,
    verify_state,
    verify_tool_policy,
)

Verifier = Callable[[ExpectedOutcomeV1, AgentEvalOutcomeV1], VerificationResult]


def task_to_golden(task: AgentEvalTaskV1) -> Golden:
    return Golden(
        input=task.input,
        expected_output=task.expected_outcome.model_dump_json(),
        context=list(task.fixtures.evidence) or None,
        expected_tools=[
            ToolCall(name=name, input_parameters={})
            for name in task.expected_outcome.tool_policy.required
        ]
        or None,
        additional_metadata={
            "schema_version": task.schema_version,
            "task_id": task.task_id,
            "priority": task.priority.value,
            "suites": [suite.value for suite in task.suites],
        },
        name=task.task_id,
        multimodal=False,
    )


def dataset_to_deepeval(dataset: AgentEvalDatasetV1) -> EvaluationDataset:
    return EvaluationDataset(goldens=[task_to_golden(task) for task in dataset.tasks])


def build_synthetic_langgraph_callback(
    task: AgentEvalTaskV1,
    thread_id: str,
) -> CallbackHandler:
    """Create a DeepEval callback that is safe only for synthetic evaluation runs."""
    return CallbackHandler(
        name=task.task_id,
        tags=[suite.value for suite in task.suites],
        metadata={"task_id": task.task_id, "synthetic_data": True},
        thread_id=thread_id,
        user_id="synthetic-eval-user",
        metrics=build_deterministic_metrics(),
    )


def outcome_to_test_case(
    task: AgentEvalTaskV1,
    outcome: AgentEvalOutcomeV1,
) -> LLMTestCase:
    return LLMTestCase(
        input=task.input,
        actual_output=outcome.model_dump_json(),
        expected_output=task.expected_outcome.model_dump_json(),
        context=list(task.fixtures.evidence) or None,
        tools_called=[
            ToolCall(name=call.name, input_parameters=dict(call.arguments))
            for call in outcome.tool_calls
        ]
        or None,
        expected_tools=[
            ToolCall(name=name, input_parameters={})
            for name in task.expected_outcome.tool_policy.required
        ]
        or None,
        token_cost=outcome.estimated_cost,
        name=task.task_id,
        tags=[suite.value for suite in task.suites],
        metadata={"task_id": task.task_id, "synthetic_data": True},
    )


class DeterministicMetric(BaseMetric):  # type: ignore[no-untyped-call]
    threshold = 1.0
    async_mode = False
    include_reason = True
    verbose_mode = False

    def __init__(self, name: str, verifier: Verifier) -> None:
        self._name = name
        self._verifier = verifier
        self.score = None
        self.reason = None
        self.success = None
        self.error = None

    @property
    def __name__(self) -> str:
        return self._name

    def measure(self, test_case: LLMTestCase) -> float:
        try:
            if test_case.expected_output is None or test_case.actual_output is None:
                raise ValueError("expected_output and actual_output are required")
            expected = ExpectedOutcomeV1.model_validate_json(test_case.expected_output)
            actual = AgentEvalOutcomeV1.model_validate_json(test_case.actual_output)
            result = self._verifier(expected, actual)
            self.score = result.score
            self.reason = result.reason
            self.success = result.passed
            self.error = None
            return result.score
        except (ValidationError, ValueError, TypeError) as exc:
            self.score = 0.0
            self.reason = "invalid structured evaluation boundary"
            self.success = False
            self.error = type(exc).__name__
            return 0.0

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)


def build_deterministic_metrics() -> list[BaseMetric]:
    return [
        DeterministicMetric("StateContract", verify_state),
        DeterministicMetric("CitationSupport", verify_citations),
        DeterministicMetric("ToolPolicy", verify_tool_policy),
        DeterministicMetric("SafetyPolicy", verify_safety),
        DeterministicMetric("SideEffectIdempotency", verify_side_effects),
    ]
