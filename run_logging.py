"""Small run logger for ARC sample-agent experiments.

The logger is intentionally local-only: it records task metadata, agent steps,
scores, and terminal errors to JSON so batch and single-task runs can be
compared without changing the ARC platform interaction model.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

WRITE_ACTION_TYPES = {
    "equipment_update",
    "employee_update",
    "material_reorder",
    "notif_create",
    "notif_update",
    "wo_create",
    "wo_update",
    "operation_add",
    "operation_update",
    "wiki_update",
    "wiki_section_insert",
}


class RunLogger:
    """Append-only-ish JSON logger for one main.py invocation."""

    def __init__(
        self,
        *,
        mode: str,
        root: str | Path = "logs",
        cost_path: str | Path = "cost.json",
        version_path: str | Path = "harness_versions.md",
        model: str | None = None,
    ) -> None:
        if mode not in {"batch", "task"}:
            raise ValueError(f"Unsupported log mode: {mode!r}")
        self.mode = mode
        self.started_at = datetime.now().astimezone()
        self.path = self._next_path(
            Path(root),
            mode,
            self.started_at,
            model_suffix=_model_log_suffix(model),
        )
        self.cost_table = _load_cost_table(Path(cost_path))
        self.harness_version = load_harness_version(version_path)
        self.data: dict[str, Any] = {
            "log_schema_version": 3,
            "mode": mode,
            "harness_version": self.harness_version,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": None,
            "status": "running",
            "metadata": {},
            "bootstrap_manifest": {},
            "usage": _empty_usage(),
            "session": {},
            "tasks": [],
            "summary": {},
            "errors": [],
        }
        self._current_task: dict[str, Any] | None = None
        self.flush()

    @staticmethod
    def _next_path(root: Path, mode: str, stamp: datetime, *, model_suffix: str) -> Path:
        folder = root / ("batch" if mode == "batch" else "tasks")
        folder.mkdir(parents=True, exist_ok=True)
        prefix = "b" if mode == "batch" else "t"
        stem = f"{prefix}_{stamp.strftime('%d%m%y_%H%M')}_{model_suffix}"
        candidate = folder / f"{stem}.json"
        if not candidate.exists():
            return candidate
        for idx in range(2, 1000):
            candidate = folder / f"{stem}_{idx:03d}.json"
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Could not allocate log filename for {stem}")

    def set_metadata(self, **metadata: Any) -> None:
        self.data["metadata"].update(_jsonable(metadata))
        self.flush()

    def set_session(self, **session: Any) -> None:
        self.data["session"].update(_jsonable(session))
        self.flush()

    def start_task(self, task: Any) -> None:
        entry = {
            "num": getattr(task, "num", None),
            "spec_id": getattr(task, "spec_id", None),
            "task_id": getattr(task, "task_id", None),
            "task_text": getattr(task, "task_text", None),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ended_at": None,
            "status": "running",
            "bootstrap": [],
            "steps": [],
            "usage": _empty_usage(),
            "result": {},
            "errors": [],
        }
        self.data["tasks"].append(entry)
        self._current_task = entry
        self.flush()

    def record_bootstrap(self, label: str, text: str) -> None:
        task = self._require_task()
        entry = _compact_bootstrap(label, text)
        if label == "system" or entry.get("ok") is False:
            task["bootstrap"].append(entry)
        else:
            _merge_bootstrap_manifest(self.data["bootstrap_manifest"], entry)
        self.flush()

    def record_step(self, **step: Any) -> None:
        task = self._require_task()
        step = _jsonable(step)
        step_usage = _step_usage(
            step,
            provider=self.data["metadata"].get("provider"),
            model=self.data["metadata"].get("model"),
            cost_table=self.cost_table,
        )
        task["steps"].append(_compact_step(step))
        _add_usage(task["usage"], step_usage)
        _add_usage(self.data["usage"], step_usage)
        self.flush()

    def record_task_error(self, error: BaseException | str) -> None:
        task = self._require_task()
        task["status"] = "error"
        task["errors"].append(_error_payload(error))
        self.flush()

    def finish_task(self, *, status: str = "completed", **result: Any) -> None:
        task = self._require_task()
        if task["status"] == "running":
            task["status"] = status
        task["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        task["result"].update(_jsonable(result))
        task["analysis"] = _task_analysis(task)
        self._refresh_usage()
        self._current_task = None
        self.flush()

    def record_error(self, error: BaseException | str) -> None:
        self.data["errors"].append(_error_payload(error))
        self.data["status"] = "error"
        self.flush()

    def finish(self, *, status: str = "completed", **summary: Any) -> None:
        if self._current_task is not None and self._current_task["status"] == "running":
            self.finish_task(status="error", error="run ended before task completed")
        if self.data["status"] == "running":
            self.data["status"] = status
        self.data["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.data["summary"].update(_jsonable(summary))
        self._refresh_usage()
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _require_task(self) -> dict[str, Any]:
        if self._current_task is None:
            raise RuntimeError("No active task in run logger")
        return self._current_task

    def _refresh_usage(self) -> None:
        run_usage = _empty_usage()
        for task in self.data["tasks"]:
            usage = task.get("usage")
            if isinstance(usage, dict):
                _add_usage(run_usage, usage)
        self.data["usage"] = run_usage


def _error_payload(error: BaseException | str) -> dict[str, str]:
    if isinstance(error, BaseException):
        return {"type": type(error).__name__, "message": str(error)}
    return {"type": "error", "message": error}


def _truncate(value: Any, limit: int = 12000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [truncated {len(value) - limit} chars]"
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _truncate(value)
    if isinstance(value, BaseException):
        return _error_payload(value)
    return _truncate(str(value))


def _compact_bootstrap(label: str, text: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"label": label}
    if text.startswith("error:"):
        entry.update({"ok": False, "error": _truncate(text, 300)})
        return entry
    entry["ok"] = True
    entry["chars"] = len(text)
    if label == "system":
        try:
            entry["data"] = json.loads(text)
        except json.JSONDecodeError:
            entry["text"] = _truncate(text, 500)
    elif label == "wiki_tree":
        paths = []
        for line in text.splitlines():
            cleaned = line.strip().strip("`- |")
            if cleaned:
                paths.append(cleaned)
        entry["items"] = paths[:80]
        if len(paths) > 80:
            entry["truncated_items"] = len(paths) - 80
    else:
        entry["loaded"] = True
    return entry


def _merge_bootstrap_manifest(manifest: dict[str, Any], entry: dict[str, Any]) -> None:
    label = entry.get("label")
    if not isinstance(label, str):
        return
    if label == "wiki_tree":
        target = manifest.setdefault("wiki_tree", {})
        target["ok"] = entry.get("ok")
        target["chars"] = entry.get("chars")
        target["item_count"] = len(entry.get("items") or []) + int(entry.get("truncated_items") or 0)
        if entry.get("truncated_items"):
            target["truncated_items"] = entry.get("truncated_items")
        target["seen_count"] = int(target.get("seen_count") or 0) + 1
        return
    if label.startswith("wiki_load:"):
        docs = manifest.setdefault("wiki_loads", {})
        doc = docs.setdefault(label.removeprefix("wiki_load:"), {})
        doc["ok"] = entry.get("ok")
        doc["chars"] = entry.get("chars")
        doc["seen_count"] = int(doc.get("seen_count") or 0) + 1
        return
    other = manifest.setdefault("other", {})
    target = other.setdefault(label, {})
    target["ok"] = entry.get("ok")
    target["chars"] = entry.get("chars")
    target["seen_count"] = int(target.get("seen_count") or 0) + 1


def _compact_step(step: dict[str, Any]) -> dict[str, Any]:
    fn_type = step.get("function_type")
    compact: dict[str, Any] = {
        "step": step.get("step"),
        "fn": fn_type,
        "ms": step.get("elapsed_ms"),
    }
    if fn_type in WRITE_ACTION_TYPES:
        compact["is_write"] = True
    raw_result = step.get("result")
    if (
        step.get("harness_block")
        or (
            isinstance(raw_result, dict)
            and (
                raw_result.get("harness_blocked")
                or raw_result.get("duplicate_successful_write_blocked")
            )
        )
    ):
        compact["is_blocked"] = True
    function_args = step.get("function_args")
    if fn_type == "respond" and isinstance(function_args, dict) and function_args.get("outcome"):
        compact["outcome"] = function_args.get("outcome")
    if step.get("task_type") and step.get("task_type") != "unknown":
        compact["task_type"] = step.get("task_type")
    plan = step.get("plan")
    if isinstance(plan, list) and plan:
        compact["plan"] = _truncate(str(plan[0]), 240)
    if step.get("ready_to_respond"):
        compact["ready"] = True
    gate_checks = step.get("gate_checks")
    if isinstance(gate_checks, list) and gate_checks:
        compact["gates"] = [_truncate(str(item), 160) for item in gate_checks[:4]]
    if function_args:
        compact["args"] = _compact_value(function_args)
    if step.get("api_error"):
        compact["error"] = _compact_value(step["api_error"])
    elif step.get("error"):
        compact["error"] = _truncate(str(step["error"]), 400)
    elif step.get("result"):
        compact["result"] = _compact_result(step["result"])
    harness_state = step.get("harness_state")
    if isinstance(harness_state, dict):
        refs = {}
        if harness_state.get("read_refs"):
            refs["read"] = harness_state["read_refs"][-8:]
        if harness_state.get("write_refs"):
            refs["write"] = harness_state["write_refs"][-8:]
        if harness_state.get("support_refs"):
            refs["support"] = harness_state["support_refs"][-8:]
        if refs:
            compact["refs"] = refs
        if harness_state.get("task_scope"):
            compact["task_scope"] = harness_state["task_scope"]
        if harness_state.get("writes_completed"):
            compact["writes_completed"] = harness_state["writes_completed"]
        open_ambiguities = harness_state.get("open_ambiguities")
        if isinstance(open_ambiguities, list):
            compact_ambiguities = [
                _compact_ambiguity(item)
                for item in open_ambiguities[-4:]
                if isinstance(item, dict) and item.get("status") == "open"
            ]
            if compact_ambiguities:
                compact["open_ambiguities"] = compact_ambiguities
        candidate_ledger = harness_state.get("candidate_ledger")
        if isinstance(candidate_ledger, list) and candidate_ledger:
            compact["candidate_ledger"] = [
                _compact_candidate_ledger(item)
                for item in candidate_ledger[-3:]
                if isinstance(item, dict)
            ]
        if harness_state.get("ambiguity_nudges"):
            compact["ambiguity_nudges"] = harness_state["ambiguity_nudges"]
    return {k: v for k, v in compact.items() if v not in (None, {}, [], "")}


def _compact_ambiguity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in ("entity_type", "count", "candidate_ids", "source_action")
        if value.get(key) not in (None, {}, [], "")
    }


def _compact_candidate_ledger(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in ("entity_type", "count", "ids", "source_action")
        if value.get(key) not in (None, {}, [], "")
    }


def _task_analysis(task: dict[str, Any]) -> dict[str, Any]:
    steps = task.get("steps") if isinstance(task.get("steps"), list) else []
    final_outcome = None
    write_count = 0
    blocked_count = 0
    step_error_count = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("is_write"):
            write_count += 1
        if step.get("is_blocked"):
            blocked_count += 1
        if step.get("error"):
            step_error_count += 1
        if step.get("fn") == "respond" and step.get("outcome"):
            final_outcome = step.get("outcome")

    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    analysis = {
        "final_outcome": final_outcome,
        "score": result.get("score"),
        "step_count": len(steps),
        "write_count": write_count,
        "blocked_count": blocked_count,
        "error_count": step_error_count + len(task.get("errors") or []),
    }
    return {k: v for k, v in analysis.items() if v not in (None, {}, [], "")}


def _compact_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return _truncate(str(value), 500)
    if value.get("harness_blocked"):
        return {
            "blocked": value.get("reason"),
            "suggested_next": value.get("suggested_next"),
        }
    if value.get("duplicate_successful_write_blocked"):
        return {
            "blocked_duplicate_write": True,
            "action": value.get("action"),
        }
    if value.get("ok") is True:
        return {"ok": True, "action": value.get("action")}
    summary: dict[str, Any] = {}
    for key in ("found", "total", "next_offset", "status"):
        if key in value:
            summary[key] = value[key]
    for key in ("work_order", "notification", "equipment", "employee", "material", "operation"):
        if isinstance(value.get(key), dict):
            summary[key] = _entity_summary(value[key])
    for key in ("work_orders", "notifications", "equipments", "employees", "materials", "operations"):
        if isinstance(value.get(key), list):
            items = [_entity_summary(item) for item in value[key][:8] if isinstance(item, dict)]
            summary[key] = items
            if len(value[key]) > 8:
                summary[f"{key}_truncated"] = len(value[key]) - 8
    if not summary:
        summary["keys"] = list(value.keys())[:20]
    return summary


def _entity_summary(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "floc",
        "short_desc",
        "description",
        "status",
        "work_center",
        "execution_date",
        "in_stock",
        "min_stock",
        "max_stock",
    )
    return {
        key: _truncate(str(value[key]), 160) if isinstance(value.get(key), str) else value.get(key)
        for key in keys
        if key in value
    }


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return _truncate(str(value), 200)
    if isinstance(value, dict):
        return {str(k): _compact_value(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        items = [_compact_value(v, depth=depth + 1) for v in value[:8]]
        if len(value) > 8:
            items.append({"truncated_items": len(value) - 8})
        return items
    if isinstance(value, str):
        return _truncate(value, 500)
    return value


def _load_cost_table(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _model_log_suffix(model: str | None) -> str:
    """Return a compact, recognizable model id for log filenames."""
    if not model:
        return "unk"

    raw = model.strip().lower().split("/")[-1]
    raw = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", raw)

    if "nemotron" in raw:
        return "nmtr"

    oss_match = re.search(r"gpt[-_]?oss[-_]?(\d+)b", raw)
    if oss_match:
        return f"g{oss_match.group(1)}"

    gemini_match = re.search(r"gemini[-_]?(\d+(?:\.\d+)?)", raw)
    if gemini_match:
        version = gemini_match.group(1).replace(".", "")
        family = ""
        if "flash" in raw:
            family = "f"
        elif "pro" in raw:
            family = "p"
        return f"gm{version}{family}"

    gpt_match = re.search(r"gpt[-_]?(\d+(?:\.\d+)?)", raw)
    if gpt_match:
        version = gpt_match.group(1)
        compact_version = version.replace(".", "") if version.startswith("4.") else version
        suffix = "m" if "mini" in raw else ""
        return f"g{compact_version}{suffix}"

    tokens = re.findall(r"[a-z0-9]+", raw)
    if not tokens:
        return "unk"
    return "".join(token[0] if token.isalpha() else token for token in tokens)[:6]


def load_harness_version(path: str | Path = "harness_versions.md") -> str:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"

    for line in text.splitlines():
        match = re.match(r"^#+\s+(\d+\.\d+(?:\.\d+)?)\b", line.strip())
        if match:
            return match.group(1)
    return "unknown"


def _empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost": {
            "currency": "USD",
            "input_usd": 0.0,
            "output_usd": 0.0,
            "total_usd": 0.0,
            "price_id": None,
            "pricing_available": False,
        },
    }


def _step_usage(
    step: dict[str, Any],
    *,
    provider: Any,
    model: Any,
    cost_table: dict[str, Any],
) -> dict[str, Any]:
    input_tokens = _int_or_zero(step.get("prompt_tokens"))
    output_tokens = _int_or_zero(step.get("completion_tokens"))
    usage = _empty_usage()
    usage["input_tokens"] = input_tokens
    usage["output_tokens"] = output_tokens
    usage["total_tokens"] = input_tokens + output_tokens
    usage["cost"] = _estimate_cost(
        provider=str(provider or ""),
        model=str(model or ""),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_table=cost_table,
    )
    return usage


def _add_usage(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["input_tokens"] += _int_or_zero(source.get("input_tokens"))
    target["output_tokens"] += _int_or_zero(source.get("output_tokens"))
    target["total_tokens"] += _int_or_zero(source.get("total_tokens"))

    target_cost = target.setdefault("cost", _empty_usage()["cost"])
    source_cost = source.get("cost") if isinstance(source.get("cost"), dict) else {}
    target_cost["input_usd"] = round(
        float(target_cost.get("input_usd") or 0.0)
        + float(source_cost.get("input_usd") or 0.0),
        8,
    )
    target_cost["output_usd"] = round(
        float(target_cost.get("output_usd") or 0.0)
        + float(source_cost.get("output_usd") or 0.0),
        8,
    )
    target_cost["total_usd"] = round(
        float(target_cost.get("total_usd") or 0.0)
        + float(source_cost.get("total_usd") or 0.0),
        8,
    )
    if source_cost.get("pricing_available"):
        target_cost["pricing_available"] = True
        target_cost["price_id"] = source_cost.get("price_id")
        target_cost["currency"] = source_cost.get("currency", target_cost.get("currency", "USD"))


def _estimate_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_table: dict[str, Any],
) -> dict[str, Any]:
    currency = str(cost_table.get("currency") or "USD")
    price_id = _resolve_price_id(provider=provider, model=model, cost_table=cost_table)
    prices = cost_table.get("prices") if isinstance(cost_table.get("prices"), dict) else {}
    price = prices.get(price_id) if price_id else None
    if not isinstance(price, dict):
        return {
            "currency": currency,
            "input_usd": 0.0,
            "output_usd": 0.0,
            "total_usd": 0.0,
            "price_id": price_id,
            "pricing_available": False,
        }

    input_usd = input_tokens / 1_000_000 * float(price.get("input_per_1m") or 0.0)
    output_usd = output_tokens / 1_000_000 * float(price.get("output_per_1m") or 0.0)
    return {
        "currency": currency,
        "input_usd": round(input_usd, 8),
        "output_usd": round(output_usd, 8),
        "total_usd": round(input_usd + output_usd, 8),
        "price_id": price_id,
        "pricing_available": True,
        "rates_per_1m": {
            "input": price.get("input_per_1m"),
            "output": price.get("output_per_1m"),
        },
    }


def _resolve_price_id(*, provider: str, model: str, cost_table: dict[str, Any]) -> str | None:
    providers = cost_table.get("providers") if isinstance(cost_table.get("providers"), dict) else {}
    provider_cfg = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    models = provider_cfg.get("models") if isinstance(provider_cfg.get("models"), dict) else {}
    if model in models:
        return str(models[model])

    # OpenAI dated model ids often append a release date to a base model id.
    # Use the longest configured prefix only when it is separated by "-".
    candidates = [
        (configured_model, price_id)
        for configured_model, price_id in models.items()
        if model.startswith(f"{configured_model}-")
    ]
    if candidates:
        candidates.sort(key=lambda item: len(item[0]), reverse=True)
        return str(candidates[0][1])

    prices = cost_table.get("prices") if isinstance(cost_table.get("prices"), dict) else {}
    direct_id = f"{provider}/{model}" if provider and model else model
    if direct_id in prices:
        return direct_id
    if model in prices:
        return model
    return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
