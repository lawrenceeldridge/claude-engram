---
alwaysApply: true
---

# Tooling

## Technical standards

Python 3 with full type hints, `ruff` for linting/formatting, `unittest`/`pytest`
for tests. No async requirement — hooks are short-lived processes and recall is a
synchronous, sub-10ms cosine scan.

## The stdlib-first dependency contract

**`plugins/engram/core/**` must import and run with the Python standard library
alone.** This is the project's defining constraint, not a nicety:

- The default `embedding=hash` (lexical stub) and `distiller=heuristic` (line
  extraction) are **zero-dependency** — the plugin works out of the box with no pip
  install and no network.
- Real semantic recall (`fastembed`) and LLM distillation are **opt-in adapters**.
  `fastembed` is imported lazily inside `core/adapters/fastembed_gw.py`, never at
  core import time. `tree-sitter-language-pack` is optional too — code indexing
  falls back to stdlib `ast` for Python when it is absent.
- **Never add a hard third-party import to `core/**`.** If a feature needs a heavy
  dependency, it goes behind an adapter interface and degrades gracefully when absent.

## The self-provisioned managed venv

When `embedding=fastembed`, the plugin **provisions its own private venv** under
`${CLAUDE_PLUGIN_DATA}` on first use and re-execs the hook into it — no manual pip
needed (see `core/provision.py`, `bin/_bootstrap.py`, `plugins/engram/requirements.txt`).
Pin an existing interpreter that already has `fastembed` via the `python` userConfig
/ `ENGRAM_PYTHON` env var to skip provisioning.

## Running things

All commands run from `plugins/engram/`:

```bash
cd plugins/engram
python3 -m unittest discover -s tests   # test suite (all stdlib)
python3 bin/engram doctor                  # resolved config, project identity, counts
python3 bin/engram demo                    # capture sample facts then recall (end-to-end)
python3 bin/engram eval --backends hash    # recall-quality benchmark
python3 bin/engram viewer                  # localhost memory/index viewer
ruff check . && ruff format .           # lint + format
```

Install the plugin for iterating:

```bash
claude --plugin-dir ./plugins/engram       # session-scoped
```

## Configuration

Behaviour is driven by the plugin's `userConfig` (exposed to scripts as
`CLAUDE_PLUGIN_OPTION_*`) or `ENGRAM_*` env vars for standalone use. The full key
list, defaults, and meanings are in [README.md § Configuration](../../../README.md)
and `plugins/engram/.claude-plugin/plugin.json` — that manifest is the source of truth
for defaults; keep README and manifest in sync when either changes.

## See also

- [../00-quality/02-testing.md](../00-quality/02-testing.md) — the test + benchmark surfaces.
- [../02-architecture/00-overview.md](../02-architecture/00-overview.md) — why the core/adapter split exists.
