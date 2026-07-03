# claude-ltm's Locked-in POEAA Pattern Defaults

This file is the source of truth for **which patterns claude-ltm uses in which layer**.
The choices below are not preferences — they are committed conventions, grounded in
[`DESIGN.md` § POEAA / Cosmic Python](../../../../DESIGN.md) and the core file map. The
skill's `recommend` and `apply` modes default to these answers; the `audit` mode flags
deviations as findings.

If a future change genuinely needs to revisit one of these, treat it as a
**re-architecture proposal**: spike it, get explicit alignment, update this file and
[`.claude/rules/02-architecture/01-poeaa-and-layers.md`](../../../rules/02-architecture/01-poeaa-and-layers.md)
in the same PR.

claude-ltm is a **single Python package** — one bounded context, not a monorepo. There
are no per-package divergences to track; see `single-package.md`. The whole plugin is
**CQRS + Hexagonal (Ports & Adapters)**:

- **Write side (capture)** — heavy, batch, latency-tolerant. Runs detached at
  `Stop` / `SessionEnd` / `PreCompact`. Zero interactive-token cost.
- **Read side (recall)** — tiny, hot-path, token- and latency-critical. Runs in
  `UserPromptSubmit` / `SessionStart` hooks.

The two sides have opposite performance profiles, so they are split — that split *is*
the top-level architectural decision every pattern below serves.

### The core file map (memorise this)

| File | Pattern role |
|---|---|
| `core/store.py` | **Repository over Data Mapper** — all memory + index access; owns the SQLite schema/migrations |
| `core/service.py` | **Command / Handler** — the capture pipeline, idempotent per fact |
| `core/recall.py` | **Query Object** (`search`, `search_fused`) + **DTO / Null Object** (`render_block`) |
| `core/distill.py` | **Functional Core** (pure `heuristic_facts`, parsers) + **Strategy behind Separated Interface** (`Distiller` ABC) |
| `core/scoring.py`, `core/quantize.py`, `core/fusion.py`, `core/confidence.py`, `core/lexical.py` | **Functional Core** — pure ranking / quantisation / fusion |
| `core/embedding.py` | **Gateway + Separated Interface** (`EmbeddingGateway` ABC, `get_embedder`) |
| `core/adapters/fastembed_gw.py` | **Secondary (driven) adapter** — the only place heavy deps import |
| `core/daemon_client.py` | thin client → resident daemon, **fail-open in-process fallback** |
| `core/indexer.py`, `chunking.py`, `code_symbols.py`, `treesitter_symbols.py` | code/docs index pipeline (same Repository, Functional-Core parsers) |
| `bin/*` (`recall_prompt.py`, `capture.py`, `mcp_server.py`, `daemon.py`, `ltm`, …), `bin/_bootstrap.py` | **Composition Roots** — read config, pick adapters, call the core |

---

## Domain Logic — Functional Core + function-style Service Layer

**Chosen:** Functional Core / Imperative Shell (`11-architectural-style.md`) with a
**function-style Service Layer** (`service.py`, `recall.py` entry functions).
**Rejected:** rich Domain Model with behaviour-carrying entities; Transaction Script;
Table Module.

**Why.** There is no rich domain to model — a "fact" is a short string plus a quantised
vector plus bookkeeping (project key, frequency, recency, status). Behaviour lives in
*pure functions over that data*, not on objects. Distillation, ranking, scoring,
quantisation, and fusion are all pure (`distill.heuristic_facts`, `scoring`,
`quantize`, `fusion`); the imperative shell (`service.capture_text`,
`recall.search` callers, `bin/*`) does the I/O around them. This is what keeps the core
testable on the standard library with no mocks.

The Cosmic-Python **function-based** Service Layer shape applies exactly:
`add_records(*, store, embedder, distiller, project_key, text, …)` takes its
dependencies (the Repository, the Gateway, the Distiller) as parameters, calls the pure
core, and persists — no `class XService`. This is what lets each `bin/*` Composition
Root stay small.

**Not an Anemic Domain Model.** Anemic-domain-model is the failure mode of a project
that *has* a rich domain but drains it into services. claude-ltm has no rich domain by
design — the "model" is data, the logic is pure functions. Do not add persistence or
behaviour methods to the row/dataclass types to "un-anemic" them.

**Citations.** `DESIGN.md` § Distillation, § Memory lifecycle; `core/service.py`,
`core/distill.py`, `core/scoring.py`.

---

## Data Source — Repository over Data Mapper (never Active Record)

**Chosen:** **Repository over Data Mapper**, hand-written on the stdlib `sqlite3`
module in `core/store.py`.
**Rejected:** Active Record, Row/Table Data Gateway, and any ORM.

