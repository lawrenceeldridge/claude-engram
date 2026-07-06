# Object-Relational Behavioral Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 11.

These three patterns describe **runtime behaviour** of an O/R layer. They are ORM-session behaviours — and claude-engram has **no ORM** and **no long-lived object graph** (hooks are short-lived processes). So all three are **deliberately minimal or N/A** here; the absence is the design.

---

## Unit of Work

> "Maintains a list of objects affected by a business transaction and coordinates the writing out of changes and the resolution of concurrency problems."
> — <https://martinfowler.com/eaaCatalog/unitOfWork.html>

**How it works.** Track every object created, modified, or deleted during a business transaction. At commit time, compute the SQL needed and write it in dependency order, in one transaction.

**When to use.** Any non-trivial Domain Model. Without it, every change writes immediately and you lose atomicity, ordering control, and the ability to roll back cleanly.

**When NOT to use.** Real-time pipelines where each event must persist immediately, or strictly single-statement updates.

**Required pairings.** Data Mapper (the thing the UoW orchestrates), Identity Map (to avoid double-writing the same object).

**claude-engram applicability.** ⚠️ **Minimal — no UoW abstraction.** There is one short-lived `sqlite3.Connection` per process, with explicit transactions inside `core/store.py`. Capture is **batched per session** and **idempotent per fact** (content-hash `fact_id` + `Store.exists` / `Store.reinforce`), which gives the atomicity benefit a Unit of Work would provide, without the machinery. **Guard:** don't build a UoW / session abstraction to "complete the pattern set" — a hook runs, writes, and exits.

**Cosmic Python refinement / claude-engram shape.** Cosmic Ch 6 wraps a database session in an **async context manager** so every command body is bracketed by entry/exit semantics (enter opens the UoW, exit commits or rolls back). claude-engram deliberately does *not* adopt that shape: there is no session to manage across a request, no async event loop in the capture path, and no cross-aggregate transaction. The `Store` opens a connection, does its explicit `BEGIN`/`COMMIT` around a batch, and closes — the collapsed form of Cosmic's UoW for a stdlib-`sqlite3`, single-writer, detached-batch world.

---

## Identity Map

> "Ensures that each object gets loaded only once by keeping every loaded object in a map."
> — <https://martinfowler.com/eaaCatalog/identityMap.html>

**How it works.** When a row is loaded, the mapper checks an in-memory map keyed by primary key. If the object is already there, return it; otherwise load and register it. Two requests for the same row in the same session yield the same Python object.

**When to use.** Always, when using Data Mapper. Without it, the same row materialised twice can produce divergent state and lost updates.

**When NOT to use.** Single-shot read-only queries where identity doesn't matter (e.g., aggregations).

**Required pairings.** Data Mapper.

**claude-engram applicability.** ❌ **N/A.** There is no ORM session and no in-memory object graph to deduplicate. Identity is a **content-hash `fact_id`** established at the Data Source layer (see `04-or-structural.md` § Identity Field), not an in-memory map keyed by surrogate id. Recall reads rows fresh, ranks them as plain data, and discards them when the process exits — there is nothing to keep loaded-once.

---

## Lazy Load

> "An object that doesn't contain all of the data you need but knows how to get it."
> — <https://martinfowler.com/eaaCatalog/lazyLoad.html>

**How it works.** Four variants:
1. **Lazy Initialization** — a sentinel field (often null); on first access, load and cache.
2. **Virtual Proxy** — a stand-in object with the same interface; loads on first method call.
3. **Value Holder** — wraps the real object behind `getValue()`.
4. **Ghost** — the real object created in stripped-down form; loads fully on first method call.

**When to use.** When loading a parent object would otherwise drag in many children that may not be used.

**When NOT to use.** When the children are nearly always needed — eager loading is faster and avoids N+1 queries.

**Required pairings.** Data Mapper.

**Anti-pattern guard.** Lazy loading without query control causes the **N+1 problem**: iterating N parents triggers N child queries.

**claude-engram applicability.** ❌ **N/A — and would be a smell here.** Fact and chunk rows are fully materialised or not read at all; there are no object relationships to lazy-load and no parent→children graph. The embedding blob is read whole with its row (Serialized LOB — see `04-or-structural.md`), never fetched on demand. Introducing a proxy/ghost over a fact row would add exactly the indirection the plain-data design avoids.

---

## How they fit together

In a full ORM these three compose into a canonical read–modify–write. In claude-engram they **collapse into "one connection, explicit commits, plain rows"**:

1. A hook process opens one short-lived `sqlite3.Connection`.
2. Reads return plain `sqlite3.Row` data — no Identity Map, no proxies.
3. Writes are a batched, idempotent-per-fact `BEGIN`/`COMMIT` inside `Store` — the atomicity a Unit of Work would give, without the abstraction.
4. The process exits; nothing is kept loaded.

The **absence** of Unit of Work / Identity Map / Lazy Load machinery is the design, not a gap. Don't import an ORM or build a session/identity-map layer to fill it — see `references/engram-defaults.md` § O-R Behavioral.
