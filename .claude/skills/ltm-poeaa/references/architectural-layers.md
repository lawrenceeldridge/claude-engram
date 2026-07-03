# claude-ltm Architectural Layers — Hexagonal View

This file is a **complementary cross-reference** to the catalog organised by Fowler's 10
categories. It re-organises claude-ltm by the four hexagonal-architecture layers Cosmic
Python uses in its Appendix A master diagram (Percival & Gregory 2020, Appendix A —
Summary Diagram and Table). Use this view when the question is "where does this code live
in claude-ltm?" rather than "which Fowler category does this pattern belong to?".

The layer-based view and the category-based catalog are not in tension — they cut the same
code in two directions. A single Repository (`store.py`) appears in `05-or-metadata.md`
(category) and as a driven adapter / persistence layer (here); a single capture handler
appears in `01-domain-logic.md` and as a Service Layer entry here.

claude-ltm is **one package** (`plugins/ltm/`) with one internal hexagonal shape — see
`single-package.md`. Source for the layer model:
<https://www.cosmicpython.com/book/appendix_ds1_table.html>.

---

## The four layers

```
                  Primary Adapters (entrypoints / driving)
                  ───────────────────────────────────────
                  Hooks (UserPromptSubmit, SessionStart, Stop/SessionEnd/PreCompact,
                  PreToolUse, PostToolUse) · CLI (bin/ltm) · MCP server · daemon
                                │
                                ▼ translate to
                                │
                      Service Layer (core)
                      ─────────────────────
                      capture: service.py   ·   recall: recall.py   (CQRS split)
                                │
                                ▼ orchestrate
                                │
                      Functional Core / Domain (core)
                      ───────────────────────────────
                      distill · scoring · quantize · fusion · confidence · lexical
                      (pure functions over facts + quantised vectors)
                                │
                                ▲ depends on (via ports)
                                │
                  Secondary Adapters (driven)
                  ───────────────────────────
                  EmbeddingGateway impls · Distiller impls · daemon_client · Store
                  via ABC ports (EmbeddingGateway, Distiller)
                                │
                                ▼
                  Infrastructure (SQLite memory.db · fastembed model ·
                  resident daemon · local LLM / claude CLI)
```

The dependency direction is **always inward**. Adapters depend on ports; the core does not
depend on adapters. Concrete implementations are wired in the per-entry-point Composition
Root (`bin/*` + `bin/_bootstrap.py` — see `catalog/11-architectural-style.md` § Composition
Root).

---

## Layer 1 — Functional Core / Domain

**Purpose.** The decisions claude-ltm makes about facts: how to distil text into atomic
facts, how to rank candidates, how to quantise a vector, how to fuse lexical and semantic
scores, how to score confidence. **Pure functions and plain data** — no DB, no model, no
subprocess, no clock, no randomness.

**Cosmic taxonomy.** Value Object, (pure) domain function. There is **no rich Domain Model
/ Entity / Aggregate** — a fact is data, not an object with behaviour (see
`ltm-defaults.md` § Domain Logic).

**claude-ltm files.** `core/distill.py` (pure `heuristic_facts`, `parse_records`,
`observations_to_facts`), `core/scoring.py`, `core/quantize.py`, `core/fusion.py`,
`core/confidence.py`, `core/lexical.py`. Value objects: `DistilledFact`, `Observation`,
`Hit`, the frozen `Config` (`core/config.py`).

**Forbidden in this layer.** `import sqlite3` use, model loading, `subprocess`,
`urllib` calls, reading the clock, filesystem I/O. If a function here needs data, the
**shell** gathers it and passes it in. This is what makes the core stdlib-testable without
mocks.

---

## Layer 2 — Service Layer (core, orchestration)

**Purpose.** The jobs the plugin performs — orchestrate the pure core, open the store,
call the driven adapters via ports, return a DTO. Thin: it orchestrates; it does not decide
(decisions live in the functional core).

