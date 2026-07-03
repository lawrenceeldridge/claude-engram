# claude-ltm — design

Token-first, cross-project long-term memory for Claude Code, packaged as a plugin.

## The one constraint

Claude only consumes **text tokens**; bytes never enter the model — they enter
the *search* layer. So "efficiency" is two separate budgets:

- **Token budget** — tokens that reach the context window (recall injection).
- **Latency budget** — wall-clock added to a turn (query embedding + search).

Every decision below optimises one or both.

## Architecture (CQRS + Hexagonal)

Capture and recall have opposite performance profiles, so they are split:

- **Write side (capture)** — heavy, batch, latency-tolerant. Runs detached at
  `SessionEnd` / `PreCompact`. Zero interactive-token cost.
- **Read side (recall)** — tiny, hot-path, token- and latency-critical.

```
UserPromptSubmit ─► recall (embed → rank → gated inject)      ← hot path, tail of context
SessionStart     ─► core inject (small, stable)               ← joins cached prefix
SessionEnd/PreCompact ─► spawn detached capture worker        ← fire & forget
                              │ distil → embed → persist
                              ▼
              ${CLAUDE_PLUGIN_DATA}/memory.db   ◄── read-only ── localhost viewer
              (facts + int8/binary embeddings, rows tagged by project)
```

**Planned (not yet built):** detached capture is designed to gain a durable **Command
queue** (`MemoryBus`) so a dropped connection or an `LTM_DISTILLER` outage retries rather
than degrades — opt-in, behind a Separated Interface, default `inproc` (stdlib SQLite
`work_queue`), opt-in `nats` (JetStream), **fail-open** to `inproc`, never on the recall
hot path. It is a Command queue (one handler, retry/dead-letter), **not** an Event bus.
See the [`stm-ltm-membus` design](docs/generated/designs/stm-ltm-consolidation-and-memory-bus.md).

### POEAA / Cosmic Python patterns

| Role | Pattern | File |
|---|---|---|
| Overall shape | CQRS + Hexagonal (Ports & Adapters) | whole plugin |
| Capture pipeline | Command/Handler, idempotent per fact | `core/service.py` |
| Distil/rank/quantise | Functional Core / Imperative Shell | `core/distill.py`, `recall.py`, `quantize.py` |
| Memory access | Repository over Data Mapper (never Active Record) | `core/store.py` |
| Query params | Query Object | `core/recall.py::search` |
| Embedding provider | Gateway + Separated Interface | `core/embedding.py`, `adapters/` |
| Injected payload | DTO (deliberately one line/fact) | `core/recall.py::render_block` |
| Empty recall | Special Case / Null Object (inject nothing) | `render_block` returns `""` |
| Wiring | Composition Root | `bin/*` entry points |

## Token efficiency

1. **Hooks, not always-on tools.** Recall is a hook (zero standing cost, zero
   model agency). An MCP `recall` tool would only add value as an optional
   deep-search escape hatch — deferred tool schemas make its standing cost ~zero
   in Claude Code v2.1+, but it still needs the model to decide → search → call.
2. **Just-in-time + threshold-gated.** `UserPromptSubmit` injects only when a
   fact clears `min_sim`, capped at `top_k` / `max_chars`. Irrelevant turns cost
   nothing.
3. **Distil, don't store transcripts.** Atomic facts (~15 tokens) instead of
   transcript chunks (hundreds). Lossy compression tuned for relevance.

## Cache efficiency

Hook `additionalContext` is wrapped in a system-reminder and inserted into the
`messages` array **at the point the hook fired**:

- **SessionStart** → near the head → stable all session → joins the prompt-cache
  prefix → read at ~0.1× on every later turn. Used for the **stable project core**.
- **UserPromptSubmit** → tail → does *not* bust the earlier cached prefix, but is
  never a same-turn cache hit and varies per turn. Used for **JIT episodic** recall
  only, kept tiny.

This is why recall is a **hybrid**: cache-friendly core + relevance-driven JIT.

## Latency efficiency

