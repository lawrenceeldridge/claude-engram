# Object-Relational Metadata Mapping Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 13.

These three patterns are about **describing** the O/R mapping and **constructing queries**, not the underlying data access mechanism. They build on top of Data Mapper. In claude-ltm the relevant pair is a **Query Object** (`recall.search` / `recall.search_fused`) sitting on top of a **lightweight Repository** (the `Store` class over a hand-written Data Mapper) — there is no ORM and therefore no metadata-driven mapping.

---

## Metadata Mapping

> "Holds details of object-relational mapping in metadata."
> — <https://martinfowler.com/eaaCatalog/metadataMapping.html>

**How it works.** Mapping rules between objects and tables (column names, types, FKs, inheritance discriminators) are stored as data — declarative metadata — rather than hand-written code. Generic engine code reads the metadata and performs the mapping.

**When to use.** Whenever the volume of mappings is large enough that hand-rolled mapper code would be tedious and fragile. Almost any non-trivial Data Mapper layer.

**claude-ltm applicability.** ⚠️ **Minimal — deliberately.** claude-ltm has no ORM and therefore no mapping metadata. The schema is hand-declared SQL inside the `core/store.py` migration ladder; column names and types live in `CREATE TABLE` statements and the row↔dict shaping, not in class attributes an engine reads. This is explicit SQL, not metadata-driven mapping — acceptable at this scale, where there is exactly one store and one obvious set of tables. Do **not** introduce an ORM (SQLAlchemy or otherwise) to "get" Metadata Mapping; the absence of a mapping engine is the design, and the store stays inspectable, sweepable and migratable precisely because nothing generative sits between the rows and the SQL.

---

## Query Object

> "An object that represents a database query."
> — <https://martinfowler.com/eaaCatalog/queryObject.html>

**How it works.** A query is built up as a structure of objects (criteria, joins, ordering) referencing **classes and fields**, not tables and columns. The engine translates the object graph into SQL appropriate to the dialect.

**When to use.** When SQL would otherwise duplicate across many call sites, when queries must be composed dynamically, or when the team's SQL knowledge is uneven.

**When NOT to use.** When parameterised finder methods on a Repository cover the use case — Query Object adds complexity for benefits you don't need.

**Required pairings.** Data Mapper (provides the type info that lets the Query Object speak in classes/fields).

**claude-ltm applicability.** ✅ **Default.** `recall.search(...)` and `recall.search_fused(...)` take a *bundled* set of parameters — the frozen `Config` (ranking weights), the query vector, `min_sim`, `top_k`, and the cross-project penalty — rather than a sprawling positional signature. That bundle *is* the Query Object in the Fowler sense: a value carrier that describes the search, extended by adding a field rather than another positional argument. Reads fuse FTS5 (bm25) with vector cosine via reciprocal-rank fusion in `core/fusion.py`, then re-rank by priority in `core/scoring.py`. New search knobs go on the query/`Config` object; do not grow `search` into a ten-argument function, and do not concatenate raw SQL strings above the `Store` boundary — the only SQL (including FTS5 `MATCH`) lives inside `core/store.py`.

---

## Repository

> "Mediates between the domain and data mapping layers using a collection-like interface for accessing domain objects."
> — <https://martinfowler.com/eaaCatalog/repository.html>

**How it works.** A Repository looks to its callers like an in-memory collection of domain objects. Behind the facade it constructs Query Objects, calls into the Data Mapper, and returns domain objects. Clients submit specifications (criteria) and receive matching objects.

**When to use.** Complex Domain Models with many entities and intensive querying. When the same query logic is duplicated across many service-layer functions, factor into a Repository.

**When NOT to use.** Trivial CRUD where the query layer already gives a one-liner — adds a redundant layer.

**Required pairings.** Data Mapper (always), Query Object (often), Identity Map (always).

**Comparison vs Data Mapper.** Repository sits *above* Data Mapper. The Mapper translates objects↔rows; the Repository hides that translation behind a collection-like API and concentrates query logic. You can have a Data Mapper without a Repository; you cannot have a Repository without a Data Mapper.

**claude-ltm applicability.** ✅ **Default — the `Store` class is the lightweight Repository over the hand-written Data Mapper.** It exposes collection-like query methods with clear names (`active_rows_for_project`, `fts_search`, `chunk_fts_search`, `chunk_outline`) and hides all SQL, cursors and quantisation behind them. There is **one** store and one obvious query surface, so there is **no** separate `<Aggregate>Repository` class — do not add one. Empty-shell wrappers around a single `Store` call are the **Thin Repository** anti-pattern (`references/anti-patterns.md` § 12). If a genuinely new access pattern appears, add a named method to `Store`; never hand a cursor or a `select(...)` construct to a caller.

**Cosmic Python refinement / claude-ltm shape.** Cosmic Ch 2 defines Repository more opinionatedly than Fowler: it's **always paired with Unit of Work**, **always defined behind a `typing.Protocol` port** (so adapters are swappable), and **never exposes raw SQL or ORM types** to callers. claude-ltm matches the "never leak SQL or rows above the boundary" part — callers get plain `sqlite3.Row` data or DTOs, never a cursor or SQL string. It deliberately does *not* match the Protocol part: the store is **concrete** (one `sqlite3` backend), not swapped behind an interface, because there is exactly one storage backend by design. The seam that Cosmic gets from a swappable port, claude-ltm gets for free from having a single store with a small, named method surface. See `11-architectural-style.md` § Hexagonal Architecture for where claude-ltm *does* spend a Protocol (the embedding/distiller Gateways), and `references/ltm-defaults.md` § Data Source for the wider Repository-over-Data-Mapper commitment.

---

## How they fit together

```
Service Layer
    └─ Repository  (collection-like interface)
        └─ Query Object  (criteria as objects)
            └─ Data Mapper  (objects ↔ rows)
                └─ Database
```

In claude-ltm this stack is concrete and single-backend:

```
Service Layer (core/service.py capture, core/recall.py read)
    └─ Store  ← lightweight Repository (active_rows_for_project, fts_search, chunk_outline)
        └─ search / search_fused params + frozen Config  ← Query Object (fused via RRF in core/fusion.py)
            └─ hand-written Data Mapper on stdlib sqlite3 (row ↔ dict, quantize)
                └─ SQLite (${CLAUDE_PLUGIN_DATA}/memory.db)
```

This is the supported shape. Do not introduce an ORM, a mapping-metadata engine, or a second query surface, and do not leak SQL above the `Store` boundary. See `references/ltm-defaults.md` § O-R Metadata and § Data Source.
