# Harness Version History

Track runtime-impacting changes to `agent.py` and `ARC_AGENT.md` here so batch
results can be compared by harness behavior, not just by model or run date.

Model, cost, and pricing changes are not harness version changes.

## Entry Template

```md
## X.Y - YYYY-MM-DD

Change type: major | minor

Summary:
- ...

Reason for version bump:
- ...

Expected benchmark impact:
- ...
```

## 1.4 - 2026-05-18

Change type: minor

Summary:
- Adds a provider/model protocol smoke test before running tasks, using the
  same structured-output request path as the harness loop.
- Adds dispatch guards for duplicate successful writes to the same API target
  with the same effective payload.
- Adds response-contract validation before `respond` dispatch for invalid
  ground refs and missing refs to successfully written entities.
- Adds a narrow `wo_update` baseline rule: require cached full work-order state
  before update dispatch, strip unchanged optional fields, and block no-op
  updates.

Reason for version bump:
- These are bounded harness reliability controls for write discipline,
  response grounding, and provider compatibility. Runtime task guidance is not
  changed beyond the version identifier.

Expected benchmark impact:
- Should reduce duplicate-write failures, noisy work-order update payloads,
  malformed/missing final references, and wasted batch runs on incompatible
  provider/model request shapes.
- May add a corrective `wo_get` step when a model attempts to update a work
  order without first establishing a baseline.

## 1.3 - 2026-05-17

Change type: minor

Summary:
- Current tracked harness baseline.
- Adds dynamic task-contract/write-readiness behavior already present in
  `agent.py`: task context tracking, loaded wiki/API evidence tracking,
  mutating-action readiness checks, repair prompting before unsafe writes, and
  post-write readback for supported write actions.
- Adds matching `ARC_AGENT.md` runtime guidance to validate writes against DTO
  fields, task-specific requirements, policy/process evidence, and relevant
  optional fields.

Reason for version bump:
- The harness is materially stronger than the earlier 1.x revisions, but it
  has not yet added semantic action guards, stagnation detection, repeated
  system-call limits, or provider request-shape smoke tests. Those are reserved
  for a future 2.0 control-loop upgrade.

Expected benchmark impact:
- Should reduce unsafe or under-evidenced writes and improve auditability after
  writes.
- May increase step count or cause additional evidence-gathering when the model
  drafts a write without enough loaded support.

## 1.2 - before 2026-05-17

Change type: minor

Summary:
- Enhanced execution logging with per-step result summaries, run summaries,
  token accounting, and estimated cost reporting.
- Added cost configuration support for comparing model/provider runs.

Reason for version bump:
- Better reliability analysis and model comparison, without changing the core
  agent decision architecture.

Expected benchmark impact:
- No direct task-solving change expected from logging/cost tracking alone, but
  post-run diagnosis and regression analysis became more reliable.

## 1.1 - before 2026-05-17

Change type: minor

Summary:
- Improved provider handling around structured outputs and OpenRouter raw chat
  completions, including schema/tool recovery and provider-specific strategy
  selection.

Reason for version bump:
- Expanded the harness beyond the original direct OpenAI structured-output path
  while keeping the same general agent loop.

Expected benchmark impact:
- Improved ability to run non-default providers and diagnose parsing/tool-call
  failures.

## 1.0 - before 2026-05-17

Change type: major

Summary:
- First material harness upgrade beyond the original sample agent.
- Established the current structured-output maintenance loop, bootstrapped
  system/wiki context, runtime guidance loading from `ARC_AGENT.md`, and
  execution through `MaintenanceClient.dispatch()`.

Reason for version bump:
- Changed the default sample into the first locally adapted ARC maintenance
  harness.

Expected benchmark impact:
- Established the baseline task-solving behavior used by later 1.x revisions.

## 0.0 - original default agent

Change type: baseline

Summary:
- Original default/sample `agent.py` before local runtime reliability changes.

Reason for version bump:
- None. This is the zero baseline for comparing later harness revisions.

Expected benchmark impact:
- Historical default behavior only; old logs should still be compared by
  commit/date/model plus manual inspection where no explicit version was logged.