**Why.** All memory and index access goes through the `Store` class. Facts and chunks
are **plain data** (`sqlite3.Row` / dicts); they do **not** carry their own persistence
methods. This is the seam that lets the store be inspected by the read-only localhost
viewer, swept, pruned, migrated (`_v1`…`_v8`), and A/B-benchmarked without any of that
logic leaking onto the data. An `obj.save()` API would couple every fact to the schema
and to quantisation — exactly what the Repository keeps out.

**Implementation rules.**
- Persist via `Store.add(...)` / `Store.replace_source_chunks(...)`; read via
  `Store.active_rows_for_project(...)`, `Store.fts_search(...)`, `Store.chunk_outline(...)`.
- **No** `def save(self)` / `def find(cls, …)` on any row or dataclass type.
- Raw SQL lives **only** inside `store.py` (including FTS5 `MATCH` and the migration
  ladder). Callers never see a SQL string or a cursor.
- Idempotency is a Data-Source concern here: `Store.fact_id(project_key, text)` is a
  content hash, and `Store.exists()` / `Store.reinforce()` make re-capture a no-op-or-boost
  rather than a duplicate (see § Offline Concurrency).

**Citations.** `DESIGN.md` § Compact storage, § Memory lifecycle; `core/store.py`.

---

## O-R Behavioral — deliberately minimal (no ORM session machinery)

**Chosen:** a single short-lived `sqlite3.Connection` per process, explicit transactions
inside `Store`.
**Not applicable in the Fowler-heavy sense:** Unit of Work, Identity Map, and Lazy Load
are ORM-session behaviours; claude-ltm has no ORM and no long-lived object graph, so
these patterns collapse into "one connection, explicit commits, plain rows".

**Why.** Hooks are short-lived processes (a recall hook runs, injects, exits). There is
no request-scoped session to manage, no identity map to maintain across a graph, and
nothing to lazy-load — a fact row is fully materialised or not read at all. Capture
writes are **batched per session** and **idempotent per fact**, which gives the
atomicity benefit a Unit of Work would, without the machinery.

**Guard.** Don't import an ORM or build a session/identity-map abstraction to "complete
the pattern set". The absence is the design.

**Citations.** `DESIGN.md` § Latency efficiency; `core/store.py`.

---

## O-R Structural — Identity Field (content hash), Serialized LOB (embeddings), Embedded Value

**Chosen.**
- **Identity Field — content hash.** `fact_id = hash(project_key, text)` and
  `chunk_id(project_key, source_path, anchor)`. Identity is derived from content, which
  is what makes capture idempotent. Every row is **tagged by project key** (the
  marker-walk identity — see `DESIGN.md` § Project identity).
- **Serialized LOB — quantised embedding blobs.** The int8 vector (primary search rep)
  and the binary sign-bit vector (32× smaller Hamming pre-filter) are stored as opaque
  blob columns. They are read and written whole, never filtered inside — a textbook
  Serialized LOB in its *modern* (non-XML) form.
- **Embedded Value — the vector as a compact fingerprint.** The embedding is inlined on
  the fact/chunk row, not a separate table; it is the fact's semantic value, meaningless
  on its own.

**Avoided.** Inheritance mapping of any kind (no hierarchy to map); auto-increment
surrogate keys where a content hash gives idempotency for free.

**Citations.** `DESIGN.md` § Compact storage, § Cross-project; `core/store.py`,
`core/quantize.py`.

---

## O-R Metadata — Query Object + lightweight Repository

**Chosen.**
- **Query Object** — `recall.search(...)` / `recall.search_fused(...)` take a bundled set
  of parameters (`Config` weights, query vector, `min_sim`, `top_k`, cross-project
  penalty) rather than a sprawling positional signature. Extend the *object*, not the
  argument list.
- **Repository (lightweight)** — the `Store` methods (`active_rows_for_project`,
  `fts_search`, `chunk_fts_search`, `chunk_outline`) are the collection-like query
  surface. There is no separate `<Aggregate>Repository` class because there is one store
  and one obvious set of queries.

**Avoided.** Raw SQL strings above the `Store` boundary; growing `search` into a
ten-positional-argument function instead of extending the query/config object.

**Rule.** New search knobs go on the query/`Config` object. If a new access pattern
appears, add a named `Store` method — don't hand a cursor to the caller.

**Citations.** `DESIGN.md` § Memory lifecycle (hybrid re-rank), § Code & docs index
(hybrid ranking / RRF); `core/recall.py`, `core/fusion.py`, `core/store.py`.

---

## Web Presentation — localhost viewer only; otherwise N/A

**Chosen.** claude-ltm is a plugin, not a web app. The only presentation surface is the
**read-only localhost viewer** (`viewer/`, stdlib `http.server`) that browses memory and
the index across projects. It is a thin Front-Controller-shaped dispatcher over the
`Store` — no framework, no SPA, no server-side templating engine.

