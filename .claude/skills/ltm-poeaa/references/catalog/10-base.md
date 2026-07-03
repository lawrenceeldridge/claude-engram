# Base Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 18.

Eleven small but foundational patterns. They underpin the named patterns in other categories. Most are usable independently and compose freely with everything else.

claude-ltm leans on these **heavily** — they are the plumbing of its stdlib-first, swappable-backend design. The embedding seam and the distiller seam are both built out of Gateway + Separated Interface + Service Stub + Plugin, and empty recall is a Null Object. See `references/ltm-defaults.md` § Base for the locked-in verdicts.

---

## Gateway

> "An object that encapsulates access to an external system or resource."
> — <https://martinfowler.com/eaaCatalog/gateway.html>

**How it works.** A simple object interface in front of a complex external API (a database, a SaaS, a legacy system). Application code calls the Gateway; the Gateway translates to whatever the external system speaks.

**When to use.** Anywhere your code talks to an external system — to isolate that system's API style, ease testing, and prepare for substitution.

**Required pairings.** Often paired with **Separated Interface** + **Service Stub** for testability.

**claude-ltm applicability.** ✅ **Default.** Every door to a heavy/optional dep or a subprocess is a Gateway:

- **`EmbeddingGateway`** (`core/embedding.py`) — the door to the embedding model. The core never imports `fastembed`; the concrete `core/adapters/fastembed_gw.py` does, behind the Gateway.
- **The `Distiller` interface** (`core/distill.py`) — the door to distillation. `ClaudeCliDistiller` shells out to the `claude` CLI; `HTTPDistiller` POSTs to an OpenAI-compatible endpoint. The core never spawns a subprocess or opens a socket directly — it calls the `Distiller`.

The core stays free of heavy deps and I/O; the Gateway does the talking.

---

## Service Stub

> "Removes dependence upon problematic services during testing."
> — <https://martinfowler.com/eaaCatalog/serviceStub.html>

**How it works.** A local in-memory implementation of a Gateway interface that returns canned or computed responses. Used in tests instead of the real external service.

**When to use.** Whenever an external dependency is slow, flaky, costly, or hard to seed for tests.

**Required pairings.** Gateway + Separated Interface — the stub implements the interface; production code uses the Gateway via the interface.

**claude-ltm applicability.** ✅ **Default — and it doubles as the zero-dep default.** `HashEmbedding` (a lexical embedding stub, `core/embedding.py`) and `HeuristicDistiller` (line-extraction distillation, `core/distill.py`) are **both** the local-first defaults **and** the test fakes. They need no network and no model download, so the core is testable on the standard library with no mocks — the same object that ships as the fallback is the object the tests run against.

---

## Record Set

🪦 **Dated** — see `references/dated-patterns.md`. Tabular data binding superseded by component-based UIs and DTO-shaped APIs. Modern equivalent: DTO + plain rows.

> "An in-memory representation of tabular data."
> — <https://martinfowler.com/eaaCatalog/recordSet.html>

**How it works.** A generic, schema-aware collection of rows, often produced directly by SQL queries and consumed by data-aware UI controls. Allows business logic to live between the database and the UI without forcing object instantiation.

**When to use.** Frameworks built around tabular data binding (classic .NET DataGrid, JavaServer Faces tables).

**claude-ltm applicability.** ❌ **Not used.** claude-ltm uses plain rows (`sqlite3.Row` / dicts) and DTOs (`recall.render_block`, MCP tool responses), not a data-bound Record Set abstraction. There is no data-aware UI control to bind to.

---

## Mapper

> "An object that sets up a communication between two independent objects."
> — <https://martinfowler.com/eaaCatalog/mapper.html>

**How it works.** A general pattern: an intermediary that knows about both sides but the two sides do not know about each other. Data Mapper is a *specialisation* of this for object↔database; an Assembler that maps Domain → DTO is another specialisation.

**claude-ltm applicability.** ✅ Two clear uses:

