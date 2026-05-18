# ARC Harness / Model Reliability Report

Date: 2026-05-17

Scope: objective observations across the latest 8 batch logs by JSON
`started_at`, with recommendations aimed at structural, cross-run fixes rather
than model-specific workarounds.

## Logs Reviewed

- `b_17052026_1024_001.json`
- `b_17052026_1105_002.json`
- `b_17052026_1111_003.json`
- `b_17052026_1113_004.json`
- `b_17052026_1229_005.json`
- `b_17052026_1235_007.json`
- `b_17052026_1358_008.json`
- `b_17052026_1359_009.json`

## Run Summary

| Log | Provider / Model | Completed | Score | Main Signal |
|---|---|---:|---:|---|
| `b_17052026_1024_001` | OpenRouter / `moonshotai/kimi-k2.6` | no | 2/2 scored | stopped on `notification_raise`; system-placeholder loop |
| `b_17052026_1105_002` | OpenRouter / `openai/gpt-5.4-mini` | yes | 4/13 | severe repeated `system` loop; several no-response tasks |
| `b_17052026_1111_003` | OpenRouter / `qwen/qwen3.6-plus` | yes | 2/13 | structured-output incompatibility; many unparseable responses |
| `b_17052026_1113_004` | OpenAI / `gpt-4.1-mini` | yes | 6/13 | valid loop; duplicate writes and reasoning/search misses |
| `b_17052026_1229_005` | OpenRouter / `nvidia/nemotron-3-super-120b-a12b:free` | yes | 6/13 | valid loop; duplicate writes and reasoning/search misses |
| `b_17052026_1235_007` | OpenAI / `gpt-4.1-mini` | yes | 4/13 | valid loop; duplicate writes/creates and reasoning/search misses |
| `b_17052026_1358_008` | OpenAI / `gpt-4.1-mini` | yes | 4/13 | valid loop; duplicate writes, minimal-update issues, reasoning/search misses |
| `b_17052026_1359_009` | OpenRouter / `nvidia/nemotron-3-super-120b-a12b:free` | yes | 6/13 | valid loop; duplicate writes, minimal-update issues, reasoning/search misses |

## High-Level Takeaways

- The best candidate models for this harness are not failing because they cannot
  complete the loop. Direct `gpt-4.1-mini` and Nemotron consistently complete
  every task and respond.
- The dominant structural issues in compatible runs are:
  - duplicate successful writes
  - over-broad update payloads
  - wrong task outcomes due to search/reasoning gaps
  - missing/malformed final references
- Provider/model compatibility remains important:
  - Qwen mostly failed before valid `NextStep` actions.
  - Kimi got stuck on `notification_raise`.
  - OpenRouter `gpt-5.4-mini` had severe repeated `system` loops.
- The latest evidence does **not** justify broad semantic action guards. The
  evidence supports narrower controls and targeted runtime guidance.

## Failure Pattern Counts

Approximate failure-pattern counts across the latest 8 batch logs:

| Pattern | Count | Notes |
|---|---:|---|
| clarification / ambiguity / wrong outcome | 17 | includes `which_one_boss`, `operation_update`, and premature clarification/unsupported choices |
| no response / `got none` | 12 | concentrated in Qwen and OpenRouter GPT repeated-loop failures |
| capacity calculation wrong | 6 | repeated across compatible models |
| duplicate wiki update | 5 | repeated across direct GPT and Nemotron |
| missing or bad reference | 4 | final response/entity reference issue |
| unexpected changed fields | 4 | mostly `wo_update` over-preservation |
| duplicate or missing work-order update count | 3 | scheduling/update tasks |
| notification search miss | 3 | Red/open notification search issues |
| unsupported vs security wrong outcome | 3 | work center creation task |
| duplicate notification create | 1 | direct GPT notification raise |
| wrong risk ranking | 1 | direct GPT notification raise |

## Structural Issues And Recommendations

### 1. Duplicate Successful Writes

Observed:

- `document_review_2` repeatedly failed because models issued multiple
  `wiki_update` calls:
  - direct `gpt-4.1-mini`: 4, 10, and 3 wiki updates in separate runs.
  - Nemotron: 2 wiki updates in separate runs.
- `notification_raise` failed once because direct GPT created 2 notifications
  for the same FLOC/task.
- `work_scheduling` failed because Nemotron issued 3 `wo_update` calls.
- `not_supported` under Nemotron issued 2 `wo_create` calls during a task that
  should not create anything.

Why this is structural:

- The prompt already says not to repeat successful writes, but compatible
  models still repeat writes after successful platform responses.
- This is cross-model and directly causes evaluator failures.

Recommended fix:

