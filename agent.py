"""
Maintenance-ops sample agent - structured-output loop.

Uses OpenAI structured output (response_format) with provider-specific
Pydantic schema variants over all available API requests. The
MaintenanceClient.dispatch() method
handles routing - no manual tool dispatcher needed.

OpenAI and OpenRouter are supported out of the box through the OpenAI SDK.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Callable, TypeVar
from typing import Annotated, Any, List, Literal, Union

from annotated_types import MaxLen, MinLen
from openai import OpenAI
from pydantic import BaseModel, Field

from ogchallenge_client import CoreClient, MaintenanceClient, TaskInfo, ApiException
from ogchallenge_client.dtos import (
    GroundRef,
    Req_EquipmentList, Req_GetEquipment, Req_UpdateEquipment, Req_EquipmentSearch,
    Req_EmployeeList, Req_GetEmployee, Req_UpdateEmployee, Req_EmployeeSearch,
    Req_MaterialList, Req_MaterialGet, Req_MaterialSearch, Req_MaterialReorder,
    Req_NotifCreate, Req_NotifGet, Req_NotifSearch, Req_NotifUpdate,
    Req_WOList, Req_WOSearch, Req_WOCreate, Req_WOGet, Req_WOUpdate,
    Req_OperationAdd, Req_OperationUpdate, Req_OperationList,
    Req_WikiTree, Req_WikiLoad, Req_WikiSearch, Req_WikiUpdate,
    Req_Respond,
)
from run_logging import RunLogger

CLI_GREEN = "\x1b[32m"
CLI_RED = "\x1b[31m"
CLI_CYAN = "\x1b[36m"
CLI_YELLOW = "\x1b[33m"
CLI_BLUE = "\x1b[34m"
CLI_CLR = "\x1b[0m"

T = TypeVar("T")
TRANSIENT_API_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
TRANSIENT_API_ERROR_CODES = {"network_error", "timeout", "rate_limit"}
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
}
GROUND_REF_TYPES = {
    "equipment",
    "employee",
    "material",
    "notification",
    "work_order",
    "operation",
    "wiki",
}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    default_headers: dict[str, str] | None = None


def make_llm_client(config: LLMConfig) -> OpenAI:
    kwargs: dict = {"api_key": config.api_key}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.default_headers:
        kwargs["default_headers"] = config.default_headers
    return OpenAI(**kwargs)


TaskType = Literal["lookup", "write", "review", "unknown"]


@dataclass
class HarnessState:
    """Small BitGN-style scratchpad maintained outside the model."""

    task_type: TaskType = "unknown"
    last_state: str = ""
    last_action: str = ""
    last_result: str = ""
    steps_taken: int = 0
    writes_completed: int = 0
    task_scope: str = "single_target"
    task_tokens: list[str] | None = None
    read_refs: list[str] | None = None
    write_refs: list[str] | None = None
    support_refs: list[str] | None = None
    candidate_ledger: list[dict[str, Any]] | None = None
    open_ambiguities: list[dict[str, Any]] | None = None
    ambiguity_nudges: int = 0

    def __post_init__(self) -> None:
        if self.task_tokens is None:
            self.task_tokens = []
        if self.read_refs is None:
            self.read_refs = []
        if self.write_refs is None:
            self.write_refs = []
        if self.support_refs is None:
            self.support_refs = []
        if self.candidate_ledger is None:
            self.candidate_ledger = []
        if self.open_ambiguities is None:
            self.open_ambiguities = []

    def render(self, *, steps_left: int) -> str:
        budget = ""
        if steps_left <= max(3, MAX_STEPS // 5):
            budget = (
                "\n<budget-warning>"
                f"{steps_left} steps remain. If enough evidence is available, "
                "finalize with respond instead of continuing to search."
                "</budget-warning>"
            )
        payload = {
            "task_type": self.task_type,
            "steps_taken": self.steps_taken,
            "steps_left": steps_left,
            "writes_completed": self.writes_completed,
            "task_scope": self.task_scope,
            "last_action": self.last_action,
            "last_result": self.last_result,
            "read_refs": self.read_refs[-12:],
            "write_refs": self.write_refs[-12:],
            "support_refs": self.support_refs[-8:],
            "open_ambiguities": self.unresolved_ambiguities()[-4:],
            "last_state": self.last_state,
            "instructions": [
                "Use this as scratchpad state, not as task evidence.",
                "Do not repeat a successful write.",
                "If open_ambiguities remain and no evidence selects one candidate, respond none_clarification_needed.",
                "Before respond, verify outcome, side effects, and ground_refs.",
            ],
        }
        return (
            "<harness-scratchpad>\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            "</harness-scratchpad>"
            f"{budget}"
        )

    def update_from_step(self, *, step: BaseModel, fn: Any, result_payload: Any) -> None:
        self.steps_taken += 1
        self.last_state = getattr(step, "current_state", "") or ""
        self.task_type = getattr(step, "task_type", self.task_type) or self.task_type
        fn_type = getattr(fn, "type", type(fn).__name__)
        self.last_action = fn_type
        self.last_result = _summarize_result(result_payload)
        if _is_write_success(fn_type, result_payload):
            self.writes_completed += 1
        self._collect_refs(fn, result_payload)
        self._update_candidate_ledger(fn, result_payload)
        if fn_type == "respond" and getattr(fn, "outcome", "") == "none_clarification_needed":
            self._resolve_open_ambiguities("responded_clarification")

    def _collect_refs(self, fn: Any, result_payload: Any) -> None:
        fn_type = getattr(fn, "type", "")
        args = fn.model_dump(exclude_none=True) if hasattr(fn, "model_dump") else {}
        if fn_type.startswith("wiki_") and args.get("path"):
            self._remember(self.write_refs if fn_type == "wiki_update" else self.read_refs, f"wiki:{args['path']}")
        if fn_type in WRITE_ACTION_TYPES and _is_write_success(fn_type, result_payload):
            _collect_write_arg_refs(fn_type, args, self.write_refs)
            _collect_entity_refs(result_payload, self.write_refs)
        elif fn_type != "respond":
            _collect_entity_refs(result_payload, self.read_refs)

    def _update_candidate_ledger(self, fn: Any, result_payload: Any) -> None:
        fn_type = getattr(fn, "type", "")
        if fn_type in WRITE_ACTION_TYPES or fn_type == "respond" or not _is_success_payload(result_payload):
            return
        args = fn.model_dump(exclude_none=True, exclude={"type"}) if hasattr(fn, "model_dump") else {}
        for entity_type, candidates in _extract_collection_candidates(result_payload).items():
            ids = [candidate["id"] for candidate in candidates]
            entry = {
                "entity_type": entity_type,
                "count": len(candidates),
                "ids": ids[:8],
                "source_action": fn_type,
                "query": _compact_query_args(args),
            }
            self.candidate_ledger.append(entry)
            del self.candidate_ledger[:-12]
            if len(candidates) > 1 and self.task_scope == "single_target":
                self._remember_ambiguity(entity_type, candidates, fn_type, args)
            elif len(candidates) == 1:
                self._resolve_if_narrowed(entity_type, ids[0])

    def _remember_ambiguity(
        self,
        entity_type: str,
        candidates: list[dict[str, str]],
        source_action: str,
        args: dict[str, Any],
    ) -> None:
        ids = [candidate["id"] for candidate in candidates]
        key = f"{entity_type}:{','.join(ids)}"
        resolved_by = _resolve_candidates_by_task_tokens(self.task_tokens or [], candidates)
        ambiguity = {
            "key": key,
            "entity_type": entity_type,
            "count": len(candidates),
            "candidate_ids": ids[:8],
            "candidate_labels": [candidate.get("label", "") for candidate in candidates[:4]],
            "source_action": source_action,
            "query": _compact_query_args(args),
            "status": "resolved" if resolved_by else "open",
        }
        if resolved_by:
            ambiguity["resolved_by"] = resolved_by
        for idx, existing in enumerate(self.open_ambiguities):
            if existing.get("key") == key:
                self.open_ambiguities[idx] = ambiguity
                return
        self.open_ambiguities.append(ambiguity)
        del self.open_ambiguities[:-6]

    def _resolve_if_narrowed(self, entity_type: str, candidate_id: str) -> None:
        for ambiguity in self.open_ambiguities:
            if (
                ambiguity.get("status") == "open"
                and ambiguity.get("entity_type") == entity_type
                and candidate_id in ambiguity.get("candidate_ids", [])
            ):
                ambiguity["status"] = "resolved"
                ambiguity["resolved_by"] = candidate_id

    def _resolve_open_ambiguities(self, reason: str) -> None:
        for ambiguity in self.open_ambiguities:
            if ambiguity.get("status") == "open":
                ambiguity["status"] = "resolved"
                ambiguity["resolved_by"] = reason

    def unresolved_ambiguities(self) -> list[dict[str, Any]]:
        return [
            ambiguity
            for ambiguity in (self.open_ambiguities or [])
            if ambiguity.get("status") == "open"
        ]

    def should_nudge_ambiguity(self, fn: Req_Respond) -> bool:
        return (
            fn.outcome == "ok_answer"
            and self.ambiguity_nudges < 1
            and bool(self.unresolved_ambiguities())
        )

    def record_ambiguity_nudge(self) -> None:
        self.ambiguity_nudges += 1

    def remember_support_refs(self, refs: list[str]) -> None:
        for ref in refs:
            self._remember(self.support_refs, ref)

    @staticmethod
    def _remember(target: list[str] | None, value: str) -> None:
        if target is not None and value not in target:
            target.append(value)


def _is_transient_api_error(exc: ApiException) -> bool:
    code = getattr(exc.api_error, "code", "")
    return (
        exc.status_code == 0
        or exc.status_code in TRANSIENT_API_STATUS_CODES
        or code in TRANSIENT_API_ERROR_CODES
    )


def _api_retry(description: str, call: Callable[[], T], *, attempts: int = 4) -> T:
    delay_sec = 2.0
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ApiException as exc:
            if not _is_transient_api_error(exc) or attempt == attempts:
                raise
            print(
                f"    {CLI_YELLOW}Transient platform error during {description}: "
                f"{exc}. Retrying in {delay_sec:.1f}s "
                f"({attempt}/{attempts})...{CLI_CLR}"
            )
            time.sleep(delay_sec)
            delay_sec = min(delay_sec * 2, 30)
    raise RuntimeError("unreachable")


def _action_signature(fn: Any) -> str:
    payload = fn.model_dump(exclude_none=True) if hasattr(fn, "model_dump") else {"type": str(type(fn))}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _summarize_result(result_payload: Any, *, limit: int = 600) -> str:
    if result_payload is None:
        return ""
    if isinstance(result_payload, str):
        text = result_payload
    else:
        text = json.dumps(result_payload, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "... [truncated]"


def _is_success_payload(result_payload: Any) -> bool:
    if not isinstance(result_payload, dict):
        return True
    if result_payload.get("error") or result_payload.get("api_error"):
        return False
    if result_payload.get("duplicate_successful_write_blocked") or result_payload.get("harness_blocked"):
        return False
    return True


def _is_write_success(fn_type: str, result_payload: Any) -> bool:
    return fn_type in WRITE_ACTION_TYPES and _is_success_payload(result_payload)


def _success_payload(fn_type: str) -> dict[str, Any]:
    if fn_type == "respond":
        return {
            "ok": True,
            "action": fn_type,
            "message": "Answer submitted.",
        }
    if fn_type in WRITE_ACTION_TYPES:
        return {
            "ok": True,
            "action": fn_type,
            "message": (
                "Write completed successfully. "
                "Do not repeat this write unless a later read proves it failed."
            ),
        }
    return {
        "ok": True,
        "action": fn_type,
        "message": "API call completed successfully.",
    }


def _harness_block_payload(*, reason: str, message: str, suggested_next: str) -> dict[str, Any]:
    return {
        "harness_blocked": True,
        "reason": reason,
        "message": message,
        "suggested_next": suggested_next,
    }


def _collect_write_arg_refs(fn_type: str, args: dict[str, Any], write_refs: list[str] | None) -> None:
    for ref in _write_arg_ref_tokens(fn_type, args):
        _remember_ref(write_refs, ref)


def _write_arg_ref_tokens(fn_type: str, args: dict[str, Any]) -> list[str]:
    arg_refs = {
        "equipment_update": (("equipment", "floc"),),
        "employee_update": (("employee", "emp_id"),),
        "material_reorder": (("material", "mat_id"),),
        "notif_create": (("equipment", "floc"),),
        "notif_update": (("notification", "notif_id"),),
        "wo_create": (("notification", "notification_id"),),
        "wo_update": (("work_order", "wo_id"), ("equipment", "floc")),
        "operation_add": (("work_order", "workorder_id"),),
        "operation_update": (("work_order", "workorder_id"), ("operation", "op_id")),
    }
    refs: list[str] = []
    for ref_type, arg_name in arg_refs.get(fn_type, ()):
        ref_id = args.get(arg_name)
        if ref_id is not None:
            refs.append(f"{ref_type}:{ref_id}")
    if fn_type in {"operation_add", "operation_update"}:
        for material in args.get("materials") or []:
            if isinstance(material, dict) and material.get("mat_id") is not None:
                refs.append(f"material:{material['mat_id']}")
    return refs


def _collect_entity_refs(result_payload: Any, target_refs: list[str] | None) -> None:
    if not isinstance(result_payload, dict):
        return

    entity_keys = {
        "equipment": "equipment",
        "employee": "employee",
        "material": "material",
        "notification": "notification",
        "work_order": "work_order",
        "operation": "operation",
    }
    collection_keys = {
        "equipments": "equipment",
        "employees": "employee",
        "materials": "material",
        "notifications": "notification",
        "work_orders": "work_order",
        "operations": "operation",
    }
    for key, ref_type in entity_keys.items():
        value = result_payload.get(key)
        if isinstance(value, dict):
            ref_id = value.get("id") or value.get("floc")
            if ref_id is not None:
                _remember_ref(target_refs, f"{ref_type}:{ref_id}")
    for key, ref_type in collection_keys.items():
        values = result_payload.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    ref_id = value.get("id") or value.get("floc")
                    if ref_id is not None:
                        _remember_ref(target_refs, f"{ref_type}:{ref_id}")


def _extract_collection_candidates(result_payload: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(result_payload, dict):
        return {}
    collection_keys = {
        "equipments": "equipment",
        "employees": "employee",
        "materials": "material",
        "notifications": "notification",
        "work_orders": "work_order",
        "operations": "operation",
    }
    candidates_by_type: dict[str, list[dict[str, str]]] = {}
    for key, entity_type in collection_keys.items():
        values = result_payload.get(key)
        if not isinstance(values, list):
            continue
        candidates = []
        for value in values:
            candidate = _candidate_summary(value)
            if candidate is not None:
                candidates.append(candidate)
        if candidates:
            candidates_by_type[entity_type] = candidates
    return candidates_by_type


def _candidate_summary(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    ref_id = value.get("id") or value.get("floc")
    if ref_id is None:
        return None
    label = (
        value.get("short_desc")
        or value.get("description")
        or value.get("name")
        or value.get("status")
        or ""
    )
    return {
        "id": str(ref_id),
        "label": str(label)[:120],
    }


TASK_SCOPE_MULTI_CUES = {
    "all",
    "list",
    "total",
    "count",
    "capacity",
    "remaining",
    "every",
    "open",
    "notifications",
    "materials",
    "workorders",
    "orders",
}
TASK_TOKEN_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "have",
    "has",
    "been",
    "please",
    "what",
    "when",
    "where",
    "which",
    "all",
    "list",
    "open",
    "work",
    "order",
    "orders",
    "workorder",
    "workorders",
    "notification",
    "notifications",
    "equipment",
    "material",
    "materials",
    "valve",
    "pipeline",
    "separator",
    "replace",
    "replacement",
    "planned",
    "repair",
    "candidate",
    "system",
    "team",
    "week",
}


def _classify_task_scope(task_text: str) -> str:
    tokens = set(_text_tokens(task_text))
    if tokens & TASK_SCOPE_MULTI_CUES:
        return "multi_result"
    if any(phrase in task_text.lower() for phrase in ("remaining capacity", "how many", "how much")):
        return "multi_result"
    return "single_target"


def _task_distinctive_tokens(task_text: str) -> list[str]:
    return [
        token
        for token in _text_tokens(task_text)
        if token not in TASK_TOKEN_STOPWORDS and (len(token) >= 4 or any(ch.isdigit() for ch in token))
    ]


def _text_tokens(text: str) -> list[str]:
    return [
        token.strip("-_")
        for token in re.findall(r"[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*", text.lower())
        if token.strip("-_")
    ]


def _resolve_candidates_by_task_tokens(task_tokens: list[str], candidates: list[dict[str, str]]) -> str | None:
    if not task_tokens:
        return None
    token_set = set(task_tokens)
    best_id: str | None = None
    best_score = 0
    tied = False
    for candidate in candidates:
        candidate_text = f"{candidate.get('id', '')} {candidate.get('label', '')}".lower()
        candidate_tokens = set(_text_tokens(candidate_text))
        score = len(token_set & candidate_tokens)
        for token in token_set:
            if token and token in candidate_text:
                score += 2 if any(ch.isdigit() for ch in token) else 1
        if score > best_score:
            best_id = candidate.get("id")
            best_score = score
            tied = False
        elif score == best_score and score > 0:
            tied = True
    if best_id is not None and best_score >= 2 and not tied:
        return best_id
    return None


def _compact_query_args(args: dict[str, Any], *, limit: int = 120) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in args.items():
        if key == "type":
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            compact[key] = value[:limit]
        elif isinstance(value, (int, float, bool)):
            compact[key] = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
            compact[key] = text[:limit]
    return compact


def _remember_ref(target: list[str] | None, value: str) -> None:
    if target is not None and value not in target:
        target.append(value)


def _ground_ref_key(ref: Any) -> tuple[str, str] | None:
    ref_type = getattr(ref, "type", None)
    ref_id = getattr(ref, "id", None)
    if not isinstance(ref_type, str) or not isinstance(ref_id, str):
        return None
    if ref_type not in GROUND_REF_TYPES or not _looks_like_ref_id(ref_id):
        return None
    return ref_type, ref_id


def _looks_like_ref_id(value: str) -> bool:
    clean = value.strip()
    if not clean or len(clean) > 180:
        return False
    return not any(ch in clean for ch in "{}[]\n\r\t")


def _token_to_ground_ref(token: str) -> GroundRef | None:
    ref_type, sep, ref_id = token.partition(":")
    if sep != ":":
        return None
    if ref_type not in GROUND_REF_TYPES or not _looks_like_ref_id(ref_id):
        return None
    return GroundRef(type=ref_type, id=ref_id)


def _ground_refs_from_tokens(tokens: list[str]) -> list[GroundRef]:
    refs: list[GroundRef] = []
    seen: set[tuple[str, str]] = set()
    for token in tokens:
        ref = _token_to_ground_ref(token)
        if ref is None:
            continue
        key = (ref.type, ref.id)
        if key in seen:
            continue
        refs.append(ref)
        seen.add(key)
    return refs


def _augment_respond_refs(fn: Req_Respond, harness_state: HarnessState) -> int:
    existing: list[GroundRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in fn.ground_refs or []:
        key = _ground_ref_key(ref)
        if key is None or key in seen:
            continue
        existing.append(ref)
        seen.add(key)

    added = 0
    for token in [*(harness_state.write_refs or []), *(harness_state.read_refs or []), *(harness_state.support_refs or [])]:
        ref = _token_to_ground_ref(token)
        if ref is None:
            continue
        key = (ref.type, ref.id)
        if key in seen:
            continue
        existing.append(ref)
        seen.add(key)
        added += 1

    fn.ground_refs = existing
    return added


Action = Union[
    Req_EquipmentList, Req_GetEquipment, Req_UpdateEquipment, Req_EquipmentSearch,
    Req_EmployeeList, Req_GetEmployee, Req_UpdateEmployee, Req_EmployeeSearch,
    Req_MaterialList, Req_MaterialGet, Req_MaterialSearch, Req_MaterialReorder,
    Req_NotifCreate, Req_NotifGet, Req_NotifSearch, Req_NotifUpdate,
    Req_WOList, Req_WOSearch, Req_WOCreate, Req_WOGet, Req_WOUpdate,
    Req_OperationAdd, Req_OperationUpdate, Req_OperationList,
    Req_WikiTree, Req_WikiLoad, Req_WikiSearch, Req_WikiUpdate,
    Req_Respond,
]

# OpenRouter and many non-OpenAI models are more reliable with explicit
# discriminator metadata on unions.
DiscriminatedAction = Annotated[
    Action,
    Field(discriminator="type"),
]


class NextStep(BaseModel):
    """OpenAI-compatible structured output schema."""

    task_type: TaskType = Field(
        "unknown",
        description="Classify this task as lookup, write, review, or unknown.",
    )
    current_state: str = Field(..., description="Brief summary of what you know so far")
    gate_checks: Annotated[List[str], MaxLen(6)] = Field(
        default_factory=list,
        description="Short evidence checks completed or still required before writes/respond.",
    )
    plan: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ..., description="Remaining steps to complete the task (most important first)"
    )
    ready_to_respond: bool = Field(
        False,
        description="True when evidence and side-effect checks are sufficient to call respond now.",
    )
    task_completed: bool = Field(False, description="Set to true only when calling respond")
    function: Action = Field(
        ...,
        description="The next API call to execute",
    )


class NextStepDiscriminated(BaseModel):
    """Structured output schema with explicit union discriminator metadata."""

    task_type: TaskType = Field(
        "unknown",
        description="Classify this task as lookup, write, review, or unknown.",
    )
    current_state: str = Field(..., description="Brief summary of what you know so far")
    gate_checks: List[str] = Field(
        default_factory=list,
        description="Short evidence checks completed or still required before writes/respond.",
    )
    plan: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ..., description="Remaining 3 steps to complete the task (most important first)"
    )
    ready_to_respond: bool = Field(
        False,
        description="True when evidence and side-effect checks are sufficient to call respond now.",
    )
    task_completed: bool = Field(False, description="Set to true only when calling respond")
    function: DiscriminatedAction = Field(
        ...,
        description="The next API call to execute",
    )


def _response_model_for_provider(provider: str) -> type[BaseModel]:
    # OpenAI response_format rejects oneOf in this schema position, while
    # OpenRouter generally benefits from discriminator+oneOf for reliability.
    return NextStep if provider == "openai" else NextStepDiscriminated


def _first_plan_item(plan: Any) -> str:
    if isinstance(plan, list) and plan and isinstance(plan[0], str) and plan[0].strip():
        return plan[0]
    return "No plan provided."


def _explicitly_requests_material_reorder(task_text: str) -> bool:
    text = task_text.lower()
    return any(
        phrase in text
        for phrase in (
            "reorder",
            "re-order",
            "restock",
            "re-stock",
            "order material",
            "order spare",
            "procure",
            "raise purchase",
        )
    )


def _to_responses_input(log: list[dict]) -> list[dict]:
    """Convert internal chat-style log into Responses API input messages."""
    items: list[dict] = []
    for msg in log:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in {"system", "developer"}:
            items.append({"role": "developer", "content": content})
        elif role in {"user", "assistant"}:
            items.append({"role": role, "content": content})
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "tool")
            items.append({"role": "user", "content": f"[tool:{tool_call_id}]\n{content}"})
    return items


RUNTIME_PROMPT_PATH = Path(__file__).with_name("ARC_agents.md")


def _load_runtime_prompt() -> str:
    return RUNTIME_PROMPT_PATH.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = _load_runtime_prompt()

MAX_STEPS = 30
COMMON_WIKI_DOCS = (
    "index.md",
    "system_reference/system.md",
    "governance/raci.md",
    "maintenance_and_integrity/notification_guidelines.md",
    "maintenance_and_integrity/wo_guidelines.md",
    "maintenance_and_integrity/work_planning.md",
    "safety_and_risk/RAM.md",
    "safety_and_risk/incidents.md",
)


def run_agent(
    api: CoreClient,
    task: TaskInfo,
    *,
    llm_config: LLMConfig,
    run_logger: RunLogger | None = None,
) -> None:
    """Run the agent for a single task."""

    client = make_llm_client(llm_config)
    maint = api.get_maintenance_client(task)

    print(f"\n{CLI_CYAN}Task {task.num}: {task.spec_id}{CLI_CLR}")
    print(f"  {task.task_text}\n")

    bootstrap_log = _bootstrap(maint)

    log: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    for label, text in bootstrap_log:
        print(f"  {CLI_GREEN}AUTO {label}{CLI_CLR}: {text[:120]}")
        log.append({"role": "user", "content": f"[{label}]\n{text}"})
        if run_logger:
            run_logger.record_bootstrap(label, text)

    log.append({"role": "user", "content": task.task_text})

    response_model = _response_model_for_provider(llm_config.provider)
    harness_state = HarnessState(
        task_scope=_classify_task_scope(task.task_text),
        task_tokens=_task_distinctive_tokens(task.task_text),
    )
    successful_write_signatures: set[str] = set()
    seen_nonwrite_signatures: dict[str, int] = {}
    ready_read_count = 0
    last_failed_write: dict[str, Any] | None = None
    failed_write_response_nudges = 0
    consecutive_blocked_steps = 0
    force_respond_mode = False

    for i in range(MAX_STEPS):
        step_id = f"step_{i + 1}"
        print(f"  Step {i + 1}... ", end="", flush=True)
        step_event: dict = {"step": i + 1, "step_id": step_id}
        harness_msg = {
            "role": "user",
            "content": harness_state.render(steps_left=MAX_STEPS - i),
        }

        t0 = time.time()
        try:
            if llm_config.provider == "openai":
                resp = client.responses.parse(
                    model=llm_config.model,
                    input=_to_responses_input(log + [harness_msg]),
                    text_format=response_model,
                )
            else:
                resp = client.beta.chat.completions.parse(
                    model=llm_config.model,
                    response_format=response_model,
                    messages=log + [harness_msg],
                )
        except Exception as exc:
            raise RuntimeError(
                "LLM request failed for "
                f"provider={llm_config.provider!r}, model={llm_config.model!r}. "
                "Check MODEL_PROVIDER, MODEL_ID, and the matching provider API key. "
                f"Original error: {exc}"
            ) from exc
        elapsed_ms = int((time.time() - t0) * 1000)
        step_event["elapsed_ms"] = elapsed_ms

        if llm_config.provider == "openai":
            step = resp.output_parsed
            prompt_tokens = resp.usage.input_tokens if resp.usage else None
            completion_tokens = resp.usage.output_tokens if resp.usage else None
        else:
            step = resp.choices[0].message.parsed
            prompt_tokens = resp.usage.prompt_tokens if resp.usage else None
            completion_tokens = resp.usage.completion_tokens if resp.usage else None

        if step is None:
            print(f"{CLI_RED}LLM returned unparseable response{CLI_CLR}")
            step_event["error"] = "LLM returned unparseable response"
            if run_logger:
                run_logger.record_step(**step_event)
            break

        fn = step.function
        fn_type = fn.type
        if isinstance(fn, Req_Respond):
            added_refs = _augment_respond_refs(fn, harness_state)
        else:
            added_refs = 0
        fn_args = fn.model_dump_json(exclude_none=True, exclude={"type"})
        plan_text = _first_plan_item(step.plan)
        step_event.update({
            "task_type": step.task_type,
            "current_state": step.current_state,
            "gate_checks": step.gate_checks,
            "plan": step.plan,
            "ready_to_respond": step.ready_to_respond,
            "function_type": fn_type,
            "function_args": fn.model_dump(exclude_none=True, exclude={"type"}),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        })
        if added_refs:
            step_event["harness_ref_augmented"] = added_refs
        print(f"{CLI_CYAN}{fn_type}{CLI_CLR} - {plan_text}  ({elapsed_ms}ms)")
        print(f"    {CLI_YELLOW}args:{CLI_CLR} {fn_args[:300]}")

        try:
            api.log_llm(
                task_id=task.task_id,
                completion=plan_text,
                model=llm_config.model,
                duration_sec=(time.time() - t0),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception:
            pass

        log.append({
            "role": "assistant",
            "content": plan_text,
            "tool_calls": [{
                "type": "function",
                "id": step_id,
                "function": {
                    "name": type(fn).__name__,
                    "arguments": fn.model_dump_json(exclude_none=True),
                },
            }],
        })

        result_payload: Any
        action_signature = _action_signature(fn)
        harness_block: dict[str, Any] | None = None
        if force_respond_mode and not isinstance(fn, Req_Respond):
            harness_block = _harness_block_payload(
                reason="force_respond_after_successful_write",
                message=(
                    "A write already completed successfully. Do not perform more reads or writes; "
                    "call respond now using the completed write refs."
                ),
                suggested_next="respond",
            )
        elif (
            isinstance(fn, Req_Respond)
            and fn.outcome == "ok_answer"
            and last_failed_write is not None
            and failed_write_response_nudges < 1
        ):
            failed_write_response_nudges += 1
            harness_block = _harness_block_payload(
                reason="failed_write_requires_non_ok_response",
                message=(
                    f"The requested {last_failed_write.get('type')} write failed with "
                    f"{last_failed_write.get('error')}. The side effect did not complete. "
                    "Use a non-OK outcome unless a later successful retry proves completion."
                ),
                suggested_next="non_ok_respond_or_successful_retry",
            )
        elif isinstance(fn, Req_Respond) and harness_state.should_nudge_ambiguity(fn):
            ambiguity = harness_state.unresolved_ambiguities()[0]
            harness_state.record_ambiguity_nudge()
            harness_block = _harness_block_payload(
                reason="ambiguity_pre_respond_nudge",
                message=(
                    "Unresolved candidate ambiguity remains for "
                    f"{ambiguity.get('entity_type')} candidates "
                    f"{ambiguity.get('candidate_ids')}. "
                    "Respond with none_clarification_needed, or answer only if current evidence "
                    "clearly disambiguates the intended candidate."
                ),
                suggested_next="clarify_or_justify_disambiguation",
            )
        elif fn_type == "system" and harness_state.steps_taken >= 1:
            harness_block = _harness_block_payload(
                reason="repeated_system_nonprogress",
                message=(
                    "System context is already available. Execute the planned search, read, "
                    "write, or respond instead of repeating system."
                ),
                suggested_next="search_or_respond",
            )
        elif (
            fn_type == "material_reorder"
            and not _explicitly_requests_material_reorder(task.task_text)
        ):
            harness_block = _harness_block_payload(
                reason="compensating_material_reorder_blocked",
                message=(
                    "The user did not explicitly request material reorder/restock. "
                    "Do not perform a compensating reorder just to make another write possible; "
                    "respond with the blocking stock issue or perform only the requested write if valid."
                ),
                suggested_next="respond_or_requested_write",
            )
        elif step.ready_to_respond and fn_type in WRITE_ACTION_TYPES:
            harness_block = _harness_block_payload(
                reason="ready_to_respond_blocks_write",
                message=(
                    "You marked ready_to_respond=true, so do not perform another write. "
                    "Call respond, or use at most one read/search only if final verification is needed."
                ),
                suggested_next="respond",
            )
        elif (
            last_failed_write is not None
            and fn_type in WRITE_ACTION_TYPES
            and fn_type != last_failed_write["type"]
        ):
            harness_block = _harness_block_payload(
                reason="failed_write_blocks_different_write",
                message=(
                    f"The previous {last_failed_write['type']} write failed. "
                    "Do not repair a failed write by performing a different write. "
                    "Inspect the failure, retry the same intended write if corrected, or respond."
                ),
                suggested_next="read_retry_same_write_or_respond",
            )
        elif step.ready_to_respond and not isinstance(fn, Req_Respond):
            if ready_read_count >= 1:
                harness_block = _harness_block_payload(
                    reason="ready_to_respond_read_limit",
                    message=(
                        "You already used one read/search after marking ready_to_respond=true. "
                        "Call respond with outcome, message, and inclusive ground_refs."
                    ),
                    suggested_next="respond",
                )
        elif fn_type not in WRITE_ACTION_TYPES and fn_type != "respond":
            repeat_count = seen_nonwrite_signatures.get(action_signature, 0)
            if repeat_count >= 2:
                harness_block = _harness_block_payload(
                    reason="repeated_nonwrite_nonprogress",
                    message=(
                        "This exact read/search action has already been tried twice. "
                        "Use the available result, try a different query/read, or respond."
                    ),
                    suggested_next="different_action_or_respond",
                )

        if harness_block is not None:
            result_payload = harness_block
            result_text = json.dumps(result_payload, ensure_ascii=False)
            print(f"    {CLI_YELLOW}BLOCKED {harness_block['reason']}{CLI_CLR}")
            step_event["result"] = result_payload
            step_event["harness_block"] = harness_block["reason"]
            consecutive_blocked_steps += 1
        elif fn_type in WRITE_ACTION_TYPES and action_signature in successful_write_signatures:
            result_payload = {
                "duplicate_successful_write_blocked": True,
                "action": fn_type,
                "message": (
                    "Harness blocked an exact repeat of a write that already completed. "
                    "Use respond or inspect current state instead of repeating the write."
                ),
            }
            result_text = json.dumps(result_payload, ensure_ascii=False)
            print(f"    {CLI_YELLOW}BLOCKED duplicate write{CLI_CLR}")
            step_event["result"] = result_payload
            consecutive_blocked_steps += 1
        else:
            consecutive_blocked_steps = 0
            try:
                result = _api_retry(
                    f"maintenance API call {fn_type}",
                    lambda: maint.dispatch(fn),
                )
                result_payload = result.model_dump(exclude_none=True)
                if result_payload:
                    result_text = result.model_dump_json(exclude_none=True)
                else:
                    result_text = json.dumps(_success_payload(fn_type))
                    result_payload = json.loads(result_text)
                if _is_write_success(fn_type, result_payload):
                    successful_write_signatures.add(action_signature)
                print(f"    {CLI_GREEN}->{CLI_CLR} {result_text[:200]}")
                step_event["result"] = result_payload
            except ApiException as exc:
                result_payload = {
                    "error": exc.api_error.error,
                    "code": exc.api_error.code,
                    "status_code": exc.status_code,
                }
                result_text = f'{{"error": "{exc.api_error.error}", "code": "{exc.api_error.code}"}}'
                print(f"    {CLI_RED}ERR: {exc.api_error.error}{CLI_CLR}")
                step_event["api_error"] = result_payload
            except Exception as exc:
                result_payload = {"error": str(exc)}
                result_text = f'{{"error": "{exc}"}}'
                print(f"    {CLI_RED}ERR: {exc}{CLI_CLR}")
                step_event["error"] = str(exc)

        if fn_type not in WRITE_ACTION_TYPES and fn_type != "respond":
            seen_nonwrite_signatures[action_signature] = seen_nonwrite_signatures.get(action_signature, 0) + 1
            if step.ready_to_respond and harness_block is None:
                ready_read_count += 1

        if fn_type in WRITE_ACTION_TYPES:
            if _is_write_success(fn_type, result_payload):
                last_failed_write = None
                failed_write_response_nudges = 0
            elif harness_block is None and isinstance(result_payload, dict) and result_payload.get("error"):
                write_args = fn.model_dump(exclude_none=True, exclude={"type"}) if hasattr(fn, "model_dump") else {}
                support_refs = _write_arg_ref_tokens(fn_type, write_args)
                harness_state.remember_support_refs(support_refs)
                last_failed_write = {
                    "type": fn_type,
                    "signature": action_signature,
                    "args": write_args,
                    "error": result_payload.get("error") or result_payload.get("code"),
                    "code": result_payload.get("code"),
                    "support_refs": support_refs,
                }

        log.append({"role": "tool", "content": result_text, "tool_call_id": step_id})
        harness_state.update_from_step(step=step, fn=fn, result_payload=result_payload)
        step_event["harness_state"] = {
            "task_type": harness_state.task_type,
            "task_scope": harness_state.task_scope,
            "writes_completed": harness_state.writes_completed,
            "read_refs": harness_state.read_refs[-12:],
            "write_refs": harness_state.write_refs[-12:],
            "support_refs": harness_state.support_refs[-8:],
            "candidate_ledger": harness_state.candidate_ledger[-4:],
            "open_ambiguities": harness_state.open_ambiguities[-4:],
            "ambiguity_nudges": harness_state.ambiguity_nudges,
        }
        if run_logger:
            run_logger.record_step(**step_event)

        if isinstance(fn, Req_Respond) and not (
            isinstance(result_payload, dict) and result_payload.get("harness_blocked")
        ):
            print(f"\n  {CLI_GREEN}Agent responded: {fn.outcome}{CLI_CLR}")
            print(f"  {CLI_BLUE}{fn.message}{CLI_CLR}")
            if fn.ground_refs:
                for ref in fn.ground_refs:
                    print(f"    ref: {ref.type} -> {ref.id}")
            break

        if consecutive_blocked_steps >= 3:
            if harness_state.writes_completed > 0 and not force_respond_mode:
                force_respond_mode = True
                consecutive_blocked_steps = 0
                print(f"\n  {CLI_YELLOW}Entering force-respond mode after successful write.{CLI_CLR}")
                continue
            auto_outcome = "ok_answer" if harness_state.writes_completed > 0 else "error_internal"
            auto_message = (
                "The requested write appears to have completed, but the run repeatedly selected "
                "blocked or non-progress actions after completion."
                if auto_outcome == "ok_answer"
                else "I could not complete the task because the run repeatedly selected "
                "blocked or non-progress actions."
            )
            auto_fn = Req_Respond(
                message=auto_message,
                outcome=auto_outcome,
                ground_refs=_ground_refs_from_tokens(harness_state.write_refs or []),
            )
            try:
                _api_retry(
                    "auto respond after blocked non-progress",
                    lambda: maint.dispatch(auto_fn),
                )
                print(f"\n  {CLI_YELLOW}Auto-responded after repeated blocked non-progress.{CLI_CLR}")
                if run_logger:
                    run_logger.record_step(
                        step=i + 1,
                        event="auto_respond_after_blocked_nonprogress",
                        function_type="respond",
                        function_args=auto_fn.model_dump(exclude_none=True, exclude={"type"}),
                        result=_success_payload("respond"),
                        harness_state={
                            "task_type": harness_state.task_type,
                            "task_scope": harness_state.task_scope,
                            "writes_completed": harness_state.writes_completed,
                            "read_refs": harness_state.read_refs[-12:],
                            "write_refs": harness_state.write_refs[-12:],
                            "support_refs": harness_state.support_refs[-8:],
                        },
                    )
            except Exception as exc:
                print(f"\n  {CLI_RED}Auto-response failed: {exc}{CLI_CLR}")
                if run_logger:
                    run_logger.record_step(
                        step=i + 1,
                        event="auto_respond_after_blocked_nonprogress_failed",
                        error=str(exc),
                    )
            break
    else:
        print(f"\n  {CLI_YELLOW}Reached max steps ({MAX_STEPS}) without responding.{CLI_CLR}")
        if run_logger:
            run_logger.record_step(
                step=MAX_STEPS,
                event="max_steps_reached",
                message=f"Reached max steps ({MAX_STEPS}) without responding.",
            )


def _bootstrap(maint: MaintenanceClient) -> list[tuple[str, str]]:
    """Run essential queries before the LLM loop starts."""
    results = []

    try:
        system = _api_retry("bootstrap system", maint.system)
        results.append(("system", system.model_dump_json()))
    except Exception as exc:
        results.append(("system", f"error: {exc}"))

    try:
        wiki = _api_retry("bootstrap wiki_tree", maint.wiki_tree)
        results.append(("wiki_tree", wiki.tree))
    except Exception as exc:
        results.append(("wiki_tree", f"error: {exc}"))

    for path in COMMON_WIKI_DOCS:
        try:
            doc = _api_retry(
                f"bootstrap wiki_load:{path}",
                lambda path=path: maint.wiki_load(path),
            )
            results.append((f"wiki_load:{path}", doc.content))
        except Exception as exc:
            results.append((f"wiki_load:{path}", f"error: {exc}"))

    return results