- **Row ↔ dict shaping** — the functions in `core/store.py` and `core/recall.py` that turn a `sqlite3.Row` into the dict/DTO callers see (and back) are Mappers in this general sense. Neither side carries knowledge of the other.
- **`quantize` (`core/quantize.py`)** — maps a float vector ↔ its int8 / binary sign-bit representation. It is a Mapper between two representations of the same value; nothing on either side needs to know how the other is laid out.

---

## Layer Supertype

> "A type that acts as the supertype for all types in its layer."
> — <https://martinfowler.com/eaaCatalog/layerSupertype.html>

**How it works.** A common base class in a single architectural layer captures behaviour shared by all members of that layer. Reduces duplication; centralises cross-cutting concerns.

**When to use.** When several types in the same layer share non-trivial behaviour or a common contract.

**claude-ltm applicability.** ✅ The **`EmbeddingGateway` ABC** (`core/embedding.py`) and the **`Distiller` ABC** (`core/distill.py`) are the layer supertypes for their adapters — every concrete embedder / distiller derives from them and honours their contract. There is **no ORM base model** (the store holds plain rows, not mapped entities — see `references/ltm-defaults.md` § Data Source), so the only supertypes are the two Gateway ABCs.

---

## Separated Interface

> "Defines an interface in a separate package from its implementation."
> — <https://martinfowler.com/eaaCatalog/separatedInterface.html>

**How it works.** Put the interface (Protocol / ABC) where callers can depend on it; put the concrete implementation where they cannot. Inverts the usual "depend down" direction so high-level code does not depend on low-level details.

**When to use.** Whenever you want to break a forbidden dependency direction (e.g., core depending on infrastructure) or to make implementations swappable (Plugin).

**Required pairings.** Often pairs with **Gateway**, **Plugin**, **Service Stub**.

**claude-ltm applicability.** ✅ **Default.** `EmbeddingGateway(ABC)` (`core/embedding.py`) and `Distiller(ABC)` (`core/distill.py`) are the separated interfaces. `core/` imports the ABC; the concrete impls live behind it — `core/adapters/fastembed_gw.py`, and the distiller classes (`HeuristicDistiller`, `ClaudeCliDistiller`, `HTTPDistiller`). This is the seam that keeps heavy deps out of the core: the inward code sees only the interface.

---

## Registry

> "A well-known object that other objects can use to find common objects and services."
> — <https://martinfowler.com/eaaCatalog/registry.html>

**How it works.** A globally accessible lookup. Given a key, returns the canonical object/service. Often realised as a singleton or static factory.

**When to use.** When you cannot pass a reference through the call chain and need to look one up.

**Anti-pattern guard.** Module-level mutable singletons make tests brittle and hide dependencies. Prefer dependency injection (Registry-via-DI) over module-level Registry.

**claude-ltm applicability.** ⚠️ **Avoided.** Config and dependencies are passed *explicitly* into the Service Layer (`service.py` / `recall.py` entry functions take the `Store`, the embedder, and the distiller as parameters); there are no module-level mutable singletons. The `bin/*` Composition Root wires the graph — it does not register globally. `get_embedder(cfg)` / `get_distiller(cfg)` are **factory functions** that select an implementation from config, not a mutable registry object something later mutates.

---

## Value Object

> "A small simple object, like money or a date range, whose equality isn't based on identity."
> — <https://martinfowler.com/eaaCatalog/valueObject.html>

**How it works.** Small immutable objects compared by value, not by reference. Two equal-valued instances are equal even though they are different Python objects.

**When to use.** Domain quantities with no identity — dates, ranges, scores, decision buckets, a resolved config snapshot.

