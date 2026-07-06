# Architectural-Style Patterns

Source: Percival & Gregory, *Architecture Patterns with Python* ("Cosmic Python", 2020) — <https://www.cosmicpython.com/>. Plus original sources cited per pattern.

These four patterns are **architectural-shape** patterns — they organise the codebase as a whole rather than solve a single local problem. They do not appear in Fowler's 2003 POEAA catalog, but claude-engram uses all four, and here they are the **structural baseline** — all four load-bearing. They live in this file rather than in `01–10` because they are **not Fowler-category patterns**: they apply across every Fowler category at once.

The four are largely orthogonal. CQRS pairs naturally with Hexagonal; Functional Core / Imperative Shell pairs with Hexagonal at the inner-core layer; Composition Root is the wiring point that ties everything together. In claude-engram the top-level shape is **CQRS + Hexagonal**, with a pure Functional Core and one Composition Root per `bin/*` entry point — see the per-pattern applicability below and `DESIGN.md` § Architecture.

---

## Hexagonal Architecture (Ports and Adapters)

> "Allow an application to equally be driven by users, programs, automated test or batch scripts, and to be developed and tested in isolation from its eventual run-time devices and databases."
> — Cockburn, "Hexagonal Architecture" (2005), <https://alistair.cockburn.us/hexagonal-architecture/>

Also documented in: Percival & Gregory 2020, Chapter 2 (introduction) + Appendix A (the master diagram).

**How it works.** The application core (domain + service layer) sits in the centre. It exposes **ports** (abstract interfaces — ABCs in Python) for everything it needs from the outside world. Concrete **adapters** implement those ports against real infrastructure. The dependency direction is **always inward**: adapters depend on ports, the core does not depend on adapters.

Two adapter classes:

- **Primary (driving) adapters** — initiate calls into the core. They translate external input into core invocations.
- **Secondary (driven) adapters** — implement infrastructure-facing ports. The core calls them through their port interface; the adapter does the actual I/O.

**When to use.** Whenever the application has more than one entry channel or needs to be testable without spinning up infrastructure. claude-engram qualifies on both counts.

**When NOT to use.** Trivial scripts where the cost of defining ports exceeds the benefit. The pattern is overkill when there is one entry point and infrastructure is the application.

**Required pairings.** Separated Interface (Fowler `10-base.md`) for the port definitions. Plugin (Fowler `10-base.md`) for the swap. Service Stub (Fowler `10-base.md`) for fake adapters in tests.

**Forbidden alongside.** Direct infrastructure imports inside the core (defeats the inversion). Page Controller (`06-web-presentation.md`) — Hexagonal demands a Front Controller / composition-root shape.

**claude-engram applicability.** ✅ **Default, load-bearing.** The core depends on **ports** (`EmbeddingGateway`, the `Distiller` interface), never on `fastembed` or a subprocess — heavy deps import **only** inside `core/adapters/`. Dependencies point inward.

| Hexagonal layer | claude-engram examples |
|-----------------|---------------------|
| **Domain (core)** | `core/store.py` (facts + embeddings + chunks data), `core/recall.py` (`Hit`), `core/distill.py` (`DistilledFact`, `Observation`), and the pure functions in `core/scoring.py`, `core/quantize.py`, `core/fusion.py`, `core/confidence.py`, `core/lexical.py` |
| **Service Layer (core, orchestration)** | `core/service.py` (capture entry functions — the write side), `core/recall.py` (`search` / `search_fused` entry functions — the read side) |
| **Secondary (driven) adapters** | `core/adapters/fastembed_gw.py` (the only place heavy deps import), the `Distiller` impls (`ClaudeCliDistiller`, `HTTPDistiller`), `core/daemon_client.py` (thin client → resident embedder, fail-open) |
| **Primary (driving) adapters** | the `bin/*` hooks (`recall_prompt.py`, `recall_session_start.py`, `capture.py`), the `engram` CLI, `bin/mcp_server.py`, `bin/daemon.py` |

Ports are the `EmbeddingGateway` / `Distiller` ABCs (`core/embedding.py`, `core/distill.py`); concrete implementations live in the adapter locations above. The `bin/*` Composition Roots (with `bin/_bootstrap.py`) do the wiring — see Composition Root below.

The cross-reference document `references/architectural-layers.md` maps this four-layer view onto the claude-engram tree explicitly with concrete file examples.

---

## CQRS — Command Query Responsibility Segregation

> "Class methods should be either commands that perform an action, or queries that return data to the caller, but not both."
> — Meyer, *Object-Oriented Software Construction* (1988) — Command-Query Separation (CQS), the local-method principle.

