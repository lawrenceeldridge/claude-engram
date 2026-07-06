---
alwaysApply: true
---

# Code Quality & Refinement Standards

> **Priority:** This folder defines foundational quality standards that apply to
> ALL code in this repo. Read these before area-specific rules.

These rules cover the code-refinement principles every change follows, the
development-workflow checklist, and the testing model. All files here load on
every conversation (`alwaysApply: true`).

## Files in this folder

| File | Covers |
|---|---|
| [01-code-refinement.md](./01-code-refinement.md) | Five refinement principles (preserve functionality, apply standards, enhance clarity, maintain balance, focus scope) and the development-workflow checklist — reshaped for a stdlib-first, token-conscious Python plugin. |
| [02-testing.md](./02-testing.md) | The test model (`unittest`/`pytest` under `plugins/engram/tests/`) plus the recall-quality benchmark (`engram eval`). Thin pointer to the [`engram-test`](../../skills/engram-test/SKILL.md) skill for operational depth. |

## See also

- [../01-general/](../01-general/) — project intro, tooling, commit & PR conventions.
- [../02-architecture/](../02-architecture/) — CQRS + Hexagonal shape, POEAA pattern map, hook fail-open + token/latency budgets.
- [DESIGN.md](../../../DESIGN.md) — the two-budget model and the pattern choices these standards protect.
