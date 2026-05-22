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
import time
from typing import Callable, TypeVar
from typing import Annotated, Any, List, Literal, Union

from annotated_types import MaxLen, MinLen
from openai import OpenAI
from pydantic import BaseModel, Field

from ogchallenge_client import CoreClient, MaintenanceClient, TaskInfo, ApiException
from ogchallenge_client.dtos import (
    Req_System,
    Req_EquipmentList, Req_GetEquipment, Req_UpdateEquipment, Req_EquipmentSearch,
    Req_EmployeeList, Req_GetEmployee, Req_UpdateEmployee, Req_EmployeeSearch,
    Req_MaterialList, Req_MaterialGet, Req_MaterialSearch, Req_MaterialReorder,
    Req_NotifCreate, Req_NotifGet, Req_NotifSearch, Req_NotifUpdate,
    Req_WOList, Req_WOSearch, Req_WOCreate, Req_WOGet, Req_WOUpdate,
    Req_OperationAdd, Req_OperationUpdate, Req_OperationList,
    Req_WikiTree, Req_WikiLoad, Req_WikiSearch, Req_WikiUpdate,
    Req_Respond,
)
from harness import RunLogger

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
    read_refs: list[str] | None = None
    write_refs: list[str] | None = None

    def __post_init__(self) -> None:
        if self.read_refs is None:
            self.read_refs = []
        if self.write_refs is None:
            self.write_refs = []

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
            "last_action": self.last_action,
            "last_result": self.last_result,
            "read_refs": self.read_refs[-12:],
            "write_refs": self.write_refs[-12:],
            "last_state": self.last_state,
            "instructions": [
                "Use this as scratchpad state, not as task evidence.",
                "Do not repeat a successful write.",
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
        if (
            fn_type in WRITE_ACTION_TYPES
            and not (
                isinstance(result_payload, dict)
                and result_payload.get("duplicate_successful_write_blocked")
            )
        ):
            self.writes_completed += 1
        self._collect_refs(fn, result_payload)

    def _collect_refs(self, fn: Any, result_payload: Any) -> None:
        fn_type = getattr(fn, "type", "")
        args = fn.model_dump(exclude_none=True) if hasattr(fn, "model_dump") else {}
        if fn_type.startswith("wiki_") and args.get("path"):
            self._remember(self.write_refs if fn_type == "wiki_update" else self.read_refs, f"wiki:{args['path']}")
        _collect_entity_refs(result_payload, self.read_refs, self.write_refs)

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


def _collect_entity_refs(result_payload: Any, read_refs: list[str] | None, write_refs: list[str] | None) -> None:
    if not isinstance(result_payload, dict):
        return

    def remember(target: list[str] | None, value: str) -> None:
        if target is not None and value not in target:
            target.append(value)

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
                remember(write_refs if key in {"notification", "work_order", "operation"} else read_refs, f"{ref_type}:{ref_id}")
    for key, ref_type in collection_keys.items():
        values = result_payload.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    ref_id = value.get("id") or value.get("floc")
                    if ref_id is not None:
                        remember(read_refs, f"{ref_type}:{ref_id}")


Action = Union[
    Req_System,
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
    plan: List[str]= Field(
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


SYSTEM_PROMPT = """\
You are a maintenance operations agent on NOVA-7, a gas production platform.
You interact with the platform's maintenance management system through API calls.

Your workflow:
1. Start with system to learn your role and today's date.
2. Read relevant wiki documents to understand policies and SOPs before acting.
3. Investigate the situation using search/get/list endpoints.
4. Take action if your role permits it - or refuse if policy forbids it.
5. Call respond with a clear summary, the correct outcome code, and entity ground refs.

Outcome codes:
- ok_answer              - task completed, clear answer given
- ok_not_found           - requested information doesn't exist
- denied_security        - your role or policy doesn't permit the action
- none_clarification_needed - task is ambiguous, need more info
- none_unsupported       - can't do this with available tools
- error_internal         - unexpected error

Always check your authority in raci.md before performing write actions.
Always consult RAM.md and incidents.md before assigning risk assessments.
Include ground_refs to entities you referenced or acted on in your respond call.

Minimal operating harness:
- Keep current_state as a compact task ledger: task type, known facts, open questions, and last action result.
- Classify the task before acting: LOOKUP/ANSWER means answer only and perform no writes; WRITE means the user explicitly asked to create, update, close, reorder, reschedule, or modify a specific object.
- Treat every write as gated. Before creating or updating notifications, work orders, operations, equipment, employees, materials, or wiki pages, verify role authority in governance/raci.md and confirm the requested target is unambiguous.
- After any successful write/update/create/close/reorder API call, do not repeat that write. If the side effect completed, either respond or perform only a read needed to verify the final answer.
- For risk, priority, safety, incident, or maintenance-plan decisions, consult RAM.md, incidents.md, and the relevant SOP/wiki page before acting.
- If a lookup returns zero or multiple plausible targets after reasonable search, do not guess. Use API data and policy docs to decide between ok_not_found and none_clarification_needed.
- If policy or role authority forbids the requested action, respond denied_security and explain the specific policy reason.
- If the requested operation has no available API/tool or documented system capability, respond none_unsupported, not denied_security.
- Before respond, verify that all required side effects succeeded, the outcome code matches the real result, and ground_refs include every entity read, created, or changed that supports the answer.
- Do not rely on benchmark-specific shortcuts or memorized task patterns. Derive each decision from the task text, loaded policy/wiki content, API schemas, and API results available in the current run.
"""

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
    harness_state = HarnessState()
    successful_write_signatures: set[str] = set()

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
        fn_args = fn.model_dump_json(exclude_none=True, exclude={"type"})
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
        print(f"{CLI_CYAN}{fn_type}{CLI_CLR} - {step.plan[0]}  ({elapsed_ms}ms)")
        print(f"    {CLI_YELLOW}args:{CLI_CLR} {fn_args[:300]}")

        try:
            api.log_llm(
                task_id=task.task_id,
                completion=step.plan[0],
                model=llm_config.model,
                duration_sec=(time.time() - t0),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception:
            pass

        log.append({
            "role": "assistant",
            "content": step.plan[0],
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
        if fn_type in WRITE_ACTION_TYPES and action_signature in successful_write_signatures:
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
        else:
            try:
                result = _api_retry(
                    f"maintenance API call {fn_type}",
                    lambda: maint.dispatch(fn),
                )
                result_payload = result.model_dump(exclude_none=True)
                if result_payload:
                    result_text = result.model_dump_json(exclude_none=True)
                else:
                    result_text = json.dumps(
                        {
                            "ok": True,
                            "action": fn_type,
                            "message": (
                                "The API call completed successfully. "
                                "Do not repeat this write unless a later read proves it failed."
                            ),
                        }
                    )
                    result_payload = json.loads(result_text)
                if fn_type in WRITE_ACTION_TYPES:
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

        log.append({"role": "tool", "content": result_text, "tool_call_id": step_id})
        harness_state.update_from_step(step=step, fn=fn, result_payload=result_payload)
        step_event["harness_state"] = {
            "task_type": harness_state.task_type,
            "writes_completed": harness_state.writes_completed,
            "read_refs": harness_state.read_refs[-12:],
            "write_refs": harness_state.write_refs[-12:],
        }
        if run_logger:
            run_logger.record_step(**step_event)

        if isinstance(fn, Req_Respond):
            print(f"\n  {CLI_GREEN}Agent responded: {fn.outcome}{CLI_CLR}")
            print(f"  {CLI_BLUE}{fn.message}{CLI_CLR}")
            if fn.ground_refs:
                for ref in fn.ground_refs:
                    print(f"    ref: {ref.type} -> {ref.id}")
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
