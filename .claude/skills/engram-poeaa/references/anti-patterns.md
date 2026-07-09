# Anti-Pattern Combinations — Forbidden Mixes

Not every pair of POEAA patterns can coexist. Some are mutually exclusive at the same
scope; others combine in ways that produce well-known anti-patterns. claude-engram adds a
project-specific class on top — violations of the **stdlib-first / dependency-direction /
token-latency-budget / fail-open** contracts that this catalog treats as first-class
anti-patterns alongside Fowler's.

This file is the **block list** the `audit` mode checks against and the **veto list** the
`recommend` mode applies.

A combination is forbidden when:
1. The two patterns answer the **same question** in incompatible ways for the same scope, **or**
2. The combination produces a recognised anti-pattern (Active-Record creep, leaky core, etc.), **or**
3. (claude-engram–specific) it breaks the Hexagonal dependency direction, the CQRS hot-path
   budget, or the fail-open guarantee.

---

## Mutually exclusive at the same scope

These pairs cannot both be in force for the **same data / seam / responsibility**.

| Pattern A | Pattern B | Scope | Why incompatible |
|-----------|-----------|-------|------------------|
| Active Record | Data Mapper / Repository | same fact/row | Two owners of persistence — `fact.save()` vs `Store.add(fact)` |
| Rich Domain Model | plain-data + Functional Core | same seam | Contradictory mental models; claude-engram chose data + pure functions |
| Heavy dep imported in `core/` | Gateway behind an interface | same dependency | Defeats Hexagonal — the core must not import `fastembed` / spawn `claude` directly |
| I/O inside a pure function | Functional Core / Imperative Shell | same function | A core function that reads the DB / loads a model can't be tested without mocks |
| Synchronous model load on the recall hook | daemon-client Remote Facade | recall hot path | Reloads the model every turn; blows the latency budget |
| Placeholder/error on empty recall | Special Case / Null Object | `render_block` | Spends tokens on irrelevant turns |
| **Event** bus / pub-sub | detached capture / durable Command queue | capture | claude-engram has no *Event* bus (no publishers/subscribers, no broadcast). A durable **Command** queue (`WorkQueue`) is permitted — one handler, retry/dead-letter, not pub-sub. See `engram-defaults.md` §§ Distribution / Offline Concurrency |
| Server/Database Session State | stateless short-lived hooks | cross-turn state | There is no session; the durable store is Domain data, not a session store |

(Patterns can of course coexist in *different* seams — e.g. the viewer's HTML dispatch and
the MCP tool dispatch are both Front-Controller-shaped without clashing.)

---

## Recognised anti-patterns the combinations produce

### 1. Active Record creep
**Combination.** Persistence methods added to a fact/chunk row or a `DistilledFact` dataclass
(`fact.save()`, `fact.delete()`, `Fact.find_by(...)`).
**Symptom.** The data type now knows about SQLite, the schema, and quantisation.
**Diagnostic tells.** `def save(self)` / `def delete(self)` / `@classmethod def find…` on any
type in `core/`; a caller mutating a row and calling a persistence method on it.
**Fix.** All persistence goes through `core/store.py` (`Store.add`, `Store.supersede`,
`Store.sweep`). Rows stay plain data. See `catalog/02-data-source.md`.

### 2. Leaky core (I/O in a pure function)
**Combination.** Functional Core + I/O inside it.
**Symptom.** A scoring/distillation/quantisation/fusion function reads the DB, loads a model,
spawns a subprocess, reads the clock, or uses randomness.
**Diagnostic tells.** `import sqlite3` / a `Store` reference / `subprocess` / `urllib` / model
loading / `time.time()` / `random` inside `core/scoring.py`, `distill.py` (the pure fns),
`quantize.py`, `fusion.py`, `confidence.py`, `lexical.py`.
**Fix.** Move the I/O to the shell (`service.py`, `recall.py` callers, `bin/*`); pass
already-resolved values into the pure function. Keep the core testable without mocks. See
`catalog/11-architectural-style.md` § Functional Core / Imperative Shell.

### 3. Heavy dependency in the core (Hexagonal violation)
**Combination.** `core/` (outside `core/adapters/`) importing `fastembed`, opening the daemon
socket, or shelling out to `claude` directly.
**Symptom.** The core no longer runs on the standard library alone; the stdlib-first guarantee
breaks; the dep can't be swapped without editing the core.
**Diagnostic tells.** `import fastembed` anywhere but `core/adapters/`; a `subprocess`/`urllib`
call in `core/` that isn't inside a `Distiller` impl; an `if backend == "fastembed": …` branch
in the core.
**Fix.** Put the dep behind the existing interface (`EmbeddingGateway` / `Distiller` ABC) in a
new/existing adapter under `core/adapters/`; select it with `get_embedder` / `get_distiller` in
the Composition Root. See `catalog/10-base.md` § Gateway / Separated Interface / Plugin.