**Forbidden alongside.** Mutable VOs (defeats equality semantics). VO with an `id` field (now it's an entity).

**Terminology trap.** Different from DTO. The early-Java "Value Object" meaning *is* the modern DTO. Fowler's Value Object is **identity-by-value**, **immutable**, often small. See `references/catalog/07-distribution.md` § DTO.

**claude-ltm applicability.** ✅ **Default.** `DistilledFact` and `Observation` (`core/distill.py`), `Hit` (`core/recall.py`), and the frozen `Config` (`core/config.py`) are immutable value carriers compared by content, not by reference. The `Config` in particular is a frozen value object of resolved settings — passed inward, never mutated.

**Cosmic Python citation.** Percival & Gregory 2020, Chapter 1 — Domain Modeling, draws the "value vs entity" line: entities have identity; value objects don't, and are compared by value. claude-ltm follows this with `@dataclass(frozen=True)` (or an equivalently frozen carrier) for `DistilledFact`, `Observation`, `Hit`, and `Config` — immutable, value-equal, no `id`. A `DistilledFact` is *content* (a short string plus bookkeeping); a stored fact's *identity* is the content hash `Store.fact_id(project_key, text)`, which lives in the Data Source (see `references/ltm-defaults.md` § O-R Structural), not on the value object.

---

## Money

> "Represents a monetary value."
> — <https://martinfowler.com/eaaCatalog/money.html>

**How it works.** A specialised Value Object with currency-aware arithmetic, correct rounding to the smallest unit, and explicit allocation. Never use `float` for money.

**When to use.** Any monetary value.

**claude-ltm applicability.** ❌ **N/A.** claude-ltm has no monetary domain — it stores facts and embeddings, not amounts. If a cost/budget accounting feature ever needs money, introduce a `Money` VO then; today there is nothing to model.

---

## Special Case

> "A subclass that provides special behavior for particular cases."
> — <https://martinfowler.com/eaaCatalog/specialCase.html>

**How it works.** Instead of returning `None` and forcing every caller to null-check, return a subclass (or sentinel) that responds politely to the same interface — the **Null Object** variant does nothing, harmlessly.

**When to use.** When `None` would force repetitive null-checking and obscure intent.

**Anti-pattern guard.** Special Cases that lie (returning a fake value for a missing measurement) are worse than `None`. The Special Case must be honest about being special.

**claude-ltm applicability.** ✅ **The Null Object here is load-bearing.** `recall.render_block` returns `""` on empty recall (`core/recall.py`) — inject nothing, never a placeholder and never an error. Because the empty case renders to an empty string, an irrelevant turn costs **zero tokens** and the hook stays silent. This is the honest Null Object: "nothing to say" is represented by "" and consumed transparently by the hook that injects it.

---

## Plugin

> "Links classes during configuration rather than compilation."
> — <https://martinfowler.com/eaaCatalog/plugin.html>

**How it works.** Implementation choice is deferred to runtime configuration rather than compile-time wiring. Different environments plug in different implementations behind the same Separated Interface.

**When to use.** When you need to swap implementations by config or environment (e.g., a real model vs a deterministic stub).

**Required pairings.** **Separated Interface** (the contract being plugged into).

**claude-ltm applicability.** ✅ `get_embedder(cfg)` / `get_distiller(cfg)` select the implementation from the frozen `Config` at runtime; the `LTM_DAEMON` env var selects daemon-vs-in-process embedding. Every selection is **fail-open**: fastembed → hash, daemon → in-process, LLM distiller → heuristic. The Plugin choice is made once, in the `bin/*` Composition Root, behind the `EmbeddingGateway` / `Distiller` interfaces.

---

## How they fit together

These eleven patterns are the **plumbing** that makes claude-ltm's swappable, stdlib-first design work:

- **Gateway + Separated Interface + Service Stub + Plugin** = a testable, swappable, stdlib-first external dependency. This is exactly the shape of both the **embedding seam** (`EmbeddingGateway` ↔ `fastembed_gw` / `HashEmbedding`) and the **distiller seam** (`Distiller` ↔ `ClaudeCliDistiller` / `HTTPDistiller` / `HeuristicDistiller`): the ABC is the interface, the zero-dep stub is both default and fake, `get_*` is the Plugin selection, and the core never touches the heavy dep.
- **Special Case / Null Object** = zero-token empty recall (`render_block` → `""`).
- **Value Object** (`DistilledFact`, `Observation`, `Hit`, frozen `Config`) + **Mapper** (row↔dict, `quantize`) carry and reshape data without leaking persistence concerns.

**Fail-open is a base-layer contract.** Every Gateway / Plugin selection degrades safely — a missing optional dep never breaks capture or recall. Use these patterns liberally; they rarely conflict with each other. See `references/ltm-defaults.md` § Base.
