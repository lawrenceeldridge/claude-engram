# Domain Logic Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 9.

These four patterns are **mutually exclusive choices** for organising business logic. Pick one per bounded context. claude-engram has **no rich domain to model** — a "fact" is data (a short string, a quantised vector, and bookkeeping) — so it chose **Functional Core / Imperative Shell + a function-style Service Layer**, deliberately **not** Domain Model.

---

## Transaction Script

🪦 **Dated for complex domains** — see `references/dated-patterns.md`. Acceptable for trivial admin tools / one-shot scripts. Modern equivalent for claude-engram: Functional Core + function-style Service Layer.

> "Organizes business logic by procedures where each procedure handles a single request from the presentation."
> — <https://martinfowler.com/eaaCatalog/transactionScript.html>

**How it works.** Each request from the presentation layer maps to a single procedure that runs the business logic top-to-bottom — typically calling the database directly or via a thin wrapper. Common subroutines are extracted to avoid duplication.

**When to use.** Simple business applications where each request is a self-contained transaction. CRUD-heavy admin tools and quick data-entry apps.

**When NOT to use.** Complex domains with overlapping rules — duplication explodes and there is nowhere good to factor common policy. Avoid when business logic will grow in volume and interconnectedness.

**Forbidden alongside.** Domain Model in the same context (produces an Anemic Domain Model — domain objects become bags of data while real logic sits in scripts). See `references/anti-patterns.md`.

**Natural pairings.** Row Data Gateway or Table Data Gateway for data access. Page Controller for the web tier.

**claude-engram applicability.** ❌ Not used; dated for anything beyond trivial scripts. claude-engram's shell functions (`bin/*`, the I/O in `service.py`) *orchestrate a pure core* — they call `distill`, `scoring`, `quantize` and persist via the `Store` Repository — rather than running procedural DB scripts inline. The `bin/*` hook scripts look transaction-script-shaped on the surface but delegate everything to service functions and the pure core.

---

## Domain Model

> "An object model of the domain that incorporates both behavior and data."
> — <https://martinfowler.com/eaaCatalog/domainModel.html>

**How it works.** A web of interconnected objects, each representing some meaningful individual in the domain — from large aggregates down to small line items. Behaviour and data live together on the same object.

**When to use.** Business logic is complex, with many rules and cases, and the rules are stable enough that an OO model pays back the up-front cost.

**When NOT to use.** Logic is thin and unlikely to grow — Transaction Script is cheaper.

**Required pairings.** Service Layer (entry points + transaction boundary), Data Mapper (keeps the model free of persistence), Unit of Work + Identity Map (consistency under concurrent access). A Domain Model without these is rarely viable.

**Forbidden alongside.** Table Module (different mental model — one object handles all rows) and Transaction Script in the same bounded context.

**claude-engram applicability.** ❌ **Deliberately NOT chosen.** This is the key contrast — Domain Model is *not* the default here. claude-engram has no rich domain to model: a fact is a short string + a quantised vector + bookkeeping (project key, frequency, recency, status). Behaviour lives in *pure functions over data* (`core/distill.py`, `core/scoring.py`, `core/quantize.py`), not on behaviour-carrying entities. The row/dataclass types are intentionally plain data — this is not an Anemic Domain Model (there is no rich domain being drained), it is a data-plus-functions design. Do not add persistence or behaviour methods to the row/fact types.

**No rich aggregates.** claude-engram has no aggregates in the DDD sense — facts and chunks are data, not consistency-boundary object graphs. Identity is a content hash at the Data Source layer (see `04-or-structural.md`), not an in-memory aggregate root.

---

## Table Module

🪦 **Dated** — see `references/dated-patterns.md`. Tied to .NET DataSet / Java RecordSet style. Modern equivalent: Functional Core + function-style Service Layer.

> "A single instance that handles the business logic for all rows in a database table or view."
> — <https://martinfowler.com/eaaCatalog/tableModule.html>

**How it works.** One class per database table (or view). Methods take a row identifier or a Record Set as input and return Record Sets. There is no per-row object; one Table Module instance handles every row in the table.

**When to use.** Frameworks built around RecordSets (classic .NET DataSet, ADO) where the UI binds directly to tabular data and you want business rules on top.

**When NOT to use.** Logic is naturally object-based or aggregate-rooted. The pattern is awkward when entities have rich relationships or when polymorphism matters.