- Capture is fully **detached** — the hook spawns a worker and returns.
- Recall is brute-force cosine over **int8** vectors — sub-10ms for a personal
  store; no ANN index needed until ~500k facts.
- Hooks are **short-lived processes**, so a real embedding model would reload
  every turn. The optional **resident daemon** holds it warm; the hook is a thin
  client that **falls back to in-process** on any failure (fail-open).

## Embedding backend — measured, not assumed

`ltm eval` runs a labelled paraphrase benchmark (Recall@1/@3, MRR@10) through the
real quantised search path. Findings that drove the defaults:

| backend | Recall@1 | Recall@3 | MRR@10 | bytes/fact |
|---|---|---|---|---|
| hash (lexical stub) | 0.07 | 0.36 | 0.27 | 288 |
| fastembed bge-small int8 | 0.36 | 0.71 | 0.57 | 432 |
| fastembed bge-small float | 0.36 | 0.71 | 0.57 | 1536 |
| **fastembed bge-base int8 (default)** | **0.79** | **0.86** | **0.85** | 864 |

- **int8 ≈ float** — quantization loss is negligible, so the compact int8 store
  stays and float-rescore was measured *not* worth building.
- **Model size is the lever** — bge-base ~2.2× bge-small's Recall@1 for ~5ms/query
  (absorbed by the warm daemon). Hence bge-base is the default; bge-small remains
  available via `embedding_model` for constrained environments.

## Distillation — heuristic vs LLM

Retrieval quality is capped by *what is stored*, so the distiller is the largest
quality lever. Strategy pattern behind one interface:

- **HeuristicDistiller** (default) — dependency-free line extraction. Cannot detect
  conflicts, so it leans on similarity-based supersession.
- **ClaudeCliDistiller** (`distiller=claude`) — headless `claude -p`, defaulting to
  **Haiku** (the right tier for cheap extraction).
- **HTTPDistiller** (`distiller=ollama`) — POSTs to any OpenAI-compatible endpoint
  via stdlib urllib; point it at a local Ollama / LM Studio / llama.cpp / vLLM
  server for **zero-token, offline** distillation.

Both LLM backends run in the detached capture worker (off the interactive path),
produce genuinely atomic facts *and* explicit `supersedes` links — fixing the
vocabulary-disjoint conflict case (Paris → London) that similarity cannot — and
fall back to the heuristic on any failure so capture never breaks.

## Hard expiry (TTL sweep)

Recency decay only *de-ranks* old facts; a TTL sweep *retires* them. On capture (if
`ttl_days > 0`, off the interactive path) or via `ltm sweep`, active facts unseen for
longer than the TTL are marked `expired` — unless reinforced past `ttl_keep_frequency`
(consolidation protects durable facts). Expiry is reversible (status flag, not delete),
and recall already filters to `status='active'`.

## Compact storage — the "bytes" layer

Per fact: the text (must stay text — it is what gets injected) + a quantised
embedding. int8 (~4× smaller, primary search rep) + binary sign-bits (32×, fast
Hamming pre-filter). The embedding *is* the compact semantic fingerprint.

## Memory lifecycle (cognitive model)

Standard vector similarity recalls stale and irrelevant facts. Three ideas from
memory research are layered on top, split cleanly by responsibility:

| Concept | Implementation | Where |
|---|---|---|
| Forgetting curve | exponential recency decay `e^(-λt)` (λ from `half_life_days`) | `core/scoring.py` |
| Consolidation | frequency boost — a fact seen again reinforces (freq++, recency refreshed) instead of duplicating | `store.reinforce`, `service.add_facts` |
| Context-dependent retrieval | similarity gate (`min_sim`) suppresses facts whose cue doesn't match | `recall.search` |
| Retroactive interference | **hard supersession** — a near-identical newer fact archives older ones (`status='superseded'`, filtered at SQL) | `store.supersede`, `service._find_superseded` |

**Retrieval is a hybrid re-rank, not raw similarity.** Each candidate that clears
the similarity gate gets a Priority Score `sim·Ws + decay·Wr + freq·Wf`, and the
top-k by priority are injected.