### 4. Fail-closed capture or recall
**Combination.** A hook (or a Gateway) that raises out to the caller instead of degrading.
**Symptom.** A capture error, an embedding error, or a daemon outage breaks the user's turn.
**Diagnostic tells.** A `bin/*` hook without a top-level guard that exits 0 and injects nothing
on error; a Gateway/Plugin selection with no fallback; capture that runs inline instead of
detached.
**Fix.** Every hook exits 0 on any error and injects `""`. Every selection degrades:
fastembed → hash, daemon → in-process, LLM distiller → heuristic. Capture spawns a detached
worker. See `engram-defaults.md` § Base (fail-open) and `.claude/rules/02-architecture/02-hooks-and-budgets.md`.

### 5. Un-capped / placeholder recall payload
**Combination.** DTO without the Null Object + cap.
**Symptom.** Recall injects a placeholder ("no relevant memory"), an error, a raw row, or an
un-capped block — spending tokens on irrelevant turns or blowing the byte cap.
**Diagnostic tells.** `render_block` (or a caller) returning a non-empty string on zero hits;
injection paths that bypass `max_chars`; a raw `sqlite3.Row` reaching `additionalContext`.
**Fix.** `render_block` returns `""` on empty (Null Object), one line per fact, hard-capped at
`max_chars`. See `catalog/07-distribution.md` § DTO and `catalog/10-base.md` § Special Case.

### 6. Thin Repository / speculative Repository class
**Combination.** A `FooRepository` wrapping a single `Store` call, or a second Repository class
for a second "aggregate".
**Symptom.** An empty-shell layer that costs reading time and buys nothing; or invented bounded
contexts.
**Fix.** There is one `Store`; add a named method to it. Don't create per-aggregate Repository
classes — claude-engram is single-store, single-context. See `catalog/05-or-metadata.md` § Repository
and `single-package.md`.

### 7. Query signature sprawl
**Combination.** Query Object abandoned — new recall knobs added as positional args or globals.
**Symptom.** `search(vec, min_sim, top_k, w_sim, w_rec, w_freq, penalty, cross, …)` grows without
bound; behaviour leaks into module-level state.
**Fix.** Extend the frozen `Config` / the bundled `search` params. See `catalog/05-or-metadata.md`
§ Query Object.

### 8. Ordering/conflict conflation
**Combination.** Folding conflict resolution into the ranking score instead of a hard filter.
**Symptom.** A stale-but-frequent superseded fact leaks back into recall because recency decay
only *de-ranked* it instead of supersession *retiring* it.
**Diagnostic tells.** Supersession logic living in `scoring.py`; superseded rows not filtered at
SQL in `Store`.
**Fix.** Keep them separate: hard supersession (`Store.supersede`, filtered at SQL) retires
conflicts; recency decay (`scoring.py`) only orders non-conflicting facts. See `DESIGN.md` §
Memory lifecycle and `catalog/08-offline-concurrency.md`.

### 9. Module-level mutable Registry
**Combination.** Registry implemented as a module-level mutable singleton.
**Symptom.** Tests interfere; hidden global state; can't substitute in test.
**Diagnostic tells.** `_EMBEDDER = None; def get(): global _EMBEDDER; if _EMBEDDER is None: …`
holding a mutable instance at import time in domain code.
**Fix.** Pass config and dependencies explicitly into the Service Layer; let the `bin/*`
Composition Root construct them. `get_embedder(cfg)` / `get_distiller(cfg)` are pure factory
functions, not a mutable registry — keep them that way. See `catalog/10-base.md` § Registry.

### 10. Rebuilding ORM machinery
**Combination.** Introducing a Unit-of-Work / Identity-Map / session abstraction, or an ORM.
**Symptom.** A session/identity-map layer over `sqlite3` to "complete the pattern set".
**Fix.** claude-engram uses one short-lived connection + explicit transactions + content-hash
identity by design (`catalog/03-or-behavioral.md`). Don't add the machinery — the absence is
the design. This is a "not applicable, don't add it" finding, distinct from a "dated" one.

### 11. DTO masquerading as Value Object (terminology trap)
**Combination.** Calling an injected/returned payload a "Value Object", or making a real VO
mutable.
**Fix.** DTO = *Data Transfer Object* (crosses a boundary: `render_block`, MCP responses, daemon
vectors). Value Object = identity-by-value, immutable (`DistilledFact`, `Observation`, `Hit`,
`Config`). See `catalog/07-distribution.md` § DTO and `catalog/10-base.md` § Value Object.

---

## claude-engram–specific anti-patterns (budget / fail-open / dependency direction)

These do not appear verbatim in Fowler's catalog but are first-class architectural violations
in claude-engram.

### 12. Embedding work on the recall hot path
**Combination.** A synchronous model load or heavy embedding in `UserPromptSubmit` /
`SessionStart` without the daemon-backed-with-fallback path.
**Symptom.** Every turn reloads the model; the latency budget (≈ single-digit ms target for
personal-store recall) is blown; a slow embed stalls the turn.
**Fix.** Use `daemon_client` (Remote Facade, model kept warm) with an in-process fallback, and
keep the `hash` stub as the zero-dep floor. Threshold-gate (`min_sim`) and byte-cap the result.
See `catalog/07-distribution.md` § Remote Facade and `.claude/rules/02-architecture/02-hooks-and-budgets.md`.

