from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from time import monotonic_ns
from typing import Protocol

from ai_butler.evaluation.deepeval_adapter import (
    build_deterministic_metrics,
    outcome_to_test_case,
)
from ai_butler.evaluation.schema import (
    AgentEvalDatasetV1,
    AgentEvalOutcomeV1,
    AgentEvalTaskV1,
    AgentEvalTrialV1,
    EvalGateResultV1,
    EvalPriority,
    EvalReportV1,
    MetricResultV1,
)


class AgentEvalRunner(Protocol):
    @property
    def model_name(self) -> str: ...

    async def run(self, task: AgentEvalTaskV1) -> AgentEvalOutcomeV1: ...


async def run_trials(
    dataset: AgentEvalDatasetV1,
    tasks: Sequence[AgentEvalTaskV1],
    runner: AgentEvalRunner,
    trials_per_task: int,
) -> EvalReportV1:
    if trials_per_task < 1:
        raise ValueError("trials_per_task must be positive")

    trials: list[AgentEvalTrialV1] = []
    for task in tasks:
        for trial_index in range(1, trials_per_task + 1):
            started_at = monotonic_ns()
            outcome = await runner.run(task)
            duration_ms = max(0, (monotonic_ns() - started_at) // 1_000_000)
            test_case = outcome_to_test_case(task, outcome)
            metric_results: list[MetricResultV1] = []
            for metric in build_deterministic_metrics():
                score = metric.measure(test_case)
                metric_results.append(
                    MetricResultV1(
                        name=metric.__name__,
                        score=score,
                        passed=metric.is_successful() is True,
                        reason=metric.reason or "",
                    )
                )
            trials.append(
                AgentEvalTrialV1(
                    task_id=task.task_id,
                    trial_index=trial_index,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    metrics=tuple(metric_results),
                    passed=all(result.passed for result in metric_results),
                )
            )

    return _build_report(dataset, runner.model_name, trials_per_task, trials)


def _build_report(
    dataset: AgentEvalDatasetV1,
    model_name: str,
    trials_per_task: int,
    trials: Sequence[AgentEvalTrialV1],
) -> EvalReportV1:
    if not trials:
        raise ValueError("at least one trial is required")
    passed = sum(trial.passed for trial in trials)
    per_task: dict[str, list[bool]] = defaultdict(list)
    for trial in trials:
        per_task[trial.task_id].append(trial.passed)
    consistently_passed = sum(all(results) for results in per_task.values())
    durations = sorted(trial.duration_ms for trial in trials)
    return EvalReportV1(
        dataset_version=dataset.dataset_version,
        graph_version=dataset.graph_version,
        prompt_bundle_version=dataset.prompt_bundle_version,
        model=model_name,
        trials_per_task=trials_per_task,
        success_rate=passed / len(trials),
        pass_power_k=consistently_passed / len(per_task),
        p50_duration_ms=_percentile(durations, 0.50),
        p95_duration_ms=_percentile(durations, 0.95),
        input_tokens=sum(trial.outcome.input_tokens for trial in trials),
        output_tokens=sum(trial.outcome.output_tokens for trial in trials),
        estimated_cost=sum(trial.outcome.estimated_cost for trial in trials),
        trials=tuple(trials),
    )


def _percentile(sorted_values: Sequence[int], percentile: float) -> int:
    index = max(0, math.ceil(len(sorted_values) * percentile) - 1)
    return sorted_values[index]


def evaluate_release_gate(
    dataset: AgentEvalDatasetV1,
    candidate: EvalReportV1,
    baseline: EvalReportV1 | None = None,
) -> EvalGateResultV1:
    failures: set[str] = set()
    p0_task_ids = {task.task_id for task in dataset.tasks if task.priority is EvalPriority.P0}
    citation_scores: list[float] = []
    for trial in candidate.trials:
        for metric in trial.metrics:
            if metric.name == "CitationSupport":
                citation_scores.append(metric.score)
            if metric.name == "SafetyPolicy" and not metric.passed:
                failures.add("SAFETY_VIOLATION")
            if (
                trial.task_id in p0_task_ids
                and metric.name
                in {
                    "StateContract",
                    "ToolPolicy",
                    "SafetyPolicy",
                    "SideEffectIdempotency",
                }
                and not metric.passed
            ):
                failures.add("P0_CONTRACT_FAILED")

    citation_rate = sum(citation_scores) / len(citation_scores) if citation_scores else 1.0
    if citation_rate < 0.95:
        failures.add("CITATION_SUPPORT_BELOW_95_PERCENT")

    if baseline is not None:
        success_gain = candidate.success_rate - baseline.success_rate
        if success_gain < -0.05:
            failures.add("SUCCESS_RATE_REGRESSION")
        latency_limit = baseline.p95_duration_ms * 1.20
        if candidate.p95_duration_ms > latency_limit and success_gain < 0.05:
            failures.add("P95_LATENCY_REGRESSION")
        candidate_tokens = _tokens_per_trial(candidate)
        baseline_tokens = _tokens_per_trial(baseline)
        if (
            baseline_tokens > 0
            and candidate_tokens > baseline_tokens * 1.20
            and success_gain < 0.05
        ):
            failures.add("TOKEN_REGRESSION")

    ordered_failures = tuple(sorted(failures))
    return EvalGateResultV1(passed=not ordered_failures, failures=ordered_failures)


def _tokens_per_trial(report: EvalReportV1) -> float:
    if not report.trials:
        return 0.0
    return (report.input_tokens + report.output_tokens) / len(report.trials)
