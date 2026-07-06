# Session State Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 17.

Three patterns describe **where session state lives** between requests. Mostly mutually exclusive *per session-scope datum*; many web systems mix them across different data (e.g., feature flags client-side, audit-trail server-side).

For claude-engram this whole category is essentially **N/A**. The plugin holds **no session state**: hooks are short-lived processes (a recall hook runs, injects, exits); there is no server, no cookie, no per-user session store. The only state that survives a turn is the durable memory/index store (Domain data) and the prompt-cache prefix (a caching optimisation). The patterns are documented here for completeness and to explain *why* each is a smell if it appears.

---

## Client Session State

> "Stores session state on the client."
> — <https://martinfowler.com/eaaCatalog/clientSessionState.html>

**How it works.** Each request carries the session state the server needs — via cookies, hidden form fields, URL parameters, or modern HTTP headers. The server stays stateless.

**When to use.** Stateless backends (REST, serverless), horizontal scaling without sticky sessions, low-volume per-request state.

**When NOT to use.** Large session payloads (each request balloons), or sensitive data that cannot be tampered with on the client without robust signing.

**Trade-offs.** Tampering risk → must sign or encrypt. Bandwidth cost. Statelessness benefit is large.

**claude-engram applicability.** ⚠️ **N/A — there is no client/browser session.** claude-engram is a plugin, not a web app; there is no request/response wire to carry state on, no cookie, no header auth. The read-only localhost viewer (`viewer/`) is stateless per request over the store, and holds nothing session-scoped.

---

## Server Session State

🪦 **Dated** — see `references/dated-patterns.md`. In-memory sessions need sticky-session deploys; rare in modern architectures. Modern equivalent: Client Session State + cache for derived data.

> "Keeps the session state on a server system in a serialized form."
> — <https://martinfowler.com/eaaCatalog/serverSessionState.html>

**How it works.** State sits in server memory or a fast in-memory store, keyed by a session ID the client carries.

**When to use.** Large session payloads, sensitive data, complex state that's expensive to reconstruct each request.

**When NOT to use.** Horizontal scaling without sticky sessions or shared store. Stateless deploy targets.

**Forbidden alongside.** Don't shadow-store the same datum in Client Session State — you'll get drift.

**claude-engram applicability.** ❌ **Dated, not used.** There is no long-lived server process to hold in-memory sessions. The one resident process — `bin/daemon.py` — keeps only a warm embedding model (a performance cache), not per-session state; if it dies, `core/daemon_client.py` falls open to in-process embedding and nothing is lost.

---

## Database Session State

🪦 **Dated** — see `references/dated-patterns.md`. Persisting "session" as DB rows is anti-stateless. Modern equivalent: Client Session State + cache.

> "Stores session data as committed data in the database."
> — <https://martinfowler.com/eaaCatalog/databaseSessionState.html>

**How it works.** Session data is persisted as ordinary database rows. Survives restarts, is durable, can be queried.

**When to use.** Audit-grade session continuity, multi-server systems where an in-memory store is unsuitable, or "session" that is really long-lived business state (cart, draft).

**When NOT to use.** Hot-path per-request reads — DB I/O is too slow.

**claude-engram applicability.** ❌ **Not used — and mind the nuance.** claude-engram *does* have a durable SQLite store (`core/store.py`), but it holds **Domain/index data** — distilled facts, quantised embeddings, doc/code chunks — **not** session state. Do not conflate persistent memory with session state: the store is the *point* of the plugin (cross-turn continuity), not a session cache keyed by a session ID. Likewise the `SessionStart` core injection joins the prompt-cache prefix — that is a **caching optimisation** (see `DESIGN.md` § Cache efficiency), not stored session state.

---

## How to choose

claude-engram uses **no session-state pattern**. There are only two things that cross a turn boundary, and neither is session state:

- the **durable memory/index store** — Domain data (facts + embeddings + chunks), the deliberate cross-turn continuity that *is* the plugin; and
- the **prompt-cache prefix** the `SessionStart` core injection joins — a caching optimisation, not a store.

So the "choice" is trivial: **nothing session-shaped belongs here.** If you find yourself reaching for a session store, a Redis, a cookie, or an in-memory cross-turn cache keyed by a session ID, that is a smell — the cross-turn need is already served by the memory store (Domain data) or by the prompt cache. See `references/engram-defaults.md` § Session State and `references/anti-patterns.md`.
