# Harness Versions

## 3.0

- Changed repeated blocked/non-progress no-write auto-responses from
  `error_internal` to `none_clarification_needed`.
- Grounded no-write forced clarification responses with write, read, and support
  refs so clarification outcomes keep available evidence.

## 2.6

- Aligned model-selectable action DTOs with arc-ogchallenge 0.8.5.
- Removed deprecated broad-list and material-reorder actions from structured
  planning schemas and OSS aliases.
- Updated runtime prompt for the current outcome/action contract.
- Made OSS wiki section insert dispatch use explicit `wiki_update`
  `replace_range` mode.
- Added OSS-only underscore action aliases for notification and work-order
  near-miss names.
- Added safe OSS `respond.message` normalization from answer/summary/response
  fields and completed-response state.
- Logged compact OSS tool-call snippets when OpenRouter returns tool-call
  shaped repair output without JSON content.

## 2.5

- Added OpenRouter gpt-oss schema-alias normalization for known wrapper/action
  names.
- Tightened OSS JSON contract examples for `task_type` and `function.type`.
- Logged compact OSS normalization hints inside parse-failure metadata.


## 2.4

- Added run-level wiki evidence storage so full `wiki_load` content is logged
  once per run and step results reference it by evidence key.
- Added wiki search and wiki write evidence slots for fuller audit trails
  without duplicating large payloads inside every step.
- Added three-stage `respond_trace` logging: model parsed payload, augmented
  payload, and dispatched payload or harness block reason.
- Added compact OSS parse-failure metadata with short raw/repair snippets and
  finish reasons.

## 2.3

- Deduplicated successful non-system bootstrap records into a run-level
  `bootstrap_manifest` while keeping per-task `system` context and bootstrap
  errors.
- Added `log_schema_version` and compact task-level analysis fields for later
  success/failure workflow analysis.
- Added small step-level flags for writes, blocked actions, and respond
  outcomes without removing step workflow evidence.

## 2.2

- Reduced fixed wiki bootstrap to `system_reference/system.md` only, so policy,
  SOP, risk, and planning docs must be loaded on demand from `wiki_tree`.

## 2.1

- Reduced false ambiguity nudges with task-scope and qualifier-based candidate resolution.
- Added failed-write response steering with support refs from failed write arguments.
- Reworked repeated-block fallback to force response after successful writes before auto-submitting.

## 2.0

- Added an in-process candidate ledger and compact open-ambiguity scratchpad state.
- Added a one-shot pre-respond ambiguity nudge for unresolved multi-candidate answers.
- Logged compact candidate and ambiguity summaries without changing model schemas.
- Changed task and batch log filenames to use compact model suffixes instead of default `_001`, `_002` timestamp collision counters.
- Kept a rare collision fallback that appends a numeric suffix only if the same timestamp and model log already exists.

## 1.3

- Removed `system` from model-selectable actions because system context is
  already provided during bootstrap.
- Updated runtime guidance to treat bootstrap `system` data as the source for
  role and date, and to avoid compensating writes that the user did not ask for.
- Added a compact material-reorder guard for unrequested stock remediation.
- Added an early auto-response after repeated harness-blocked non-progress.
- Matched OpenRouter plan validation to OpenAI by requiring at least one plan
  item.

## 1.2

- Moved the ARC agent runtime prompt into `ARC_agents.md`.
- Replaced the embedded `SYSTEM_PROMPT` body in `agent.py` with a file loader.
- Added compact runtime guidance for source priority, outcome-code selection,
  write gating, ambiguity handling, ground refs, and non-heuristic behavior.
- Added a failed-write guard that blocks unrelated follow-on writes.
- Relaxed ready-to-respond handling to permit one final read/search, but not writes.

## 1.1

- Success-aware write accounting and duplicate-write tracking.
- Read/write ground-ref tracking by action class, with respond-time ref augmentation.
- Lightweight loop guards for ready-to-respond drift and repeated non-progress calls.
- Compact log summaries for harness-blocked actions.

## 1.0

- Default ARC-sample agent
- Added logging of task and batch runs.