**Avoided.** Page Controller, Transform View, Two Step View, and any server-rendered
templating ceremony. The viewer renders simple HTML from store rows; keep it that way.

**Note.** The MCP server (`bin/mcp_server.py`) is a request dispatcher too, but it belongs
to **Distribution** (tool-call boundary), not Web Presentation.

**Citations.** `README.md` § Layout (viewer); `DESIGN.md` § Cross-project.

---

## Distribution — DTO + Gateway + thin daemon client (Remote Facade)

claude-ltm crosses three boundaries, and a **DTO** carries data across each; there is
**no Event system / pub-sub**. A **durable Command queue is permitted** — opt-in, behind
the `MemoryBus` Separated Interface, default `inproc` — for detached per-memory
processing (capture, re-distil, consolidation). See § Offline Concurrency and the
`stm-ltm-membus` design (`docs/generated/designs/`). The distinction is load-bearing:
these are **Commands** (one handler, failures retry/dead-letter), *not* Events (pub-sub,
many handlers, log-and-skip). Making the existing Command/Handler durable is not adding
an event bus.

| # | Boundary | Mechanism | Wire shape (DTO) |
|---|----------|-----------|------------------|
| 1 | plugin → Claude's context window | hook `additionalContext` | `recall.render_block` — deliberately **one line per fact** (token budget) |
| 2 | model → plugin (on-demand) | MCP server (`bin/mcp_server.py`) | tool responses: recall verdict, ranked outlines, symbol/section bodies |
| 3 | hook → resident embedder | `core/daemon_client.py` → `bin/daemon.py` | vectors over a local socket; **falls back to in-process on any failure** |

**Rules.**
- **`render_block` is the injected DTO.** It is shaped for the *token budget*, not for
  the store — one atomic line per fact, capped at `max_chars`. Never inject raw rows or a
  transcript.
- **The daemon client is a Remote Facade** over the embedding model: coarse-grained
  (embed a batch, get vectors), and **fail-open** — a daemon outage silently degrades to
  loading the model in-process, never breaks a turn.
- **MCP tool responses are DTOs**, not store internals — return outlines + anchors +
  freshness verdicts, let the model fetch one body with `get_symbol`.

**Avoided.** An **Event** bus / pub-sub dispatcher (no publishers/subscribers, no
broadcast-to-many, no domain events); returning raw `sqlite3.Row` objects across any
boundary. A durable *Command* queue behind `MemoryBus` is **not** in this list — it is a
job-claim substrate (see § Offline Concurrency), not pub-sub.

**Citations.** `DESIGN.md` § Token efficiency, § Latency efficiency (detached capture,
warm daemon, fail-open); `core/recall.py::render_block`, `core/daemon_client.py`,
`bin/mcp_server.py`.

---

## Offline Concurrency — single-flight + idempotent capture + TTL sweep (no offline locks)

claude-ltm has no interactive multi-request edit flow, so Fowler's Optimistic /
Pessimistic Offline Lock **do not apply**. The concurrency concerns are (a) detached
capture workers piling up, and (b) stale facts accumulating.

**Chosen.**
- **Single-flight capture.** The capture hook spawns *one* detached worker/daemon and
  returns; a second capture for the same session does not stack a second worker. This is
  the concurrency primitive that matters (it replaced a worker/daemon pileup — see the
  commit history).
- **Idempotent-per-fact capture.** `fact_id` content-hash + `Store.exists` /
  `Store.reinforce` make re-processing the same transcript a no-op-or-reinforce, never a
  duplicate. Re-running capture is always safe (the batch-worker analogue of the
  Job-claim pattern).
- **TTL sweep (hard expiry).** `Store.sweep(...)` retires facts unseen past `ttl_days`
  (unless reinforced past `ttl_keep_frequency`). Reversible (a `status` flag, not a
  delete). Runs off the interactive path or via `ltm sweep`.
- **Durable Command queue (opt-in, `MemoryBus`).** For per-memory processing that must
  survive a dropped connection or an `LTM_DISTILLER` outage, single-flight is *extended*
  (not replaced) by a durable job-claim queue behind the `MemoryBus` Separated Interface:
  default `inproc` (a SQLite `work_queue` table — lease + retry + dead-letter, all
  stdlib), opt-in `nats` (JetStream) adapter, **fail-open** to `inproc`. This is the
  at-least-once, retry-able form of the existing Job-claim; idempotency is still the
  content-hash `msg_id`. It is a Command queue, **not** an Event bus (see § Distribution).

**Avoided.** Optimistic/Pessimistic Offline Lock, `SELECT … FOR UPDATE`, version columns
— there is no contended mutable aggregate to guard.

**Citations.** `DESIGN.md` § Hard expiry, § Memory lifecycle (consolidation); commits
`b30889a` (single-flight), `6889374`; `core/store.py` (`sweep`, `reinforce`, `supersede`).

