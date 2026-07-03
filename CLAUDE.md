# CLAUDE.md — claude-ltm

**claude-ltm** = token-first, cross-project **long-term memory + code/docs index**
for Claude Code, packaged as a plugin. Local-first: no API key and no network in
the default configuration, no telemetry. The core runs on the Python **standard
library alone**; real semantic recall, the index, and LLM distillation are opt-in.

This file governs work on **the plugin itself** (this repo). It is developer
tooling — it is *not* shipped to anyone who installs the `ltm` plugin. Only the
contents of [`plugins/ltm/`](./plugins/ltm/) (as declared in
[`.claude-plugin/marketplace.json`](./.claude-plugin/marketplace.json)) reach
installers. Keep that boundary: dev config lives in `.claude/` + `CLAUDE.md`;
shipped behaviour lives under `plugins/ltm/`.

---

## The one constraint (why this project exists)

Claude only consumes **text tokens**; bytes never enter the model — they enter the
*search* layer. So "efficiency" is two separate budgets, optimised independently
(full analysis in [DESIGN.md](./DESIGN.md)):

- **Token budget** — tokens that reach the context window (recall injection).
- **Latency budget** — wall-clock added to a turn (embed + search).

Every design decision serves one or both. When you change behaviour, say which
budget it touches and why it is still a net win.

---

## Hard Prohibitions

No mechanical hook enforcement exists in this repo yet (unlike sibling projects) —
these are prose rules for now. Prefer fixing the cause over bypassing the guard.

| # | Forbidden | Use instead | Reference |
|---|-----------|-------------|-----------|
| 1 | `git push` directly to `main` | Branch first, push the feature branch, open a PR. Recovery: `git branch <feat>; git reset --hard origin/main; git checkout <feat>; git push -u origin <feat>; gh pr create` | [`ltm-git`](./.claude/skills/ltm-git/SKILL.md) |
| 2 | `git commit --no-verify` / `--no-gpg-sign` | Fix whatever the pre-commit / test step is complaining about | [01-general/02-commit-conventions.md](./.claude/rules/01-general/02-commit-conventions.md) |
| 3 | Adding a hard third-party dependency to `plugins/ltm/core/**` | The core is stdlib-only by contract; real embeddings / LLM distillation are **opt-in adapters** under `core/adapters/` and self-provision a venv (`core/provision.py`). Never `import fastembed` at core import time. | [02-architecture/00-overview.md](./.claude/rules/02-architecture/00-overview.md) |
| 4 | A hook that can raise into the interactive turn | Every hook **fails open** — exit 0 on any error and inject nothing. A broken hook must never break a turn. | [02-architecture/02-hooks-and-budgets.md](./.claude/rules/02-architecture/02-hooks-and-budgets.md) |
| 5 | Doing capture / distillation / embedding **on the interactive path** | Capture is detached (spawned worker); recall is byte-capped + threshold-gated. Interactive-token cost must stay ~zero. | [02-architecture/02-hooks-and-budgets.md](./.claude/rules/02-architecture/02-hooks-and-budgets.md) |

---

## Project layout

The authoritative layout, MCP-tool list, config keys, and benchmark live in
[README.md](./README.md) and [DESIGN.md](./DESIGN.md). In brief:

| Path | Purpose |
|---|---|
| `.claude-plugin/marketplace.json` | Marketplace catalogue (lists the `ltm` plugin) |
| `plugins/ltm/.claude-plugin/plugin.json` | Plugin manifest + `userConfig` (**shipped**) |
| `plugins/ltm/core/` | Pure-Python core — Ports & Adapters / CQRS: `store`, `service`, `distill`, `recall`, `indexer`, `embedding`, `adapters/` |
| `plugins/ltm/bin/` | Composition roots — hook entry points, CLI (`ltm`), MCP server, daemon |
| `plugins/ltm/hooks/hooks.json` | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd, PreCompact |
| `plugins/ltm/tests/` | stdlib `unittest` / `pytest` suite |
| `plugins/ltm/bench/` | Labelled recall benchmark + dataset (`ltm eval`) |
| `plugins/ltm/viewer/` | Localhost browser (stdlib `http.server`) |
| `.claude/` + `CLAUDE.md` | **Dev-only** tooling for maintaining this repo (not shipped) |

---

## Rules

Detailed standards live in `.claude/rules/` as numbered folders. Universal folders
load on every conversation (`alwaysApply: true`); area folders can be `paths:`-scoped.

| Folder | Scope | When loaded |
|--------|-------|-------------|
| [`00-quality/`](./.claude/rules/00-quality/) | Code refinement principles, the development-workflow checklist, the testing model | Always |
| [`01-general/`](./.claude/rules/01-general/) | Project intro, tooling (stdlib-first, ruff, unittest/pytest, self-provisioned venv), commit & PR conventions | Always |
| [`02-architecture/`](./.claude/rules/02-architecture/) | CQRS + Hexagonal shape, POEAA pattern map + layering, hook fail-open + token/latency budgets | Always |

Skills (invokable workflows) live in `.claude/skills/` and are prefixed `ltm-`:
[`ltm-analyse`](./.claude/skills/ltm-analyse/SKILL.md),
[`ltm-context`](./.claude/skills/ltm-context/SKILL.md),
[`ltm-git`](./.claude/skills/ltm-git/SKILL.md),
[`ltm-plan`](./.claude/skills/ltm-plan/SKILL.md),
[`ltm-poeaa`](./.claude/skills/ltm-poeaa/SKILL.md),
[`ltm-test`](./.claude/skills/ltm-test/SKILL.md).

---

## AI Assistant Notes

1. **Check `.claude/rules/`** — architecture and quality standards live there, not here.
2. **Stdlib-first core** — `plugins/ltm/core/**` must import cleanly with the standard
   library alone. Semantic embeddings (`fastembed`) and LLM distillation are opt-in
   adapters behind interfaces; they self-provision a venv and fall back gracefully.
3. **Token-first, always detached where possible** — recall is threshold-gated and
   byte-capped; capture runs off the interactive path. State the budget impact of any change.
4. **Hooks fail open** — exit 0 and inject nothing on any error. A 5s hook timeout is the ceiling.
5. **POEAA is real here** — the pattern choices in [DESIGN.md § POEAA / Cosmic Python](./DESIGN.md)
   (Repository over Active Record, Gateway + Separated Interface for embeddings,
   Query Object, Functional Core / Imperative Shell) are load-bearing. Invoke
   [`/ltm-poeaa`](./.claude/skills/ltm-poeaa/SKILL.md) before adding a new pattern.
6. **Measure retrieval changes** — anything touching embeddings, ranking, quantisation,
   or distillation is A/B'd with `python3 bin/ltm eval` before it ships. See
   [DESIGN.md § Embedding backend — measured, not assumed](./DESIGN.md).
7. **Project identity is a marker-walk**, never `basename(cwd)` — see
   [DESIGN.md § Project identity](./DESIGN.md).
8. **GitHub only** — this repo uses git + GitHub PRs. There is no Linear/Slack
   integration; do not add ticket-ID or Slack-notification ceremony to commits or PRs.
