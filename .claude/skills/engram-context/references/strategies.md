# Context-Gathering Strategies

How to gather context on this repo cheaply, matched to the shape of the question.
Every strategy is an instance of the same memory-first stop rule: **consult
memory and the index first, widen to Grep/Glob/Read only when they are weak or
empty.** This is exactly the ordering the repo's own `ENGRAM_ENFORCE` guard steers
toward ([README § Memory-first enforcement](../../../../README.md)).

The four surfaces, cheapest first:

1. **`recall`** — distilled prior facts/decisions (verdict `ok` /
   `low_confidence` / `no_memory`).
2. **`search_code` / `search_docs`** — ranked outlines of indexed symbols / doc
   sections (+ freshness), then `get_symbol` / `get_doc_section` for one span.
3. **README / DESIGN** — the authoritative narrative for architecture, patterns,
   budgets, config, benchmark.
4. **Grep / Glob / Read** — the fallback, when 1–3 miss.

---

## Strategy 1 — `recall` first (decisions & prior work)

**Use for:** "what did we decide", "did we already do X", "why is it done this
way", resuming a task.

`recall` (MCP tool) or `engram recall "<query>"` / `engram core`. Read the verdict:

| Verdict | Action |
|---|---|
| `ok` | Trust it. Confirm with at most one `get_symbol` / doc read; skip the wide search. |
| `low_confidence` | Treat as a weak hint. Proceed to Strategy 2. |
| `no_memory` | Nothing stored yet. Proceed to Strategy 2, then widen. |

**Stop condition:** a confident, on-topic fact answers "have we / did we / why".
Do not Grep to "double-check" a confident recall — say the answer came from
memory and move on.

---

## Strategy 2 — search the index (where is X / how does Y work)

**Use for:** locating a symbol or doc section; understanding a mechanism without
reading whole files.

- `search_code "<query>"` → ranked symbol outlines (qualname + signature +
  anchor + freshness). Then `get_symbol <anchor>` for the one body you need.
- `search_docs "<query>"` → ranked doc-section outlines. Then
  `get_doc_section <anchor>`.
- `code_outline` / `doc_outline` → the structure of a file/project when you want
  the shape, not a specific match.

**Read the freshness verdict** on a symbol before trusting its body:

| Freshness | Meaning |
|---|---|
| `fresh` | Index matches disk — trust the outline/body. |
| `edited` / `stale` | File changed since indexing — re-fetch with `get_symbol` (symbol-precise) or Read the span. |
| `gone` | Symbol no longer at that anchor — search again or Grep. |

**Stop condition:** a strong, `fresh` hit points you at the right file. Open one
or two symbols to confirm; skip the Glob/Read sweep.

**Empty result:** if `search_code` / `search_docs` return nothing, the project
likely isn't indexed yet — run the `index_docs` MCP tool once and retry before
falling back. (`SessionStart` normally auto-indexes; a fresh clone may not have.)

---

## Strategy 3 — README / DESIGN (architecture, patterns, budgets)

**Use for:** "what's the overall shape", "which pattern owns X", "why is recall a
hook not a tool", config keys, the benchmark. These are *documented*, so read the
section — don't reverse-engineer it from source.

| Question | Where |
|---|---|
| Layout, MCP-tool table, CLI, config keys, benchmark, project identity | [README.md](../../../../README.md) |
| CQRS + Hexagonal shape, POEAA pattern → file map, token/cache/latency budgets | [DESIGN.md](../../../../DESIGN.md) |
| Memory-lifecycle model (decay, consolidation, supersession, TTL), risks | [DESIGN.md](../../../../DESIGN.md) |
| `core/` → `bin/` file-per-role surface map | [`module-registry.md`](./module-registry.md) |
| Hard prohibitions, layering, hook fail-open, budgets | [`.claude/rules/02-architecture/`](../../../../.claude/rules/02-architecture/) |

**Stop condition:** the doc section answers the architecture question directly.

---

## Strategy 4 — widen to Grep/Glob/Read (fallback only)

**Use only when** recall is `low_confidence` / `no_memory` **and** the index
search is empty or off-target (or the project isn't indexed and can't be).

- Grep for a literal string the index wouldn't rank (a config key, an error
  message, a magic constant).
- Glob to enumerate files when you genuinely need the filesystem listing.
- Read a file directly when you already know its path (a single-file lookup was
  never a job for this skill).

**Report honestly:** when you widen, say the answer came from a fresh search, not
from memory or the index.

---

## Worked routing examples

| Question | Route |
|---|---|
| "Did we decide int8 or float for storage?" | `recall` → `ok` cites the benchmark decision; confirm in [DESIGN § Embedding backend](../../../../DESIGN.md). Stop. |
| "Where's the recency-decay formula?" | `search_code "recency decay"` → `core/scoring.py`; `get_symbol`. Stop. |
| "How does the capture worker stay off the interactive path?" | `search_docs "detached capture"` + [DESIGN § Latency efficiency](../../../../DESIGN.md); confirm `bin/capture.py` via `get_symbol`. |
| "What config key controls the injection cap?" | [README § Configuration](../../../../README.md) (`max_chars`) — documented, no search. |
| "Find every place `CLAUDE_PLUGIN_OPTION_` is read" | Index won't rank a literal prefix well → Grep (Strategy 4), note it was a fresh search. |
| "Open `core/store.py` and change `reinforce`" | Known path → just `Read` + `Edit`; this skill isn't needed. |

---

## Anti-patterns

- **Grepping before recalling.** The guard exists because the wide sweep is the
  expensive path. Memory/index first, every time.
- **Reading a whole indexed code file to find one symbol.** Use `search_code` +
  `get_symbol`; under `ENGRAM_ENFORCE=strict` reading a large indexed file whole is
  denied outright.
- **Trusting a `stale` / `gone` outline's body.** Re-fetch or Read the live span.
- **Presenting a fresh Grep as if it were recall.** Always attribute the source.
- **Invoking this skill for a one-file question you can answer with a single
  Read.** That's gold-plating.