**Forbidden alongside.** Domain Model. The two represent contradictory mental models — one object per row vs one object per table.

**Natural pairings.** Record Set, Table Data Gateway.

**claude-engram applicability.** ❌ Not used, and dated. claude-engram has no Record Set–oriented framework underneath; ranking/scoring logic is pure functions over rows, not methods on a table object.

---

## Service Layer

> "Defines an application's boundary with a layer of services that establishes a set of available operations and coordinates the application's response in each operation."
> — <https://martinfowler.com/eaaCatalog/serviceLayer.html>

**How it works.** A layer of service objects that define the application's API to the outside world. Each service operation orchestrates domain objects, manages the transaction, and coordinates the response. Multiple presentation channels (HTTP API, message handlers, batch jobs) call the same service operations.

**When to use.** Whenever there is more than one entry into the application or when transactions span multiple aggregates. Almost always paired with Domain Model.

**When NOT to use.** Trivial CRUD with one channel — adds layers without payoff.

**Required pairings.** Domain Model (the thing being orchestrated). Often paired with Remote Facade at the HTTP boundary.

**Forbidden alongside.** Nothing structurally — but the Service Layer must not contain business logic itself; that lives on the domain. If the service layer becomes thick, you have drifted into Transaction Script anti-pattern.

**claude-engram applicability.** ✅ **Default — function-style (Cosmic Ch 4).** `core/service.py` capture handlers (`add_records`, `capture_text`, `add_facts`) and `core/recall.py` entry functions (`search`, `search_fused`, `render_block`) *are* the Service Layer. Each takes its dependencies — the `Store` Repository, the `EmbeddingGateway`, the `Distiller` — as parameters, calls the pure core, and persists. There is no `class XService`. Multiple entry points call the same service functions: the `bin/*` hooks, the `engram` CLI, and the MCP server (`bin/mcp_server.py`). Paired with Functional Core / Imperative Shell rather than a rich Domain Model.

**Cosmic Python refinement / claude-engram shape.** Cosmic Ch 4 favours **function-based** services over class-based ones, and claude-engram's service functions are exactly that shape: `add_records(*, store, embedder, distiller, project_key, records, …)` takes the Repository and the Gateways as parameters, calls the pure core, and returns plain data. No `class XService`, no `__init__` for orchestration. This refinement is what lets each `bin/*` Composition Root stay small and testable — see `11-architectural-style.md` § Composition Root.

---

# Cosmic Python additions — Domain Logic composition patterns

The four Fowler patterns above are choices for *how to organise* business logic. The three patterns below are *artefacts that compose with* the chosen organisation, codified for Python in Percival & Gregory, *Architecture Patterns with Python* (2020). They are not in Fowler's 2003 catalog. claude-engram uses **Command** and **Handler** in their function-shaped Cosmic sense, but deliberately does **not** use Domain Event — there is no message bus or event system.

---

## Domain Event

> "An object that represents something that happened in the domain... in past tense, named in domain language."
> — Percival & Gregory 2020, Chapter 8 — Events and the Message Bus.

Original concept: Evans, *Domain-Driven Design* (2003); explicit catalog entry in Vernon, *Implementing Domain-Driven Design* (2013).

**How it works.** An object (often a frozen schema) that records that something happened — named in past tense. Aggregates emit events when their state changes; a message bus drains and dispatches them to handlers. Events carry just enough payload that handlers can do their work without reaching back into the aggregate.

**When to use.** When a state change has consequences outside the originating command's natural scope — cross-context reactions, analytics, downstream updates. When you find yourself adding `if X: also do Y` branches in commands, the event-and-handler pattern is what you want.

**When NOT to use.** When the reaction is a simple synchronous call that belongs in the same command. Don't define an event schema speculatively for a consumer that doesn't exist yet — the shape gets guessed wrong and rots before any handler reads it.

**Required pairings.** Aggregate (records the events), Message Bus (`07-distribution.md`) for dispatch, Handler (below) for the actual reaction logic. Unit of Work (`03-or-behavioral.md`) — events fire after commit.

**Forbidden alongside.** Events with rich behaviour (becomes a leaky aggregate); events should be immutable value schemas with no methods other than serialisation.

**claude-engram applicability.** ❌ **NOT used.** claude-engram has no event system and no message bus. Capture is detached fire-and-forget work (a `Stop`/`SessionEnd` hook spawns one worker that runs the pipeline and exits), not pub/sub. There are no aggregates emitting events and no consumers subscribing to them. Do not introduce events — see `references/engram-defaults.md` § Distribution ("no message bus and no event system").

