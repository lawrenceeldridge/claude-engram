# Pattern Combinations — Natural Pairings

The patterns in the catalog (Fowler's POEAA plus Cosmic Python additions) are not
independent. Many *require* others to function; many *naturally pair* and reinforce each
other; a smaller number *clash* (those are in `anti-patterns.md`).

Use this file in `recommend` and `apply` modes: when you propose a pattern, also surface
the patterns it brings.

---

## Required pairings (you cannot have one without the other)

| If you adopt | You must also adopt | Why |
|--------------|---------------------|-----|
| Repository | Data Mapper | Repository is built *on top of* the mapper (in claude-engram both are `store.py`) |
| Query Object | Data Mapper | The Query Object speaks in fields/params; the mapper turns them into SQL |
| Gateway | Separated Interface | The core depends on the interface (`EmbeddingGateway` / `Distiller` ABC), not the impl |
| Service Stub | Gateway + Separated Interface | The stub fakes a Gateway; both satisfy the same interface |
| Plugin | Separated Interface | The contract the plugin selection satisfies (`get_embedder` picks an `EmbeddingGateway`) |
| Remote Facade | Data Transfer Object | The wire shape the facade speaks (`daemon_client` → vectors; MCP → outline DTOs) |
| Embedded Value | Value Object | Embedded Value is the persistence side of a value (the quantised vector inlined on the row) |
| Functional Core / Imperative Shell | Composition Root | The shell's outermost point wires the pure core to its adapters |
| Command / Handler | Composition Root | Handlers are entry points; something must wire and invoke them (`bin/*`) |

---

## Strong natural pairings (commonly used together)

| Pattern A | Pattern B | Together they enable |
|-----------|-----------|---------------------|
| Gateway | Service Stub | Gateway provides the interface; stub provides the zero-dep / test impl |
| Gateway | Plugin | Different Gateway impls selected per config (`hash` vs `fastembed`; heuristic vs `claude` vs `ollama`) |
| Separated Interface | Plugin | The contract + the runtime selection |
| Separated Interface | Layer Supertype | The ABC is both the port and the base type for its adapters |
| Repository | Query Object | Named `Store` methods construct the search/params, callers never see SQL |
| Query Object | Functional Core | The query gathers candidates; pure scoring/fusion re-ranks them |
| DTO | Mapper (general) | An assembler shapes rows → the injected/returned DTO |
| DTO | Special Case / Null Object | Empty result → the DTO degrades to `""` (`render_block`), never a placeholder |
| Value Object | Special Case | The "no value" case is a Null-Object-shaped VO |
| Remote Facade | Service Stub | Facade over the daemon; the in-process fallback is effectively the stub path |
| CQRS | Functional Core / Imperative Shell | Read and write paths each wrap the same pure core differently |
| Composition Root | Plugin | The root selects which plugin to wire based on config/env |

---

## claude-engram's adopted bundles

When claude-engram reaches for one pattern, it brings the whole bundle. Don't propose any
pattern in a bundle without confirming the rest are present.

### Testability / swappable-backend bundle (embeddings + distillers)

```
Separated Interface (EmbeddingGateway ABC / Distiller ABC)   ← core/embedding.py, core/distill.py
    ├─ Gateway (real impl: fastembed_gw, ClaudeCliDistiller, HTTPDistiller)
    ├─ Service Stub (zero-dep default AND test fake: HashEmbedding, HeuristicDistiller)
    └─ Plugin (get_embedder(cfg) / get_distiller(cfg) — runtime selection)
        └─ Composition Root (bin/* picks the impl from config/env; ENGRAM_DAEMON toggles daemon)
            └─ fail-open contract (fastembed→hash, daemon→in-process, LLM→heuristic)
```

This is the bundle that keeps the core stdlib-testable and lets a backend be swapped
without touching `core/`. Adding a backend = a new adapter behind the existing interface.

### Persistence / read bundle (recall)

```
Service Layer (recall.py: search / search_fused)
    └─ Query Object (Config weights + query vec + min_sim/top_k/penalty — bundle, don't grow args)
        └─ Repository (Store methods: active_rows_for_project, fts_search, chunk_outline)
            └─ Data Mapper (hand-written on stdlib sqlite3, in store.py)
                ├─ Identity Field (content-hash fact_id / chunk_id; rows tagged by project)
                ├─ Serialized LOB (int8 + binary embedding blobs, read/written whole)
                └─ Embedded Value (quantised vector inlined on the row)
        └─ Functional Core (scoring priority re-rank + fusion RRF — pure)
            └─ DTO / Null Object (render_block: one-line-per-fact, max_chars cap; "" on empty)
```

If any link is missing, recall either leaks tokens (no cap / no Null Object), leaks SQL
(no Repository boundary), or loses testability (I/O in the scoring core).

### Capture / write bundle

```
Primary adapter (bin/capture.py — Stop/SessionEnd/PreCompact)
    └─ Single-flight (spawn ONE detached worker; don't stack workers/daemons)
        └─ Command / Handler (service.capture_* — function-style)
            └─ Functional Core (distill: pure heuristic_facts / parsers)
                └─ Gateway (embed via EmbeddingGateway / daemon_client)
                    └─ Repository (Store.add) — idempotent per fact_id
                        ├─ Consolidation (Store.reinforce on repeat — freq++, recency refresh)
                        ├─ Supersession (Store.supersede — newer near-duplicate archives older)
                        └─ TTL sweep (Store.sweep — retire stale, reversible)
            └─ fail-open (any failure: exit 0, capture nothing; LLM distiller → heuristic)
```

Capture is detached and fail-open end to end — it never breaks a turn, and re-running it
is always safe (idempotent per fact).

### Distribution bundle — the injected payload

```
Service Layer (recall)
    └─ DTO (render_block — shaped for the TOKEN budget, not the store)
        ├─ one line per fact
        ├─ max_chars hard cap
        └─ Special Case / Null Object → "" on empty recall
```

### Distribution bundle — the daemon boundary

```
Recall hook (bin/recall_prompt.py)
    └─ Remote Facade (daemon_client → bin/daemon.py resident embedder)
        ├─ coarse call (embed a batch → vectors over a local socket)
        └─ fail-open in-process fallback (daemon down → load model in-process → hash if unavailable)
```

### Architectural-shape bundle (whole plugin)

```
Composition Root (bin/* + _bootstrap.py)
    └─ Hexagonal (core depends on ports; heavy deps only in core/adapters/)
        ├─ CQRS (capture write side / recall read side — separate hooks, opposite profiles)
        └─ Functional Core / Imperative Shell (pure distill/score/quantize/fuse; shell does I/O)
```

---

## How to surface pairings

In `recommend` mode, after stating the primary pattern, add:

```markdown
### Required pairings (from references/combinations.md)
- <Pattern A> — already in claude-engram ✅ / needs introducing
- <Pattern B> — already in claude-engram ✅ / needs introducing
```

If a required pairing is missing and the user is not prepared to introduce it, the
recommendation is unsafe — flag it.

For any new embedding or distiller backend, the **testability / swappable-backend bundle**
must be confirmed end to end (new adapter behind the interface, selected by Plugin, wired in
the Composition Root, with a fail-open fallback). Skipping the interface and importing the
dep into the core breaks Hexagonal — see `anti-patterns.md`.

For any change to recall, the **DTO / Null Object** link must stay intact — an un-capped or
placeholder-injecting payload spends tokens on irrelevant turns.