> "Command-Query Responsibility Segregation simply means we have separate read and write models" — applied at the architectural scale to classes, modules, code paths, and even databases.
> — Percival & Gregory 2020, Chapter 12 — Command-Query Responsibility Segregation; building on Greg Young's CQRS work (~2010).

**How it works.** Every operation is classified at design time as either a **command** (changes state) or a **query** (reads state, no side effects). Different code paths, different data shapes, often different performance profiles. The write side validates and persists; the read side is shaped for the consumer.

CQRS is **not** Event Sourcing. It is **not** "two databases". It is the discipline that read paths and write paths are designed independently because they have different concerns.

**When to use.** When read traffic and write traffic differ structurally — read-heavy on a hot path, writes that are heavier and latency-tolerant.

**When NOT to use.** Trivial CRUD where the same model serves both read and write well, and you don't have separate performance pressures. Also avoid full event-sourced CQRS as a starting position.

**Required pairings.** Service Layer (`01-domain-logic.md`) — the natural home for commands; Query Object (`05-or-metadata.md`) for the read side; often Repository (`05-or-metadata.md`) for the write side.

**Forbidden alongside.** Mixed command/query methods (`def add_and_return_total(...)` — pick one). Reading from the write path in the performance-critical read path (defeats the segregation).

**claude-engram applicability.** ✅ **Default, and the top-level split of the whole plugin.** The two sides have opposite performance profiles and run in different hooks:

- **Write side (capture)** — `bin/capture.py` → `core/service.py`. Heavy, batch, **detached** at `Stop` / `SessionEnd` / `PreCompact`. Latency-tolerant; zero interactive-token cost.
- **Read side (recall)** — `bin/recall_prompt.py` / `bin/recall_session_start.py` → `core/recall.py`. Tiny, hot-path, token- and latency-critical; runs in `UserPromptSubmit` / `SessionStart`.

Different code paths, different hooks, different data shapes. This is **not** event sourcing and **not** two databases — it is one SQLite store read one way and written another. When query patterns demand it, add a named `Store` query method or extend the Query Object (`core/recall.py`), never overload the capture path onto the read path.

---

## Functional Core, Imperative Shell

> "Push the I/O outside, push the logic into the core, and watch your code's shape change."
> — Bernhardt, "Boundaries" (RailsConf 2012) and "Functional Core, Imperative Shell" (DestroyAllSoftware screencast, 2012).

Also discussed in: Percival & Gregory 2020, Chapter 3 — A Brief Interlude: On Coupling and Abstractions.

**How it works.** Pure functions in the core compute decisions from inputs without performing I/O. The imperative shell collects inputs (read from disk / DB / subprocess), calls the pure core, then performs side effects based on what the core decided. Tests for the core need no mocking — they're plain unit tests on functions. Tests for the shell are integration tests that exercise the I/O paths.

The metaphor: a thin imperative shell wraps a large functional core. The shell deals with the messy world; the core stays clean.

**When to use.** When logic has a clear separation between "decide what to do" (pure) and "do it" (impure I/O).

**When NOT to use.** Streaming pipelines where decisions and effects are tightly interleaved, or when the I/O is the application.

**Required pairings.** Hexagonal Architecture (the shell *is* the adapters; the core *is* the domain). 

**Forbidden alongside.** I/O calls inside pure functions (defeats the pattern — you can no longer test the core without mocks). Database calls, subprocess spawns, socket reads, file reads, time-of-day reads, randomness — all belong in the shell.

**claude-engram applicability.** ✅ **Default.** The core is pure:

- **`core/distill.py`** — `heuristic_facts` and the parsers compute facts from transcript text with no I/O.
- **`core/scoring.py`, `core/quantize.py`, `core/fusion.py`, `core/confidence.py`, `core/lexical.py`** — ranking, quantisation, RRF fusion, confidence and lexical scoring are all pure functions on their inputs.

The imperative shell is **`core/service.py`** (talks to the DB, drives the embedder/distiller, spawns the detached capture worker) and the **`bin/*`** entry points. Tests for the pure core need no mocks. When adding a scoring / ranking / quantisation step, make it a **pure function** — even if the shell has to gather more data first.

---

## Composition Root

> "A Composition Root is a (preferably) unique location in an application where modules are composed together."
> — Seemann, *Dependency Injection in .NET* (2011), Chapter 3.

Also documented in: Percival & Gregory 2020, Chapter 13 — Dependency Injection (and Bootstrapping).

**How it works.** A single location — invoked once at startup — that constructs every concrete dependency, wires them into ports, and hands the assembled graph to the entry point. There is exactly one place per entry-point type that knows about the concrete world. Everything else receives dependencies as parameters.

**When to use.** Always, in any application using Hexagonal Architecture or Dependency Injection. Without a Composition Root, dependency wiring scatters across modules and the "single direction inward" property of Hexagonal breaks.

