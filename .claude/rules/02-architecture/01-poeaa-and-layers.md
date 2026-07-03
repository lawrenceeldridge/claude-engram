---
alwaysApply: true
---

# POEAA Patterns & Layering

claude-ltm uses **Patterns of Enterprise Application Architecture** (POEAA) /
Cosmic Python patterns deliberately. These are not decoration — they are the reason
the plugin stays testable on the stdlib, swaps embedding backends without touching
the core, and keeps capture off the hot path. The canonical map (from
[DESIGN.md § POEAA / Cosmic Python](../../../DESIGN.md)):

| Role | Pattern | File |
|---|---|---|
| Overall shape | CQRS + Hexagonal (Ports & Adapters) | whole plugin |
| Capture pipeline | Command / Handler, idempotent per fact | `core/service.py` |
| Distil / rank / quantise | Functional Core / Imperative Shell | `core/distill.py`, `recall.py`, `quantize.py` |
| Memory access | **Repository over Data Mapper** (never Active Record) | `core/store.py` |
| Query params | Query Object | `core/recall.py::search` |
| Embedding provider | **Gateway + Separated Interface** | `core/embedding.py`, `core/adapters/` |
| Injected payload | DTO (deliberately one line per fact) | `core/recall.py::render_block` |
| Empty recall | Special Case / Null Object (inject nothing) | `render_block` returns `""` |
| Wiring | Composition Root | `bin/*` entry points |

## Layer seams (do not collapse)

```
bin/*  (composition roots, driving adapters: hooks, CLI, MCP server, daemon)
   │ wires
   ▼
core/  (domain + ports: service, store, recall, indexer, scoring, distill, embedding)
   │ depends on interfaces, not implementations
   ▼
core/adapters/  (driven adapters: fastembed_gw, …)  ← the only place heavy deps import
```

- **Dependencies point inward.** `core/` never imports from `bin/`; `core/` depends
  on the *interface* (`embedding.py`, the distiller protocol), not on `fastembed`.
- **Adapters are the seam for optional deps.** `fastembed` is imported lazily in
  `core/adapters/fastembed_gw.py`. Adding a new embedding or distiller backend means
  a new adapter behind the existing interface — not an `if backend == …` in the core.
- **Composition roots wire, they don't compute.** `bin/*` reads config, picks
  adapters, and calls the core. Business logic does not live in a hook script.

## Rules

1. **Repository, not Active Record.** All memory access goes through `core/store.py`.
   Facts are plain data + a mapper; they do not carry their own persistence methods.
2. **Query Object for search params.** Extend the query object, don't grow a
   positional-argument signature on `search`.
3. **Functional core / imperative shell.** Distillation, ranking, scoring, and
   quantisation are pure functions over data; I/O (DB, model, spawning workers) lives
   in the shell. Keep new logic pure where it can be — it is what makes the core
   stdlib-testable.
4. **Null Object for empty recall.** No match → inject `""`, never a placeholder or
   an error. Irrelevant turns cost zero tokens.
5. **New pattern? Invoke [`/ltm-poeaa`](../../skills/ltm-poeaa/SKILL.md) first** — it
   carries the catalogue, decision trees, anti-patterns, and this project's defaults.

## See also

- [DESIGN.md](../../../DESIGN.md) — full rationale, memory lifecycle, cache analysis.
- [02-hooks-and-budgets.md](./02-hooks-and-budgets.md) — the fail-open + budget contract the composition roots enforce.
