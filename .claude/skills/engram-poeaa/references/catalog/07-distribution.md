# Distribution Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 15.

Remote Facade and DTO are **inseparable** in practice — Remote Facade exposes the API, DTO carries the data across. claude-engram crosses **three** boundaries, and a **DTO** carries data across each. There is **no message bus and no event system** — capture is detached fire-and-forget work spawned by a hook, not pub/sub.

---

## Remote Facade

> "Provides a coarse-grained facade on fine-grained objects to improve efficiency over a network."
> — <https://martinfowler.com/eaaCatalog/remoteFacade.html>

**How it works.** Behind the network boundary lives a fine-grained Domain Model; in front sits a coarse-grained facade designed for chunky, infrequent calls. The facade gathers the data needed for one user interaction in a single round trip.

**When to use.** Whenever objects must communicate across processes, machines, or process-to-process within the same machine. Network round trips are expensive — coarse-grained calls are essential.

**When NOT to use.** Within one address space — fine-grained calls are fine and clearer. Adding a Remote Facade in-process is friction without payoff.

**Required pairings.** **Data Transfer Object** for moving data across the boundary. Often paired with Service Layer (the facade is *thinly* on top of the service layer, exposing the subset that crosses the wire).

**Anti-pattern guard.** A "remote-shaped" API that forces N round trips per page (N+1 distribution problem). Any time the client code does `for x in xs: client.fetch(x.id)`, the facade is too fine-grained.

**claude-engram applicability.** ✅ **Default.** `core/daemon_client.py` is a coarse Remote Facade over the embedding model — hand it a batch of text, get vectors back over a local socket, in one round trip rather than one call per string. It is **fail-open**: any failure (no daemon, socket error, timeout) silently degrades to loading the model in-process, so a daemon outage never breaks a turn. `bin/mcp_server.py` is the second coarse facade — it exposes a deliberately chunky tool surface (a recall *verdict*, ranked *outlines*, one symbol body via `get_symbol`) rather than fine-grained row access, so the model gathers what it needs for a turn in a few calls, not dozens. Neither facade lets fine-grained store internals leak across the boundary.

---

## Data Transfer Object

> "An object that carries data between processes in order to reduce the number of method calls."
> — <https://martinfowler.com/eaaCatalog/dataTransferObject.html>

**How it works.** A serialisable bag of data, shaped for the call rather than the domain. An Assembler maps between domain objects and DTOs on each side of the boundary.

**When to use.** Always when crossing a remote boundary. The DTO is the wire contract; the domain object is the in-process model.

**When NOT to use.** Within a process — pass the domain object directly.

**Terminology trap.** Originally called "Value Object" in early Java/J2EE; this conflicts with Fowler's **Value Object** (which is about identity-by-value). The community settled on "Transfer Object" / "DTO". They are different patterns. See `references/catalog/10-base.md` § Value Object.

**Required pairings.** Remote Facade.

**Forbidden alongside.** Domain models leaking across the boundary (common in poorly-layered codebases — the moment a domain object goes out as a serialised payload, you have a hidden DTO that drifts when you refactor the model).

**claude-engram applicability.** ✅ **Default.** claude-engram has three DTOs, one per boundary:

| DTO | Where | Shaped for |
|---|---|---|
| **`recall.render_block`** | `core/recall.py` | the injected `additionalContext` payload — deliberately **one line per fact**, capped at `max_chars` (shaped for the token budget, not the store) |
| **MCP tool responses** | `bin/mcp_server.py` | outlines + anchors + freshness verdicts, not raw rows — the model fetches one body on demand via `get_symbol` |
| **daemon socket payload** | `core/daemon_client.py` | vectors over the local socket |

Never return a raw `sqlite3.Row` across any boundary — map it to the DTO shaped for that call. `render_block` is the sharpest example: it is shaped for the *token budget*, not the store, so it emits one atomic line per fact and caps total characters rather than dumping rows or a transcript.

**Cosmic Python citation — Tolerant Reader / Postel's Law for inbound DTOs.** Percival & Gregory 2020, Appendix E — Validation, applies Postel's Law to DTO design: "Be conservative in what you do, be liberal in what you accept from others." Inbound DTOs (here, the argument bags of the MCP tools) should accept the fields they need and tolerate unknown ones rather than reject them, so a caller can evolve its payload ahead of the recipient. claude-engram's tool handlers read the arguments they know and ignore the rest; outbound DTOs (`render_block`, the tool responses) are the plugin's contract to honour and stay tight and capped.

---

## Message Bus

> "A message bus is just an internal pub/sub system... it routes events from publishers to handlers."
> — Percival & Gregory 2020, Chapter 8 — Events and the Message Bus + Chapter 9 — Going to Town on the Message Bus.

**How it works.** A central object that receives messages and dispatches each to its registered handlers. Two message classes flow through the bus:

- **Events** (past-tense facts): broadcast to all interested handlers; handlers are independent; failures are logged and skipped, not propagated.
- **Commands** (imperative instructions): dispatched to exactly one handler; handlers report success or failure back; failures are propagated.

The bus itself is a thin coordinator — in Cosmic's classic in-process form, a dict mapping message types to handler lists.

**When to use.** When the codebase has cross-aggregate reactions that don't belong in any single command's call site. Cosmic Ch 8 is explicit: introduce the bus when you find yourself adding `if X: also do Y` branches across multiple commands.

**When NOT to use.** When all reactions can live as direct calls inside a single Service Layer command without obscuring intent. Premature introduction of a Message Bus adds indirection without payoff.

**claude-engram applicability.** ❌ **NOT used.** claude-engram has **no** in-process or distributed message bus and **no** event system. Capture is not an event — it is detached, fire-and-forget work: a hook spawns one worker at `Stop` / `SessionEnd` / `PreCompact`, that worker distils, embeds and persists, then exits. There are no publishers, no subjects, no handlers, and nothing subscribes to anything. There are no cross-aggregate reactions to untangle (there is one store and one bounded context), so the trigger for a bus never arises. **Do not introduce a bus or events** to "complete the set" — the absence is the design. Concurrency and re-processing safety are handled by single-flight + idempotent capture + supersession, not by a bus (see `08-offline-concurrency.md`).

---

## How they fit together for claude-engram

Three boundaries, each with its own DTO; no events anywhere:

| # | Boundary | Mechanism (Remote Facade) | Wire shape (DTO) |
|---|----------|---------------------------|------------------|
| 1 | plugin → Claude's context window | hook `additionalContext` | `recall.render_block` — one line per fact, capped at `max_chars` |
| 2 | model → plugin (on demand) | `bin/mcp_server.py` tool dispatch | tool responses: recall verdict, ranked outlines, one symbol/section body |
| 3 | hook → resident embedder | `core/daemon_client.py` → `bin/daemon.py` | vectors over a local socket; **fail-open** to in-process |

For each boundary: define the DTO shaped for the call (not the store), map explicitly (never emit a raw `sqlite3.Row`), and keep the payload **coarse and capped** — a recall injection ships the top-k facts in one block, not a follow-up per fact. No events, no bus, no outbox. See `references/engram-defaults.md` § Distribution.