- Implement a duplicate-successful-write guard in `agent.py`.
- Track accepted mutating calls and target identities per task.
- Block or repair repeated successful writes to the same target unless the
  previous write failed or the next write is explicitly different and necessary.

Suggested target keys:

- `wiki_update`: `path`
- `wo_update`: `wo_id`
- `operation_update`: `workorder_id` + `op_id`
- `notif_create`: `floc` + similar `short_desc`
- `wo_create`: source notification ID when available
- `material_reorder`: `mat_id`

Expected impact:

- Should directly address repeated wiki edits, duplicate notification creation,
  and repeated work-order updates.
- This is the highest-confidence structural harness fix from the reviewed logs.

File impact:

- `agent.py`

Version impact:

- Minor if implemented narrowly.
- Major only if bundled with broader dispatch/semantic validation.

### 2. Minimal Update Payload Discipline

Observed:

- `workorder_completion` failed because `wo_update` sent unchanged optional
  fields while intending only to set status:
  - unexpected fields: `execution_date`, `floc`, `long_desc`, `short_desc`.
- `work_scheduling` failed because `wo_update` sent unrelated fields:
  - unexpected fields: `floc`, `long_desc`, `short_desc`, `status`.

Why this is structural:

- Models often try to preserve existing values by re-sending them.
- In this benchmark, re-sending unchanged optional fields can count as changing
  fields or expanding the write surface.

Recommended fix:

- Enforce or repair minimal update payloads in `agent.py`.
- For update endpoints, include only fields directly requested by the task or
  required by API validation.
- If a model includes unchanged optional fields, strip them where safe or ask
  for a repair before dispatch.

Useful prompt support:

- Add a short `ARC_AGENT.md` reminder: update only the requested fields unless
  the API requires more.

Expected impact:

- Should reduce unexpected changed-field failures in completion and scheduling
  tasks.

File impact:

- `agent.py` for enforcement.
- Optional `ARC_AGENT.md` guidance.

Version impact:

- Minor for narrow endpoint-specific enforcement.
- Major if implemented as a generalized write-normalization layer.

### 3. Search Breadth, Calculation, And Ambiguity Guidance

Observed:

- `planner_assist` repeatedly failed remaining-capacity calculations across
  models.
- `which_one_boss` repeatedly chose one plausible answer when the evaluator
  expected `none_clarification_needed`.
- `notification_search` sometimes returned `ok_not_found` or missed required
  notification references.
- `not_supported` was often misclassified as `denied_security` or
  `none_clarification_needed` instead of `none_unsupported`.
- `operation_update` was often treated as actionable when evaluator expected
  clarification.

Why this is structural:

- These are common process/search/reasoning failures, not model-specific schema
  failures.
- Broad semantic guards would not reliably fix these because the selected
  actions are often plausible.

Recommended fix:

- Add narrow, evidence-backed `ARC_AGENT.md` guidance. Keep it general and avoid
  exact IDs/spec recipes.

Suggested guidance themes:

- Notification search:
  - before `ok_not_found`, broaden status/risk filters and confirm risk rating
    representation.
- Capacity:
  - include all relevant work orders for the current week and include operation
    man-hours before computing remaining capacity.
- Ambiguity:
  - if multiple plausible equipment/work orders match a natural-language
    reference, do not choose one; respond `none_clarification_needed`.
- Unsupported vs security:
  - distinguish unsupported tool/API surface from role-based access denial.
- Operation/material update:
  - if operation ID, material availability, or target operation does not exactly
    match the user's request, prefer clarification over workaround writes or
    reorders.

Expected impact:

- Should improve recurring task-reasoning failures without hardcoding task
  answers.

File impact:

- `ARC_AGENT.md`

Version impact:

- Minor if written as general guidance.
- Requires human approval because it changes runtime prompt guidance.

### 4. Final Response / Ground Reference Validation

Observed:

- Some tasks performed the right action but failed due to missing or malformed
  final references.
- Examples include expected material/notification refs not being present or
  malformed ground ref IDs after successful platform actions.

Why this is structural:

- The final `respond` call is the benchmark contract boundary.
- A model can complete the platform action and still fail by referencing the
  wrong/malformed entity.

Recommended fix:

- Add `respond` validation in `agent.py` before dispatch.
- Validate:
  - allowed ground ref types
  - ID syntax
  - no malformed fragments such as `},{`
  - after successful writes, the created/updated entity ID is referenced
  - material/work order/notification refs are present when they are central to
    the answer

Expected impact:

- Should reduce failures where the action succeeded but response grounding was
  invalid.

File impact:

- `agent.py`
- optional `ARC_AGENT.md` reminder to use exact IDs from platform results

Version impact:

- Minor for syntax/contract validation.
- Major if it becomes outcome/evidence-aware enough to change task decisions.

