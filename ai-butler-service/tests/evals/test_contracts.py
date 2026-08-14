from __future__ import annotations

import pytest
from deepeval import assert_test

from ai_butler.evaluation.dataset import load_eval_dataset
from ai_butler.evaluation.deepeval_adapter import (
    build_deterministic_metrics,
    outcome_to_test_case,
)
from ai_butler.evaluation.schema import (
    AgentEvalOutcomeV1,
    AgentEvalTaskV1,
    EvalSuite,
    EvalToolCallV1,
)


def _suite_marks(task: AgentEvalTaskV1) -> list[pytest.MarkDecorator]:
    mark_by_suite = {
        EvalSuite.SMOKE: pytest.mark.eval_smoke,
        EvalSuite.CORE: pytest.mark.eval_core,
        EvalSuite.SECURITY: pytest.mark.eval_security,
    }
    return [mark_by_suite[suite] for suite in task.suites if suite in mark_by_suite]


def _reference_outcome(task: AgentEvalTaskV1) -> AgentEvalOutcomeV1:
    expected = task.expected_outcome
    return AgentEvalOutcomeV1(
        status=expected.status,
        output_text=f"synthetic contract result for {task.task_id}",
        state=expected.state,
        citations=expected.citations,
        tool_calls=tuple(EvalToolCallV1(name=name) for name in expected.tool_policy.required),
        side_effects=expected.side_effects,
    )


DATASET = load_eval_dataset()
PARAMS = [pytest.param(task, id=task.task_id, marks=_suite_marks(task)) for task in DATASET.tasks]


@pytest.mark.parametrize("task", PARAMS)
def test_expected_contract_passes_deterministic_metrics(task: AgentEvalTaskV1) -> None:
    test_case = outcome_to_test_case(task, _reference_outcome(task))
    assert_test(test_case, build_deterministic_metrics(), run_async=False)
