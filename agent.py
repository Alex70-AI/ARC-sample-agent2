"""
Maintenance-ops sample agent - structured-output loop.

Uses OpenAI structured output (response_format) with provider-specific
Pydantic schema variants over all available API requests. The
MaintenanceClient.dispatch() method
handles routing - no manual tool dispatcher needed.

OpenAI and OpenRouter are supported out of the box through the OpenAI SDK.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Annotated, Any, List, Union, get_args

from annotated_types import MaxLen, MinLen
from openai import OpenAI
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, Field

from execution_log import ExecutionLog
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

CLI_GREEN = "\x1b[32m"
CLI_RED = "\x1b[31m"
CLI_CYAN = "\x1b[36m"
CLI_YELLOW = "\x1b[33m"
CLI_BLUE = "\x1b[34m"
CLI_CLR = "\x1b[0m"

SYSTEM_REFERENCE_PATH = "system_reference/system.md"
RUNTIME_GUIDANCE_PATH = Path("ARC_AGENT.md")
HARNESS_VERSION = "1.4"


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


@dataclass(frozen=True)
class LLMStrategy:
    api_mode: str
    response_model: type[BaseModel]
    schema_variant: str
    extra_body: dict[str, Any] | None = None
    repair_attempts: int = 2


@dataclass(frozen=True)
class LLMCallResult:
    step: BaseModel | None
    usage: Any
    prompt_tokens: int | None
    completion_tokens: int | None
    elapsed_ms: int
    raw_response_excerpt: str | None = None
    raw_reasoning_excerpt: str | None = None
    raw_tool_calls_excerpt: str | None = None
    finish_reason: str | None = None
    error: str | None = None


@dataclass
class TaskContractState:
    task_text: str
    current_user: str | None = None
    role: str | None = None
    today: str | None = None
    wiki_tree: str | None = None
    loaded_docs: dict[str, str] | None = None
    observations: list[dict[str, Any]] | None = None
    write_attempts: list[dict[str, Any]] | None = None
    accepted_writes: list[dict[str, Any]] | None = None
    work_orders_by_id: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.loaded_docs is None:
            self.loaded_docs = {}
        if self.observations is None:
            self.observations = []
        if self.write_attempts is None:
            self.write_attempts = []
        if self.accepted_writes is None:
            self.accepted_writes = []
        if self.work_orders_by_id is None:
            self.work_orders_by_id = {}


@dataclass(frozen=True)
class WriteReadinessResult:
    ready: bool
    reason: str | None = None
    missing_fields: list[str] | None = None
    suggested_search_terms: list[str] | None = None


@dataclass(frozen=True)
class DispatchGuardResult:
    ready: bool
    function: BaseModel | None = None
    reason: str | None = None
    details: dict[str, Any] | None = None


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

    current_state: str = Field(..., description="Brief summary of what you know so far")
    plan: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ..., description="Remaining steps to complete the task (most important first)"
    )
    task_completed: bool = Field(False, description="Set to true only when calling respond")
    function: Action = Field(
        ...,
        description="The next API call to execute",
    )


class NextStepDiscriminated(BaseModel):
    """Structured output schema with explicit union discriminator metadata."""

    current_state: str = Field(..., description="Brief summary of what you know so far")
    plan: List[str]= Field(
        ..., description="Remaining 3 steps to complete the task (most important first)"
    )
    task_completed: bool = Field(False, description="Set to true only when calling respond")
    function: DiscriminatedAction = Field(
        ...,
        description="The next API call to execute",
    )


OPENROUTER_EXTRA_BODY = {
    "provider": {"require_parameters": True},
    "reasoning": {"exclude": True},
}

# Keep discriminated OpenRouter schemas opt-in. Most OpenRouter routes are more
# portable with the base union schema because it avoids oneOf/discriminator.
OPENROUTER_DISCRIMINATED_MODEL_ALLOWLIST: set[str] = set()
_BASE_SCHEMA_FALLBACK_CACHE: set[tuple[str, str]] = set()


def _strategy_cache_key(config: LLMConfig) -> tuple[str, str]:
    return (config.provider.lower(), config.model.lower())


def _strategy_for_config(config: LLMConfig) -> LLMStrategy:
    provider = config.provider.lower()
    cache_key = _strategy_cache_key(config)

    if provider == "openai":
        return LLMStrategy(
            api_mode="responses",
            response_model=NextStep,
            schema_variant="next_step",
        )

    if provider == "openrouter":
        use_discriminated = (
            cache_key not in _BASE_SCHEMA_FALLBACK_CACHE
            and config.model.lower() in OPENROUTER_DISCRIMINATED_MODEL_ALLOWLIST
        )
        response_model = NextStepDiscriminated if use_discriminated else NextStep
        schema_variant = "next_step_discriminated" if use_discriminated else "next_step"
        return LLMStrategy(
            api_mode="chat_completions_raw",
            response_model=response_model,
            schema_variant=schema_variant,
            extra_body=OPENROUTER_EXTRA_BODY,
        )

    return LLMStrategy(
        api_mode="chat_completions",
        response_model=NextStep,
        schema_variant="next_step",
    )


def _response_model_for_provider(provider: str, model: str = "") -> type[BaseModel]:
    return _strategy_for_config(LLMConfig(provider=provider, model=model, api_key="")).response_model


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


def _to_raw_chat_messages(log: list[dict]) -> list[dict]:
    """Flatten pseudo tool-call history for providers that may emit native tool_calls."""
    items: list[dict] = []
    for msg in log:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in {"system", "developer"}:
            items.append({"role": "system", "content": content})
        elif role in {"user", "assistant"}:
            items.append({"role": role, "content": content})
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "tool")
            items.append({"role": "user", "content": f"[tool:{tool_call_id}]\n{content}"})
    return items


def _get_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _truncate(value: Any, limit: int = 1200) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated {len(text) - limit} chars]"


def _usage_token(usage: Any, *names: str) -> int | None:
    for name in names:
        value = _get_field(usage, name)
        if value is not None:
            return value
    return None


def _message_excerpt(message: Any) -> str | None:
    content = _get_field(message, "content")
    if content is not None:
        return _truncate(content)
    refusal = _get_field(message, "refusal")
    if refusal is not None:
        return _truncate(refusal)
    return None


def _response_finish_reason(resp: Any) -> str | None:
    output = _get_field(resp, "output") or []
    if output:
        return _get_field(output[0], "finish_reason")
    return None


def _content_to_text(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            else:
                text = _get_field(item, "text")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(parts) if parts else None
    return str(content)


def _json_schema_response_format(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": model.__name__,
            "strict": True,
            "schema": to_strict_json_schema(model),
        },
    }


def _balanced_json_objects(text: str) -> list[str]:
    candidates: list[str] = []
    in_string = False
    escape = False
    start: int | None = None
    depth = 0

    for idx, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:idx + 1])
                start = None

    return candidates


def _json_candidates_from_text(text: str | None) -> list[str]:
    if not text:
        return []

    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        fenced = match.group(1).strip()
        if fenced:
            candidates.append(fenced)

    candidates.extend(_balanced_json_objects(text))

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


ACTION_TYPE_BY_TOOL_NAME = {
    cls.__name__: cls.model_fields["type"].default
    for cls in get_args(Action)
}
ACTION_TYPE_BY_TOOL_NAME.update({
    value: value for value in ACTION_TYPE_BY_TOOL_NAME.values()
})
ACTION_CLASS_BY_TYPE = {
    cls.model_fields["type"].default: cls
    for cls in get_args(Action)
}
MUTATING_ACTION_TYPES = {
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
READBACK_BY_WRITE_TYPE = {
    "equipment_update": "equipment_get",
    "employee_update": "employee_get",
    "notif_create": "notif_get",
    "notif_update": "notif_get",
    "wo_create": "wo_get",
    "wo_update": "wo_get",
}


def _tool_parameters_schema(action_model: type[BaseModel]) -> dict[str, Any]:
    schema = json.loads(json.dumps(action_model.model_json_schema()))
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("type", None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [item for item in required if item != "type"]
    schema["additionalProperties"] = False
    return schema


def _openrouter_tools() -> list[dict[str, Any]]:
    tools = []
    for action_type, action_model in ACTION_CLASS_BY_TYPE.items():
        tools.append({
            "type": "function",
            "function": {
                "name": action_type,
                "description": f"ARC maintenance API action: {action_type}",
                "parameters": _tool_parameters_schema(action_model),
            },
        })
    return tools


def _loads_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _validate_next_step_candidate(candidate: Any) -> BaseModel:
    if isinstance(candidate, str):
        return NextStep.model_validate_json(candidate)
    return NextStep.model_validate(candidate)


def _next_step_from_text(text: str | None) -> tuple[BaseModel | None, str | None]:
    errors: list[str] = []
    for candidate in _json_candidates_from_text(text):
        try:
            return _validate_next_step_candidate(candidate), None
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        return None, errors[-1]
    return None, "no JSON candidate found in message content"


def _wrap_action_as_next_step(action: dict[str, Any], tool_name: str | None) -> dict[str, Any]:
    action_data = dict(action)
    if "type" not in action_data and tool_name in ACTION_TYPE_BY_TOOL_NAME:
        action_data["type"] = ACTION_TYPE_BY_TOOL_NAME[tool_name]
    return {
        "current_state": "Recovered a native tool call from the model response.",
        "plan": ["Execute the recovered platform action."],
        "task_completed": action_data.get("type") == "respond",
        "function": action_data,
    }


def _next_step_from_tool_calls(tool_calls: Any) -> tuple[BaseModel | None, str | None]:
    if not tool_calls:
        return None, "no tool_calls present"

    errors: list[str] = []
    for tool_call in tool_calls:
        function = _get_field(tool_call, "function")
        tool_name = _get_field(function, "name")
        arguments = _get_field(function, "arguments")
        try:
            parsed_args = _loads_json_value(arguments)
        except Exception as exc:
            errors.append(f"tool_call arguments are not JSON: {exc}")
            continue

        for candidate in (
            parsed_args,
            _wrap_action_as_next_step(parsed_args, tool_name) if isinstance(parsed_args, dict) else None,
        ):
            if candidate is None:
                continue
            try:
                return _validate_next_step_candidate(candidate), None
            except Exception as exc:
                errors.append(str(exc))

    return None, errors[-1] if errors else "tool_calls did not contain a valid NextStep"


def _raw_message_diagnostics(message: Any) -> dict[str, str | None]:
    return {
        "content": _truncate(_content_to_text(_get_field(message, "content"))),
        "reasoning": _truncate(
            _get_field(message, "reasoning")
            or _get_field(message, "reasoning_content")
        ),
        "tool_calls": _truncate(_get_field(message, "tool_calls")),
    }


def _extract_responses_parse_result(resp: Any, elapsed_ms: int) -> LLMCallResult:
    usage = _get_field(resp, "usage")
    step = _get_field(resp, "output_parsed")
    raw_excerpt = _truncate(_get_field(resp, "output_text"))
    return LLMCallResult(
        step=step,
        usage=usage,
        prompt_tokens=_usage_token(usage, "input_tokens", "prompt_tokens"),
        completion_tokens=_usage_token(usage, "output_tokens", "completion_tokens"),
        elapsed_ms=elapsed_ms,
        raw_response_excerpt=raw_excerpt,
        finish_reason=_response_finish_reason(resp),
        error=None if step is not None else "output_parsed is empty",
    )


def _extract_chat_parse_result(resp: Any, elapsed_ms: int) -> LLMCallResult:
    usage = _get_field(resp, "usage")
    choices = _get_field(resp, "choices") or []
    if not choices:
        return LLMCallResult(
            step=None,
            usage=usage,
            prompt_tokens=_usage_token(usage, "prompt_tokens", "input_tokens"),
            completion_tokens=_usage_token(usage, "completion_tokens", "output_tokens"),
            elapsed_ms=elapsed_ms,
            error="empty choices",
        )

    choice = choices[0]
    message = _get_field(choice, "message")
    step = _get_field(message, "parsed")
    diagnostics = _raw_message_diagnostics(message)
    return LLMCallResult(
        step=step,
        usage=usage,
        prompt_tokens=_usage_token(usage, "prompt_tokens", "input_tokens"),
        completion_tokens=_usage_token(usage, "completion_tokens", "output_tokens"),
        elapsed_ms=elapsed_ms,
        raw_response_excerpt=diagnostics["content"] or _message_excerpt(message),
        raw_reasoning_excerpt=diagnostics["reasoning"],
        raw_tool_calls_excerpt=diagnostics["tool_calls"],
        finish_reason=_get_field(choice, "finish_reason"),
        error=None if step is not None else "message.parsed is empty",
    )


def _extract_chat_raw_result(resp: Any, elapsed_ms: int) -> LLMCallResult:
    usage = _get_field(resp, "usage")
    choices = _get_field(resp, "choices") or []
    if not choices:
        return LLMCallResult(
            step=None,
            usage=usage,
            prompt_tokens=_usage_token(usage, "prompt_tokens", "input_tokens"),
            completion_tokens=_usage_token(usage, "completion_tokens", "output_tokens"),
            elapsed_ms=elapsed_ms,
            error="empty choices",
        )

    choice = choices[0]
    message = _get_field(choice, "message")
    diagnostics = _raw_message_diagnostics(message)

    step, content_error = _next_step_from_text(_content_to_text(_get_field(message, "content")))
    tool_error = None
    if step is None:
        step, tool_error = _next_step_from_tool_calls(_get_field(message, "tool_calls"))

    error = None
    if step is None:
        error = f"raw response did not contain a valid NextStep; content={content_error}; tool_calls={tool_error}"

    return LLMCallResult(
        step=step,
        usage=usage,
        prompt_tokens=_usage_token(usage, "prompt_tokens", "input_tokens"),
        completion_tokens=_usage_token(usage, "completion_tokens", "output_tokens"),
        elapsed_ms=elapsed_ms,
        raw_response_excerpt=diagnostics["content"],
        raw_reasoning_excerpt=diagnostics["reasoning"],
        raw_tool_calls_excerpt=diagnostics["tool_calls"],
        finish_reason=_get_field(choice, "finish_reason"),
        error=error,
    )


def _request_next_step(
    client: OpenAI,
    llm_config: LLMConfig,
    strategy: LLMStrategy,
    log: list[dict],
) -> LLMCallResult:
    t0 = time.time()
    if strategy.api_mode == "responses":
        resp = client.responses.parse(
            model=llm_config.model,
            input=_to_responses_input(log),
            text_format=strategy.response_model,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        return _extract_responses_parse_result(resp, elapsed_ms)

    if strategy.api_mode == "chat_completions_raw":
        kwargs: dict[str, Any] = {
            "model": llm_config.model,
            "response_format": _json_schema_response_format(strategy.response_model),
            "messages": _to_raw_chat_messages(log),
            "tools": _openrouter_tools(),
            "tool_choice": "auto",
        }
        if strategy.extra_body is not None:
            kwargs["extra_body"] = strategy.extra_body
        resp = client.chat.completions.create(**kwargs)
        elapsed_ms = int((time.time() - t0) * 1000)
        return _extract_chat_raw_result(resp, elapsed_ms)

    kwargs: dict[str, Any] = {
        "model": llm_config.model,
        "response_format": strategy.response_model,
        "messages": log,
    }
    if strategy.extra_body is not None:
        kwargs["extra_body"] = strategy.extra_body
    resp = client.beta.chat.completions.parse(**kwargs)
    elapsed_ms = int((time.time() - t0) * 1000)
    return _extract_chat_parse_result(resp, elapsed_ms)


def _is_response_format_schema_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "response_format" in text
        and any(term in text for term in ("400", "bad request", "schema", "oneof", "discriminator"))
    )


def _is_repairable_llm_exception(exc: Exception) -> bool:
    text = str(exc).lower()
    if any(term in text for term in ("401", "403", "api key", "authentication", "permission")):
        return False
    return any(
        term in text
        for term in (
            "validation error",
            "invalid json",
            "json_invalid",
            "expected value",
            "parse",
            "parsed",
            "unparseable",
        )
    )


def _llm_error_payload(
    *,
    llm_config: LLMConfig,
    strategy: LLMStrategy,
    step_number: int,
    retry_count: int,
    error: str,
    raw_response_excerpt: str | None = None,
    raw_reasoning_excerpt: str | None = None,
    raw_tool_calls_excerpt: str | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "llm_parse_error",
        "provider": llm_config.provider,
        "model": llm_config.model,
        "api_mode": strategy.api_mode,
        "schema_variant": strategy.schema_variant,
        "step_number": step_number,
        "retry_count": retry_count,
        "finish_reason": finish_reason,
        "raw_response_excerpt": raw_response_excerpt,
        "raw_reasoning_excerpt": raw_reasoning_excerpt,
        "raw_tool_calls_excerpt": raw_tool_calls_excerpt,
        "error": error,
    }


def _record_llm_error(
    execution_log: ExecutionLog | None,
    log_task_index: int | None,
    payload: dict[str, Any],
) -> None:
    if execution_log is None:
        return
    execution_log.add_error(payload)
    if log_task_index is not None and hasattr(execution_log, "add_task_error"):
        execution_log.add_task_error(log_task_index, payload)


def _compact_payload(payload: dict[str, Any]) -> str:
    return _truncate(payload, limit=2000) or "{}"


def _build_repair_message(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "[llm_parse_repair]\n"
            "Your previous response could not be parsed as the required structured output. "
            "Either return one JSON object matching NextStep or call exactly one provided ARC API tool. "
            "If returning JSON, include current_state, plan, task_completed, and function; the function object must include its type. "
            "Do not include commentary, markdown, code fences, channel markers, or any text before or after JSON.\n"
            f"Parse error details: {_compact_payload(payload)}"
        ),
    }


def _build_action_repair_message(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "[llm_action_repair]\n"
            "The previous NextStep selected an API function that did not match its plan. "
            "Choose the correct function.type inside the JSON object or call exactly one provided ARC API tool. "
            "Use system only to retrieve current user, role, date, and public/private context. "
            "For equipment/FLOC lookup use equipment_search or equipment_get. "
            "Return only one valid NextStep JSON object with no extra text.\n"
            f"Action error details: {_compact_payload(payload)}"
        ),
    }


def _build_dispatch_guard_repair_message(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "[harness_dispatch_guard]\n"
            "The harness did not dispatch the requested action because it would violate a "
            "general API safety or response-contract rule. Choose the next corrective action. "
            "If an update needs a current entity baseline, read that exact entity first. "
            "If a write already succeeded, do not repeat it; respond using the accepted result. "
            "If a response has invalid or missing references, repair the response with exact IDs "
            "from API results and the benchmark ground_ref contract.\n"
            f"Guard details: {_compact_payload(payload)}"
        ),
    }


def _openrouter_action_mismatch(step: BaseModel) -> str | None:
    fn = _get_field(step, "function")
    if not isinstance(fn, Req_System):
        return None

    plan_items = _get_field(step, "plan") or []
    plan_text = " ".join(str(item) for item in plan_items)
    current_state = str(_get_field(step, "current_state") or "")
    text = f"{current_state} {plan_text}".lower()

    action_terms = (
        "search", "find", "locate", "lookup", "look up", "retrieve", "get ",
        "load", "create", "raise", "update", "close", "reschedule", "add ",
        "floc", "equipment", "notification", "material", "work order", "wiki",
    )
    system_terms = (
        "current user", "current role", "confirm role", "check role",
        "today", "date", "system context",
    )

    if any(term in text for term in action_terms) and not any(term in text for term in system_terms):
        return (
            "function.type is system, but the plan/current_state describes a platform "
            "lookup or write action. system only returns user, role, date, and context."
        )
    return None


def _field_terms(name: str) -> list[str]:
    terms = [name, name.replace("_", " "), name.replace("_", "-")]
    return [term.lower() for term in terms if term]


def _text_has_any(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(term in lower for term in terms)


def _action_domain_terms(action_type: str) -> list[str]:
    parts = [part for part in action_type.split("_") if part not in {"create", "update", "add", "reorder"}]
    terms = [action_type, action_type.replace("_", " ")]
    terms.extend(parts)
    if "notif" in parts:
        terms.append("notification")
    if "wo" in parts:
        terms.extend(["work order", "workorder"])
    return list(dict.fromkeys(term for term in terms if term))


def _required_and_optional_fields(fn: BaseModel) -> tuple[list[str], list[str]]:
    required: list[str] = []
    optional: list[str] = []
    for name, field in fn.__class__.model_fields.items():
        if name == "type":
            continue
        if field.is_required():
            required.append(name)
        else:
            optional.append(name)
    return required, optional


def _missing_arg_fields(fn: BaseModel, fields: list[str]) -> list[str]:
    data = fn.model_dump(exclude_none=True, exclude={"type"})
    return [field for field in fields if field not in data]


def _loaded_doc_text(contract: TaskContractState) -> str:
    if not contract.loaded_docs:
        return ""
    return "\n".join(
        content
        for path, content in contract.loaded_docs.items()
        if path != SYSTEM_REFERENCE_PATH
    )


def _proposal_text(step: BaseModel, fn: BaseModel) -> str:
    return "\n".join([
        str(_get_field(step, "current_state") or ""),
        " ".join(str(item) for item in (_get_field(step, "plan") or [])),
        fn.model_dump_json(exclude_none=True),
    ])


def _has_policy_evidence_for_action(contract: TaskContractState, action_type: str) -> bool:
    if not contract.loaded_docs:
        return False
    terms = _action_domain_terms(action_type)
    for path, content in contract.loaded_docs.items():
        if path == SYSTEM_REFERENCE_PATH:
            continue
        haystack = f"{path}\n{content}".lower()
        if any(term.lower() in haystack for term in terms):
            return True
    return False


def _has_omit_justification(text: str, fields: list[str]) -> bool:
    lower = text.lower()
    if not any(term in lower for term in ("omit", "irrelevant", "not needed", "not required", "optional")):
        return False
    return any(_text_has_any(lower, _field_terms(field)) for field in fields)


def _loaded_docs_mention_any_field(contract: TaskContractState, fields: list[str]) -> bool:
    doc_text = _loaded_doc_text(contract)
    if not doc_text:
        return False
    return any(_text_has_any(doc_text, _field_terms(field)) for field in fields)


def _build_write_search_terms(fn: BaseModel, missing_optional: list[str]) -> list[str]:
    action_type = fn.type
    terms = _action_domain_terms(action_type)
    terms.extend(missing_optional)
    terms.extend(field.replace("_", " ") for field in missing_optional)
    return list(dict.fromkeys(term for term in terms if term))


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        data = value.model_dump(exclude_none=True)
    elif isinstance(value, dict):
        data = value
    else:
        data = getattr(value, "__dict__", {})
    return data if isinstance(data, dict) else {}


def _cache_work_order(contract: TaskContractState, work_order: Any) -> None:
    data = _as_dict(work_order)
    wo_id = data.get("id") or data.get("wo_id")
    if wo_id is not None:
        contract.work_orders_by_id[str(wo_id)] = data


def _cache_work_orders_from_result(contract: TaskContractState, result: Any) -> None:
    data = _as_dict(result)
    work_order = data.get("work_order") or data.get("workorder")
    if isinstance(work_order, dict):
        _cache_work_order(contract, work_order)
    work_orders = data.get("work_orders")
    if isinstance(work_orders, list):
        for item in work_orders:
            if isinstance(item, dict):
                _cache_work_order(contract, item)


def _wo_update_mutable_fields(fn: Req_WOUpdate) -> dict[str, Any]:
    data = fn.model_dump(exclude_none=True, exclude={"type", "wo_id"})
    return {
        field: value
        for field, value in data.items()
        if field in {"short_desc", "long_desc", "status", "execution_date", "floc"}
    }


def _normalize_wo_update(fn: Req_WOUpdate, contract: TaskContractState) -> DispatchGuardResult:
    baseline = contract.work_orders_by_id.get(str(fn.wo_id))
    if not baseline:
        return DispatchGuardResult(
            ready=False,
            reason="wo_update requires a current work-order baseline before dispatch",
            details={
                "action_type": fn.type,
                "wo_id": fn.wo_id,
                "required_next_action": {"type": "wo_get", "wo_id": fn.wo_id},
            },
        )

    changed: dict[str, Any] = {"wo_id": fn.wo_id}
    stripped: dict[str, Any] = {}
    for field, value in _wo_update_mutable_fields(fn).items():
        if baseline.get(field) == value:
            stripped[field] = value
        else:
            changed[field] = value

    if len(changed) == 1:
        return DispatchGuardResult(
            ready=False,
            reason="wo_update has no effective field changes against the current baseline",
            details={
                "action_type": fn.type,
                "wo_id": fn.wo_id,
                "stripped_unchanged_fields": sorted(stripped),
                "baseline_excerpt": {
                    key: baseline.get(key)
                    for key in ("short_desc", "long_desc", "status", "execution_date", "floc")
                    if key in baseline
                },
            },
        )

    normalized = Req_WOUpdate(**changed)
    return DispatchGuardResult(
        ready=True,
        function=normalized,
        details={
            "action_type": fn.type,
            "wo_id": fn.wo_id,
            "stripped_unchanged_fields": sorted(stripped),
            "dispatched_fields": sorted(k for k in changed if k != "wo_id"),
        },
    )


def _write_target_key(fn: BaseModel) -> tuple[Any, ...] | None:
    if isinstance(fn, Req_WikiUpdate):
        return (fn.type, fn.path)
    if isinstance(fn, Req_WOUpdate):
        return (fn.type, fn.wo_id)
    if isinstance(fn, Req_OperationUpdate):
        return (fn.type, fn.workorder_id, fn.op_id)
    if isinstance(fn, Req_NotifCreate):
        short_desc = re.sub(r"\s+", " ", fn.short_desc.strip().lower())
        return (fn.type, fn.floc, short_desc)
    if isinstance(fn, Req_WOCreate):
        return (fn.type, fn.notification_id)
    if isinstance(fn, Req_MaterialReorder):
        return (fn.type, fn.mat_id)
    if isinstance(fn, Req_NotifUpdate):
        return (fn.type, fn.notif_id)
    if isinstance(fn, Req_UpdateEquipment):
        return (fn.type, fn.floc)
    if isinstance(fn, Req_UpdateEmployee):
        return (fn.type, fn.emp_id)
    return None


def _effective_write_payload(fn: BaseModel) -> dict[str, Any]:
    data = fn.model_dump(exclude_none=True, exclude={"type"})
    for identity in ("path", "wo_id", "workorder_id", "op_id", "notif_id", "floc", "emp_id", "mat_id", "notification_id"):
        data.pop(identity, None)
    return data


def _content_line_overlap(left: Any, right: Any) -> bool:
    def lines(value: Any) -> set[str]:
        return {
            re.sub(r"\s+", " ", line.strip().lower())
            for line in str(value or "").splitlines()
            if line.strip()
        }

    return bool(lines(left) & lines(right))


def _is_repeated_successful_write(fn: BaseModel, contract: TaskContractState) -> DispatchGuardResult:
    target = _write_target_key(fn)
    if target is None:
        return DispatchGuardResult(ready=True, function=fn)

    current_payload = _effective_write_payload(fn)
    for accepted in contract.accepted_writes:
        if accepted.get("target_key") != list(target):
            continue
        previous_payload = accepted.get("effective_payload")
        duplicate_reason = None
        if previous_payload == current_payload:
            duplicate_reason = "same effective payload"
        elif (
            isinstance(fn, Req_WikiUpdate)
            and isinstance(previous_payload, dict)
            and _content_line_overlap(previous_payload.get("content"), current_payload.get("content"))
        ):
            duplicate_reason = "overlapping wiki content for the same path"
        if duplicate_reason is not None:
            return DispatchGuardResult(
                ready=False,
                reason=f"duplicate successful write to the same API target ({duplicate_reason})",
                details={
                    "action_type": fn.type,
                    "target_key": target,
                    "effective_payload": current_payload,
                },
            )
    return DispatchGuardResult(ready=True, function=fn)


def _created_or_updated_refs(contract: TaskContractState) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for accepted in contract.accepted_writes:
        action_type = accepted.get("type")
        args = accepted.get("args") if isinstance(accepted.get("args"), dict) else {}
        result = accepted.get("result") if isinstance(accepted.get("result"), dict) else {}
        if action_type in {"wo_update", "wo_create"}:
            wo_id = args.get("wo_id")
            work_order = result.get("work_order") or result.get("workorder") or {}
            wo_id = wo_id or work_order.get("id") or work_order.get("wo_id")
            if wo_id is not None:
                refs.append({"type": "work_order", "id": str(wo_id)})
        elif action_type in {"notif_create", "notif_update"}:
            notif_id = args.get("notif_id")
            notification = result.get("notification") or {}
            notif_id = notif_id or notification.get("id") or notification.get("notif_id")
            if notif_id is not None:
                refs.append({"type": "notification", "id": str(notif_id)})
        elif action_type == "wiki_update":
            path = args.get("path")
            if path:
                refs.append({"type": "wiki", "id": str(path)})
        elif action_type == "material_reorder":
            mat_id = args.get("mat_id")
            if mat_id is not None:
                refs.append({"type": "material", "id": str(mat_id)})
        elif action_type == "equipment_update":
            floc = args.get("floc")
            if floc:
                refs.append({"type": "equipment", "id": str(floc)})
        elif action_type == "employee_update":
            emp_id = args.get("emp_id")
            if emp_id is not None:
                refs.append({"type": "employee", "id": str(emp_id)})
    return refs


def _validate_respond(fn: Req_Respond, contract: TaskContractState) -> DispatchGuardResult:
    refs = fn.ground_refs or []
    allowed_types = {"work_order", "notification", "equipment", "material", "employee", "operation", "wiki"}
    malformed: list[dict[str, str]] = []
    for ref in refs:
        ref_type = str(ref.type)
        ref_id = str(ref.id)
        if ref_type not in allowed_types:
            malformed.append({"type": ref_type, "id": ref_id, "reason": "ground_ref type is not allowed by this harness contract"})
        elif not ref_id.strip():
            malformed.append({"type": ref_type, "id": ref_id, "reason": "ground_ref id is empty"})
        elif any(fragment in ref_id for fragment in ("},{", "{", "}", "[", "]")):
            malformed.append({"type": ref_type, "id": ref_id, "reason": "ground_ref id contains malformed JSON-like fragments"})
    if malformed:
        return DispatchGuardResult(
            ready=False,
            reason="respond contains invalid ground_refs",
            details={"invalid_refs": malformed},
        )

    present_refs = {(str(ref.type), str(ref.id)) for ref in refs}
    missing_refs = [
        ref for ref in _created_or_updated_refs(contract)
        if (ref["type"], ref["id"]) not in present_refs
    ]
    if missing_refs and fn.outcome == "ok_answer":
        return DispatchGuardResult(
            ready=False,
            reason="respond omits ground_refs for entity/entities successfully written during this task",
            details={"missing_refs": missing_refs},
        )

    return DispatchGuardResult(ready=True, function=fn)


def _prepare_for_dispatch(fn: BaseModel, contract: TaskContractState) -> DispatchGuardResult:
    prepared = fn
    if isinstance(prepared, Req_WOUpdate):
        wo_guard = _normalize_wo_update(prepared, contract)
        if not wo_guard.ready:
            return wo_guard
        prepared = wo_guard.function

    if isinstance(prepared, Req_Respond):
        return _validate_respond(prepared, contract)

    if prepared.type in MUTATING_ACTION_TYPES:
        return _is_repeated_successful_write(prepared, contract)

    return DispatchGuardResult(ready=True, function=prepared)


def _write_readiness(step: BaseModel, fn: BaseModel, contract: TaskContractState) -> WriteReadinessResult:
    action_type = fn.type
    if action_type not in MUTATING_ACTION_TYPES:
        return WriteReadinessResult(ready=True)

    required_fields, optional_fields = _required_and_optional_fields(fn)
    missing_required = _missing_arg_fields(fn, required_fields)
    missing_optional = _missing_arg_fields(fn, optional_fields)
    if missing_required:
        return WriteReadinessResult(
            ready=False,
            reason="write is missing DTO-required fields",
            missing_fields=missing_required,
            suggested_search_terms=_build_write_search_terms(fn, missing_required),
        )
    if isinstance(fn, Req_WOUpdate):
        return WriteReadinessResult(ready=True)

    proposal_text = _proposal_text(step, fn)
    combined_task_and_proposal = f"{contract.task_text}\n{proposal_text}"
    doc_text = _loaded_doc_text(contract)
    has_omit_justification = _has_omit_justification(proposal_text, missing_optional)

    if missing_optional and not has_omit_justification and not _loaded_docs_mention_any_field(contract, missing_optional):
        return WriteReadinessResult(
            ready=False,
            reason=(
                "write omits optional DTO fields, and loaded policy/process evidence "
                "does not yet explain whether those fields are relevant or safely irrelevant"
            ),
            missing_fields=missing_optional,
            suggested_search_terms=_build_write_search_terms(fn, missing_optional),
        )

    relevant_missing = []
    for field in missing_optional:
        terms = _field_terms(field)
        if _text_has_any(combined_task_and_proposal, terms) or _text_has_any(doc_text, terms):
            relevant_missing.append(field)

    if relevant_missing and not _has_omit_justification(proposal_text, relevant_missing):
        return WriteReadinessResult(
            ready=False,
            reason=(
                "write omits optional field(s) that appear relevant from task text, "
                "drafted command, or loaded policy evidence"
            ),
            missing_fields=relevant_missing,
            suggested_search_terms=_build_write_search_terms(fn, relevant_missing),
        )

    return WriteReadinessResult(ready=True)


def _build_write_readiness_repair_message(
    readiness: WriteReadinessResult,
    fn: BaseModel,
    step: BaseModel,
) -> dict[str, str]:
    payload = {
        "action_type": fn.type,
        "reason": readiness.reason,
        "missing_or_challenged_fields": readiness.missing_fields or [],
        "suggested_wiki_search_terms": readiness.suggested_search_terms or [],
        "draft_function": fn.model_dump(exclude_none=True),
        "draft_plan": _get_field(step, "plan") or [],
    }
    return {
        "role": "user",
        "content": (
            "[write_readiness_repair]\n"
            "Do not perform the drafted write yet. Build or repair the task contract first. "
            "Use DTO field names, task wording, wiki_tree, wiki_search, wiki_load, and API observations to gather missing evidence. "
            "Then return the next non-mutating evidence-gathering action, or return a corrected write only if all DTO-required and task/policy-relevant fields are present or explicitly justified as irrelevant.\n"
            f"Readiness failure: {_compact_payload(payload)}"
        ),
    }


def _readback_action_for_write(fn: BaseModel, result: Any) -> BaseModel | None:
    action_type = fn.type
    result_data = result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else _get_field(result, "__dict__", {})

    try:
        if action_type == "notif_create":
            notification = result_data.get("notification", {}) if isinstance(result_data, dict) else {}
            notif_id = notification.get("id")
            return Req_NotifGet(notif_id=notif_id) if notif_id is not None else None
        if action_type == "notif_update":
            return Req_NotifGet(notif_id=fn.notif_id)
        if action_type == "equipment_update":
            return Req_GetEquipment(floc=fn.floc)
        if action_type == "employee_update":
            return Req_GetEmployee(emp_id=fn.emp_id)
        if action_type == "wo_update":
            return Req_WOGet(wo_id=fn.wo_id)
        if action_type == "wo_create":
            work_order = result_data.get("work_order", result_data.get("workorder", {})) if isinstance(result_data, dict) else {}
            wo_id = work_order.get("wo_id") or work_order.get("id")
            return Req_WOGet(wo_id=wo_id) if wo_id is not None else None
    except Exception:
        return None
    return None


def _update_contract_from_bootstrap(contract: TaskContractState, label: str, text: str) -> None:
    if label == "system":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
        contract.current_user = data.get("current_user")
        contract.role = data.get("role")
        contract.today = data.get("today")
    elif label == "wiki_tree":
        contract.wiki_tree = text
    elif label.startswith("benchmark_contract"):
        contract.loaded_docs[SYSTEM_REFERENCE_PATH] = text


def _update_contract_after_result(contract: TaskContractState, fn: BaseModel, result: Any) -> None:
    result_text = result.model_dump_json(exclude_none=True) if hasattr(result, "model_dump_json") else str(result)
    if isinstance(fn, Req_WikiLoad):
        path = _get_field(result, "path") or fn.path
        content = _get_field(result, "content") or result_text
        contract.loaded_docs[str(path)] = str(content)
    elif isinstance(fn, (Req_WOGet, Req_WOSearch, Req_WOUpdate, Req_WOCreate)):
        _cache_work_orders_from_result(contract, result)
        contract.observations.append({
            "type": fn.type,
            "args": fn.model_dump(exclude_none=True, exclude={"type"}),
            "result_excerpt": _truncate(result_text),
        })
    elif isinstance(fn, Req_WikiSearch):
        contract.observations.append({
            "type": fn.type,
            "args": fn.model_dump(exclude_none=True, exclude={"type"}),
            "result_excerpt": _truncate(result_text),
        })
    elif isinstance(fn, Req_WikiTree):
        contract.wiki_tree = _get_field(result, "tree") or result_text
    elif not isinstance(fn, Req_Respond):
        contract.observations.append({
            "type": fn.type,
            "args": fn.model_dump(exclude_none=True, exclude={"type"}),
            "result_excerpt": _truncate(result_text),
        })

def _next_step_with_repair(
    client: OpenAI,
    llm_config: LLMConfig,
    strategy: LLMStrategy,
    log: list[dict],
    *,
    execution_log: ExecutionLog | None,
    log_task_index: int | None,
    step_number: int,
) -> tuple[LLMCallResult, LLMStrategy]:
    repair_count = 0

    while True:
        try:
            result = _request_next_step(client, llm_config, strategy, log)
        except Exception as exc:
            if (
                strategy.response_model is not NextStep
                and _is_response_format_schema_error(exc)
            ):
                payload = _llm_error_payload(
                    llm_config=llm_config,
                    strategy=strategy,
                    step_number=step_number,
                    retry_count=repair_count,
                    error=f"response_format schema rejected; falling back to NextStep: {exc}",
                )
                _record_llm_error(execution_log, log_task_index, payload)
                _BASE_SCHEMA_FALLBACK_CACHE.add(_strategy_cache_key(llm_config))
                strategy = _strategy_for_config(llm_config)
                continue

            payload = _llm_error_payload(
                llm_config=llm_config,
                strategy=strategy,
                step_number=step_number,
                retry_count=repair_count,
                error=str(exc),
            )
            if repair_count < strategy.repair_attempts and _is_repairable_llm_exception(exc):
                print(f"{CLI_YELLOW}parse repair {repair_count + 1}/{strategy.repair_attempts}{CLI_CLR}")
                _record_llm_error(execution_log, log_task_index, payload)
                log.append(_build_repair_message(payload))
                repair_count += 1
                continue

            _record_llm_error(execution_log, log_task_index, payload)
            raise RuntimeError(
                "LLM request failed after "
                f"{repair_count} parse repair attempt(s): {_compact_payload(payload)}"
            ) from exc

        if result.step is not None:
            return result, strategy

        payload = _llm_error_payload(
            llm_config=llm_config,
            strategy=strategy,
            step_number=step_number,
            retry_count=repair_count,
            error=result.error or "unparseable structured output",
            raw_response_excerpt=result.raw_response_excerpt,
            raw_reasoning_excerpt=result.raw_reasoning_excerpt,
            raw_tool_calls_excerpt=result.raw_tool_calls_excerpt,
            finish_reason=result.finish_reason,
        )
        if repair_count < strategy.repair_attempts:
            print(f"{CLI_YELLOW}parse repair {repair_count + 1}/{strategy.repair_attempts}{CLI_CLR}")
            _record_llm_error(execution_log, log_task_index, payload)
            log.append(_build_repair_message(payload))
            repair_count += 1
            continue

        _record_llm_error(execution_log, log_task_index, payload)
        raise RuntimeError(
            "LLM returned unparseable response after "
            f"{repair_count} parse repair attempt(s): {_compact_payload(payload)}"
        )


def smoke_test_llm_protocol(llm_config: LLMConfig) -> None:
    """Verify the selected model can return one valid NextStep via the real request path."""
    client = make_llm_client(llm_config)
    strategy = _strategy_for_config(llm_config)
    log = [
        {
            "role": "system",
            "content": (
                "You are running a protocol smoke test for an ARC maintenance agent. "
                "Return exactly one valid NextStep object. The next API call must be system."
            ),
        },
        {
            "role": "user",
            "content": (
                "Smoke test only: choose the system action to retrieve current user, role, "
                "date, and context. Do not solve any benchmark task."
            ),
        },
    ]
    llm_result, _ = _next_step_with_repair(
        client,
        llm_config,
        strategy,
        log,
        execution_log=None,
        log_task_index=None,
        step_number=0,
    )
    step = llm_result.step
    if step is None or not isinstance(step.function, Req_System):
        action_type = getattr(getattr(step, "function", None), "type", None)
        raise RuntimeError(
            "LLM protocol smoke test returned a valid NextStep but not the expected "
            f"system action; got {action_type!r}."
        )


SYSTEM_PROMPT_BASE = """\
You are a maintenance operations agent on NOVA-7, a gas production platform.
You interact with the platform's maintenance management system through API calls.