**Conflicts vs ordering are deliberately separate.** Genuine conflicts are removed
by *hard supersession* (a superseded fact can never resurface); soft recency decay
only *orders* non-conflicting facts. Folding conflict-resolution into the score
(as a single weighted formula would) lets a stale-but-frequent fact leak — the
hard filter prevents that.

**Honest limit on conflict detection.** Supersession fires on embedding
*similarity*, so it catches near-duplicates ("deploy target is X" → "deploy target
is Y") but not semantically-conflicting rewrites that share little vocabulary
("I live in Paris" vs "I moved to London"). Precise conflict detection needs
entity/attribute extraction — the LLM-distiller drop-in, which can emit explicit
`supersedes` links.

**Planned extension (not yet built) — explicit STM/LTM tiers + a "sleep" pass.** The
lifecycle above implements the multi-store model's *control processes* inline. A designed
(not yet implemented) extension makes them explicit: a capacity-bounded **short-term
store** that promotes to long-term on rehearsal, an offline **consolidation pass**
(replay / refine / rescue, mirroring active systems consolidation + REM), and an
importance-weighted **forgetting model** (SHY-style global downscale + prune) that keeps
the active set small enough that brute-force search stays viable. Full design:
[`docs/generated/designs/stm-ltm-consolidation-and-memory-bus.md`](docs/generated/designs/stm-ltm-consolidation-and-memory-bus.md).

## Cross-project

One **global** store under `${CLAUDE_PLUGIN_DATA}` (survives plugin updates),
every row tagged with a project key. Recall defaults to the current project;
`cross_project` enables a penalised fallback. The viewer is the one component
that intentionally spans all projects.

### Project identity — marker-walk (not `basename(cwd)`)

claude-mem keys on `basename(cwd)`, which fragments monorepos, subdirectory
launches, and same-named folders (filed bugs). Instead we walk up from `cwd` to
the nearest marker (`.git`, `pyproject.toml`, …) and key on that directory's
absolute path (hashed), with its basename as a display label. Stable regardless
of launch subdirectory; configurable for monorepo granularity via `markers`.

## Risks

| Risk | Mitigation |
|---|---|
| Hot-path embedding latency | local embedding + resident daemon; 5s hook timeout; fail open |
| Hook error breaks a turn | every hook exits 0 on any error, injects nothing |
| Irrelevant recall pollutes context | `min_sim` threshold + `top_k` + `max_chars` cap + project scoping |
| Cross-project leakage | project-scoped by default; fallback penalised and opt-in |
| Store growth / stale facts | recency decay + supersession de-rank/retire old facts; idempotent capture; viewer prune |
| Over-eager supersession retires a distinct fact | conservative default threshold (0.85); superseded rows are archived (reversible), not deleted |
| Distillation quality (heuristic) | pluggable distiller; LLM adapter is the drop-in |
| Plugin/hook API drift | thin Claude-Code adapter; core is framework-agnostic |
| Planned durable queue becomes a de-facto dependency | `MemoryBus` is opt-in behind a Separated Interface; default `inproc` is stdlib SQLite; `nats` adapter fails open to `inproc`; core stays importable without a broker |

## Status of the levers

Done and measured:
- **Semantic embeddings** — `fastembed` (bge-base default), benchmarked vs the stub.
- **LLM distiller** — atomic facts + explicit `supersedes` links, via local Ollama
  (`distiller=ollama`, zero-token) or Claude on Haiku (`distiller=claude`).
- **Conflict resolution** — similarity supersession *and* explicit LLM links for
  vocabulary-disjoint conflicts.
- **Hard expiry** — TTL sweep with frequency protection.

Remaining:
- **`hash`/heuristic remain the zero-dep defaults** — real recall needs
  `embedding=fastembed` (and an LLM distiller for best quality); these cost a
  dependency / tokens (or a local model), so they are opt-in.
- **Eval set is small** (14 queries) — widen for tighter numbers.
- **LLM distiller latency/cost** is unbounded per session — batching / a cheaper
  model for distillation is a future tuning knob.
