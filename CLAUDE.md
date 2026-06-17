# crypto trading arena development guide for dummies

This file provides guidance to Claude Code, Codex, and other AI coding assistants when working on the crypto trading arena codebase.

## Dependency management and development environment

Project dependencies are managed with **`uv`**. 
- Do not hand-edit `pyproject.toml` — use `uv add <pkg>` so `uv.lock` stays canonical.
- Use `uv run` to execute project files and tests.

## Architecture

crypto trading arena uses **event-driven agents that consume data streams through Kafka**. Each agent is safe to deploy on its own host, and can consume data-streams to make decisions on realtime data.

## Conventions

- **Commits/PRs landing on `main` use conventional-commit prefixes**: `feat:`, `fix:`, `chore:`, `docs:`,
  `refactor:`, `test:`, `perf:`, `style:`. Pick the narrowest accurate one. PR titles follow the same style.
- Ruff clean for new/changed files.
- Comments and docstrings explain *why*, not *what*.

## Sub-agents

- When planned work is large, you may spawn sub-agents to split up or parallelize the work where possible
- Always spawn sub-agents with the opus model and xhigh thinking effort
- Spawn intelligent sub-agents generously for any kind of review work, investigation, and intel gathering. The goal is a wide and deep fan-out.

## Test driven development

- When implementing any code, please follow test driven development principles using the skill `/test-driven-development`
- Use the skill `/pytest-coverage` to check your test completeness.

## Deep implementation reviews

- Use `/pr-review-toolkit:review-pr` to deeply review the code changes for:
    - functional bugs and issues,
    - anti-patterns,
    - test coverage,
    - documentation correctness & coverage
- Review implementations using the `/simplify` skill to surface any potential design or implementation simplifications using more elegant, well-engineered solutions or designs.
- In certain cases, when prompted, you may have to go through multiple rounds of deep reviews for code changes. In these events, the review is not considered done until the findings from consecutive review rounds converge towards no critical or must-fix issues.
- Spawn intelligent sub-agents generously for any kind of review work, investigation, and intel gathering. The goal is a wide and deep fan-out.

## Development: calfkit agents SDK

- This project dogfoods the calfkit event-driven and distributed agents SDK.
- If you reach use cases that calfkit geniunely does not support, causing you to either reach into calfkit internals or implement a hacky workaround, please create a new issue in the calfkit repo, providing a clear explanation of what you were trying to achieve or design and how calfkit's API surface was insufficient: https://github.com/calf-ai/calfkit-sdk/issues
- If you run into any verifiable bugs or issues in the calfkit SDK, please create an issue explaining the bug clearly and how to reproduce: https://github.com/calf-ai/calfkit-sdk/issues

## Design discussions

- Use the `/grill-with-docs` skill when planning and designing features/implementations
- Use the `/brainstorming` skill in addition to `/grill-with-docs` when the user provides open-ended feature requests without a clear solution or implementation in mind

## Documentation writing

- Use the `/diataxis-docs-writer` skill to write documentation.

## PR final steps

- When an implementation is ready for final review and merge, evaluate whether any decisions during your task qualify for editing or creating ADRs. You can refer to the file `.agents/skills/grill-with-docs/ADR-FORMAT.md` to get guidance on what qualifies.