---

## Command

> "Commands are sent by one actor to another specific actor with the expectation that a particular thing will happen as a result. ... we name commands with imperative mood verb phrases like 'allocate stock' or 'delay shipment'."
> — Percival & Gregory 2020, Chapter 10 — Commands and Command Handler.

**How it works.** A command captures **intent**, named in the imperative mood. Commands have a single recipient (a Handler), expect a result, and propagate failures back to the sender. Imperative-mood naming distinguishes them from past-tense events.

The command and its handler are the Cosmic-Python form of Fowler's Service Layer "operation" — the command is the *input*, the handler is the *behaviour*.

**When to use.** When a request to mutate state needs to flow through a uniform dispatcher (so middleware, transactions, retry policy apply uniformly). When the call site and the implementation are far apart.

**When NOT to use.** When a Service Layer function called directly with parameters is clearer (no command-object indirection needed). claude-engram uses the latter shape: the capture functions are called directly, not dispatched through a uniform bus. The function parameters serve as the command's fields.

**Required pairings.** Handler (below) — exactly one per command. Often Message Bus (`07-distribution.md`) for dispatch; Unit of Work (`03-or-behavioral.md`) for atomicity.

**Distinct from Event.** Commands and events are different message types per Cosmic Ch 10. Errors propagate from commands (the sender expects to know if it failed); errors are logged but don't propagate from events. Commands are dispatched to one handler; events are broadcast to many.

**Forbidden alongside.** Class-based command objects with `execute()` methods that mutate state inside themselves (drifts toward Active Record + Command-Handler-as-class). Cosmic-style commands are pure data; the handler is a separate function.

**claude-engram applicability.** ✅ **Implicit.** The `core/service.py` capture functions (`add_records`, `capture_text`, `add_facts`) are Cosmic commands in function-parameter shape — the signature *is* the command's fields. There is no separate command dataclass and no dispatcher; the functions are called directly by the `bin/*` Composition Roots. The function-parameter shape is sufficient because there is no uniform bus and no cross-context command dispatch to give a command a wire format.

---

## Handler

> "A handler is a function that takes a single command or event and does the work for it."
> — Percival & Gregory 2020, Chapters 4 (Service Layer), 8 (Events), and 10 (Commands).

**How it works.** A function (or callable) that receives a message, calls domain logic, persists results, and optionally records new events. Each handler is an entry into the application core; primary adapters translate external input into handler invocations.

Two flavours per Cosmic:

- **Command handlers** — exactly one per command type. Reraise exceptions on failure. Domain logic concentrated here.
- **Event handlers** — many per event type. Catch and log exceptions; subsequent handlers still run.

**When to use.** Whenever a Message Bus is in play, you have handlers. Even without a uniform bus, the *function shape* — taking inputs + dependencies, returning a result — is the same shape claude-engram's service functions have. Cosmic frames them all as handlers.

**When NOT to use.** When the work doesn't go through any indirection (no bus, no command pattern) — direct function calls in a Service Layer don't need to be called "handlers" just because the framing is fashionable.

**Required pairings.** Command (above) for command handlers; Domain Event (above) for event handlers. Composition Root (`11-architectural-style.md`) for the wiring.

**Forbidden alongside.** Handler that calls another handler directly (skips whatever cross-cutting wrapping the boundary provides).

**claude-engram applicability.** ✅ Every capture and recall entry function is a handler in the Cosmic sense — it takes inputs plus its dependencies (Store, Gateways), calls the pure core, and returns plain data or persists. Each `bin/*` hook script is a **primary adapter** that translates hook input (transcript JSON, prompt text) into a handler call. There is no uniform in-process bus — handlers are called directly by the Composition Roots.

---

## Choosing among the four

| Question | Answer → Use |
|----------|--------------|
| Is the logic simple and procedural? | Transaction Script |
| Is the framework built around RecordSets? | Table Module |
| Is there a rich domain with object behaviour to model? | Domain Model + Service Layer |
| Is the "domain" really just data with pure transforms over it? | **Functional Core + function-style Service Layer** |

For claude-engram, the answer is fixed: **Functional Core / Imperative Shell + a function-style Service Layer** — no Domain Model (there is no rich domain to model), no Table Module, no Transaction Script. Do not propose any of those without an explicit re-architecture conversation.
