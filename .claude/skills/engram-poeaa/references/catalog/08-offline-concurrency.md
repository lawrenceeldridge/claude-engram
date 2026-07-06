# Offline Concurrency Patterns

Source: <https://martinfowler.com/eaaCatalog/> — Fowler, *Patterns of Enterprise Application Architecture*, Chapter 16.

Four patterns. "Offline" because they cover **business transactions that span multiple system transactions** — i.e., multiple requests, sessions, or hand-offs. The first two (Optimistic vs Pessimistic) are mutually exclusive **for the same resource**. The other two (Coarse-Grained, Implicit) layer on top.

**For claude-engram these are all N/A.** There is **no contended interactive edit flow** — no user holding a mutable aggregate open across requests, nothing two writers race to change. So Optimistic, Pessimistic, Coarse-Grained and Implicit Offline Lock have nothing to guard. claude-engram's real concurrency concerns are different: (a) detached capture workers piling up, and (b) stale facts accumulating — handled by single-flight + idempotent capture + supersession + a TTL sweep, described in the substitute section below.

---

## Optimistic Offline Lock

> "Prevents conflicts between concurrent business transactions by detecting a conflict and rolling back the transaction."
> — <https://martinfowler.com/eaaCatalog/optimisticOfflineLock.html>

**How it works.** Each aggregate carries a version (counter, timestamp, or hash). At commit time, the writer checks the version is unchanged since read; if it has changed, the write is rejected and the user is asked to reconcile.

**When to use.** When conflicts are expected to be rare and detection-after-the-fact is tolerable.

**When NOT to use.** When conflicts are common and forcing users to redo work would be painful — Pessimistic is kinder there.

**Required pairings.** Identity Field (so versions can be compared). Often pairs with Coarse-Grained Lock to lock a whole aggregate, not just one row.

**Forbidden alongside.** Pessimistic Offline Lock for the same resource.

**claude-engram applicability.** ❌ **N/A** — there is no contended mutable aggregate to version. A fact is content-addressed (`fact_id = hash(project_key, text)`) and immutable; re-capture is idempotent, not a competing edit, so there is no lost-update race to detect.

---

## Pessimistic Offline Lock

🪦 **Dated for stateless, short-lived processes** — see `references/dated-patterns.md`. Modern equivalent: Optimistic Offline Lock + version field; a claim/single-flight primitive for batch workers.

> "Prevents conflicts between concurrent business transactions by allowing only one business transaction at a time to access data."
> — <https://martinfowler.com/eaaCatalog/pessimisticOfflineLock.html>

**How it works.** Acquire a lock before modifying the data. Lock is held across multiple system transactions. Other transactions see the lock and queue, retry, or are rejected.

**When to use.** Long-running edits where late-detected conflicts would waste real human work, *and* you have infrastructure to track stateful locks across requests.

**When NOT to use.** Stateless or short-lived processes where there is no obvious place to anchor lock ownership across requests.

**Forbidden alongside.** Optimistic Offline Lock for the same resource.

**claude-engram applicability.** ❌ **N/A (and dated).** Hooks are short-lived processes with nowhere to anchor a cross-request lock, and there is no interactive edit to protect in the first place. See the single-flight substitute below for how concurrent capture is actually kept in order.

---

## Coarse-Grained Lock

> "Locks a set of related objects with a single lock."
> — <https://martinfowler.com/eaaCatalog/coarseGrainedLock.html>

**How it works.** Instead of locking each child object individually, attach a single lock to the aggregate root. Locking the parent locks the whole graph.

**When to use.** Whenever objects are edited together as a group — typical for any DDD aggregate.

**Required pairings.** Either Optimistic or Pessimistic Offline Lock — Coarse-Grained Lock specifies *what* is locked, not *how*.

**claude-engram applicability.** ❌ **N/A** — there is no aggregate graph edited as a unit and no offline lock to make coarse. Facts are flat, independent rows.

---

## Implicit Lock

🪦 **Dated as a manual implementation** — see `references/dated-patterns.md`. The conceptual pattern is alive where framework-level locking exists; the hand-rolled form is dated.

> "Allows framework or layer supertype code to acquire offline locks."
> — <https://martinfowler.com/eaaCatalog/implicitLock.html>

**How it works.** A framework or Layer Supertype acquires the lock automatically on every relevant operation. Application code does not have to remember to call `lock()` — forgetting the lock is the most common source of corruption.

**When to use.** Always, when you have offline locking. Manual `lock()` calls scattered through commands inevitably get forgotten.

**Required pairings.** Always paired with Optimistic OR Pessimistic Offline Lock — the thing being made implicit.

**claude-engram applicability.** ❌ **N/A** — there is no offline lock to make implicit. The safety that this pattern automates (never forgetting to guard a write) is provided instead by idempotent-per-fact capture, which needs no lock at all.

---

## claude-engram substitute — single-flight, idempotent capture, supersession

claude-engram's concurrency problem is not contended edits; it is keeping detached capture in order and keeping the store from filling with stale facts. Four mechanisms cover it, none of them a lock:

**Single-flight capture.** The capture hook spawns exactly **one** detached worker/daemon and returns; a second capture for the same session does **not** stack another worker on top. This replaced an earlier worker/daemon pileup (commit `b30889a`). It is the concurrency primitive that actually matters here — bound the fan-out at the point work is spawned, rather than lock the resource work touches.

**Idempotent-per-fact capture.** `Store.fact_id(project_key, text)` is a content hash, and `Store.exists` / `Store.reinforce` make re-processing the same transcript a **no-op-or-reinforce**, never a duplicate. Re-running capture is always safe — a fact seen again boosts frequency and refreshes recency instead of inserting a second row. This is the safety an Implicit Lock buys (you can't corrupt state by forgetting a guard), achieved by content-addressing rather than locking.

**Supersession.** `Store.supersede` is the **conflict-resolution** mechanism: a near-identical newer fact archives older ones (`status='superseded'`, filtered at SQL, reversible — not a delete). It resolves the "which fact wins" question that an Optimistic Lock's version check would handle in an edit flow — but after the fact and by similarity, not by rejecting a concurrent write.

**TTL sweep (hard expiry).** `Store.sweep(...)` retires facts unseen past `ttl_days` unless reinforced past `ttl_keep_frequency` (consolidation protects durable facts). Reversible (a `status` flag, not a delete), and it runs off the interactive path or via `engram sweep`.

---

## How they fit together

claude-engram has **no offline locks**, because there is no contended interactive edit. Its concurrency stack is a pipeline of safety mechanisms instead:

```
single-flight capture   (one detached worker per session — commit b30889a)
    └─ idempotent-per-fact capture   (fact_id content hash + Store.exists / Store.reinforce)
        └─ supersession   (Store.supersede — newer near-identical fact archives older ones)
            └─ TTL sweep   (Store.sweep — retire facts unseen past ttl_days, reversible)
```

Do not add version columns, `SELECT … FOR UPDATE`, or an offline-lock abstraction — there is no contended mutable aggregate to guard, and adding one would be machinery without a workload. See `references/engram-defaults.md` § Offline Concurrency and `DESIGN.md` § Hard expiry, § Memory lifecycle.
