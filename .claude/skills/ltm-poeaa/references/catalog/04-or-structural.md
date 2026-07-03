# Object-Relational Structural Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 12.

These ten patterns describe **how objects map onto tables**. They are largely orthogonal to each other — most projects use several. The three inheritance patterns at the bottom **are** mutually exclusive **for a single hierarchy**. claude-ltm uses **Identity Field (content hash)**, **Serialized LOB (embedding blobs)**, and **Embedded Value (the vector fingerprint)**; it uses **no inheritance mapping** — there is no hierarchy.

---

## Identity Field

> "Saves a database ID field in an object to maintain identity between an in-memory object and a database row."
> — <https://martinfowler.com/eaaCatalog/identityField.html>

**How it works.** Store the database primary key as a field on the in-memory object. Used by the Identity Map to recognise the same row across loads.

**When to use.** Always — anywhere persistent identity matters.

**claude-ltm applicability.** ✅ **Default — a content hash, not a surrogate auto-increment.** `fact_id = hash(project_key, text)` and `chunk_id(project_key, source_path, anchor)`. Identity is *derived from content*, which is exactly what makes capture idempotent — re-capturing the same fact resolves to the same id and becomes a no-op-or-reinforce rather than a duplicate (see `03-or-behavioral.md` § Unit of Work). Every row is also tagged by **project key** (the marker-walk identity — see `DESIGN.md` § Project identity), so one store cleanly separates many projects. No auto-increment sequence is needed; the content hash gives free deduplication.

---

## Foreign Key Mapping

> "Maps an association between objects to a foreign key reference between tables."
> — <https://martinfowler.com/eaaCatalog/foreignKeyMapping.html>

**How it works.** A direct object reference becomes a foreign-key column. The mapper translates between the two on read and write.

**When to use.** One-to-many and many-to-one associations.

**claude-ltm applicability.** ⚠️ **Minimal / mostly N/A.** There is no rich relational object graph. The only references are lightweight: a chunk row references its `source_path`, and fact supersession links reference other `fact_id`s. These are plain scalar columns read as data, not mapper-resolved object associations. No cascade, no relationship declarations.

---

## Association Table Mapping

> "Saves an association as a table with foreign keys to the tables that are linked by the association."
> — <https://martinfowler.com/eaaCatalog/associationTableMapping.html>

