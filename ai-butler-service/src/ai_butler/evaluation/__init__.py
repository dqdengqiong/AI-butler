"""Local-first Agent evaluation contracts and DeepEval adapters."""

from ai_butler.evaluation.dataset import load_eval_dataset, select_tasks
from ai_butler.evaluation.schema import (
    AgentEvalDatasetV1,
    AgentEvalOutcomeV1,
    AgentEvalTaskV1,
    EvalSuite,
)

__all__ = [
    "AgentEvalDatasetV1",
    "AgentEvalOutcomeV1",
    "AgentEvalTaskV1",
    "EvalSuite",
    "load_eval_dataset",
    "select_tasks",
]