### 13. Capture on the interactive path
**Combination.** Running distillation / whole-session embedding / LLM calls inline in a hook
instead of a detached worker.
**Symptom.** Capture costs interactive tokens or blocks the turn; a `claude`/`ollama` call in the
foreground stalls the user.
**Fix.** Capture spawns **one** detached worker (single-flight) at Stop/SessionEnd/PreCompact and
returns immediately; all heavy work runs there, off the hot path. See `catalog/08-offline-concurrency.md`
§ claude-engram substitute and `engram-defaults.md` § Offline Concurrency.

### 14. Worker / daemon pileup (lost single-flight)
**Combination.** A capture hook that spawns a new worker/daemon on every fire without the
single-flight guard.
**Symptom.** Multiple capture workers (or daemons) stack up on rapid Stop/PreCompact events,
contending on the store.
**Diagnostic tells.** No single-flight lock/marker before spawning in `bin/capture.py`; a second
daemon launched when one is already resident.
**Fix.** Single-flight — one worker/daemon per session; a second capture does not stack another.
This was a real regression fixed in commit `b30889a`. See `engram-defaults.md` § Offline Concurrency.

### 15. Non-idempotent capture
**Combination.** Capture that inserts without the content-hash `fact_id` / `exists` check.
**Symptom.** Re-running capture (or re-processing a transcript on PreCompact then SessionEnd)
duplicates facts instead of reinforcing them.
**Fix.** Identity is `Store.fact_id(project_key, text)`; `Store.exists` / `Store.reinforce` make
re-capture a no-op-or-boost. See `catalog/04-or-structural.md` § Identity Field.

### 16. SQL / raw rows above the Store boundary
**Combination.** A SQL string, a cursor, or a raw `sqlite3.Row` used outside `core/store.py`.
**Symptom.** Persistence details leak into `service.py`, `recall.py`, `bin/*`, or the viewer;
the mapper seam is bypassed.
**Fix.** All SQL lives in `Store`; callers get plain dicts / value objects / DTOs, never a cursor
or a `Row`. See `catalog/02-data-source.md` and `05-or-metadata.md`.

### 17. Filtering inside the embedding LOB
**Combination.** Treating the int8/binary embedding blob as queryable structured data.
**Symptom.** Code that decodes the blob to filter/branch in a query path instead of using it as
an opaque semantic fingerprint fed to the ranking core.
**Fix.** The embedding is a Serialized LOB / Embedded Value — searched by cosine/Hamming in the
ranking core, never filtered inside via SQL. See `catalog/04-or-structural.md` § Serialized LOB.

### 18. Inventing bounded contexts
**Combination.** Treating a seam ("recall", "index") as a separate bounded context with its own
divergent defaults.
**Symptom.** Two sets of pattern choices in one package; "per-context" exceptions.
**Fix.** claude-engram is one package, one context, one set of defaults. See `single-package.md`.

---

## Audit checklist

When auditing code, run this list against the target:

**Fowler-classic checks**
- [ ] Is there `save()` / `delete()` / `find_by` on a fact/chunk row or dataclass? (Active Record creep)
- [ ] Are raw `sqlite3.Row` objects or SQL strings used outside `core/store.py`? (Leaky persistence)
- [ ] Is there a `FooRepository` wrapping a single `Store` call, or a second Repository class? (Thin / speculative Repository)
- [ ] Are new recall knobs added as positional args / globals instead of on the Query Object/Config? (Signature sprawl)
- [ ] Is there a module-level mutable singleton holding an embedder/distiller? (Module-level Registry)
- [ ] Is a UoW / Identity Map / session / ORM abstraction being introduced? (Rebuilding ORM machinery — not applicable)
- [ ] Is an injected/returned payload called a "Value Object", or a real VO mutable? (DTO/VO trap)

**claude-engram–specific checks**
- [ ] Does any `core/` file outside `core/adapters/` import `fastembed`, spawn `claude`, or open the daemon socket? (Heavy dep in core)
- [ ] Does a pure function in `scoring/distill/quantize/fusion/confidence/lexical` do I/O, read the clock, or use randomness? (Leaky core)
- [ ] Does recall load a model synchronously on `UserPromptSubmit`/`SessionStart` without the daemon-with-fallback path? (Hot-path embedding)
- [ ] Does capture run inline instead of a detached, single-flight worker? (Capture on the interactive path / worker pileup)
- [ ] Does capture insert without the `fact_id`/`exists` idempotency check? (Non-idempotent capture)
- [ ] Does a hook fail to exit 0 / inject `""` on error, or a Gateway lack a fail-open fallback? (Fail-closed)
- [ ] Does `render_block` (or a caller) return a placeholder/error/un-capped block on empty or many hits? (Un-capped / placeholder payload)
- [ ] Is supersession logic in `scoring.py`, or are superseded rows not filtered at SQL? (Ordering/conflict conflation)
- [ ] Is the embedding blob decoded and filtered in a query path? (Filtering inside the LOB)
- [ ] Is a seam being treated as a separate bounded context with divergent defaults? (Invented context)

Each "yes" is a finding. Cite the file:line and the anti-pattern by name in the audit report.
