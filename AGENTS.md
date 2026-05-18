# AGENTS.md

## Repository Role

This file is for humans and coding agents working on this repository. Runtime
benchmark behavior for the ARC maintenance agent lives in `ARC_AGENT.md` and is
loaded by `agent.py`.

## Development Rules

- Keep benchmark/runtime prompt guidance out of this file unless it is also
  needed by repository maintainers.
- Put ARC task-solving guidance in `ARC_AGENT.md`.
- After prompt or agent-loop changes, run at least:

```bash
python -m py_compile agent.py main.py
```

- Do not commit secrets from `.env` or logs containing credentials.
- Preserve execution logs unless the user explicitly asks to delete them.

## Harness Versioning

Runtime behavior versions are tracked for changes to `agent.py` and
`ARC_AGENT.md`.

Before editing either file, propose whether the change is:

- no version bump
- minor version bump
- major version bump

Explain the reason and proposed new version. Do not change version identifiers
until the user confirms.

When a version identifier changes, add an entry to
`logs/harness_versions.md` with the version number, change type, concise summary, reason for the bump, and expected benchmark impact.

Use no version bump for comments, formatting, tests-only changes, non-runtime documentation, and model/cost/pricing changes.

Use a minor version bump for bounded runtime fixes within the existing harness
architecture, such as retry classification additions, small prompt
clarifications, preflight improvements, extra logging, or targeted validation
for an already-known failure mode.

Use a major version bump for changes that alter action selection, dispatch
eligibility, stopping conditions, repair policy, evidence requirements, write
gating, provider strategy, or prompt architecture across the benchmark.

## Post-Batch Feedback Loop

After a batch run, analyze recent execution logs before changing runtime
guidance:

- Discover logs by reading `logs/*.json`, excluding `*_summary.json`, and
  parsing JSON content. Do not assume a specific filename pattern.
- Read logs with UTF-8 BOM tolerance, for example `encoding="utf-8-sig"`.
- Identify batch logs by JSON field `mode == "batch"` and sort by JSON
  `started_at`, not by filename or filesystem modified time.
- Prefer the last 4-5 batch logs when available; if fewer exist, analyze all
  available batch logs.
- Include single-task logs only when they are deliberate reruns of a specific
  failed scenario.
- Compare failures and successes by task/spec, including search/action
  sequences, first wrong assumptions, zero-result searches, API errors,
  outcome/ref correctness, and repeated or missing writes.
- Summarize failed tasks, improved/regressed tasks, successful search/action
  patterns, repeated failed patterns, and confidence level for each reusable
  lesson.

Material changes to `ARC_AGENT.md` require human approval:

- First show the proposed guidance change or concise patch summary.
- Explain which log evidence supports each change.
- Do not implement material runtime guidance changes until the user approves.

Never promote exact entity IDs, spec-specific recipes, one-off lucky searches,
or task answers into `ARC_AGENT.md`.
