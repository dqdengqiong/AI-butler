from __future__ import annotations

from pathlib import Path

from ai_butler.evaluation.schema import (
    AgentEvalDatasetV1,
    AgentEvalTaskV1,
    EvalSuite,
)

DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "v1.json"


def load_eval_dataset(path: Path = DEFAULT_DATASET_PATH) -> AgentEvalDatasetV1:
    return AgentEvalDatasetV1.model_validate_json(path.read_text(encoding="utf-8"))


def select_tasks(
    dataset: AgentEvalDatasetV1,
    suite: EvalSuite,
) -> tuple[AgentEvalTaskV1, ...]:
    return tuple(task for task in dataset.tasks if suite in task.suites)
