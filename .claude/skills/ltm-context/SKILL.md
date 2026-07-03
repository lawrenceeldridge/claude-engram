---
name: ltm-context
description: Gather context on the claude-ltm codebase the token-first way — consult the repo's own ltm-memory MCP tools (recall, search_code/search_docs, get_symbol/get_doc_section) and README/DESIGN before any broad Grep/Glob/Read. Use when starting or resuming work on this plugin, when the user asks "where is X / how does Y work / what did we decide / did we already do this", or before a speculative sweep of unfamiliar core/ or bin/ code. Do NOT use for a trivial single-file lookup you can answer with one Read.
metadata:
  author: Lawrence Eldridge
---

# Gather claude-ltm context (memory & index first)

This repo *is* the memory-first tooling it ships. Its `ltm-memory` MCP server
(`recall`, `search_code`, `search_docs`, `get_symbol`, `get_doc_section`,
`code_outline`, `doc_outline`) is the cheap "gather project context" mechanism —
so context-gathering here means **driving those tools**, not writing a bespoke
extractor. Fall back to Grep/Glob/Read only when they come back weak or empty.

This mirrors the repo's own memory-first guard (`LTM_ENFORCE`, see
[README § Memory-first enforcement](../../../README.md)) and the global
memory-first rule in `~/.claude/CLAUDE.md`. Treat that ordering as the workflow.

## The stop rule

1. **`recall`** — prior decisions/facts for this project ("what did we decide,
   where is X, did we already do this"). Returns a verdict: `ok` /
   `low_confidence` / `no_memory`.
2. **`search_code` / `search_docs`** — ranked outlines of *indexed* symbols and
   doc sections (qualname/heading + summary + freshness), far cheaper than a scan.
3. **`get_symbol` / `get_doc_section`** — pull one exact span once search points
   at it, instead of reading a whole file.
4. **README / DESIGN sections** — the authoritative narrative: layout, POEAA
   pattern map, budgets, benchmark, config keys.
5. **Grep / Glob / Read** — widen to these *only* when 1–3 are weak/empty.

**Stop as soon as it's confident.** A confident `recall` or a strong
`search_code` hit means you skip the wide search — open at most one or two files
they point at to confirm. Report honestly whether an answer came from
memory/index or from a fresh search; never present a fresh search as recall.

For the detailed decision tree per question shape, read
[`references/strategies.md`](./references/strategies.md).

## Workflow

### Step 1 — recall first

Ask memory before searching. Either the MCP tool or the CLI:

```bash
python3 plugins/ltm/bin/ltm recall "how does supersession work"
python3 plugins/ltm/bin/ltm core        # the stable session-start core block
```

Or call the `recall` MCP tool directly. Trust `verdict: ok`; treat
`low_confidence` / `no_memory` as "index/search or widen".

### Step 2 — search the index

The index covers this project's code (Python via stdlib `ast`, TS/JS via
tree-sitter) and docs (Markdown by heading). Query it before scanning files:

- `search_code "quantize embedding"` → ranked symbol outlines + freshness.
- `search_docs "token budget"` → ranked doc-section outlines.
- `code_outline` / `doc_outline` → whole-file or project structure when you need
  the shape rather than a match.

Then fetch exactly what you need:

- `get_symbol <anchor>` → one symbol's full source, with a symbol-precise
  freshness check (`fresh` / `edited` / `stale` / `gone`).
- `get_doc_section <anchor>` → one doc section's body.

If `search_code` / `search_docs` return **nothing**, the project may not be
indexed yet — run the `index_docs` MCP tool (or the `SessionStart` auto-index has
not run). Only then fall back to a normal search.

### Step 3 — ground in README / DESIGN

For architecture questions, the module registry and pattern map are documented,
not searched. Read the relevant section rather than reverse-engineering source:

- [README.md](../../../README.md) — layout, MCP-tool table, CLI, config keys,
  benchmark, project identity.
- [DESIGN.md](../../../DESIGN.md) — CQRS + Hexagonal shape, POEAA / Cosmic Python
  pattern → file map, token/cache/latency budgets, memory-lifecycle model, risks.
- [`references/module-registry.md`](./references/module-registry.md) — the
  `core/` → `bin/` → `tests/` / `bench/` / `viewer/` surface map with the key
  file per POEAA role, mirroring DESIGN.md's table against the *real* dirs.

### Step 4 — widen only when weak

If recall is `low_confidence` / `no_memory` **and** the index search is empty or
off-target, then Grep/Glob/Read the source. Note in your answer that this was a
fresh search, not memory.

## CLI helpers

These run against the same store/index the MCP tools use — handy for a quick
terminal check or when confirming state:

```bash
python3 plugins/ltm/bin/ltm doctor      # resolved config, project identity, fact count
python3 plugins/ltm/bin/ltm recall <q>  # just-in-time recall for this project
python3 plugins/ltm/bin/ltm core        # stable session-start memory block
python3 plugins/ltm/bin/ltm stats       # recall telemetry + estimated searches/tokens saved
python3 plugins/ltm/bin/ltm projects    # every project in the global store + fact counts
```

(Full CLI list in [README § CLI](../../../README.md); there is no `ltm code`
subcommand — code lookups go through the `search_code` / `get_symbol` MCP tools.)

## Examples

### "How does the recall hot path avoid model reloads?"

`recall "recall hot path daemon"` → if `ok`, answer from the fact and confirm
with `search_code "daemon"` → `get_symbol` on `core/daemon_client.py`. No file
scan. Cross-check [DESIGN § Latency efficiency](../../../DESIGN.md).

### "Where is supersession implemented?"

`search_code "supersede"` returns outlines in `core/store.py` and
`core/service.py`; `get_symbol` the two anchors. Skip Grep. See
[DESIGN § Memory lifecycle](../../../DESIGN.md).

### "What's the layout of core/?"

Read [`references/module-registry.md`](./references/module-registry.md) or run
`code_outline` — do not Glob-and-Read every file.

### Index returns nothing

`search_code` / `search_docs` are empty → run the `index_docs` MCP tool once,
retry the search, and only then fall back to Grep/Glob.

## Guardrails

- **Don't gold-plate context.** A single-file question you already know the path
  to is a `Read`, not this workflow.
- **Respect the repo's own rules** when you act on what you find:
  [`.claude/rules/02-architecture/`](../../../.claude/rules/02-architecture/)
  (CQRS + Hexagonal, hook fail-open, token/latency budgets) and
  [`.claude/rules/00-quality/`](../../../.claude/rules/00-quality/).
- **This is dev-only tooling** — it maintains this repo and is not shipped to
  installers (only `plugins/ltm/**` ships). See [CLAUDE.md](../../../CLAUDE.md).
