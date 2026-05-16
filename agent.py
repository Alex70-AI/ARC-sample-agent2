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

    log: list[dict] = [
        {"role": "system", "content": system_prompt},
    ]
    for label, text in bootstrap_log:
        print(f"  {CLI_GREEN}AUTO {label}{CLI_CLR}: {text[:120]}")
        log.append({"role": "user", "content": f"[{label}]\n{text}"})
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

        fn_type = fn.type
        if isinstance(fn, Req_Respond):
            respond_attempts += 1
        fn_args_data = fn.model_dump(exclude_none=True, exclude={"type"})
        fn_args = fn.model_dump_json(exclude_none=True, exclude={"type"})
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
            if execution_log is not None and log_task_index is not None and log_step_index is not None:
                execution_log.finish_step(
                    log_task_index,
                    log_step_index,
                    platform_result=result,
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