### 5. Repeated `system` / No-Progress Safeguards

Observed:

- OpenRouter `gpt-5.4-mini` had 160 `system` actions and 3 no-response tasks.
- Kimi stopped during `notification_raise` after repeated system-placeholder
  behavior.
- Qwen had many no-response outcomes, but mostly because it failed to produce
  parseable `NextStep` objects.
- Direct GPT and Nemotron latest runs did not have major repeated-system
  problems.

Why this is structural:

- The harness already bootstraps `system`; repeated calls usually add no value.
- Some provider/model paths can burn most of the step budget with no-progress
  actions.

Recommended fix:

- Keep this as a lower-priority structural safeguard if proceeding with direct
  GPT or Nemotron.
- If implemented:
  - allow at most one extra `system` call after bootstrap
  - stop/repair when repeated no-op actions occur
  - log no-progress reason clearly

Expected impact:

- Helps provider/model loop failures.
- Less relevant to the latest compatible-model runs.

File impact:

- `agent.py`

Version impact:

- Minor for a simple repeated-system cap.
- Major for broad stagnation/no-progress control.

### 6. Provider Request-Shape Preflight

Observed:

- Qwen's dominant issue was structured-output incompatibility:
  - arrays instead of a `NextStep` object
  - multiple tool-like objects
  - nested `parameters`/`arguments`
  - string `plan` instead of list
- Earlier OpenRouter routes also failed due to provider routing/request shape.

Why this is structural:

- Current preflight can confirm a model exists but not whether it supports the
  actual harness request shape.
- Full benchmark runs are expensive ways to discover request-shape mismatch.

Recommended fix:

- Add a real request-shape smoke test at startup:
  - same API mode
  - same response format/tool exposure
  - same OpenRouter extra body
  - tiny expected `NextStep`

Expected impact:

- Fail fast on incompatible provider/model routes.
- Does not improve compatible-model task accuracy directly.

File impact:

- `agent.py` and/or `main.py`

Version impact:

- Minor.

## Changes Not Recommended Right Now

### Broad Semantic Action Guards

Not recommended as the next step.

Reason:

- The reviewed logs do not show a single broad schema-valid wrong-action
  pattern that dominates across compatible models.
- The strongest failures are narrower:
  - repeated writes after success
  - over-broad update payloads
  - final reference issues
  - task reasoning/search gaps
- Broad semantic guards would add complexity and could block legitimate
  exploratory actions.

### Model-Specific Workarounds

Not recommended in this report.

Reason:

- The goal is structural fixes.
- Kimi/Qwen issues are noted for model-selection/provider compatibility, but the
  proposed changes should not be tailored to a single model's quirks.

## Recommended Change Order

1. `agent.py`: duplicate successful write guard.
2. `agent.py`: minimal update payload enforcement, starting with `wo_update`.
3. `ARC_AGENT.md`: narrow evidence-backed guidance for search breadth,
   capacity calculation, ambiguity, unsupported-vs-security, and exact
   operation/material clarification.
4. `agent.py`: final `respond` reference validation.
5. `agent.py` / `main.py`: provider request-shape smoke test.
6. `agent.py`: repeated-system cap or no-progress detection only if chosen
   provider/model starts looping again.

## Implementation Split

### `agent.py`

Use for enforceable harness behavior:

- duplicate successful write guard
- minimal update payload enforcement
- final `respond` reference validation
- repeated-system/no-progress safeguards
- parse repair classification

### `main.py`

Use for startup/preflight behavior:

- provider request-shape smoke test, if implemented outside `agent.py`

### `ARC_AGENT.md`

Use for general runtime task-solving guidance:

- search breadth before `ok_not_found`
- capacity calculation completeness
- ambiguity recognition
- unsupported vs denied-security distinction
- exact operation/material clarification
- minimal update intent reminder

### `AGENTS.md`

Use for maintainer process only:

- versioning rules
- approval requirements
- post-batch analysis workflow

## Final Conclusion

Across the latest 8 batch logs, the best structural improvements are narrow
controls around writes and final response quality, plus targeted runtime
guidance for repeated reasoning/search failures.

The evidence does not support broad semantic action guards as the next step.
The harness should remain relatively simple and focus on:

- preventing repeated successful writes
- preventing over-broad update payloads
- improving final response grounding
- improving general search/reasoning guidance
- detecting provider/model request-shape incompatibility early

Direct OpenAI `gpt-4.1-mini` and OpenRouter Nemotron appear harness-compatible:
they complete the agent loop, and their remaining failures are mostly write
discipline and task reasoning issues. Qwen/Kimi-style failures are better
treated as model/provider fit issues rather than reasons to complicate the
runtime harness around broad semantic guards.
