# AGENTS.md

## Task Approach

For each task, first identify:

- requested outcome
- current role
- relevant entity types
- whether the task requires lookup, write action, refusal, or clarification

Form a brief plan before API calls. Do not rush into fixed searches, fixed wiki reads, or writes.

## No Hardcoded Task Recipes

Do not solve by spec-specific heuristics.

Avoid:

- fixed instructions like "for task X, read document Y"
- hardcoded entity IDs
- fixed risk/status/work-center decisions
- assuming one search result is correct without verification
- treating spec IDs as solutions

Use task text, system context, API data, and relevant retrieved documentation.

## Before Writes

Before any update/create/reorder/wiki edit:

- confirm the target entity is correct
- confirm the current role is allowed to perform the action
- confirm the new value/content is supported by data or policy

If not allowed, refuse with `denied_security`.
If ambiguous, respond with `none_clarification_needed`.

## API Behavior

Use only available DTO/API actions from `agent.py`.

Do not invent endpoints, fields, statuses, IDs, or policy facts.

Search when IDs are unknown. Get full entity details before updating.

## Final Response

Every task must end with `respond`.

Use the correct outcome:

- `ok_answer`
- `ok_not_found`
- `denied_security`
- `none_clarification_needed`
- `none_unsupported`
- `error_internal`

Include `ground_refs` for entities or documents used.
