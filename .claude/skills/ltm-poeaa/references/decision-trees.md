# Decision Trees — quick choice points

When the user is at one of these forks, do not enumerate every option — answer the fork
directly using the tree below, then point at the relevant catalog file for depth. These
are grounded in `ltm-defaults.md`; when a tree says "always X in claude-ltm", that is a
locked-in default, not a preference.

---

## "Which seam does this work belong in?"

claude-ltm is one package (see `single-package.md`); the question is *seam*, not package.

```
Is it a pure decision/computation over data (distil, rank, score, quantise, fuse)?
    └─ yes ──► Functional Core  (core/distill.py, scoring.py, quantize.py, fusion.py, …)

Is it orchestration — open the store, call the core, embed, persist/render?
    ├─ a write (capture) ──► core/service.py   (Command/Handler)
    └─ a read (recall)   ──► core/recall.py    (Query Object + render_block DTO)

Is it persistence / a query surface?
    └─ yes ──► core/store.py   (Repository over Data Mapper — the ONLY place SQL lives)

Is it I/O against a model / subprocess / socket?
    └─ yes ──► a Gateway behind an interface  (core/embedding.py + core/adapters/,
                                               core/distill.py distiller impls, core/daemon_client.py)

Is it reading config, picking adapters, and invoking a handler for a hook/CLI/MCP call?
    └─ yes ──► a Composition Root  (bin/*, sharing bin/_bootstrap.py)
```

If the work needs a heavy dependency, it goes **behind an interface in `core/adapters/`** —
never imported into `core/` proper. That's Hexagonal's inward-dependency rule.

---

## "Where should this logic live?"

```
Is it a pure decision computable from its inputs (no I/O)?
    ├─ yes ──► Functional Core — a pure function (core/scoring.py, distill.py, quantize.py, fusion.py)
    └─ no
        Does it open the store / embed / spawn a worker / render a payload?
            ├─ yes ──► Imperative Shell — service.py (capture) or recall.py (read), or a bin/* root
            └─ no ──► back up — re-examine the step
```

If you find yourself putting a DB read, a model load, a subprocess, a clock read, or
randomness **inside** a scoring/distillation function, stop — that I/O belongs in the shell,
which passes already-resolved values into the pure core. Otherwise the core stops being
testable without mocks (Operating Principle 9).

There is **no rich Domain Model** to push behaviour onto — a fact is data. Don't add methods
to the row/dataclass types.

---

## "How should this be persisted?"

```
Is it memory (a fact) or index data (a code/doc chunk)?
    └─ yes ──► through core/store.py — Store.add(...) / replace_source_chunks(...)
                 └─ Identity Field: content-hash fact_id / chunk_id  (makes capture idempotent)
                 └─ tag the row with the project key (marker-walk identity)
                 └─ the quantised vector is an Embedded Value inlined on the row
                 └─ the int8 + binary blobs are a Serialized LOB — stored whole, never filtered inside
```

Never reach for Active Record — there is **no** `fact.save()`. `Store` owns all SQL.
Never write raw SQL above the `store.py` boundary.

---

## "Active Record or Repository/Data Mapper?"

In claude-ltm: **always Repository over Data Mapper** (`core/store.py`). Facts are plain
`sqlite3.Row` data with no persistence methods. Active Record is **forbidden** — it would
couple every fact to the schema and to quantisation and defeat the seam that lets the store
be swept, pruned, migrated, benchmarked, and browsed independently.

---

## "Should this be its own Repository class?"

```
Is there more than one storage backend, or a second aggregate with its own query surface?
    ├─ no  ──► NO — there is ONE Store; add a named method to it (fts_search, chunk_outline, …)
    └─ yes ──► reconsider — claude-ltm is single-store by design; this is a re-architecture
```

A `FooRepository.get(id)` that just wraps a single `Store` call is the **Thin Repository**
anti-pattern — don't add it. The `Store` *is* the repository.

---

## "Where should new search knobs go?"

```
Adding a ranking weight / threshold / limit / penalty to recall?
    └─► extend the Query Object — the frozen Config and the search() params
        NOT a new positional argument on search(); NOT a new global
```

`recall.search` / `search_fused` take a bundled parameter set (`Config` weights + query
vector + `min_sim` + `top_k` + cross-project penalty). Grow the object, not the signature.
See `catalog/05-or-metadata.md` § Query Object.

---

## "Should this external dependency be a Gateway?"

