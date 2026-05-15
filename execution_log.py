"""Draft JSON execution logging for ARC sample-agent runs."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any


LOG_DIR = Path("logs")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class ExecutionLog:
    """Small append/update logger that rewrites one JSON file as the run evolves."""

    def __init__(
        self,
        *,
        mode: str,
        llm_provider: str,
        llm_model: str,
        workspace: str | None = None,
    ) -> None:
        if mode not in {"single", "batch"}:
            raise ValueError(f"unsupported log mode: {mode!r}")

        LOG_DIR.mkdir(exist_ok=True)
        started_at = _now()
        prefix = "t" if mode == "single" else "b"
        today = datetime.now().strftime("%Y%m%d")
        seq = self._next_sequence(prefix, today)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.path = LOG_DIR / f"{prefix}_{seq:03d}_{timestamp}.json"
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "mode": mode,
            "sequence_date": today,
            "sequence_number": seq,
            "started_at": started_at,
            "completed_at": None,
            "workspace": workspace,
            "llm": {
                "provider": llm_provider,
                "model": llm_model,
            },
            "session": None,
            "tasks": [],
            "summary": {
                "task_count": 0,
                "completed_count": 0,
                "scores": [],
            },
            "errors": [],
        }
        self.save()

    @staticmethod
    def _next_sequence(prefix: str, today: str) -> int:
        pattern = f"{prefix}_*_{today}_*.json"
        numbers = []
        for path in LOG_DIR.glob(pattern):
            parts = path.stem.split("_", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                numbers.append(int(parts[1]))
        return max(numbers, default=0) + 1

    def set_session(self, session: Any) -> None:
        self.data["session"] = _jsonable(session)
        self.save()

    def start_task(self, task: Any) -> int:
        task_entry = {
            "task": _jsonable(task),
            "started_at": _now(),
            "completed_at": None,
            "bootstrap": [],
            "steps": [],
            "respond": None,
            "completion": None,
            "errors": [],
        }
        self.data["tasks"].append(task_entry)
        self.data["summary"]["task_count"] = len(self.data["tasks"])
        self.save()
        return len(self.data["tasks"]) - 1

    def add_bootstrap(self, task_index: int, label: str, result: str) -> None:
        self.data["tasks"][task_index]["bootstrap"].append({
            "label": label,
            "result": result,
            "logged_at": _now(),
        })
        self.save()

    def add_step(
        self,
        task_index: int,
        *,
        step_number: int,
        current_state: str,
        plan: list[str],
        function_type: str,
        function_args: Any,
        llm_duration_ms: int,
        usage: Any = None,
    ) -> int:
        step = {
            "step_number": step_number,
            "started_at": _now(),
            "completed_at": None,
            "current_state": current_state,
            "plan": plan,
            "function": {
                "type": function_type,
                "args": _jsonable(function_args),
            },
            "llm": {
                "duration_ms": llm_duration_ms,
                "usage": _jsonable(usage),
            },
            "platform_result": None,
            "error": None,
        }
        self.data["tasks"][task_index]["steps"].append(step)
        self.save()
        return len(self.data["tasks"][task_index]["steps"]) - 1

    def finish_step(
        self,
        task_index: int,
        step_index: int,
        *,
        platform_result: Any = None,
        error: Any = None,
    ) -> None:
        step = self.data["tasks"][task_index]["steps"][step_index]
        step["completed_at"] = _now()
        step["platform_result"] = _jsonable(platform_result)
        step["error"] = _jsonable(error)
        self.save()

    def set_respond(self, task_index: int, respond: Any) -> None:
        self.data["tasks"][task_index]["respond"] = _jsonable(respond)
        self.save()

    def finish_task(self, task_index: int, completion: Any = None, error: Any = None) -> None:
        task = self.data["tasks"][task_index]
        task["completed_at"] = _now()
        if completion is not None:
            task["completion"] = _jsonable(completion)
        if error is not None:
            task["errors"].append({"logged_at": _now(), "error": str(error)})
        self.data["summary"]["completed_count"] = sum(
            1 for item in self.data["tasks"] if item["completed_at"]
        )
        eval_info = getattr(completion, "eval", None) if completion is not None else None
        if eval_info is not None:
            self.data["summary"]["scores"].append({
                "spec_id": task["task"].get("spec_id"),
                "score": getattr(eval_info, "score", None),
            })
        self.save()

    def add_error(self, error: Any) -> None:
        self.data["errors"].append({"logged_at": _now(), "error": str(error)})
        self.save()

    def finish_run(self) -> None:
        self.data["completed_at"] = _now()
        self.save()

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
