"""Run ARC benchmark batches from a JSON plan.

Each run is delegated to ``main.py`` with MODEL_PROVIDER and MODEL_ID set in the
child process environment. Stages execute in order. Runs within a stage execute
sequentially by default, or concurrently when the stage has ``"parallel": true``.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, wait
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


DEFAULT_PLAN = "batch_runs.json"
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "logs" / "batch_orchestrator"


class PlanError(ValueError):
    """Raised when the batch plan is malformed."""


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "run"


def _load_plan(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise PlanError(f"Plan file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError("Batch plan must be a JSON object.")
    return data


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlanError(f"{label} must be an object.")
    return value


def _as_runs(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"{label} must be a non-empty array.")
    runs = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise PlanError(f"{label}[{index}] must be an object.")
        runs.append(item)
    return runs


def _stages(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if "stages" in plan:
        stages = plan["stages"]
        if not isinstance(stages, list) or not stages:
            raise PlanError("stages must be a non-empty array.")
        for index, stage in enumerate(stages, start=1):
            if not isinstance(stage, dict):
                raise PlanError(f"stages[{index}] must be an object.")
            _as_runs(stage.get("runs"), f"stages[{index}].runs")
        return stages
    if "runs" in plan:
        return [{"name": "default", "runs": _as_runs(plan["runs"], "runs")}]
    raise PlanError("Plan must contain either stages or runs.")


def _run_command(
    *,
    run: dict[str, Any],
    defaults: dict[str, Any],
    dry_run: bool,
    ordinal: int,
) -> tuple[list[str], dict[str, str], Path]:
    provider = str(run.get("provider") or defaults.get("provider") or "").strip()
    model = str(run.get("model") or defaults.get("model") or "").strip()
    if not provider:
        raise PlanError(f"Run {ordinal} is missing provider.")
    if not model:
        raise PlanError(f"Run {ordinal} is missing model.")

    workspace = str(run.get("workspace") or defaults.get("workspace") or "dev").strip()
    name = str(run.get("name") or f"{provider}-{model}-{workspace}").strip()
    spec = str(run.get("spec") or "").strip()

    cmd = [sys.executable, str(ROOT / "main.py"), "--workspace", workspace]
    if spec:
        cmd.extend(["--spec", spec])

    env = os.environ.copy()
    env["MODEL_PROVIDER"] = provider
    env["MODEL_ID"] = model
    for key, value in _as_mapping(defaults.get("env"), "defaults.env").items():
        env[str(key)] = str(value)
    for key, value in _as_mapping(run.get("env"), f"run {ordinal}.env").items():
        env[str(key)] = str(value)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"{ordinal:03d}_{_slug(name)}"
    if dry_run:
        suffix = f"dry_{suffix}"
    log_path = OUTPUT_DIR / f"{timestamp}_{suffix}.log"
    return cmd, env, log_path


def _execute_run(
    *,
    run: dict[str, Any],
    defaults: dict[str, Any],
    dry_run: bool,
    ordinal: int,
) -> int:
    cmd, env, log_path = _run_command(
        run=run,
        defaults=defaults,
        dry_run=dry_run,
        ordinal=ordinal,
    )
    model_label = f"{env['MODEL_PROVIDER']}/{env['MODEL_ID']}"
    spec_label = f" spec={run['spec']!r}" if run.get("spec") else ""
    print(f"[{ordinal:03d}] start {model_label}{spec_label} -> {log_path}")
    if dry_run:
        print(f"[{ordinal:03d}] dry-run command: {' '.join(cmd)}")
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as stream:
        stream.write(f"command: {' '.join(cmd)}\n")
        stream.write(f"MODEL_PROVIDER={env['MODEL_PROVIDER']}\n")
        stream.write(f"MODEL_ID={env['MODEL_ID']}\n\n")
        stream.flush()
        completed = subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    print(f"[{ordinal:03d}] done exit={completed.returncode} log={log_path}")
    return completed.returncode


def _run_parallel_stage(
    *,
    runs: list[dict[str, Any]],
    defaults: dict[str, Any],
    dry_run: bool,
    max_parallel: int,
    next_ordinal: int,
    stop_on_failure: bool,
) -> tuple[int, list[int]]:
    import concurrent.futures

    failures: list[int] = []
    pending_runs = list(enumerate(runs, start=next_ordinal))
    active: dict[Future[int], int] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
        while pending_runs or active:
            while pending_runs and len(active) < max_parallel:
                ordinal, run = pending_runs.pop(0)
                future = executor.submit(
                    _execute_run,
                    run=run,
                    defaults=defaults,
                    dry_run=dry_run,
                    ordinal=ordinal,
                )
                active[future] = ordinal

            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                ordinal = active.pop(future)
                code = future.result()
                if code != 0:
                    failures.append(ordinal)
                    if stop_on_failure:
                        pending_runs.clear()
        return next_ordinal + len(runs), failures


def run_plan(plan: dict[str, Any], *, dry_run: bool) -> int:
    defaults = _as_mapping(plan.get("defaults"), "defaults")
    stop_on_failure = bool(plan.get("stop_on_failure", True))
    failures: list[int] = []
    ordinal = 1

    for stage_index, stage in enumerate(_stages(plan), start=1):
        name = str(stage.get("name") or f"stage-{stage_index}")
        runs = _as_runs(stage.get("runs"), f"stages[{stage_index}].runs")
        parallel = bool(stage.get("parallel", False))
        max_parallel = int(stage.get("max_parallel") or plan.get("max_parallel") or len(runs))
        max_parallel = max(1, min(max_parallel, len(runs)))

        mode = f"parallel x{max_parallel}" if parallel else "sequential"
        print(f"\nStage {stage_index}: {name} ({mode}, {len(runs)} run(s))")
        if parallel:
            ordinal, stage_failures = _run_parallel_stage(
                runs=runs,
                defaults=defaults,
                dry_run=dry_run,
                max_parallel=max_parallel,
                next_ordinal=ordinal,
                stop_on_failure=stop_on_failure,
            )
            failures.extend(stage_failures)
        else:
            for run in runs:
                code = _execute_run(
                    run=run,
                    defaults=defaults,
                    dry_run=dry_run,
                    ordinal=ordinal,
                )
                if code != 0:
                    failures.append(ordinal)
                    if stop_on_failure:
                        print("Stopping after failure because stop_on_failure=true.")
                        return 1
                ordinal += 1

        if failures and stop_on_failure:
            print("Stopping before next stage because stop_on_failure=true.")
            return 1

    if failures:
        print(f"\nCompleted with failed run(s): {', '.join(map(str, failures))}")
        return 1
    print("\nCompleted all planned runs.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ARC batches from JSON.")
    parser.add_argument(
        "plan",
        nargs="?",
        default=DEFAULT_PLAN,
        help=f"Path to batch plan JSON (default: {DEFAULT_PLAN})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commands without running them.",
    )
    args = parser.parse_args()

    try:
        plan = _load_plan(Path(args.plan))
        exit_code = run_plan(plan, dry_run=args.dry_run)
    except PlanError as exc:
        print(f"Plan error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
