# Knowledge Sources

Registry of the knowledge sources available to the analyse skill, organised by type.
Each entry documents the path, what it contains, and which workflows should consult
it. The two cheapest sources — the plugin's own memory (`recall`) and its code/docs
index (`search_code`/`search_docs`) — come first, because you consult them *before*
any broad search.

## Contents

- [ltm-memory MCP tools (consult first)](#ltm-memory-mcp-tools-consult-first)
- [Core Documentation](#core-documentation)
- [Rules Files](#rules-files)
- [Source Layout](#source-layout)
- [Diagnostics and Benchmark](#diagnostics-and-benchmark)
- [Sibling Skills](#sibling-skills)
- [Index Freshness](#index-freshness)
- [Pattern Delegation Pattern](#pattern-delegation-pattern)

---

## ltm-memory MCP tools (consult first)

claude-ltm ships an `ltm-memory` MCP server. These are the project's *own* memory and
index — the same surfaces the plugin injects at runtime — and they are cheaper than a
file scan. Consult them before Grep/Glob/Read (the memory-first stop rule).

| # | Tool | Returns | Use for |
|---|------|---------|---------|
| 1 | `recall` | Distilled facts for this project + verdict (`ok`/`low_confidence`/`no_memory`) | Prior decisions, rationale, "did we already do this" — every workflow |
| 2 | `search_code` | Ranked code-symbol outlines (qualname + signature + anchor + freshness) | Locate the affected symbols — Impact, Audit, Deps |
| 3 | `get_symbol` | One symbol's full source by anchor + symbol-precise freshness | Read one function/class once search points at it |
| 4 | `code_outline` | Whole-file / project symbol outline | Structural overview of a module |
| 5 | `search_docs` | Ranked doc-section outlines | Find the relevant README/DESIGN section |
| 6 | `get_doc_section` | One doc section's body by anchor | Pull an exact DESIGN.md section |
| 7 | `index_docs` | (Re)index the project's code + docs | When freshness says a symbol is `edited`/`stale`/`gone` |
| 8 | `list_projects` | Every project in the global store + active-fact count | Confirm this project has memory at all |

### The 3-step access pattern

Never jump straight to reading whole files. Narrow first:

1. **`recall`** — broad discovery of prior decisions/facts, with a calibrated verdict.
2. **`search_code` / `search_docs`** — ranked outlines for the affected area (cheap; no file bodies).
3. **`get_symbol` / `get_doc_section`** — pull the one exact span the outline points at.

**Stop rule.** Trust confident results; open one or two files to confirm. Widen to
Grep/Glob/Read/Explore only when `recall` is `low_confidence`/`no_memory` or the index
search is weak or empty.

### Graceful degradation

If the `ltm-memory` MCP server is unavailable, proceed with README.md / DESIGN.md /
rules + a normal search, and note it: "Note: the ltm-memory MCP server was unavailable
for this analysis; findings come from a direct search of the source and docs."

### What memory uniquely provides

Distilled facts hold what code and git history can't: *why* a default was chosen, what
was tried and failed, the rationale for a pattern, and cross-session continuity.

---

## Core Documentation

The two canonical documents that define the project.

| # | Source | Path | Description | Workflows |
|---|--------|------|-------------|-----------|
| 9 | README.md | `README.md` | Layout, MCP-tool list, CLI, config keys, benchmark, project identity | All |
| 10 | DESIGN.md | `DESIGN.md` | The two budgets, POEAA / Cosmic Python map, memory lifecycle, cache analysis, embedding-backend findings, risk register | All (primary for Budget, Audit, Risk) |

**When to read:** DESIGN.md for any budget/POEAA/risk analysis (it is the source of
truth for pattern choices and the risk register). README.md for the layout, config
keys, MCP tools, and benchmark numbers. Prefer `search_docs` -> `get_doc_section` over
reading either whole.

---

## Rules Files

The dev-only architecture and quality contract in `.claude/rules/` (numbered folders,
`alwaysApply: true`).

| # | Source | Path | Scope | Workflows |
|---|--------|------|-------|-----------|
| 11 | 00-quality/00-overview.md | `.claude/rules/00-quality/00-overview.md` | Code-refinement principles | Audit, Risk |
| 12 | 00-quality/01-code-refinement.md | `.claude/rules/00-quality/01-code-refinement.md` | Refinement checklist | Audit |
| 13 | 00-quality/02-testing.md | `.claude/rules/00-quality/02-testing.md` | Testing model + benchmark trigger | Test |
| 14 | 01-general/01-tooling.md | `.claude/rules/01-general/01-tooling.md` | stdlib-first, ruff, unittest/pytest, self-provisioned venv | Audit, Test |
| 15 | 01-general/02-commit-conventions.md | `.claude/rules/01-general/02-commit-conventions.md` | Commit & PR conventions (GitHub only) | Deps |
| 16 | 02-architecture/00-overview.md | `.claude/rules/02-architecture/00-overview.md` | CQRS + Hexagonal shape, stdlib-core contract | Impact, Audit, Risk |
| 17 | 02-architecture/01-poeaa-and-layers.md | `.claude/rules/02-architecture/01-poeaa-and-layers.md` | POEAA map + layer seams | Audit, Impact |
| 18 | 02-architecture/02-hooks-and-budgets.md | `.claude/rules/02-architecture/02-hooks-and-budgets.md` | Hook fail-open + token/latency budgets | Budget, Risk |

**When to read:** For an audit, read `02-architecture/01-poeaa-and-layers.md` plus the
relevant `00-quality` file. For a budget/risk analysis, read
`02-architecture/02-hooks-and-budgets.md`. Select by scope — don't read all of them.

---

## Source Layout

The code itself, keyed by layer. Locate symbols with `search_code` first, then read.

| # | Path | Layer | Workflows |
|---|------|-------|-----------|
| 19 | `plugins/ltm/core/` | Domain + ports — `store`, `service`, `recall`, `distill`, `indexer`, `index_recall`, `scoring`, `fusion`, `embedding`, `quantize`, `project` | Impact, Audit, Budget |
| 20 | `plugins/ltm/core/adapters/` | Driven adapters — heavy optional deps (`fastembed`) | Audit, Impact |
| 21 | `plugins/ltm/bin/` | Composition roots — hooks, `ltm` CLI, `mcp_server.py`, `daemon.py` | Impact, Deps |
| 22 | `plugins/ltm/hooks/hooks.json` | Hook wiring (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd, PreCompact) | Impact, Budget |
| 23 | `plugins/ltm/tests/` | stdlib `unittest`/`pytest` suite | Test |
| 24 | `plugins/ltm/bench/` | Labelled recall benchmark + dataset (`ltm eval`) | Test, Budget |
| 25 | `plugins/ltm/viewer/` | Localhost browser (stdlib `http.server`) | Impact (viewer changes) |

**When to read:** These are ground truth. The index outline is a starting point;
always read the actual source for findings that matter (Medium/High severity).

---

## Diagnostics and Benchmark

Runnable tools that produce evidence for an analysis.

| # | Command | Produces | Workflows |
|---|---------|----------|-----------|
| 26 | `python3 bin/ltm doctor` | Resolved config, project identity (marker-walk), fact counts | All (grounding) |
| 27 | `python3 bin/ltm eval --backends "hash,fastembed"` | Recall@1/@3, MRR@10, bytes/fact through the real quantised search path | Budget, Test |
| 28 | `python3 -m unittest discover -s tests` | Test suite result (stdlib, no network) | Test, Risk (rollback verification) |

**When to run:** `ltm doctor` to confirm project identity and whether memory exists
before trusting `recall`. `ltm eval` whenever a change touches embeddings, ranking,
quantisation, fusion, or distillation — the A/B evidence is required before it ships.

---

## Sibling Skills

Other `ltm-*` skills carry embedded depth useful for analysis.

| # | Skill | Path | Useful For |
|---|-------|------|-----------|
| 29 | ltm-poeaa | `.claude/skills/ltm-poeaa/` | POEAA & Layering Audit (catalogue, decision trees, anti-patterns) |
| 30 | ltm-test | `.claude/skills/ltm-test/` | Test & Benchmark Strategy (scaffold, review, run `ltm eval`) |
| 31 | ltm-git | `.claude/skills/ltm-git/` | Commit / PR / branch conventions for Dependency Mapping (GitHub only) |
| 32 | ltm-plan | `.claude/skills/ltm-plan/` | Plan structure for sequencing in Dependency Mapping |

---

## Index Freshness

`search_code` / `search_docs` results carry a **freshness** verdict checked against
the live file: `fresh` / `edited` / `stale` / `gone`. The index is kept current
automatically (`SessionStart` auto-indexes; a `PostToolUse` hook re-indexes each
edited file), so it is usually fresh.

### Decision logic

1. Run `search_code` / `search_docs` for the affected area.
2. If the matching symbols are `fresh` — trust the outline, drill in with `get_symbol`.
3. If they are `edited` / `stale` / `gone` — the outline has drifted from disk. Re-index:
   ```
   index_docs        # MCP tool — (re)index the current project's code + docs
   ```
   then re-run the search. (The index is also refreshed automatically at
   `SessionStart` and by the `PostToolUse` re-index hook on every Edit/Write.)

### When to skip

If the analysis only touches files you will `Read` directly, skip the freshness check
and read the files.

---

## Pattern Delegation Pattern

When performing a POEAA & Layering Audit, use this delegation for the source of truth:

### Primary: DESIGN.md + the architecture rules

- [DESIGN.md POEAA / Cosmic Python](../../../../DESIGN.md) — the canonical pattern -> file map (Repository, Gateway + Separated Interface, Query Object, Functional Core / Imperative Shell, Null Object, Composition Root).
- `.claude/rules/02-architecture/01-poeaa-and-layers.md` — the same map plus the layer-seam rules (dependencies point inward; deps import only in adapters).

### Depth: the ltm-poeaa skill

Invoke [`/ltm-poeaa`](../../ltm-poeaa/SKILL.md) for the full catalogue, decision
trees, anti-patterns, and this project's defaults. DESIGN.md is the single source of
truth for *what this project chose*; the skill carries the *why* and the alternatives.
