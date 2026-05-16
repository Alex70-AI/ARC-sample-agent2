# ARC Agent Runtime Guidance

## Task Approach

For each task, first identify:

- requested outcome
- current role
- relevant entity types
- whether the task requires lookup, write action, refusal, or clarification

Form a brief plan before API calls. Do not rush into fixed searches, fixed wiki reads, or writes.

## Context Before Acting

Use the bootstrapped `system` result and wiki tree as the context map. Open
`index.md` or folder `Readme.md` only when the tree does not make a folder's
purpose clear.

Load business/policy/wiki documents selectively based on the task, role, entity
type, and planned action.

Choose wiki documents by topic, not by spec ID:

- authority, role permissions, discipline boundaries, or any write action -> governance/RACI material
- notification content, risk, RAM rating, or incident history -> notification guidance plus safety/risk material
- work order status, operations, closure, scheduling, capacity, or materials planning -> maintenance and planning guidance
- equipment naming, FLOC/tag ambiguity, or work-centre ownership -> asset reference material
- task execution details, job steps, or equipment-specific material needs -> the matching SOP/work instruction

Load only the few documents needed for the current decision. Use `wiki_search`
with task, entity, equipment tag, material, status, role, or policy keywords
when the relevant document is unclear.

After the first document or entity reads, update the plan. If new evidence
changes the equipment, discipline, risk basis, authority question, material
need, or lifecycle status, follow the cross-reference or search/load the next
relevant wiki page before deciding.

## Use Evidence, Not Shortcuts

Do not rely on memorized task patterns or shortcut assumptions. Decide from
the task text, current role, API data, and relevant wiki context.

Avoid:

- assuming a fixed document, entity, status, risk rating, work centre, or
  material applies before checking the task and available data
- using entity IDs or FLOCs that were not found or verified in this task
- fixed risk/status/work-center decisions
- assuming one search result is correct without verification
- treating a partial match as sufficient when the task wording is ambiguous

## Search Recovery

Do not treat one failed lookup as enough evidence for `ok_not_found` or
`none_clarification_needed`. Before giving up, run alternate searches that
match the entity type and task wording.

General recovery rules:

- after a zero-result search, try at least one broader or simpler query
- after an API error such as `floc_not_found`, search for the entity by tag
  fragment, nearby equipment, and description before asking for clarification
- if a search returns candidates, inspect full details before choosing or
  ruling them out
- if the task wording is ambiguous, identify and ground the plausible
  candidates rather than selecting one silently

Entity-specific recovery:

- equipment/FLOC: search by exact tag, tag fragment, nearby tag, description,
  and superior/location clues from the task
- work orders: search by work centre and relevant open statuses when phrase
  search fails; inspect likely candidates before asking for a work order ID
- notifications: interpret "open" using policy/status context; do not assume a
  single status is the full open set
- materials: search exact model, shorter model terms, generic part family, SOP
  material names, and equipment-linked material IDs when available
- wiki/SOP: if `wiki_search` has no matches, use the wiki tree and folder
  readmes to choose likely documents instead of stopping

For capacity questions, follow the planning policy exactly. Include all work
orders in the policy-defined committed set, including approved and exec work
orders when the policy says both count, and sum operation `man_hours` from full
work order details.

## Before Writes

Before any update/create/reorder/wiki edit:

- confirm the target entity is correct
- confirm the current role is allowed to perform the action
- confirm the new value/content is supported by data or policy

After a successful write, do not repeat the same write or try alternate row
ranges/updates unless the platform rejected the first write. For wiki edits,
perform exactly one successful `wiki_update` for the requested document.

If not allowed, refuse with `denied_security`.
If ambiguous, respond with `none_clarification_needed`.

## API Behavior

Use only available DTO/API actions from `agent.py`.

Do not invent endpoints, fields, statuses, IDs, or policy facts.

Search when IDs are unknown. Get full entity details before updating.

## Final Response

Every task must end with `respond`.

Before responding, check:

- the message answers the user's actual task
- the outcome matches the facts and policy
- any required change or write was actually accepted by the platform
- calculations follow the relevant policy formula and include the required
  statuses/entities
- not-found or clarification follows only after the recovery searches above
- all useful entity/document refs are valid under the ground ref contract

## Ground Ref Contract

The benchmark response contract loaded from `system_reference/system.md` is
authoritative. Before `respond`, check every `ground_refs` entry against that
contract:

- use only `ground_refs.type` values allowed by the benchmark reference
- use the entity identifier format expected for that type
- for wiki/document paths in this benchmark, use type `wiki`, not `document`
- if a ref is useful in the message but invalid as a `ground_refs` entry, omit
  it from `ground_refs` rather than submitting an invalid response

Never call `respond` with a ground ref type that is not explicitly allowed by
the loaded benchmark contract.

Use the correct outcome:

- `ok_answer`
- `ok_not_found`
- `denied_security`
- `none_clarification_needed`
- `none_unsupported`
- `error_internal`

Include `ground_refs` for entities or documents used.
