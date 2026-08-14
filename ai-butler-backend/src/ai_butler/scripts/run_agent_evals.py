from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from importlib import import_module
from pathlib import Path
from typing import cast

from ai_butler.config import get_settings
from ai_butler.evaluation.dataset import load_eval_dataset, select_tasks
from ai_butler.evaluation.runner import AgentEvalRunner, evaluate_release_gate, run_trials
from ai_butler.evaluation.schema import EvalReportV1, EvalSuite


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local-only Agent evaluations")
    parser.add_argument("--suite", choices=[suite.value for suite in EvalSuite], default="live")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--baseline", type=Path)
    return parser.parse_args(argv)


def _load_runner(factory_path: str) -> AgentEvalRunner:
    module_name, separator, attribute_name = factory_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("EVAL_RUNNER_FACTORY must use module.path:factory format")
    candidate: object = getattr(import_module(module_name), attribute_name)
    if not callable(candidate):
        raise TypeError("configured eval runner factory is not callable")
    factory = cast(Callable[[], object], candidate)
    runner = factory()
    if not isinstance(getattr(runner, "model_name", None), str):
        raise TypeError("eval runner must expose a string model_name")
    if not callable(getattr(runner, "run", None)):
        raise TypeError("eval runner must expose an async run(task) method")
    return cast(AgentEvalRunner, runner)


async def _run(argv: Sequence[str] | None = None) -> Path:
    args = _parse_args(argv)
    settings = get_settings()
    if not settings.eval_runner_factory:
        raise RuntimeError(
            "live Agent evaluation is unavailable until EVAL_RUNNER_FACTORY is configured"
        )
    suite = EvalSuite(args.suite)
    dataset = load_eval_dataset()
    tasks = select_tasks(dataset, suite)
    runner = _load_runner(settings.eval_runner_factory)
    report = await run_trials(dataset, tasks, runner, args.trials)
    baseline = None
    if args.baseline is not None:
        baseline = EvalReportV1.model_validate_json(args.baseline.read_text(encoding="utf-8"))
    gate = evaluate_release_gate(dataset, report, baseline)
    output_path = settings.eval_results_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if not gate.passed:
        raise RuntimeError(f"Agent evaluation release gate failed: {','.join(gate.failures)}")
    return output_path


def main() -> None:
    try:
        output_path = asyncio.run(_run())
    except (ImportError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"saved synthetic Agent evaluation report: {output_path}")


if __name__ == "__main__":
    main()
