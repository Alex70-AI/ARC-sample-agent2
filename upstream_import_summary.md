# Upstream Import Summary

Source upstream repo:

`https://github.com/AI-Solutions-Energy-Pty-Ltd/arc-sample-agent.git`

Imported upstream ref:

`upstream/main` at `e3dc518`

Local comparison copy:

`.compare/upstream-main-e3dc518-export/`

## Imported Or Aligned Changes

### SDK Version

Updated the required ARC SDK version to match upstream:

- `requirements.txt`: `arc-ogchallenge>=0.8.5`
- `pyproject.toml`: `arc-ogchallenge>=0.8.5`

The local environment was also updated to `arc-ogchallenge==0.8.5`.

This imported the current DTO/API contract, including:

- `CoreClient.start_new_task(benchmark, task=..., spec_id=...)`
- `Req_WikiUpdate.mode`
- removal of `ok_not_found` from `Req_Respond.outcome`

### Single-Task Startup

Updated `main.py` to support the upstream task-code flow while preserving local
harness logging and compatibility behavior.

Imported/aligned behavior:

- Added `--task`
- Kept `--spec` as a compatibility alias
- Resolved `task_input = args.task or args.spec`
- Changed standalone startup to:

```python
api.start_new_task(benchmark="maintenance-ops", task=task)
```

Preserved local behavior:

- `RunLogger`
- `load_harness_version()`
- `--no-log`
- `--log-root`
- retry wrapper `_api_retry`
- `ARC_AGENT_NAME` session naming

### Agent DTO Action Surface

Aligned model-selectable DTO actions in `agent.py` with the upstream 0.8.5
active planning surface.

Removed deprecated model-selectable broad-list actions:

- `Req_EquipmentList`
- `Req_EmployeeList`
- `Req_MaterialList`
- `Req_WOList`

Removed deprecated write action:

- `Req_MaterialReorder`

Kept active actions:

- equipment get/search/update
- employee get/search/update
- material get/search
- notification create/get/search/update
- work order create/get/search/update
- operation add/list/update
- wiki tree/load/search/update
- respond

The OSS-only helper action remains local:

- `wiki_section_insert`

### Wiki Update Contract

Updated internal OSS wiki section insert dispatch to use the new explicit
`Req_WikiUpdate.mode` field:

```python
mode="replace_range"
```

This preserves the previous row-range replacement behavior while matching the
0.8.5 DTO contract.

### Runtime Prompt Contract

Updated `ARC_agents.md` to align with upstream/current DTO behavior:

- replaced search/get/list guidance with search/get guidance
- discouraged broad global list endpoints
- removed obsolete `ok_not_found`
- updated zero-result guidance to use:
  - `none_clarification_needed` when a required specific target cannot be
    identified
  - `none_unsupported` when the requested operation is unavailable through
    documented capabilities

### Local Ignore Rules

Added local ignore entries for comparison and imported side folders:

- `.compare/`
- `bitgn-master/`
- `bitgn-agent/`

## Intentionally Not Imported

The upstream sample agent is much simpler than this local harness. The following
upstream changes were intentionally not imported because they would remove local
benchmark instrumentation or reduce run comparability.

### Logging Simplification

Not imported:

- upstream removal of local JSON logging
- upstream removal of `run_logging.py` integration
- upstream simplified per-step logging

Reason:

This repo uses local logs for model comparisons, failure analysis, and OSS parse
diagnostics.

### Harness Loop Simplification

Not imported:

- upstream simplified agent loop
- removal of `HarnessState`
- removal of write guards
- removal of duplicate-write protection
- removal of OSS parse repair/normalization

Reason:

Those local harness features are used to compare model behavior and reduce known
failure modes.

### System Action Reintroduction

Not imported:

- `Req_System` as a model-selectable action

Reason:

This harness already bootstraps system context before model execution. Earlier
runs showed repeated `system` calls could cause non-progress behavior.

### Bootstrap Reduction

Not imported:

- upstream README/sample behavior implying bootstrap only loads `system` and
  `wiki_tree`

Reason:

This harness intentionally preloads `system_reference/system.md` for API/schema
reference.

### Default Model And Session Naming

Not imported:

- upstream default model change to `gpt-4.1-2025-04-14`
- upstream removal of `ARC_AGENT_NAME`

Reason:

The local harness preserves model comparability and session naming used in
existing runs.

### Documentation-Only Changes

Mostly not imported:

- upstream README updates
- upstream Makefile `TASK=` helper
- upstream `.env.example`

Reason:

They are not required for runtime correctness. The functional `--task` behavior
was imported directly into `main.py`.

## Additional Local OSS Changes After Upstream Alignment

These were not upstream imports, but were added after aligning with upstream to
handle observed OSS-120 behavior:

- OSS underscore aliases, for example:
  - `notification_search` -> `notif_search`
  - `notification_create` -> `notif_create`
  - `workorders_search` -> `wo_search`
- safe OSS `respond.message` normalization from:
  - `answer`
  - `summary`
  - `response`
  - completed `current_state`
- compact OSS tool-call snippet logging when OpenRouter returns tool-call-shaped
  output instead of JSON content

These are recorded in harness version `2.7`.

## Validation Performed

Syntax checks:

```powershell
python -m py_compile agent.py main.py run_logging.py
```

SDK contract checks confirmed:

- installed `arc-ogchallenge` is `0.8.5`
- `Req_WikiUpdate.mode` exists
- `Req_Respond.outcome` no longer includes `ok_not_found`

Schema smoke checks confirmed:

- broad-list actions are no longer model-selectable
- `material_reorder` is no longer model-selectable
- OSS aliases normalize observed near-miss names
- missing OSS `respond.message` can be recovered from completed state

