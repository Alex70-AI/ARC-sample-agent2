"""
ARC sample agent - harness entry point.

Usage:
    # Run all tasks in a full session
    python main.py

    # Run a single task by spec_id (development mode)
    python main.py --spec notification_raise

Environment variables (see .env.example):
    ARC_BASE_URL       - ARC server URL (default: https://agentreliabilitychallenge.com)
    ARC_API_KEY        - API key for the platform
    MODEL_PROVIDER     - openai | openrouter
    OPENAI_API_KEY     - OpenAI API key
    OPENROUTER_API_KEY - OpenRouter API key
    MODEL_ID           - model to use
"""
from __future__ import annotations

import argparse
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from ogchallenge_client import CoreClient, ApiException

from agent import LLMConfig, make_llm_client, run_agent
from execution_log import ExecutionLog

DEFAULT_ARC_BASE_URL = "https://agentreliabilitychallenge.com"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL_ID = "gpt-4.1-2025-04-14"

CLI_RED = "\x1b[31m"
CLI_GREEN = "\x1b[32m"
CLI_BLUE = "\x1b[34m"
CLI_CLR = "\x1b[0m"


class ConfigurationError(RuntimeError):
    """Raised when required sample-agent configuration is missing or invalid."""


def _validate_http_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            f"{name} must be a full http(s) URL, got {value!r}."
        )
    return value.rstrip("/")


def _build_platform_client() -> CoreClient:
    base_url = _validate_http_url(
        "ARC_BASE_URL",
        os.getenv("ARC_BASE_URL", DEFAULT_ARC_BASE_URL),
    )
    api_key = os.getenv("ARC_API_KEY", "").strip()
    auth_token = os.getenv("ARC_AUTH_TOKEN", "").strip()
    if not api_key and not auth_token:
        raise ConfigurationError(
            "Platform credentials are missing. Set ARC_API_KEY (preferred) or ARC_AUTH_TOKEN in .env."
        )
    return CoreClient(
        base_url=base_url,
        api_key=api_key or None,
        auth_token=auth_token or None,
    )


def _build_llm_config() -> LLMConfig:
    provider = os.getenv("MODEL_PROVIDER", "openai").strip().lower()
    model = os.getenv("MODEL_ID", DEFAULT_MODEL_ID).strip()
    if not model:
        raise ConfigurationError("MODEL_ID is required.")

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is required when MODEL_PROVIDER=openai."
            )
        return LLMConfig(provider=provider, model=model, api_key=api_key)

    if provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise ConfigurationError(
                "OPENROUTER_API_KEY is required when MODEL_PROVIDER=openrouter."
            )
        referer = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
        app_name = os.getenv("OPENROUTER_APP_NAME", "ARC Sample Agent").strip()
        headers = {"X-Title": app_name}
        if referer:
            headers["HTTP-Referer"] = referer
        return LLMConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=_validate_http_url(
                "OPENROUTER_BASE_URL",
                os.getenv("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
            ),
            default_headers=headers,
        )

    raise ConfigurationError(
        f"Unsupported MODEL_PROVIDER={provider!r}. Use 'openai' or 'openrouter'."
    )


def _preflight_platform(api: CoreClient) -> None:
    print(f"Checking platform connectivity at {api.base_url} ...")
    try:
        benchmarks = api.list_benchmarks()
    except ApiException as exc:
        if exc.status_code in {401, 403}:
            raise ConfigurationError(
                "Platform authentication failed. Check ARC_API_KEY or ARC_AUTH_TOKEN."
            ) from exc
        if exc.api_error.code == "network_error" or exc.status_code == 0:
            raise ConfigurationError(
                f"Cannot reach ARC platform at {api.base_url}. Check ARC_BASE_URL, DNS, firewall, or whether the site is correct."
            ) from exc
        raise ConfigurationError(f"Platform preflight failed: {exc}") from exc

    if not any(bench.id == "maintenance-ops" for bench in benchmarks.benchmarks):
        raise ConfigurationError(
            "Platform is reachable, but benchmark 'maintenance-ops' was not found. Check that ARC_BASE_URL points to the ARC challenge platform."
        )
    print(f"{CLI_GREEN}Platform OK{CLI_CLR}")


def _preflight_llm(llm_config: LLMConfig) -> None:
    print(
        f"Checking LLM provider={llm_config.provider!r} model={llm_config.model!r} ..."
    )
    client = make_llm_client(llm_config)
    try:
        models = client.models.list()
    except Exception as exc:
        raise ConfigurationError(
            "LLM provider preflight failed. Check MODEL_PROVIDER, MODEL_ID, provider base URL, and the matching API key. "
            f"Original error: {exc}"
        ) from exc

    model_ids = {item.id for item in models.data}
    if llm_config.model not in model_ids:
        near_matches = sorted(
            mid for mid in model_ids if llm_config.model.lower() in mid.lower()
        )[:5]
        hint = f" Close matches: {', '.join(near_matches)}." if near_matches else ""
        raise ConfigurationError(
            f"MODEL_ID {llm_config.model!r} was not found for provider {llm_config.provider!r}.{hint}"
        )
    print(f"{CLI_GREEN}LLM OK{CLI_CLR}")


