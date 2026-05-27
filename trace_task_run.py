"""Run one ARC task with a full LLM and ARC request/answer trace.

This is intentionally separate from agent.py and the compact RunLogger. It is
for one-off diagnosis of what was sent to the model or ARC platform and what
came back.

Example:
    python trace_task_run.py --task notification_raise --model openai/gpt-oss-120b
    python trace_task_run.py --task notification_raise --model gpt-5.4-mini --provider openai
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Callable

from dotenv import load_dotenv
from pydantic import BaseModel


def _infer_provider(model: str) -> str:
    return "openrouter" if "/" in model else "openai"


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower() or "run"


def _default_trace_path(task: str, model: str) -> Path:
    stamp = datetime.now().astimezone().strftime("%d%m%y_%H%M%S")
    return Path("logs") / "llm_traces" / f"trace_{stamp}_{_slug(task)}_{_slug(model)}.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, type):
        if issubclass(value, BaseModel):
            return {
                "schema_model": value.__name__,
                "json_schema": value.model_json_schema(),
            }
        return value.__name__
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(exclude_none=True)
            if isinstance(dumped, dict):
                dumped.pop("usage", None)
                return _jsonable(dumped)
        except Exception:
            pass
    return str(value)


def _api_error_answer(exc: Exception) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "error": str(exc),
        "error_type": type(exc).__name__,
    }
    api_error = getattr(exc, "api_error", None)
    if api_error is not None:
        answer["api_error"] = _jsonable(api_error)
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        answer["status_code"] = status_code
    return answer


def _filename_from_path(path: str | None) -> str | None:
    if not path:
        return None
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _wiki_content_marker(path: str | None, content: str) -> dict[str, Any]:
    return {
        "omitted": "wiki_file_content",
        "path": path,
        "file": _filename_from_path(path),
        "chars": len(content),
        "lines": len(content.splitlines()),
    }


def _sanitize_arc_payload(
    payload: Any,
    *,
    endpoint: str,
    direction: str,
    request_path: str | None,
) -> Any:
    data = _jsonable(payload)
    if (
        endpoint == "/wiki/load"
        and direction == "response"
        and isinstance(data, dict)
        and isinstance(data.get("content"), str)
    ):
        path = request_path or data.get("path")
        data = dict(data)
        data["content"] = _wiki_content_marker(path, data["content"])
    return data


def _request_payload(method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"method": method}
    if args:
        payload["args"] = _jsonable(args)
    cleaned_kwargs = dict(kwargs)
    cleaned_kwargs.pop("model", None)
    payload["kwargs"] = _jsonable(cleaned_kwargs)
    return payload


def _chat_answer(resp: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {}
    try:
        message = resp.choices[0].message
    except Exception:
        return {"raw": _jsonable(resp)}

    content = getattr(message, "content", None)
    if content is not None:
        answer["content"] = _jsonable(content)
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        answer["parsed"] = _jsonable(parsed)
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        answer["tool_calls"] = _jsonable(tool_calls)
    return answer


def _responses_answer(resp: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {}
    output_text = getattr(resp, "output_text", None)
    if output_text is not None:
        answer["output_text"] = output_text
    output_parsed = getattr(resp, "output_parsed", None)
    if output_parsed is not None:
        answer["parsed"] = _jsonable(output_parsed)
    if "output_text" not in answer and "parsed" not in answer:
        dumped = _jsonable(resp)
        if isinstance(dumped, dict):
            dumped.pop("usage", None)
        answer["raw"] = dumped
    return answer


class TraceRecorder:
    def __init__(self, *, path: Path, model: str) -> None:
        self.path = path
        self.model = model
        self.step = 0
        self.data: dict[str, Any] = {"model": model, "steps": []}
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record(
        self,
        *,
        kind: str,
        outgoing_at: str,
        incoming_at: str,
        duration_ms: float,
        request: dict[str, Any],
        answer: dict[str, Any],
    ) -> None:
        self.step += 1
        self.data["steps"].append({
            "step": self.step,
            "kind": kind,
            "outgoing_at": outgoing_at,
            "incoming_at": incoming_at,
            "duration_ms": round(duration_ms, 3),
            "request": request,
            "answer": answer,
        })
        self.flush()

    def wrap_call(
        self,
        method_name: str,
        original: Callable[..., Any],
        answer_fn: Callable[[Any], dict[str, Any]],
    ) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            request = _request_payload(method_name, args, kwargs)
            outgoing_at = _now_iso()
            started = perf_counter()
            try:
                resp = original(*args, **kwargs)
            except Exception as exc:
                self.record(
                    kind="llm",
                    outgoing_at=outgoing_at,
                    incoming_at=_now_iso(),
                    duration_ms=(perf_counter() - started) * 1000,
                    request=request,
                    answer={"error": str(exc)},
                )
                raise
            self.record(
                kind="llm",
                outgoing_at=outgoing_at,
                incoming_at=_now_iso(),
                duration_ms=(perf_counter() - started) * 1000,
                request=request,
                answer=answer_fn(resp),
            )
            return resp

        return wrapped

    def wrap_client(self, client: Any) -> Any:
        client.responses.parse = self.wrap_call(
            "responses.parse",
            client.responses.parse,
            _responses_answer,
        )
        client.beta.chat.completions.parse = self.wrap_call(
            "beta.chat.completions.parse",
            client.beta.chat.completions.parse,
            _chat_answer,
        )
        client.chat.completions.create = self.wrap_call(
            "chat.completions.create",
            client.chat.completions.create,
            _chat_answer,
        )
        return client

    def wrap_arc_post(self, client: Any) -> Any:
        if getattr(client, "_trace_task_run_wrapped", False):
            return client

        original_post = client._post

        def traced_post(
            path: str,
            request_model: BaseModel | None,
            response_model: type[Any],
            token: str | None = None,
            retry: bool | None = None,
        ) -> Any:
            endpoint = "/" + path.lstrip("/")
            request_path = getattr(request_model, "path", None)
            request = {
                "method": "arc.post",
                "endpoint": endpoint,
                "request_model": type(request_model).__name__ if request_model is not None else None,
                "response_model": getattr(response_model, "__name__", str(response_model)),
                "payload": _sanitize_arc_payload(
                    request_model,
                    endpoint=endpoint,
                    direction="request",
                    request_path=request_path,
                ),
            }
            if retry is not None:
                request["retry"] = retry

            outgoing_at = _now_iso()
            started = perf_counter()
            try:
                resp = original_post(path, request_model, response_model, token=token, retry=retry)
            except Exception as exc:
                self.record(
                    kind="arc",
                    outgoing_at=outgoing_at,
                    incoming_at=_now_iso(),
                    duration_ms=(perf_counter() - started) * 1000,
                    request=request,
                    answer=_api_error_answer(exc),
                )
                raise

            answer = {
                "response_model": type(resp).__name__,
                "payload": _sanitize_arc_payload(
                    resp,
                    endpoint=endpoint,
                    direction="response",
                    request_path=request_path,
                ),
            }
            self.record(
                kind="arc",
                outgoing_at=outgoing_at,
                incoming_at=_now_iso(),
                duration_ms=(perf_counter() - started) * 1000,
                request=request,
                answer=answer,
            )
            return resp

        client._post = traced_post
        client._trace_task_run_wrapped = True
        return client

    def wrap_arc_client(self, api: Any) -> Any:
        self.wrap_arc_post(api)
        original_get_maintenance_client = api.get_maintenance_client

        def traced_get_maintenance_client(*args: Any, **kwargs: Any) -> Any:
            maint = original_get_maintenance_client(*args, **kwargs)
            return self.wrap_arc_post(maint)

        api.get_maintenance_client = traced_get_maintenance_client
        return api


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one ARC task with full LLM and ARC trace")
    parser.add_argument("--task", required=True, help="Task code or spec_id to run")
    parser.add_argument("--model", required=True, help="Model id to use")
    parser.add_argument(
        "--provider",
        choices=("openai", "openrouter"),
        help="LLM provider. Defaults to openrouter when model contains '/', else openai.",
    )
    parser.add_argument("--workspace", default="dev", help="Workspace tag, default: dev")
    parser.add_argument("--trace-path", help="Output JSON trace path")
    parser.add_argument(
        "--skip-platform-preflight",
        action="store_true",
        help="Skip ARC platform preflight check.",
    )
    args = parser.parse_args()

    load_dotenv()
    provider = args.provider or _infer_provider(args.model)
    os.environ["MODEL_PROVIDER"] = provider
    os.environ["MODEL_ID"] = args.model

    trace_path = Path(args.trace_path) if args.trace_path else _default_trace_path(args.task, args.model)
    recorder = TraceRecorder(path=trace_path, model=args.model)

    import agent as agent_module
    import main as main_module

    original_make_llm_client = agent_module.make_llm_client

    def traced_make_llm_client(config: Any) -> Any:
        return recorder.wrap_client(original_make_llm_client(config))

    agent_module.make_llm_client = traced_make_llm_client

    api = recorder.wrap_arc_client(main_module._build_platform_client())
    llm_config = main_module._build_llm_config()

    if not args.skip_platform_preflight:
        main_module._preflight_platform(api)

    print(f"Trace log: {trace_path}")
    main_module.run_single_task(api, args.task, llm_config, run_logger=None)


if __name__ == "__main__":
    main()
