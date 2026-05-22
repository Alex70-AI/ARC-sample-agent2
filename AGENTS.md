# Agent Guidance

This repo is for improving the ARC maintenance-ops sample agent and its local
run harness. Keep changes benchmark-general, compact, and easy to inspect.

## Core Rules

1. Do not propose or implement task-specific ARC agent heuristics.
   Improvements must generalize across current and future benchmark tasks. Avoid
   hardcoded spec IDs, task text matching, entity-name shortcuts, or benchmark
   answer patterns that would make the agent brittle as new tasks are added.

2. Keep log and harness changes small.
   Changes to logging, run metadata, and harness behavior should avoid large
   additions, broad abstractions, and long instruction layers. Autonomous-agent
   systems often work best when the core control logic stays small enough to
   audit. If a proposed harness change is growing substantially, pause and
   simplify the design.

## Additional Principles

3. Prefer source-of-truth data over inferred shortcuts.
   Agent behavior should be driven by task text, API results, wiki/policy
   content, and explicit runtime metadata. When changing prompts or control
   flow, favor better evidence gathering and validation over hidden assumptions.

4. Preserve run comparability.
   Harness and logging changes should keep old and new runs easy to compare.
   Add compact metadata when needed, but avoid changing score interpretation,
   mutating historical logs, or producing noisy output that obscures task
   outcomes.

5. Verify with the narrowest useful check.
   For code changes, run the smallest relevant syntax, unit, or smoke check that
   demonstrates the behavior. Do not add broad test machinery unless the change
   affects shared behavior or repeatability.