**When NOT to use.** Trivial scripts with one entry point and no abstractions — the script *is* the composition root, no extraction needed.

**Required pairings.** Plugin (Fowler `10-base.md`) — the Composition Root selects which implementation to wire. Separated Interface (Fowler `10-base.md`) — the ports. Hexagonal Architecture — the architecture this pattern serves.

**Forbidden alongside.** Concrete construction inside business logic (a hook or a core function doing `fastembed.TextEmbedding(...)` directly — that breaks the inversion). Multiple roots within one entry point.

**claude-engram applicability.** ✅ **Default — one per entry-point type.** claude-engram has several short-lived entry points rather than one long-lived app, so the Composition Root is *per-entry-point-type*; each `bin/*` script (with `bin/_bootstrap.py`) reads config, picks adapters (`get_embedder` / `get_distiller`, daemon-vs-in-process), and calls the core. Business logic never lives in a hook script.

| Entry-point type | Composition Root | Notes |
|------------------|------------------|-------|
| **Recall hooks** | `bin/recall_prompt.py`, `bin/recall_session_start.py` (+ `bin/_bootstrap.py`) | hot-path read side; pick embedder, call `recall.py`, inject |
| **Capture hook** | `bin/capture.py` (+ `bin/_bootstrap.py`) | write side; spawns the single detached worker, calls `service.py` |
| **CLI** | `bin/engram` (+ `bin/_bootstrap.py`) | index / sweep / eval / admin verbs |
| **MCP server** | `bin/mcp_server.py` (+ `bin/_bootstrap.py`) | tool-call boundary (Distribution) |
| **Daemon** | `bin/daemon.py` (+ `bin/_bootstrap.py`) | resident warm embedder |

The roots are not redundant — they have different lifetimes and pick different adapters — but they all delegate concrete construction to the shared `bin/_bootstrap.py`. **Module-level mutable singletons are avoided** (see `references/engram-defaults.md` § Base — Registry): config and deps are passed explicitly; `get_embedder` / `get_distiller` are factory functions, not a global registry. If claude-engram adds another entry-point type, it gets its own composition root sharing `_bootstrap.py`.

---

## How they fit together

The four architectural-shape patterns layer like this in claude-engram:

```
                    Composition Root (one per bin/* entry point)
                  ┌──────────────────────────────────────────┐
                  │ recall hooks   (recall_prompt/session_start)│
                  │ capture hook   (capture.py)                │
                  │ CLI            (bin/engram)                    │
                  │ MCP server     (mcp_server.py)             │
                  │ daemon         (daemon.py)                  │
                  │ (all share bin/_bootstrap.py)              │
                  └──────────────────────────────────────────┘
                                     │
                                     ▼
        ┌──────────────────────────────────────────────────────────────┐
        │                Hexagonal Architecture (the shape)            │
        │                                                              │
        │  Primary adapters (bin/* hooks, engram CLI, mcp_server, daemon) │
        │           │                                                  │
        │           ▼                                                  │
        │  Service Layer  ──── CQRS split ────                         │
        │     service.py (capture / write)  recall.py (recall / read)  │
        │           │                                                  │
        │           └──── Functional Core, Imperative Shell            │
        │                  (pure distill.heuristic_facts, scoring,     │
        │                   quantize, fusion, confidence, lexical;     │
        │                   the shell orchestrates I/O around them)    │
        │                                                              │
        │           ▼                                                  │
        │  Domain data (facts + quantised embeddings + chunks in store)│
        │           ▲                                                  │
        │           │  via ports (EmbeddingGateway, Distiller ABCs)    │
        │  Secondary adapters                                          │
        │     core/adapters/fastembed_gw, distiller impls,             │
        │     core/daemon_client (fail-open)                           │
        │           │                                                  │
        │           ▼                                                  │
        │  Infrastructure (SQLite, fastembed, local LLM / claude CLI)  │
        │                                                              │
        └──────────────────────────────────────────────────────────────┘
```

The cross-reference at `references/architectural-layers.md` shows the same shape mapped to the actual claude-engram directory tree, with concrete file examples for each layer.

These patterns are not optional in claude-engram — they're the structural baseline. In `audit` mode, the following are findings regardless of which Fowler patterns the code otherwise uses correctly:

- importing a heavy dep (e.g. `fastembed`) into the core, or spawning a subprocess outside a Gateway — violates Hexagonal;
- I/O inside a pure function (`scoring` / `quantize` / `fusion` / `distill.heuristic_facts`) — violates Functional Core;
- mixing the capture (write) and recall (read) paths — violates CQRS;
- constructing concrete dependencies outside a `bin/*` composition root — violates Composition Root.

Plus the budget / fail-open rules: embedding on the hot path, or non-detached capture, are surfaced as findings — see `references/anti-patterns.md`.