**Cosmic taxonomy.** Command, Handler. **CQRS split**: capture is the write side, recall is
the read side, and they are deliberately separate modules on separate hooks.

**claude-ltm files.**

| Side | File | Entry functions |
|------|------|-----------------|
| Write (capture) | `core/service.py` | `add_records`, `add_facts`, `capture_text`, `capture_transcript`, `maybe_capture_summary`, `recover_pending` |
| Read (recall) | `core/recall.py` | `search`, `search_fused`, `render_block` |
| Read (recall, composed) | `core/service.py` | `recall_prompt_block`, `recall_core_block`, `orientation_block`, `recall_structured` |

Each is a **function-style handler**: it takes the `Store` (Repository), the
`EmbeddingGateway`, and/or the `Distiller` as parameters, calls the pure core, and persists
or renders. No `class XService`.

**Forbidden in this layer.** Business *decisions* (those belong in the functional core —
putting branching policy here drifts toward duplicating logic across handlers). Direct
construction of adapters (`fastembed_gw.FastEmbedGateway()` inline — let the Composition
Root inject them). Importing from `bin/*`. Putting embedding work on the recall hot path
without the daemon-client-with-fallback.

---

## Layer 3 — Secondary Adapters (driven)

**Purpose.** Concrete implementations of the ports the core defines. Each speaks the
protocol of one external system — SQLite, the fastembed model, a resident daemon, a local
LLM / the `claude` CLI — and translates it into the port's interface.

**Cosmic taxonomy.** Repository, Gateway.

**Ports** are declared as ABCs next to the consumer: `EmbeddingGateway` in
`core/embedding.py`, `Distiller` in `core/distill.py`. The core imports the ABC, never the
concrete adapter.

**claude-ltm files.**

| Cosmic concept | claude-ltm instance |
|----------------|---------------------|
| Repository / Data Mapper | `core/store.py::Store` — the collection-like query surface over hand-written `sqlite3` SQL (see `catalog/05-or-metadata.md`, `catalog/02-data-source.md`) |
| Gateway (embeddings) | `core/embedding.py::HashEmbedding` (zero-dep) and `core/adapters/fastembed_gw.py` (heavy dep, lazy import) |
| Gateway (distillation) | `core/distill.py::HeuristicDistiller` (zero-dep), `ClaudeCliDistiller` (subprocess), `HTTPDistiller` (urllib) |
| Remote Facade (embedder) | `core/daemon_client.py` — thin client over the resident `bin/daemon.py`, fail-open in-process fallback |
| Service Stub (tests) | `HashEmbedding` / `HeuristicDistiller` double as the deterministic test fakes |

**Forbidden in this layer.** Importing from `core/service.py` or `core/recall.py` (a
circular dependency back to the orchestration layer). An adapter only knows about its port
and its concrete external system. A Gateway with no fail-open fallback.

---

## Layer 4 — Primary Adapters (entrypoints / driving)

**Purpose.** Translate external input — a hook invocation, a CLI command, an MCP tool call
— into Service Layer calls, then translate the result back (inject `additionalContext`,
print, return a tool response). These are the **Composition Roots**: each reads config,
picks adapters, and calls the core.

**Cosmic taxonomy.** Web/entrypoint, event/hook consumer, plus the CLI and MCP surfaces.

**claude-ltm files.**