---

## Session State — N/A (short-lived hook processes)

claude-ltm holds **no session state**. Hooks are short-lived processes; there is no
server, no cookie, no per-user session store. The only state that survives a turn is the
**SQLite store** (durable Domain/index data, not session state) and the **prompt-cache
prefix** the `SessionStart` core injection joins (a caching optimisation, not stored
state).

**Guard.** Don't introduce a session store, a Redis, or an in-memory cross-turn cache.
"Cross-turn continuity" is exactly what the memory store *is*; anything session-shaped is
a smell.

**Citations.** `DESIGN.md` § Cache efficiency, § Architecture.

---

## Base — Gateway, Separated Interface, Plugin, Service Stub, Null Object, Value Object

| Pattern | Status | claude-ltm use |
|---|---|---|
| **Gateway** | ✅ default | `EmbeddingGateway` (embeddings), the `Distiller` interface (distillation) — the only doors to heavy/optional deps and subprocesses |
| **Separated Interface** | ✅ default | `EmbeddingGateway(ABC)` in `core/embedding.py`; `Distiller(ABC)` in `core/distill.py`. Concrete impls (`fastembed_gw`, `ClaudeCliDistiller`, `HTTPDistiller`) live behind them; the core imports the ABC, never the impl |
| **Plugin** | ✅ default | `get_embedder(cfg)` / `get_distiller(cfg)` select the implementation from config at runtime; `LTM_DAEMON` selects daemon-vs-in-process. This is Plugin selection, one place per Composition Root |
| **Service Stub** | ✅ default | `HashEmbedding` (lexical, zero-dep) and `HeuristicDistiller` (line extraction, zero-dep) are the local-first defaults **and** the test fakes — no network, no model download |
| **Special Case / Null Object** | ✅ default | `render_block` returns `""` on empty recall — inject nothing, never a placeholder or an error. Irrelevant turns cost zero tokens |
| **Value Object** | ✅ default | `DistilledFact`, `Observation`, `Hit`, and the frozen `Config` are immutable value carriers compared by content |
| **Layer Supertype** | ✅ default | the `EmbeddingGateway` / `Distiller` ABCs are the layer supertypes for their adapters |
| **Mapper (general)** | ✅ default | row↔dict shaping and `quantize` (float ↔ int8/binary) are Mappers between representations |
| **Money** | ❌ n/a | no monetary domain |
| **Registry** | ⚠️ avoided | config + dependencies are passed explicitly into the Service Layer; no module-level mutable singletons. The Composition Root wires, it does not register-globally |
| **Record Set** | ❌ not used | plain rows + DTOs, not tabular data binding |

**Fail-open is a Base-layer contract.** Every Gateway/Plugin selection degrades safely:
fastembed → hash, daemon → in-process, LLM distiller → heuristic. A missing optional dep
never breaks capture or recall.

**Citations.** `DESIGN.md` § Embedding backend, § Distillation, § Risks (fail-open);
`core/embedding.py`, `core/distill.py`, `core/daemon_client.py`, `core/provision.py`.

---

## Architectural Style — Hexagonal + CQRS + Functional Core / Imperative Shell + Composition Root

All four Cosmic-Python architectural-shape patterns are load-bearing — see
`11-architectural-style.md` for the full treatment. In brief:

- **Hexagonal (Ports & Adapters).** `core/` depends on interfaces (`EmbeddingGateway`,
  `Distiller`), never on `fastembed` or a subprocess. Heavy deps import **only** inside
  `core/adapters/`. Dependencies point inward.
- **CQRS.** Capture (write) and recall (read) are split by performance profile and run in
  different hooks — the defining decision of the whole plugin.
- **Functional Core / Imperative Shell.** Distil / rank / score / quantise / fuse are
  pure; `bin/*` and the I/O in `service.py` are the shell.
- **Composition Root.** `bin/*` entry points (with `bin/_bootstrap.py`) read config, pick
  adapters, and call the core. Business logic never lives in a hook script.

**Citations.** `DESIGN.md` § Architecture (CQRS + Hexagonal);
`.claude/rules/02-architecture/01-poeaa-and-layers.md`; `bin/*`, `core/adapters/`.

---

## Update procedure

To change a default:

1. Frame the change explicitly as architectural (it outlives the change that introduces it).
2. Spike it behind the existing interface where possible (a new embedding backend is a new
   adapter, not an `if` in the core).
3. Validate against `.claude/rules/00-quality/` and the token/latency budgets in
   `.claude/rules/02-architecture/02-hooks-and-budgets.md`.
4. Update this file **and** `.claude/rules/02-architecture/01-poeaa-and-layers.md` in the
   same PR.
5. Update `combinations.md`, `anti-patterns.md`, and `dated-patterns.md` if the new default
   invalidates a prior pairing or surfaces a new clash.