```
Is the code reaching out to a model / subprocess / HTTP endpoint / socket?
    ├─ SQLite  ──► already covered by Store (Repository/Data Mapper), no separate Gateway
    └─ embedding model / local LLM / claude CLI / resident daemon
        ──► Gateway behind a Separated Interface
        ├─ Define the ABC port (EmbeddingGateway in core/embedding.py; Distiller in core/distill.py)
        ├─ One real impl in core/adapters/ (or a distiller subclass)  ← heavy deps import HERE only
        ├─ One zero-dep Service Stub that doubles as the test fake (HashEmbedding, HeuristicDistiller)
        └─ Plugin selection via get_embedder(cfg) / get_distiller(cfg) in the Composition Root
            └─ fail-open fallback (fastembed→hash, daemon→in-process, LLM→heuristic)
```

`core/` never imports `fastembed`, spawns `claude`, or opens the daemon socket directly —
always behind a Gateway. See `catalog/10-base.md` § Gateway / Separated Interface / Plugin.

---

## "Inject something, or stay silent?" (recall payload)

```
Did any candidate clear the similarity gate (min_sim)?
    ├─ no  ──► render_block returns ""  — inject NOTHING (Special Case / Null Object)
    └─ yes ──► render_block: one line per fact, hard-capped at max_chars (DTO for the token budget)
```

Never inject a placeholder ("no relevant memory"), an error, or a raw row. Irrelevant turns
must cost zero tokens. See `catalog/07-distribution.md` § DTO and `catalog/10-base.md` §
Special Case.

---

## "In-process embedding, or the daemon?"

```
Is LTM_DAEMON set and a resident daemon reachable?
    ├─ yes ──► daemon_client (Remote Facade) — embed over the local socket (model stays warm)
    └─ no / any failure ──► load the model in-process
        └─ fastembed unavailable ──► HashEmbedding (lexical stub, zero-dep)
```

The recall hook is a short-lived process, so a real model would reload every turn — the
daemon holds it warm. The client is **fail-open**: a daemon outage degrades silently, it
never breaks a turn. See `catalog/07-distribution.md` § Remote Facade.

---

## "Should this run on the hot path or detached?"

```
Does it need to affect THIS turn's context (recall)?
    ├─ yes ──► hot path (UserPromptSubmit / SessionStart) — but it MUST be cheap:
    │            daemon-backed embedding, threshold-gated, byte-capped payload
    └─ no  ──► detached (capture at Stop/SessionEnd/PreCompact)
        └─ spawn ONE worker (single-flight), fire-and-forget, fail-open
            └─ heavy work (LLM distillation, embedding a whole session) belongs HERE
```

Capture is latency-tolerant and must never cost interactive tokens or block a turn. Recall is
latency-critical. This split *is* CQRS — see `catalog/11-architectural-style.md` § CQRS and
`.claude/rules/02-architecture/02-hooks-and-budgets.md`.

---

## "How do I handle a conflicting / stale / repeated fact?"

```
Same fact captured again?
    └─► Store.reinforce (Consolidation) — freq++, recency refreshed; NOT a duplicate

A newer near-identical fact arrives?
    └─► Store.supersede (hard supersession) — older row archived (status='superseded'), filtered at SQL

A fact hasn't been seen for a long time?
    └─► Store.sweep (TTL hard expiry) — retire it (reversible status flag), unless reinforced past ttl_keep_frequency

Two facts merely differ in age, no conflict?
    └─► recency decay in scoring.py only RE-ORDERS them; it does not retire either
```

Conflicts (supersession, a hard SQL filter) and ordering (recency decay, a soft score) are
**deliberately separate** — folding conflict into the score would let a stale-but-frequent
fact leak. See `DESIGN.md` § Memory lifecycle and `catalog/08-offline-concurrency.md`.

---

## "Is this a DTO or a Value Object?"

```
Does it cross a boundary as serialized data?
    ├─ injected into the context window   ──► DTO (render_block — one line per fact, capped)
    ├─ returned by an MCP tool            ──► DTO (outline + anchor + freshness, not a raw row)
    ├─ sent over the daemon socket        ──► DTO (vectors)
    └─ no
        Is its equality based on value (not identity), and immutable?
            ├─ yes ──► Value Object (DistilledFact, Observation, Hit, frozen Config)
            └─ no  ──► reconsider — most in-core data is one of the above
```

These are different patterns. The 2003-era "Value Object" in J2EE meant *DTO*; modern usage
follows Fowler. See `catalog/07-distribution.md` § DTO and `catalog/10-base.md` § Value Object.

---

## When uncertain

If the user's question doesn't fit any of these trees cleanly, use the `recommend` mode
workflow:
1. Identify the POEAA category (table in `SKILL.md`).
2. Identify the seam (`single-package.md` seam table).
3. Read the matching `references/catalog/*.md`.
4. Cross-check `references/ltm-defaults.md`.
5. Cross-check `references/anti-patterns.md` (including the budget / fail-open / dependency-direction section).
6. Recommend with citations.

Do **not** invent new pattern names or fuse two patterns into one. The catalog (Fowler's
POEAA + Cosmic Python additions) is closed under its citation policy; if an observed
structure doesn't match any of them, say so.
