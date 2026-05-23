# Harness Versions


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
