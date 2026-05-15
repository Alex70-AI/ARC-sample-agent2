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
import time
from typing import Annotated, List, Union

from annotated_types import MaxLen, MinLen
from openai import OpenAI
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
"""

MAX_STEPS = 30


def run_agent(
    api: CoreClient,
    task: TaskInfo,
    *,
    llm_config: LLMConfig,
    execution_log: ExecutionLog | None = None,
    log_task_index: int | None = None,
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
        if execution_log is not None and log_task_index is not None:
            execution_log.add_bootstrap(log_task_index, label, text)

    log.append({"role": "user", "content": task.task_text})

    response_model = _response_model_for_provider(llm_config.provider)

    for i in range(MAX_STEPS):
        step_id = f"step_{i + 1}"
        print(f"  Step {i + 1}... ", end="", flush=True)

        t0 = time.time()
        try:
            if llm_config.provider == "openai":
                resp = client.responses.parse(
                    model=llm_config.model,
                    input=_to_responses_input(log),
                    text_format=response_model,
                )
            else:
                resp = client.beta.chat.completions.parse(
                    model=llm_config.model,
                    response_format=response_model,
                    messages=log,
                )
        except Exception as exc:
            raise RuntimeError(
                "LLM request failed for "
                f"provider={llm_config.provider!r}, model={llm_config.model!r}. "
                "Check MODEL_PROVIDER, MODEL_ID, and the matching provider API key. "
                f"Original error: {exc}"
            ) from exc
        elapsed_ms = int((time.time() - t0) * 1000)

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
            if execution_log is not None and log_task_index is not None:
                execution_log.add_error("LLM returned unparseable response")
            break

        fn = step.function
        fn_type = fn.type
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
                usage=resp.usage,
            )

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

        try:
            result = maint.dispatch(fn)
            result_text = result.model_dump_json(exclude_none=True)
            print(f"    {CLI_GREEN}->{CLI_CLR} {result_text[:200]}")
            if execution_log is not None and log_task_index is not None and log_step_index is not None:
                execution_log.finish_step(
                    log_task_index,
                    log_step_index,
                    platform_result=result,
                )
        except ApiException as exc:
            result_text = f'{{"error": "{exc.api_error.error}", "code": "{exc.api_error.code}"}}'
            print(f"    {CLI_RED}ERR: {exc.api_error.error}{CLI_CLR}")
            if execution_log is not None and log_task_index is not None and log_step_index is not None:
                execution_log.finish_step(
                    log_task_index,
                    log_step_index,
                    error={
                        "error": exc.api_error.error,
                        "code": exc.api_error.code,
                        "status_code": exc.status_code,
                    },
                )
        except Exception as exc:
            result_text = f'{{"error": "{exc}"}}'
            print(f"    {CLI_RED}ERR: {exc}{CLI_CLR}")
            if execution_log is not None and log_task_index is not None and log_step_index is not None:
                execution_log.finish_step(
                    log_task_index,
                    log_step_index,
                    error=str(exc),
                )

        log.append({"role": "tool", "content": result_text, "tool_call_id": step_id})

        if isinstance(fn, Req_Respond):
            if execution_log is not None and log_task_index is not None:
                execution_log.set_respond(log_task_index, fn)
            print(f"\n  {CLI_GREEN}Agent responded: {fn.outcome}{CLI_CLR}")
            print(f"  {CLI_BLUE}{fn.message}{CLI_CLR}")
            if fn.ground_refs:
                for ref in fn.ground_refs:
                    print(f"    ref: {ref.type} -> {ref.id}")
            break
    else:
        print(f"\n  {CLI_YELLOW}Reached max steps ({MAX_STEPS}) without responding.{CLI_CLR}")


def _bootstrap(maint: MaintenanceClient) -> list[tuple[str, str]]:
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

    return results