| Entry point | File | Hook / surface | Reads → calls |
|-------------|------|----------------|---------------|
| JIT recall | `bin/recall_prompt.py` | `UserPromptSubmit` | `recall.search` → inject capped `render_block` DTO |
| Core recall + orientation | `bin/recall_session_start.py` | `SessionStart` | `recall_core_block` / `orientation_block` (joins prompt-cache prefix) |
| Capture | `bin/capture.py` | `Stop` / `SessionEnd` / `PreCompact` | spawns detached worker → `service.capture_*` |
| Memory-first guard | `bin/prefer_memory.py` | `PreToolUse` | gates Grep/Glob per `LTM_ENFORCE` |
| Consulted marker | `bin/mark_consulted.py` | `PostToolUse` | records an ltm lookup happened |
| Index maintenance | `bin/index_docs.py`, `bin/index_edit.py` | `SessionStart` / `PostToolUse` | `indexer` over the same `Store` |
| CLI | `bin/ltm` | operator shell | `doctor`, `recall`, `capture`, `sweep`, `eval`, `viewer`, … |
| MCP tools | `bin/mcp_server.py` | MCP | `recall`, `search_code`, `get_symbol`, `search_docs`, … as DTO responses |
| Resident embedder | `bin/daemon.py` | long-lived process | holds the model warm; served via `daemon_client` |

All share `bin/_bootstrap.py` for wiring.

**Forbidden in this layer.** Business logic — an entry point's job is translation and
wiring, not decision. If a hook script contains ranking/distillation/scoring logic, it
belongs in the core. Every hook must **exit 0 on any error** and inject nothing (fail-open).

---

## How the layers compose for one claude-ltm recall

A typical `UserPromptSubmit` recall:

```
1. Claude Code fires UserPromptSubmit with the user's prompt
   (Primary adapter — bin/recall_prompt.py)

2. The hook reads config, picks the embedder (get_embedder → daemon or in-process)
   and opens the Store  (Composition Root — bin/_bootstrap.py)

3. embed_query(prompt) via the EmbeddingGateway / daemon_client
   (Secondary adapter — Gateway / Remote Facade, fail-open)

4. recall.search(store, query_vec, cfg, min_sim, top_k)
   (Service Layer — read side — recall.py)

5. Candidates cleared by the similarity gate get a priority score
   sim·Ws + decay·Wr + freq·Wf   (Functional Core — scoring.py)
   fused with lexical FTS via RRF (Functional Core — fusion.py)

6. render_block(header, hits, max_chars) → capped, one-line-per-fact DTO
   (Service Layer / DTO — recall.py); empty hits → "" (Null Object)

7. The hook injects the block as additionalContext, exits 0
   (Primary adapter)
```

Every layer transition is explicit; every cross-layer call to infrastructure goes through
a port. The Composition Root (`bin/*` + `_bootstrap.py`) is the only place that knows about
the concrete embedder/distiller. The injected payload is a DTO shaped for the token budget —
never a raw row.

The capture path is the mirror image on the write side: `bin/capture.py` spawns a
**detached** worker that runs `service.capture_*` → `distill` (pure core) → `embed` (Gateway)
→ `Store.add` (Repository), off the interactive path, fail-open.

---

## Using this file

When **adding new code**, ask the layering question first:
- Translating between the outside world and the core? → Primary adapter (`bin/*`).
- Orchestration / "what job runs"? → Service Layer (`service.py` / `recall.py`).
- A decision or a pure computation (rank, distil, quantise, fuse)? → Functional Core.
- Concrete I/O against SQLite / a model / a subprocess? → Secondary adapter
  (`core/store.py`, `core/adapters/`, a `Distiller` impl, `daemon_client`).

When **auditing** for layering violations:
- Functional-core file importing `sqlite3`, loading a model, or spawning a subprocess →
  wrong layer; push the I/O to the shell.
- Heavy dep (`fastembed`) imported outside `core/adapters/` → Hexagonal violation.
- Service Layer constructing a concrete adapter inline → DI violation; inject via the
  Composition Root.
- Hook script (`bin/*`) containing ranking/distillation logic → leak; push to the core.
- Embedding work / synchronous model load on the `UserPromptSubmit` path without the
  daemon-client fallback, or a capture path that isn't detached / fail-open → budget +
  fail-open findings (`references/anti-patterns.md`).

`audit` mode surfaces all of these as findings against the patterns in
`catalog/11-architectural-style.md` § Hexagonal Architecture and the defaults in
`ltm-defaults.md`.
