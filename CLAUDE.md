# CLAUDE.md — claude-engram

**claude-engram** = token-first, cross-project **long-term memory + code/docs index**
for Claude Code, packaged as a plugin. Local-first: no API key and no network in
the default configuration, no telemetry. The core runs on the Python **standard
library alone**; real semantic recall, the index, and LLM distillation are opt-in.

This file governs work on **the plugin itself** (this repo). It is developer
tooling — it is *not* shipped to anyone who installs the `engram` plugin. Only the
contents of [`plugins/engram/`](./plugins/engram/) (as declared in
[`.claude-plugin/marketplace.json`](./.claude-plugin/marketplace.json)) reach
installers. Keep that boundary: dev config lives in `.claude/` + `CLAUDE.md`;
shipped behaviour lives under `plugins/engram/`.

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
| 1 | `git push` directly to `main` | Branch first, push the feature branch, open a PR. Recovery: `git branch <feat>; git reset --hard origin/main; git checkout <feat>; git push -u origin <feat>; gh pr create` | [`engram-git`](./.claude/skills/engram-git/SKILL.md) |
| 2 | `git commit --no-verify` / `--no-gpg-sign` | Fix whatever the pre-commit / test step is complaining about | [01-general/02-commit-conventions.md](./.claude/rules/01-general/02-commit-conventions.md) |
| 3 | Adding a hard third-party dependency to `plugins/engram/core/**` | The core is stdlib-only by contract; real embeddings / LLM distillation are **opt-in adapters** under `core/adapters/` and self-provision a venv (`core/provision.py`). Never `import fastembed` at core import time. | [02-architecture/00-overview.md](./.claude/rules/02-architecture/00-overview.md) |
| 4 | A hook that can raise into the interactive turn | Every hook **fails open** — exit 0 on any error and inject nothing. A broken hook must never break a turn. | [02-architecture/02-hooks-and-budgets.md](./.claude/rules/02-architecture/02-hooks-and-budgets.md) |
| 5 | Doing capture / distillation / embedding **on the interactive path** | Capture is detached (spawned worker); recall is byte-capped + threshold-gated. Interactive-token cost must stay ~zero. | [02-architecture/02-hooks-and-budgets.md](./.claude/rules/02-architecture/02-hooks-and-budgets.md) |

---

## Project layout

The authoritative layout, MCP-tool list, config keys, and benchmark live in
[README.md](./README.md) and [DESIGN.md](./DESIGN.md). In brief:

| Path | Purpose |
|---|---|
| `.claude-plugin/marketplace.json` | Marketplace catalogue (lists the `engram` plugin) |
| `plugins/engram/.claude-plugin/plugin.json` | Plugin manifest + `userConfig` (**shipped**) |
| `plugins/engram/core/` | Pure-Python core — Ports & Adapters / CQRS: `store`, `service`, `distill`, `recall`, `indexer`, `embedding`, `adapters/` |
| `plugins/engram/bin/` | Composition roots — hook entry points, CLI (`engram`), MCP server, daemon |
| `plugins/engram/hooks/hooks.json` | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd, PreCompact |
| `plugins/engram/tests/` | stdlib `unittest` / `pytest` suite |
| `plugins/engram/bench/` | Labelled recall benchmark + dataset (`engram eval`) |
| `plugins/engram/viewer/` | Localhost browser (stdlib `http.server`) |
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

Skills (invokable workflows) live in `.claude/skills/` and are prefixed `engram-`:
[`engram-analyse`](./.claude/skills/engram-analyse/SKILL.md),
[`engram-context`](./.claude/skills/engram-context/SKILL.md),
[`engram-design`](./.claude/skills/engram-design/SKILL.md),
[`engram-git`](./.claude/skills/engram-git/SKILL.md),
[`engram-plan`](./.claude/skills/engram-plan/SKILL.md),
[`engram-poeaa`](./.claude/skills/engram-poeaa/SKILL.md),
[`engram-test`](./.claude/skills/engram-test/SKILL.md).

---

## AI Assistant Notes

1. **Check `.claude/rules/`** — architecture and quality standards live there, not here.
2. **Stdlib-first core** — `plugins/engram/core/**` must import cleanly with the standard
   library alone. Semantic embeddings (`fastembed`) and LLM distillation are opt-in
   adapters behind interfaces; they self-provision a venv and fall back gracefully.
3. **Token-first, always detached where possible** — recall is threshold-gated and
   byte-capped; capture runs off the interactive path. State the budget impact of any change.
4. **Hooks fail open** — exit 0 and inject nothing on any error. A 5s hook timeout is the ceiling.
5. **POEAA is real here** — the pattern choices in [DESIGN.md § POEAA / Cosmic Python](./DESIGN.md)
   (Repository over Active Record, Gateway + Separated Interface for embeddings,
   Query Object, Functional Core / Imperative Shell) are load-bearing. Invoke
   [`/engram-poeaa`](./.claude/skills/engram-poeaa/SKILL.md) before adding a new pattern.
6. **Measure retrieval changes** — anything touching embeddings, ranking, quantisation,
   or distillation is A/B'd with `python3 bin/engram eval` before it ships. See
   [DESIGN.md § Embedding backend — measured, not assumed](./DESIGN.md).
7. **Project identity defaults to the workspace root** (`identity=workspace`): the folder
   Claude was started in (`CLAUDE_PROJECT_DIR`, else cwd), hashed for a collision-free key
   with its basename as the label. `identity=marker` is the legacy marker-walk; `.engram-root`
   overrides both. Never use the raw `basename(cwd)` as the *key* — hash the path. See
   [DESIGN.md § Project identity](./DESIGN.md).
8. **GitHub only** — this repo uses git + GitHub PRs. There is no Linear/Slack
   integration; do not add ticket-ID or Slack-notification ceremony to commits or PRs.
