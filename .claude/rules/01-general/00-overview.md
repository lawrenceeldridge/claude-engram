---
alwaysApply: true
---

# claude-engram Project

> **Read first:** see [../00-quality/](../00-quality/) for the code-refinement
> principles and testing model that apply to ALL code.

claude-engram is a **token-first, cross-project long-term memory + code/docs index**
for Claude Code, packaged as a plugin. It captures sessions off the interactive
path, distils atomic facts, embeds them compactly, and injects the *relevant* ones
back into context via hooks — while also indexing code/docs into ranked outlines so
recall + `search_code`/`get_symbol` replace broad Grep/Glob/Read sweeps.

**Local-first.** No API key and no network in the default configuration, no
telemetry. The **core runs on the Python standard library alone**; semantic recall
(`fastembed`), the index, and LLM distillation are opt-in.

**This repo is the plugin's source, not an installed instance.** Work here maintains
the plugin. `.claude/` + `CLAUDE.md` are dev-only and are never shipped; only
`plugins/engram/` reaches installers (see [../../../CLAUDE.md](../../../CLAUDE.md)).

## Files in this folder

| File | Covers |
|---|---|
| [01-tooling.md](./01-tooling.md) | Python version, stdlib-first dependency contract, `ruff`, `unittest`/`pytest`, the self-provisioned managed venv, the `engram` CLI. |
| [02-commit-conventions.md](./02-commit-conventions.md) | Conventional Commits format, allowed types/scopes, branch + PR flow. GitHub-only — no Linear/Slack ceremony. |

## Repository structure

The authoritative layout lives in [README.md § Layout](../../../README.md). Key dirs:

- `plugins/engram/core/` — pure-Python core (Ports & Adapters / CQRS).
- `plugins/engram/bin/` — composition roots: hook entry points, `engram` CLI, MCP server, daemon.
- `plugins/engram/hooks/hooks.json` — the hook wiring.
- `plugins/engram/tests/`, `plugins/engram/bench/`, `plugins/engram/viewer/` — tests, benchmark, viewer.

## Before starting work

1. **Consult memory/index first** — this project practises what it ships: `recall` /
   `search_code` / `search_docs` before a broad search (see the global memory-first rule).
2. **Read [DESIGN.md](../../../DESIGN.md)** — the two-budget model and POEAA pattern
   choices are the "why" behind most rules here.
3. **Know which side you're on** — write side (capture, latency-tolerant, detached)
   vs. read side (recall, hot-path, token-critical). They have opposite profiles.
4. Present a short plan and wait for approval before implementing non-trivial changes.

## See also

- [../00-quality/](../00-quality/) — refinement principles, testing model.
- [../02-architecture/](../02-architecture/) — CQRS + Hexagonal, POEAA map, hooks & budgets.
