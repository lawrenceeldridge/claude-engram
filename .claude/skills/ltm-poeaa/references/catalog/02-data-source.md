# Data Source Architectural Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 10.

These four patterns are **mutually exclusive choices** for how application code reaches the database. Pick one per bounded context. claude-ltm has chosen **Data Mapper** — hand-written on the standard-library `sqlite3` module in `core/store.py`, **not** an ORM — with a lightweight Repository on top (see `05-or-metadata.md`).

---

## Table Data Gateway

🪦 **Dated** — see `references/dated-patterns.md`. Pre-ORM hand-rolled SQL gateway. Modern equivalent: Data Mapper.

> "An object that acts as a gateway to a database table. One instance handles all the rows in the table."
> — <https://martinfowler.com/eaaCatalog/tableDataGateway.html>

**How it works.** All SQL for a table — selects, inserts, updates, deletes — sits on one gateway object. Application code calls gateway methods; the gateway returns Record Sets or primitive values.

**When to use.** SQL skills vary across the team and DBAs need to find all SQL in one place. Works well with Table Module and Record Sets.

**When NOT to use.** Domain logic is rich — the gateway return type (Record Set / dict) becomes the lingua franca and you lose object behaviour.

**Forbidden alongside.** Direct SQL in domain models or commands (defeats the gateway).

**Natural pairings.** Table Module, Record Set, Transaction Script.

**claude-ltm applicability.** ❌ Not used, and dated. `core/store.py::Store` owns all SQL, but it is framed as a Repository over a Data Mapper (below), not a per-table gateway returning Record Sets.

---

## Row Data Gateway

🪦 **Dated** — see `references/dated-patterns.md`. Same era as Table Data Gateway. Modern equivalent: Data Mapper.

> "An object that acts as a gateway to a single record in a data source. There is one instance per row."
> — <https://martinfowler.com/eaaCatalog/rowDataGateway.html>

**How it works.** One gateway instance per row. Each instance has properties matching the columns and methods like `find()`, `insert()`, `update()`, `delete()`. **There is no domain logic on the gateway** — only persistence.

**When to use.** When you want object-shaped access to rows but no behaviour beyond persistence (e.g., Transaction Scripts that read and write rows). Faster to test than embedding SQL in domain objects.

**When NOT to use.** When domain logic belongs on the same object — that is Active Record, not Row Data Gateway.

**Forbidden alongside.** Adding domain methods to the gateway (silent slide into Active Record).

**Natural pairings.** Transaction Script.

**claude-ltm applicability.** ❌ Not used, and dated. Facts and chunks are plain `sqlite3.Row` data with no per-row gateway object.

---

## Active Record

🪦 **Dated / controversial** — see `references/dated-patterns.md`. Combines persistence with domain logic. Forbidden in claude-ltm. Modern equivalent: Data Mapper.

> "An object that wraps a row in a database table or view, encapsulates the database access, and adds domain logic on that data."
> — <https://martinfowler.com/eaaCatalog/activeRecord.html>

**How it works.** One object per row. The object has both the column data **and** the domain behaviour, and persists itself with `obj.save()` / `obj.destroy()`.

**When to use.** Domain logic is simple and tracks the database schema 1:1 (e.g., classic Rails CRUD apps). Fast to build, easy to read.

**When NOT to use.** Domain shape diverges from the table shape, or the model needs to be testable without a database. Active Record couples persistence and behaviour, which makes both harder to evolve.

**Forbidden alongside.** Data Mapper for the same entity. Choose one.

**Natural pairings.** Transaction Script for cross-cutting flows; Single Table Inheritance for hierarchies.

**claude-ltm applicability.** ❌ **Forbidden.** No `fact.save()` / `.delete()` on any row, dict, or dataclass. Persistence lives entirely in `core/store.py::Store`; facts and chunks are inert data. An `obj.save()` API would couple every fact to the schema and to quantisation — exactly what the Repository/Data-Mapper seam keeps out.

---

## Data Mapper

> "A layer of mappers that moves data between objects and a database while keeping them independent of each other and the mapper itself."
> — <https://martinfowler.com/eaaCatalog/dataMapper.html>

**How it works.** A separate mapper layer reads from and writes to the database. Domain objects know nothing about the database — they don't even know it exists. The mapper resolves identity, lazy loading, and graph persistence.

**When to use.** Whenever you want a Domain Model (or plain data) that is independent of the database schema. Required for any non-trivial project that must keep persistence out of the data.

**When NOT to use.** Trivial CRUD — Active Record is cheaper and the abstraction overhead doesn't pay back.

**Required pairings.** Identity Map (so the same row produces the same object), Unit of Work (so changes are batched and committed atomically), Metadata Mapping (so mapping rules don't explode into hand-written code).

**Forbidden alongside.** Active Record on the same entity. Domain logic on the mapper itself (the mapper is plumbing, not policy).

**claude-ltm applicability.** ✅ **Default — but hand-written on stdlib `sqlite3`, not an ORM.** `core/store.py::Store` *is* the mapper: models/facts and chunks are plain `sqlite3.Row` data, and `Store` owns all SQL, the schema, and the migration ladder (`_v1`…`_v8`). The Repository (see `05-or-metadata.md`) sits on top of this mapper. Concretely:
- Persist via `Store.add(...)` / `Store.replace_source_chunks(...)`; never `obj.save()`.
- Read via `Store.active_rows_for_project(...)`, `Store.fts_search(...)`, `Store.chunk_outline(...)`.
- Raw SQL (including FTS5 `MATCH` and the migration ladder) lives **only** inside `store.py`; callers never see a SQL string or a cursor.

The mapper is deliberately hand-rolled on `sqlite3` because the core is **stdlib-first** — that is a design choice (zero heavy deps in the core, inspectable by the read-only viewer, migratable, A/B-benchmarkable), not a regression from "a real ORM".

See `references/ltm-defaults.md` § Data Source; `core/store.py`.

---

## Choosing among the four

```
   simple logic, schema = domain shape?
       ├─ yes ──► Active Record
       └─ no  ──► Data Mapper

   no domain logic at all (just SQL access)?
       ├─ one instance per row ──► Row Data Gateway
       └─ one instance per table ──► Table Data Gateway
```

For claude-ltm: **Data Mapper**, hand-written on stdlib `sqlite3` in `core/store.py`, with a lightweight Repository on top. Always. Active Record is forbidden, and there is no ORM.
