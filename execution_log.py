"""Draft JSON execution logging for ARC sample-agent runs."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any


LOG_DIR = Path("logs")
COST_CONFIG_PATH = Path("cost.json")
HARNESS_GUIDANCE_PATH = Path("ARC_AGENT.md")
LOG_NAME_RE = re.compile(r"^(?P<prefix>[tb])_(?P<date>\d{8})_\d{4}_(?P<seq>\d{3})\.json$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _elapsed_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at) if completed_at else datetime.now().astimezone()
    except ValueError:
        return None
    return round((end - start).total_seconds(), 3)


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


def _text_size(value: Any) -> int:
    return len(json.dumps(_jsonable(value), ensure_ascii=False, default=str))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _result_summary(value: Any) -> dict[str, Any]:
    data = _jsonable(value)
    summary: dict[str, Any] = {"size_chars": _text_size(data)}
    if isinstance(data, dict):
        summary["keys"] = list(data.keys())
        if "path" in data:
            summary["path"] = data["path"]
        if "total" in data:
            summary["total"] = data["total"]
        if "matches" in data and isinstance(data["matches"], list):
            summary["match_count"] = len(data["matches"])
        if "content" in data and isinstance(data["content"], str):
            summary["content_chars"] = len(data["content"])
            summary["content_hash"] = _hash_text(data["content"])
        if "current_user" in data or "role" in data:
            summary["current_user"] = data.get("current_user")
            summary["role"] = data.get("role")
    elif isinstance(data, str):
        summary["text_chars"] = len(data)
        summary["text_hash"] = _hash_text(data)
    return summary


def _extract_bootstrap_path(result: str) -> str | None:
    if result.startswith("path: "):
        return result.splitlines()[0].removeprefix("path: ").strip()
    return None


def _compact_bootstrap_result(label: str, result: str) -> dict[str, Any]:
    summary = _result_summary(result)
    compact: dict[str, Any] = {
        "label": label,
        "summary": summary,
        "logged_at": _now(),
    }
    if label == "system":
        try:
            compact["result"] = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            compact["result"] = {"error": result}
    else:
        path = _extract_bootstrap_path(result)
        if path:
            compact["path"] = path
        compact["content_omitted"] = True
    return compact


def _compact_platform_result(value: Any) -> Any:
    data = _jsonable(value)
    if isinstance(data, list):
        return [_compact_platform_result(item) for item in data]
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        compact = dict(data)
        content = compact.pop("content")
        compact["content_omitted"] = True
        compact["content_chars"] = len(content)
        compact["content_hash"] = _hash_text(content)
        return {key: _compact_platform_result(item) for key, item in compact.items()}
    if isinstance(data, dict):
        return {key: _compact_platform_result(item) for key, item in data.items()}
    return data


def _token_accounting(usage: Any) -> dict[str, int]:
    data = _jsonable(usage)
    if not isinstance(data, dict):
        return {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "billable_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }

    input_tokens = data.get("input_tokens", data.get("prompt_tokens", 0)) or 0
    output_tokens = data.get("output_tokens", data.get("completion_tokens", 0)) or 0
    total_tokens = data.get("total_tokens", input_tokens + output_tokens) or 0

    input_details = data.get("input_tokens_details") or data.get("prompt_tokens_details") or {}
    output_details = data.get("output_tokens_details") or data.get("completion_tokens_details") or {}
    cached_input_tokens = input_details.get("cached_tokens", 0) or 0
    reasoning_tokens = output_details.get("reasoning_tokens", 0) or 0

    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "billable_input_tokens": max(input_tokens - cached_input_tokens, 0),
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _load_cost_config() -> dict[str, Any] | None:
    if not COST_CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(COST_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_harness_version() -> str | None:
    try:
        text = HARNESS_GUIDANCE_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^Harness guidance version:\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _score_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _estimate_cost(
    *,
    provider: str,
    model: str,
    token_accounting: dict[str, int],
    cost_config: dict[str, Any] | None,
) -> dict[str, Any]:
    if not cost_config:
        return {
            "estimated": False,
            "reason": f"{COST_CONFIG_PATH} not found",
        }

    model_entry = (
        cost_config
        .get("providers", {})
        .get(provider, {})
        .get("models", {})
        .get(model)
    )
    if isinstance(model_entry, str):
        pricing = cost_config.get("prices", {}).get(model_entry)
        price_id = model_entry
    else:
        pricing = model_entry
        price_id = None
    if not isinstance(pricing, dict):
        return {
            "estimated": False,
            "reason": f"pricing missing for {provider}/{model}",
        }

    input_per_1m = pricing.get("input_per_1m")
    cached_input_per_1m = pricing.get("cached_input_per_1m", input_per_1m)
    output_per_1m = pricing.get("output_per_1m")
    if input_per_1m is None or output_per_1m is None:
        return {
            "estimated": False,
            "reason": f"incomplete pricing for {provider}/{model}",
        }

    billable_input = token_accounting["billable_input_tokens"]
    cached_input = token_accounting["cached_input_tokens"]
    output = token_accounting["output_tokens"]
    estimated_usd = (
        billable_input / 1_000_000 * input_per_1m
        + cached_input / 1_000_000 * cached_input_per_1m
        + output / 1_000_000 * output_per_1m
    )

    return {
        "estimated": True,
        "estimated_usd": round(estimated_usd, 8),
        "currency": pricing.get("currency", cost_config.get("currency", "USD")),
        "pricing_source": str(COST_CONFIG_PATH),
        "price_id": price_id,
        "pricing_updated_at": cost_config.get("updated_at"),
        "input_per_1m": input_per_1m,
        "cached_input_per_1m": cached_input_per_1m,
        "output_per_1m": output_per_1m,
    }


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
        today = datetime.now().strftime("%d%m%Y")
        now = datetime.now()
        timestamp = now.strftime("%d%m%Y_%H%M")
        seq = self._next_sequence(prefix, today)
        while True:
            path = LOG_DIR / f"{prefix}_{timestamp}_{seq:03d}.json"
            summary_path = (
                LOG_DIR / f"{prefix}_{timestamp}_{seq:03d}_summary.json"
                if mode == "batch"
                else None
            )
            if summary_path is not None and summary_path.exists():
                seq += 1
                continue
            try:
                path.touch(exist_ok=False)
                break
            except FileExistsError:
                seq += 1
                continue

        self.path = path
        self.summary_path = summary_path
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._cost_config = _load_cost_config()
        self._harness_version = _load_harness_version()
        self.data: dict[str, Any] = {
            "schema_version": 2,
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
            "harness_version": self._harness_version,
            "cost_config": {
                "path": str(COST_CONFIG_PATH),
                "loaded": self._cost_config is not None,
                "updated_at": (
                    self._cost_config.get("updated_at")
                    if isinstance(self._cost_config, dict)
                    else None
                ),
            },
            "agent": {
                "max_steps": None,
                "system_prompt_chars": None,
            },
            "session": None,
            "tasks": [],
            "summary": {
                "task_count": 0,
                "completed_count": 0,
                "total_respond_attempts": 0,
                "total_respond_rejections": 0,
                "total_steps": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_billable_input_tokens": 0,
                "total_output_tokens": 0,
                "total_reasoning_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": None,
                "scores": [],
            },
            "errors": [],
        }
        self.save()

    @staticmethod
    def _next_sequence(prefix: str, today: str) -> int:
        numbers = []
        for path in LOG_DIR.glob(f"{prefix}_*.json"):
            match = LOG_NAME_RE.match(path.name)
            if match and match.group("date") == today:
                numbers.append(int(match.group("seq")))
        return max(numbers, default=0) + 1

    def set_session(self, session: Any) -> None:
        self.data["session"] = _jsonable(session)
        self.save()

    def set_agent_config(self, *, max_steps: int, system_prompt: str) -> None:
        self.data["agent"] = {
            "max_steps": max_steps,
            "system_prompt_chars": len(system_prompt),
            "system_prompt_hash": _hash_text(system_prompt),
        }
        self.save()

    def start_task(self, task: Any) -> int:
        task_data = _jsonable(task)
        task_entry = {
            "task": {
                "spec_id": task_data.get("spec_id"),
                "task_id": task_data.get("task_id"),
                "task_text": task_data.get("task_text"),
                "role": task_data.get("role"),
            },
            "effective_context": None,
            "started_at": _now(),
            "completed_at": None,
            "bootstrap": [],
            "steps": [],
            "usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "billable_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": None,
            },
            "respond": {
                "final": None,
                "accepted": False,
                "attempt_count": 0,
                "rejected_count": 0,
                "accepted_attempt_number": None,
                "rejections": [],
            },
            "completion": None,
            "errors": [],
        }
        self.data["tasks"].append(task_entry)
        self.data["summary"]["task_count"] = len(self.data["tasks"])
        self.save()
        return len(self.data["tasks"]) - 1

    def add_bootstrap(self, task_index: int, label: str, result: str) -> None:
        if label == "system":
            try:
                system_context = json.loads(result)
            except (TypeError, json.JSONDecodeError):
                system_context = None
            if isinstance(system_context, dict):
                self.data["tasks"][task_index]["effective_context"] = {
                    "current_user": system_context.get("current_user"),
                    "role": system_context.get("role"),
                    "today": system_context.get("today"),
                    "is_public": system_context.get("is_public"),
                }
        self.data["tasks"][task_index]["bootstrap"].append(
            _compact_bootstrap_result(label, result)
        )
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
        task_completed: bool = False,
        respond_attempt_number: int | None = None,
    ) -> int:
        usage_data = _jsonable(usage)
        token_accounting = _token_accounting(usage_data)
        cost = _estimate_cost(
            provider=self._llm_provider,
            model=self._llm_model,
            token_accounting=token_accounting,
            cost_config=self._cost_config,
        )
        cost_usd = cost.get("estimated_usd") if cost.get("estimated") else None
        step = {
            "step": step_number,
            "state": current_state,
            "action_reason": plan[0] if plan else None,
            "done": task_completed,
            "respond_try": respond_attempt_number,
            "call": {
                "type": function_type,
                "args": _jsonable(function_args),
            },
            "llm": {
                "ms": llm_duration_ms,
            },
            "result": None,
            "result_summary": None,
            "api_ms": None,
            "error": None,
        }
        self.data["tasks"][task_index]["steps"].append(step)
        task_usage = self.data["tasks"][task_index]["usage"]
        task_usage["input_tokens"] += token_accounting["input_tokens"]
        task_usage["cached_input_tokens"] += token_accounting["cached_input_tokens"]
        task_usage["billable_input_tokens"] += token_accounting["billable_input_tokens"]
        task_usage["output_tokens"] += token_accounting["output_tokens"]
        task_usage["reasoning_tokens"] += token_accounting["reasoning_tokens"]
        task_usage["total_tokens"] += token_accounting["total_tokens"]
        self.data["summary"]["total_steps"] += 1
        self.data["summary"]["total_input_tokens"] += token_accounting["input_tokens"]
        self.data["summary"]["total_cached_input_tokens"] += token_accounting["cached_input_tokens"]
        self.data["summary"]["total_billable_input_tokens"] += token_accounting["billable_input_tokens"]
        self.data["summary"]["total_output_tokens"] += token_accounting["output_tokens"]
        self.data["summary"]["total_reasoning_tokens"] += token_accounting["reasoning_tokens"]
        self.data["summary"]["total_tokens"] += token_accounting["total_tokens"]
        if cost_usd is not None:
            task_cost = task_usage["estimated_cost_usd"] or 0
            task_usage["estimated_cost_usd"] = round(task_cost + cost_usd, 8)
            current_cost = self.data["summary"]["estimated_cost_usd"] or 0
            self.data["summary"]["estimated_cost_usd"] = round(
                current_cost + cost_usd,
                8,
            )
        self.save()
        return len(self.data["tasks"][task_index]["steps"]) - 1

    def finish_step(
        self,
        task_index: int,
        step_index: int,
        *,
        platform_result: Any = None,
        platform_duration_ms: int | None = None,
        error: Any = None,
    ) -> None:
        step = self.data["tasks"][task_index]["steps"][step_index]
        step["result"] = _compact_platform_result(platform_result)
        step["result_summary"] = (
            _result_summary(platform_result) if platform_result is not None else None
        )
        step["api_ms"] = platform_duration_ms
        step["error"] = _jsonable(error)
        self.save()

    def set_respond(
        self,
        task_index: int,
        respond: Any,
        *,
        accepted: bool,
        platform_error: Any = None,
    ) -> None:
        respond_log = self.data["tasks"][task_index]["respond"]
        attempt_number = respond_log["attempt_count"] + 1
        platform_error_data = _jsonable(platform_error)
        respond_log["attempt_count"] = attempt_number
        respond_log["accepted"] = respond_log["accepted"] or accepted
        if accepted:
            respond_log["accepted_attempt_number"] = attempt_number
            respond_log["final"] = _jsonable(respond)
        else:
            respond_log["rejected_count"] += 1
            respond_log["rejections"].append({
                "attempt": attempt_number,
                "error": platform_error_data,
            })
            self.data["summary"]["total_respond_rejections"] += 1
        self.data["summary"]["total_respond_attempts"] += 1
        self.save()

    def finish_task(self, task_index: int, completion: Any = None, error: Any = None) -> None:
        task = self.data["tasks"][task_index]
        task["completed_at"] = _now()
        if completion is not None:
            completion_data = _jsonable(completion)
            task["completion"] = {
                "status": completion_data.get("status"),
            }
            eval_info = getattr(completion, "eval", None)
            if eval_info is not None:
                task["eval_summary"] = {
                    "score": getattr(eval_info, "score", None),
                    "logs": getattr(eval_info, "logs", None),
                }
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
        self.data["errors"].append({"logged_at": _now(), "error": _jsonable(error)})
        self.save()

    def add_task_error(self, task_index: int, error: Any) -> None:
        self.data["tasks"][task_index]["errors"].append({
            "logged_at": _now(),
            "error": _jsonable(error),
        })
        self.save()

    def finish_run(self) -> None:
        self.data["completed_at"] = _now()
        self.save()

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        if self.summary_path is not None:
            self.summary_path.write_text(
                json.dumps(self._summary_data(), indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )

    def _summary_data(self) -> dict[str, Any]:
        tasks = []
        score_total = 0.0
        for item in self.data["tasks"]:
            task = item.get("task", {})
            respond = item.get("respond", {})
            error_count = len(item.get("errors", []))
            rejected_count = respond.get("rejected_count", 0) or 0
            eval_summary = item.get("eval_summary") or {}
            score_total += _score_value(eval_summary.get("score"))
            tasks.append({
                "spec_id": task.get("spec_id"),
                "attempts": {
                    "respond": respond.get("attempt_count", 0),
                    "errored_retried": error_count + rejected_count,
                },
            })

        summary = self.data.get("summary", {})
        task_count = summary.get("task_count", 0) or 0
        completed_count = summary.get("completed_count", 0) or 0
        completion_pct = round(completed_count / task_count * 100, 2) if task_count else 0
        scores_pct = round(score_total / task_count * 100, 2) if task_count else 0
        return {
            "schema_version": 1,
            "type": "batch_summary",
            "harness_version": self.data.get("harness_version"),
            "model": self.data.get("llm", {}).get("model"),
            "tasks_completion_pct": completion_pct,
            "tasks_scores_pct": scores_pct,
            "total_time_sec": _elapsed_seconds(
                self.data.get("started_at"),
                self.data.get("completed_at"),
            ),
            "tokens": {
                "input_tokens": summary.get("total_input_tokens", 0),
                "cached_input_tokens": summary.get("total_cached_input_tokens", 0),
                "output_tokens": summary.get("total_output_tokens", 0),
            },
            "tasks": tasks,
        }