**How it works.** Many-to-many associations (which can't sit in a single foreign-key column) are persisted via a third table whose rows are the links.

**When to use.** Many-to-many associations, especially when the association itself carries data (timestamps, role, metadata).

**Forbidden alongside.** Foreign Key Mapping for the *same* association — pick one.

**claude-ltm applicability.** ❌ **N/A.** There is no many-to-many relationship in the memory or index schema.

---

## Dependent Mapping

🪦 **Dated** — see `references/dated-patterns.md`. Most "child" entities today have their own identity and lifecycle. Modern equivalent: a separate row with its own Identity Field.

> "Has one class perform the database mapping for a child class."
> — <https://martinfowler.com/eaaCatalog/dependentMapping.html>

**How it works.** A child class has no independent identity; its mapping is delegated to the parent's mapper. The child has no Identity Field of its own at the domain level.

**When to use.** When children only ever exist within one parent and are never referenced from elsewhere.

**When NOT to use.** When other tables reference the child or the child must be queryable independently.

**claude-ltm applicability.** ❌ **N/A**, and dated. There are no parent/child object mappers — chunks and facts each carry their own content-hash identity and are queried directly.

---

## Embedded Value

> "Maps an object into several fields of another object's table."
> — <https://martinfowler.com/eaaCatalog/embeddedValue.html>

**How it works.** A small value object (e.g., Money, DateRange) does not get its own table — its fields are inlined into the parent's table.

**When to use.** Value Objects that have no independent meaning ("no sane person would want a table of money values"). Currency-amount pairs, date ranges, addresses.

**Forbidden alongside.** A separate table for the same VO (defeats the point and creates a 1:1 join).

**Natural pairings.** Value Object, Money.

**claude-ltm applicability.** ✅ **The quantised embedding is inlined on the fact/chunk row**, not stored in a separate table. The vector is the fact's semantic value — meaningless on its own, never queried independently, and always read and written with the row it belongs to. That is a textbook Embedded Value: a compact fingerprint of the fact, co-located with it rather than normalised out into a 1:1 join. (The bytes themselves are a Serialized LOB — see below.)

---

## Serialized LOB

🪦 **Dated in its XML form** — see `references/dated-patterns.md`. The original pattern was XML-blob persistence. Modern equivalent: opaque binary/JSON blobs (with discipline — do not filter inside).

> "Saves a graph of objects by serializing them into a single large object (LOB), which it stores in a database field."
> — <https://martinfowler.com/eaaCatalog/serializedLOB.html>

**How it works.** Serialize a value (often as JSON, sometimes XML, or raw bytes) into one column. The LOB column is opaque to SQL queries.

**When to use.** Values that are read and written together and never queried in pieces — payload bags, audit snapshots, raw model outputs.

**When NOT to use.** When you need to filter, index, or join on individual fields inside the graph.

**Anti-pattern guard.** Storing a queryable entity as a LOB is a frequent mistake — when you find yourself parsing a blob column in WHERE clauses, the entity wanted to be normalised.

**claude-ltm applicability.** ✅ **Modern (non-XML) form.** The quantised vectors are stored as opaque blob columns: the **int8 vector** (the primary search representation) and the **binary sign-bit vector** (32× smaller, used as a Hamming pre-filter). Both are read and written whole and are **never filtered inside SQL** — ranking happens in the pure Functional Core (`core/scoring.py`, `core/fusion.py`, `core/quantize.py`) after the rows are loaded, not in a `WHERE`. This is Serialized LOB done right: opaque bytes, decoded by the mapper, scored in Python.

---

## Inheritance Mappers

🪦 **Dated as a manual implementation** — see `references/dated-patterns.md`. Inheritance-mapping plumbing is now generically provided by ORMs; hand-rolling it is rarely warranted.

> "A structure to organize database mappers that handle inheritance hierarchies."
> — <https://martinfowler.com/eaaCatalog/inheritanceMappers.html>

**How it works.** A meta-pattern: organises the mapper code so the same Single/Class/Concrete Table strategy can be applied uniformly across a hierarchy without duplication.

**claude-ltm applicability.** ❌ Not used. There is no class hierarchy to map — facts and chunks are flat plain data.

---

## Single Table Inheritance

> "Represents an inheritance hierarchy of classes as a single table that has columns for all the fields of the various classes."
> — <https://martinfowler.com/eaaCatalog/singleTableInheritance.html>

**How it works.** One table holds rows for every class in the hierarchy. A discriminator column says which class. Subclass-specific columns are nullable for non-matching subclasses.

**When to use.** Shallow hierarchies with mostly-shared columns and small subclass-specific deltas. Best query performance — no joins.

**When NOT to use.** Deep hierarchies, lots of subclass-specific fields → wide table with many nulls. Wasted storage and confusing schema.

**Forbidden alongside.** Class Table Inheritance and Concrete Table Inheritance for the **same hierarchy**.

**claude-ltm applicability.** ❌ Not used. No inheritance hierarchy exists in the schema.

---

## Class Table Inheritance

🪦 **Dated** — see `references/dated-patterns.md`. Composition is the modern default; expensive joins on every load. Modern equivalent: composition, or Single Table Inheritance for shallow hierarchies.

> "Represents an inheritance hierarchy of classes with one table for each class."
> — <https://martinfowler.com/eaaCatalog/classTableInheritance.html>

**How it works.** One table per class. Subclass tables hold subclass-specific columns and a foreign key to the superclass row.

**When to use.** Deep hierarchies with many subclass-specific fields, where storage normalisation matters.

**When NOT to use.** Performance-sensitive read paths — every load joins across the hierarchy.

**Forbidden alongside.** Single / Concrete Table Inheritance for the same hierarchy.

**claude-ltm applicability.** ❌ Not used. Avoid.

---

## Concrete Table Inheritance

🪦 **Dated** — see `references/dated-patterns.md`. Polymorphic queries need UNION across all concrete tables; rarely the right choice today. Modern equivalent: Single Table Inheritance, or composition.

> "Represents an inheritance hierarchy of classes with one table per concrete class in the hierarchy."
> — <https://martinfowler.com/eaaCatalog/concreteTableInheritance.html>

**How it works.** Only concrete (leaf) classes have tables. Each table contains all the columns inherited up the hierarchy, duplicating shared columns.

**When to use.** When subclasses are rarely queried polymorphically but each is queried often as itself. No joins.

**When NOT to use.** When polymorphic queries are common (now you must UNION across all concrete tables) or when shared columns evolve frequently (schema changes hit every table).

**Forbidden alongside.** Single / Class Table Inheritance for the same hierarchy.

**claude-ltm applicability.** ❌ Not used.

---

## claude-ltm guidance for the three inheritance patterns

claude-ltm has **no inheritance hierarchies** to map — facts and chunks are flat, plain `sqlite3.Row` data, distinguished by columns (status, kind, project key) rather than by class type. There is therefore no hierarchy to choose a strategy for, and no exception to carry.

If a future change ever genuinely needed a mapped hierarchy, that would be a re-architecture decision (see `references/ltm-defaults.md` § Update procedure) — and **Single Table Inheritance** would be the shallow-hierarchy default. Never mix two inheritance strategies on one hierarchy. But today the correct answer is: none of the three; keep the data flat.