def run_session(api: CoreClient, workspace: str, llm_config: LLMConfig) -> None:
    """Start a full session and run all tasks."""
    execution_log = ExecutionLog(
        mode="batch",
        workspace=workspace,
        llm_provider=llm_config.provider,
        llm_model=llm_config.model,
    )
    print(f"Execution log: {execution_log.path}")
    print(
        "Starting session "
        f"(benchmark=maintenance-ops, workspace={workspace!r}, model={llm_config.model!r})..."
    )
    session = api.start_session(
        benchmark="maintenance-ops",
        workspace=workspace,
        name=f"sample-agent ({llm_config.model})",
        architecture=f"{llm_config.provider} structured-output agent",
    )
    execution_log.set_session(session)
    print(f"Session ID: {session.session_id}  tasks: {session.task_count}\n")

    status = api.session_status(session.session_id)
    scores = []
    benchmark_context: dict[str, str] = {}

    for task_info in status.tasks:
        print("=" * 60)
        log_task_index = execution_log.start_task(task_info)
        api.start_task(task_info)

        try:
            run_agent(
                api,
                task_info,
                llm_config=llm_config,
                execution_log=execution_log,
                log_task_index=log_task_index,
                benchmark_context=benchmark_context,
            )
        except Exception as exc:
            print(f"  {CLI_RED}ERROR: {exc}{CLI_CLR}")
            execution_log.finish_task(log_task_index, error=exc)

        result = api.complete_task(task_info)
        execution_log.finish_task(log_task_index, completion=result)
        if result.eval:
            score = result.eval.score
            scores.append((task_info.spec_id, score))
            style = CLI_GREEN if score >= 0.8 else CLI_RED
            print(f"\n  {style}SCORE: {score:.2f}{CLI_CLR}")
            if score < 1.0 and result.eval.logs:
                explain = "\n".join(f"    {line}" for line in result.eval.logs.splitlines())
                print(f"{CLI_RED}{explain}{CLI_CLR}")

    print("\n" + "=" * 60)
    submitted = api.submit_session(session.session_id)
    print(f"Session submitted - status: {submitted.status}  score: {submitted.score:.2f}")
    execution_log.data["submission"] = {
        "status": submitted.status,
        "score": submitted.score,
    }
    execution_log.finish_run()

    if scores:
        print()
        for spec_id, score in scores:
            style = CLI_GREEN if score >= 0.8 else CLI_RED
            print(f"  {spec_id}: {style}{score:.2f}{CLI_CLR}")
        total = sum(s for _, s in scores) / len(scores) * 100
        print(f"\n  FINAL: {total:.1f}%")


def run_single_task(api: CoreClient, spec_id: str, llm_config: LLMConfig) -> None:
    """Start a standalone task by spec_id (for development/testing)."""
    execution_log = ExecutionLog(
        mode="single",
        llm_provider=llm_config.provider,
        llm_model=llm_config.model,
    )
    print(f"Execution log: {execution_log.path}")
    print(
        f"Starting standalone task: spec={spec_id!r}, provider={llm_config.provider!r}, model={llm_config.model!r}\n"
    )
    task_info = api.start_new_task(benchmark="maintenance-ops", spec_id=spec_id)
    log_task_index = execution_log.start_task(task_info)

    try:
        benchmark_context: dict[str, str] = {}
        run_agent(
            api,
            task_info,
            llm_config=llm_config,
            execution_log=execution_log,
            log_task_index=log_task_index,
            benchmark_context=benchmark_context,
        )
    except Exception as exc:
        print(f"{CLI_RED}ERROR: {exc}{CLI_CLR}")
        execution_log.finish_task(log_task_index, error=exc)

    result = api.complete_task(task_info)
    execution_log.finish_task(log_task_index, completion=result)
    execution_log.finish_run()
    if result.eval:
        score = result.eval.score
        style = CLI_GREEN if score >= 0.8 else CLI_RED
        print(f"\n{style}SCORE: {score:.2f}{CLI_CLR}")
        if score < 1.0 and result.eval.logs:
            explain = "\n".join(f"    {line}" for line in result.eval.logs.splitlines())
            print(f"{CLI_RED}{explain}{CLI_CLR}")
    else:
        print(f"\nStatus: {result.status}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ARC sample agent")
    parser.add_argument("--spec", help="Run a single task by spec_id (skips session)")
    parser.add_argument("--workspace", default="dev", help="Session workspace tag (default: dev)")
    args = parser.parse_args()

    try:
        api = _build_platform_client()
        llm_config = _build_llm_config()
        _preflight_platform(api)
        _preflight_llm(llm_config)
    except ConfigurationError as exc:
        print(f"{CLI_RED}Configuration error:{CLI_CLR} {exc}")
        raise SystemExit(2) from exc
    except Exception as exc:
        print(f"{CLI_RED}Startup failed:{CLI_CLR} {exc}")
        raise SystemExit(2) from exc

    if args.spec:
        run_single_task(api, args.spec, llm_config)
    else:
        run_session(api, workspace=args.workspace, llm_config=llm_config)


if __name__ == "__main__":
    main()