Your workflow:
1. Start with system to learn your role and today's date.
2. Read relevant wiki documents to understand policies and SOPs before acting.
3. Investigate the situation using search/get/list endpoints.
4. Take action if your role permits it - or refuse if policy forbids it.
5. Call respond with a clear summary, the correct outcome code, and entity ground refs.

The bootstrapped benchmark contract from system_reference/system.md is
authoritative for response format, allowed outcome codes, allowed ground_ref
types, and wiki/document path conventions. Before every respond call, validate
each ground_ref against that contract. Do not use a ground_ref type that the
contract does not allow.

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
"""

MAX_STEPS = 30
MAX_RESPOND_ATTEMPTS = 3


def _runtime_system_prompt() -> str:
    """Compose the runtime prompt from code-owned base rules and benchmark guidance."""
    try:
        guidance = RUNTIME_GUIDANCE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return SYSTEM_PROMPT_BASE
    if not guidance:
        return SYSTEM_PROMPT_BASE
    return f"{SYSTEM_PROMPT_BASE}\n\nAdditional benchmark guidance:\n\n{guidance}\n"


def run_agent(
    api: CoreClient,
    task: TaskInfo,
    *,
    llm_config: LLMConfig,
    execution_log: ExecutionLog | None = None,
    log_task_index: int | None = None,
    benchmark_context: dict[str, str] | None = None,
) -> None:
    """Run the agent for a single task."""

    client = make_llm_client(llm_config)
    maint = api.get_maintenance_client(task)
    system_prompt = _runtime_system_prompt()
    if execution_log is not None:
        execution_log.set_agent_config(max_steps=MAX_STEPS, system_prompt=system_prompt)

    print(f"\n{CLI_CYAN}Task {task.num}: {task.spec_id}{CLI_CLR}")
    print(f"  {task.task_text}\n")

    if benchmark_context is None:
        benchmark_context = {}
    bootstrap_log = _bootstrap(maint, benchmark_context)
    task_contract = TaskContractState(task_text=task.task_text)

    log: list[dict] = [
        {"role": "system", "content": system_prompt},
    ]
    for label, text in bootstrap_log:
        print(f"  {CLI_GREEN}AUTO {label}{CLI_CLR}: {text[:120]}")
        log.append({"role": "user", "content": f"[{label}]\n{text}"})
        _update_contract_from_bootstrap(task_contract, label, text)
        if execution_log is not None and log_task_index is not None:
            execution_log.add_bootstrap(log_task_index, label, text)

    log.append({"role": "user", "content": task.task_text})

    strategy = _strategy_for_config(llm_config)
    respond_attempts = 0

    for i in range(MAX_STEPS):
        step_id = f"step_{i + 1}"
        print(f"  Step {i + 1}... ", end="", flush=True)

        llm_result, strategy = _next_step_with_repair(
            client,
            llm_config,
            strategy,
            log,
            execution_log=execution_log,
            log_task_index=log_task_index,
            step_number=i + 1,
        )
        step = llm_result.step
        elapsed_ms = llm_result.elapsed_ms
        prompt_tokens = llm_result.prompt_tokens
        completion_tokens = llm_result.completion_tokens

        fn = step.function
        if strategy.api_mode == "chat_completions_raw":
            action_mismatch = _openrouter_action_mismatch(step)
            if action_mismatch is not None:
                payload = _llm_error_payload(
                    llm_config=llm_config,
                    strategy=strategy,
                    step_number=i + 1,
                    retry_count=0,
                    error=action_mismatch,
                    raw_response_excerpt=llm_result.raw_response_excerpt,
                    raw_reasoning_excerpt=llm_result.raw_reasoning_excerpt,
                    raw_tool_calls_excerpt=llm_result.raw_tool_calls_excerpt,
                    finish_reason=llm_result.finish_reason,
                )
                print(f"{CLI_YELLOW}action repair: {action_mismatch}{CLI_CLR}")
                _record_llm_error(execution_log, log_task_index, payload)
                log.append(_build_action_repair_message(payload))
                continue

        if isinstance(fn, Req_Respond):
            respond_attempts += 1

        guard = _prepare_for_dispatch(fn, task_contract)
        if not guard.ready:
            payload = {
                "type": "dispatch_guard_block",
                "function_type": fn.type,
                "reason": guard.reason,
                "details": guard.details or {},
                "function_args": fn.model_dump(exclude_none=True, exclude={"type"}),
            }
            print(f"{CLI_YELLOW}dispatch guard repair: {guard.reason}{CLI_CLR}")
            if execution_log is not None:
                execution_log.add_error(payload)
                if log_task_index is not None and hasattr(execution_log, "add_task_error"):
                    execution_log.add_task_error(log_task_index, payload)
                if isinstance(fn, Req_Respond) and log_task_index is not None:
                    execution_log.set_respond(
                        log_task_index,
                        fn,
                        accepted=False,
                        platform_error=payload,
                    )
            log.append(_build_dispatch_guard_repair_message(payload))
            if isinstance(fn, Req_Respond) and respond_attempts >= MAX_RESPOND_ATTEMPTS:
                print(
                    f"    {CLI_YELLOW}respond rejected {respond_attempts} times; "
                    f"stopping repair loop{CLI_CLR}"
                )
                break
            continue

        fn = guard.function or fn
        fn_type = fn.type
        fn_args_data = fn.model_dump(exclude_none=True, exclude={"type"})
        fn_args = fn.model_dump_json(exclude_none=True, exclude={"type"})

        readiness = _write_readiness(step, fn, task_contract)
        if not readiness.ready:
            task_contract.write_attempts.append({
                "type": fn_type,
                "args": fn_args_data,
                "ready": False,
                "reason": readiness.reason,
                "missing_fields": readiness.missing_fields or [],
            })
            payload = {
                "type": "write_readiness_block",
                "function_type": fn_type,
                "reason": readiness.reason,
                "missing_fields": readiness.missing_fields or [],
                "suggested_search_terms": readiness.suggested_search_terms or [],
                "function_args": fn_args_data,
            }
            print(f"{CLI_YELLOW}write readiness repair: {readiness.reason}{CLI_CLR}")
            if execution_log is not None:
                execution_log.add_error(payload)
                if log_task_index is not None and hasattr(execution_log, "add_task_error"):
                    execution_log.add_task_error(log_task_index, payload)
            log.append(_build_write_readiness_repair_message(readiness, fn, step))
            continue

        print(f"{CLI_CYAN}{fn_type}{CLI_CLR} - {step.plan[0]}  ({elapsed_ms}ms)")
        print(f"    {CLI_YELLOW}args:{CLI_CLR} {fn_args[:300]}")
        log_step_index = None
        if execution_log is not None and log_task_index is not None:
            log_step_index = execution_log.add_step(
                log_task_index,
                step_number=i + 1,
                current_state=step.current_state,
                plan=step.plan,
                function_type=fn_type,
                function_args=fn_args_data,
                llm_duration_ms=elapsed_ms,
                usage=llm_result.usage,
                task_completed=step.task_completed,
                respond_attempt_number=respond_attempts if isinstance(fn, Req_Respond) else None,
            )

        try:
            api.log_llm(
                task_id=task.task_id,
                completion=step.plan[0],
                model=llm_config.model,
                duration_sec=elapsed_ms / 1000,
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

        respond_accepted = False
        try:
            platform_t0 = time.time()
            result = maint.dispatch(fn)
            platform_elapsed_ms = int((time.time() - platform_t0) * 1000)
            result_text = result.model_dump_json(exclude_none=True)
            print(f"    {CLI_GREEN}->{CLI_CLR} {result_text[:200]}")
            respond_accepted = isinstance(fn, Req_Respond)
            platform_log_result: Any = result
            if fn_type in MUTATING_ACTION_TYPES:
                target_key = _write_target_key(fn)
                task_contract.accepted_writes.append({
                    "type": fn_type,
                    "args": fn_args_data,
                    "target_key": list(target_key) if target_key is not None else None,
                    "effective_payload": _effective_write_payload(fn),
                    "result": result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else str(result),
                })
                readback_fn = _readback_action_for_write(fn, result)
                if readback_fn is not None:
                    try:
                        readback = maint.dispatch(readback_fn)
                        readback_text = readback.model_dump_json(exclude_none=True)
                        print(f"    {CLI_GREEN}readback->{CLI_CLR} {readback_text[:200]}")
                        platform_log_result = {
                            "write_result": result.model_dump(exclude_none=True),
                            "readback": readback.model_dump(exclude_none=True),
                        }
                        result_text = json.dumps(platform_log_result, ensure_ascii=False, default=str)
                        _update_contract_after_result(task_contract, readback_fn, readback)
                    except Exception as exc:
                        platform_log_result = {
                            "write_result": result.model_dump(exclude_none=True),
                            "readback_error": str(exc),
                        }
                        result_text = json.dumps(platform_log_result, ensure_ascii=False, default=str)
                        print(f"    {CLI_YELLOW}readback warning: {exc}{CLI_CLR}")
            _update_contract_after_result(task_contract, fn, result)
            if execution_log is not None and log_task_index is not None and log_step_index is not None:
                execution_log.finish_step(
                    log_task_index,
                    log_step_index,
                    platform_result=platform_log_result,
                    platform_duration_ms=platform_elapsed_ms,
                )
            if isinstance(fn, Req_Respond) and execution_log is not None and log_task_index is not None:
                execution_log.set_respond(log_task_index, fn, accepted=True)
        except ApiException as exc:
            platform_elapsed_ms = int((time.time() - platform_t0) * 1000)
            result_text = f'{{"error": "{exc.api_error.error}", "code": "{exc.api_error.code}"}}'
            print(f"    {CLI_RED}ERR: {exc.api_error.error}{CLI_CLR}")
            platform_error = {
                "error": exc.api_error.error,
                "code": exc.api_error.code,
                "status_code": exc.status_code,
            }
            if execution_log is not None and log_task_index is not None and log_step_index is not None:
                execution_log.finish_step(
                    log_task_index,
                    log_step_index,
                    platform_duration_ms=platform_elapsed_ms,
                    error=platform_error,
                )
            if isinstance(fn, Req_Respond) and execution_log is not None and log_task_index is not None:
                execution_log.set_respond(
                    log_task_index,
                    fn,
                    accepted=False,
                    platform_error=platform_error,
                )
        except Exception as exc:
            platform_elapsed_ms = int((time.time() - platform_t0) * 1000)
            result_text = f'{{"error": "{exc}"}}'
            print(f"    {CLI_RED}ERR: {exc}{CLI_CLR}")
            if execution_log is not None and log_task_index is not None and log_step_index is not None:
                execution_log.finish_step(
                    log_task_index,
                    log_step_index,
                    platform_duration_ms=platform_elapsed_ms,
                    error=str(exc),
                )
            if isinstance(fn, Req_Respond) and execution_log is not None and log_task_index is not None:
                execution_log.set_respond(
                    log_task_index,
                    fn,
                    accepted=False,
                    platform_error=str(exc),
                )

        log.append({"role": "tool", "content": result_text, "tool_call_id": step_id})

        if isinstance(fn, Req_Respond):
            if respond_accepted:
                print(f"\n  {CLI_GREEN}Agent responded: {fn.outcome}{CLI_CLR}")
                print(f"  {CLI_BLUE}{fn.message}{CLI_CLR}")
                if fn.ground_refs:
                    for ref in fn.ground_refs:
                        print(f"    ref: {ref.type} -> {ref.id}")
                break
            if respond_attempts >= MAX_RESPOND_ATTEMPTS:
                print(
                    f"    {CLI_YELLOW}respond rejected {respond_attempts} times; "
                    f"stopping repair loop{CLI_CLR}"
                )
                break
            print(f"    {CLI_YELLOW}respond rejected; continuing so the agent can repair it{CLI_CLR}")
    else:
        print(f"\n  {CLI_YELLOW}Reached max steps ({MAX_STEPS}) without responding.{CLI_CLR}")


def _bootstrap(
    maint: MaintenanceClient,
    benchmark_context: dict[str, str],
) -> list[tuple[str, str]]:
    """Run essential queries before the LLM loop starts."""
    results = []

    try:
        system = maint.system()
        results.append(("system", system.model_dump_json()))
    except Exception as exc:
        results.append(("system", f"error: {exc}"))

    try:
        wiki = maint.wiki_tree()
        results.append(("wiki_tree", wiki.tree))
    except Exception as exc:
        results.append(("wiki_tree", f"error: {exc}"))

    if SYSTEM_REFERENCE_PATH in benchmark_context:
        results.append((
            "benchmark_contract_cached",
            f"path: {SYSTEM_REFERENCE_PATH}\n{benchmark_context[SYSTEM_REFERENCE_PATH]}",
        ))
    else:
        try:
            doc = maint.wiki_load(SYSTEM_REFERENCE_PATH)
            benchmark_context[SYSTEM_REFERENCE_PATH] = doc.content
            results.append((
                "benchmark_contract_loaded",
                f"path: {doc.path}\n{doc.content}",
            ))
        except Exception as exc:
            results.append((
                "benchmark_contract_error",
                f"path: {SYSTEM_REFERENCE_PATH}\nerror: {exc}",
            ))

    return results
