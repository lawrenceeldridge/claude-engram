# Dated Patterns — Skip unless the user explicitly overrides

Several POEAA patterns are tied to technological context that has aged out: XML/SOAP-era
distribution, .NET DataSet / RecordSet style data access, server-rendered HTML templating
with XSLT, stateful sessions on sticky-session deploys, and hand-rolled pre-ORM data
access. The skill **will not recommend these patterns by default** in `recommend` or
`apply` modes. The user can override by naming the pattern explicitly with a justifying
context.

The catalog entries for these patterns remain complete — they are still valuable for
understanding modern equivalents, recognising them in legacy code during `audit`, and for
honest side-by-side `compare`.

## Sources

- **Catalog era.** *Patterns of Enterprise Application Architecture* was published in 2002.
  Several patterns ship illustrative examples in EJB / .NET 1.x / classic ASP / J2EE —
  environments that no longer dominate modern stacks.
- **Ben Nadel** ([review](https://www.bennadel.com/blog/3353-patterns-of-enterprise-application-architecture-by-martin-fowler.htm))
  — flags that the book treats XML/SOAP as the wire format with no mention of JSON. Affirms
  most patterns remain valid; defends Transaction Script as still useful for simple cases.
- **Dishan M.** ([Medium article](https://dishanm.medium.com/enterprise-application-architecture-patterns-the-immutable-laws-of-change-48716758da84))
  — estimates ~75% of the catalog is still relevant and ~25% obsolete. Calls out Transaction
  Script (for complex domains), Serialized LOB, Page Controller, and (controversially) Active
  Record.
- **Modern reality for claude-engram.** claude-engram deliberately runs on the **standard library**
  with no ORM and no web framework, so the ORM-provided conveniences (Unit of Work, Identity
  Map, Metadata Mapping, Lazy Load, Implicit Lock) are neither used nor hand-rolled — they are
  simply **out of scope** (see `catalog/03-or-behavioral.md`). That is different from "dated":
  the patterns below are dated *and* the ORM conveniences are *not applicable*; keep the two
  reasons distinct in an audit.

---

## Dated patterns

The skill must not recommend or apply these without an explicit user override.

| Pattern | Category | Why dated | Modern equivalent (claude-engram) |
|---------|----------|-----------|--------------------------------|
| **Table Module** | Domain Logic | Tied to .NET DataSet / Java RecordSet; not how Python backends are built | Functional Core + function-style Service Layer |
| **Table Data Gateway** | Data Source | Pre-ORM hand-rolled SQL gateway | Repository over Data Mapper (`core/store.py`) |
| **Row Data Gateway** | Data Source | Same era as Table Data Gateway | Repository over Data Mapper |
| **Active Record** | Data Source | Combines persistence with domain logic; violates SRP | Repository over Data Mapper — the claude-engram default; **forbidden** |
| **Record Set** | Base | Tabular data binding superseded by DTO-shaped APIs | DTO (`render_block`, MCP responses) + plain rows |
| **Page Controller** | Web Presentation | One handler per page; superseded by front-dispatch | Front-Controller-shaped dispatch (viewer / MCP server) |
| **Transform View** | Web Presentation | XSLT-era programmatic HTML emission | N/A — claude-engram renders minimal HTML in the viewer only |
| **Two Step View** | Web Presentation | Rarely worth the ceremony; N/A for a plugin | N/A (single-format, minimal HTML) |
| **Server Session State** | Session State | In-memory sessions need sticky deploys | N/A — no session state; the durable store holds Domain data, not sessions |
| **Database Session State** | Session State | Persisting "session" as rows is anti-stateless | N/A — the SQLite store is Domain/index data, not session state |
| **Pessimistic Offline Lock** | Offline Concurrency | Needs stateful lock ownership across requests | N/A — no contended interactive edit; single-flight + idempotent capture instead |
| **Implicit Lock** | Offline Concurrency | Was a hand-rolled framework hook | N/A — no offline locking in claude-engram |
| **Concrete Table Inheritance** | O-R Structural | Polymorphic queries need UNION | N/A — no inheritance hierarchy |
| **Class Table Inheritance** | O-R Structural | Expensive joins on every load | N/A — no inheritance hierarchy |
| **Inheritance Mappers** | O-R Structural | Inheritance-mapping plumbing is ORM-provided | N/A — no ORM, no hierarchy |
| **Dependent Mapping** | O-R Structural | Most children have their own identity/lifecycle | N/A — facts/chunks have content-hash identity |
| **Serialized LOB** (XML form) | O-R Structural | XML-era opaque-blob persistence | Modern LOB: int8/binary embedding **blobs** stored whole (never filtered inside) — claude-engram **does** use this; only the *XML* form is dated |
| **Transaction Script** | Domain Logic | Dated for **complex** domains; fine for trivial one-shot scripts | Functional Core + function-style Service Layer for any non-trivial logic |

---

## Patterns that are NOT dated (claude-engram uses them freely)

- **Data access:** Repository, Data Mapper (hand-written on sqlite3), Query Object
- **Structural mapping:** Identity Field (content hash), Embedded Value, Serialized LOB (modern blob form)
- **Distribution:** Remote Facade (daemon client), Data Transfer Object (`render_block`, MCP responses)
- **Base:** Gateway, Service Stub, Mapper (general), Layer Supertype, Separated Interface, Plugin, Value Object, Special Case / Null Object
- **Architectural shape:** Hexagonal, CQRS, Functional Core / Imperative Shell, Composition Root

These map directly onto claude-engram's locked-in defaults in `engram-defaults.md`.

### Not dated, but "not applicable" (a different verdict)

Distinct from dated patterns, these are conceptually alive but **out of scope** because
claude-engram has no ORM and no rich domain — do **not** flag them as "dated", and do **not**
introduce machinery to "complete" them:

- **Domain Model** — claude-engram has no rich domain; a fact is data. (Deliberate, not a gap.)
- **Unit of Work / Identity Map / Lazy Load** — ORM-session behaviours; claude-engram uses one
  short-lived sqlite3 connection with explicit transactions and content-hash identity.
- **Metadata Mapping** — no ORM metadata; the schema is hand-declared SQL in migrations.
- **Optimistic / Coarse-Grained Lock** — no contended interactive aggregate.
- **Domain Event / Message Bus** — capture is detached fire-and-forget, not pub/sub.
- **Money** — no monetary domain.

See `catalog/03-or-behavioral.md`, `08-offline-concurrency.md`, and `engram-defaults.md` for the
"N/A by design" rationale.

---

## How each mode treats dated patterns

### `recommend`

Do not propose a dated pattern. If the user's problem could technically be solved by one but
a modern equivalent exists, recommend the equivalent and add a one-line note: *"<dated
pattern> would also fit historically but is dated — see references/dated-patterns.md."*

### `audit`

Presence of a dated pattern in current code is a finding. Report it as **"⚠️ Dated pattern in
use"** and propose the smallest viable migration. Severity:
- **Forbidden in claude-engram** (e.g. Active Record) → high; propose migration in remediation.
- **Dated but not actively forbidden** (e.g. Record Set) → medium; migrate only if the
  surrounding code is being changed anyway.

Keep dated ("aged-out tech") separate from not-applicable ("no ORM / no rich domain here") —
a hand-rolled Unit of Work abstraction is a "not-applicable, don't add it" finding, not a
"dated" one.

### `compare`

Dated patterns may appear in side-by-side comparisons — that's the point of comparing
alternatives. Mark the dated entry's column header with **🪦 Dated** so the asymmetry is
visible:

```
| Dimension              | Repository | Active Record 🪦 Dated |
```

Always conclude with the modern choice. The dated pattern is shown for context, not as an
even-handed alternative.

### `apply`

Refuse to apply a dated pattern silently. If the user explicitly names one, acknowledge the
dated status and ask for confirmation before proceeding:

> "Active Record is marked dated in `references/dated-patterns.md` and is **forbidden** in
> claude-engram — it would put `save()`/`delete()` on the fact rows and couple persistence to
> the data, defeating the Repository seam that lets the store be swept, pruned, migrated, and
> viewed independently. The default is Repository over Data Mapper (`core/store.py`). Confirm
> you want Active Record anyway, and explain the constraint that requires it."

Once confirmed, apply the pattern as canonically documented and update `references/engram-defaults.md`
(and `.claude/rules/02-architecture/01-poeaa-and-layers.md`) with the deviation note in the
same change.

---

## Override mechanism

The user may legitimately need a dated pattern. Valid override conditions:

1. **Explicit naming** — the user types the pattern name as the choice, not as a question.
2. **Justified context** — an environment constraint or a domain-specific reason that
   disables the modern equivalent.
3. **Comparison or research request** — the user wants to *understand* the dated pattern;
   the skill explains it without recommending it.

When the user overrides, treat the request as a re-architecture proposal scoped to that area.
Document the deviation; the next `audit` run should not flag it.

## Edge cases

- **Transaction Script** — borderline. Acceptable for a trivial one-shot maintenance CLI
  command; dated for anything with real logic. claude-engram's `bin/*` scripts stay clean because
  they are thin Composition Roots that delegate to `service.py` / `recall.py` — not transaction
  scripts.
- **Serialized LOB** — dated only in its XML form. claude-engram's int8/binary embedding blobs are
  a current, supported LOB use. The discipline rule applies: never filter inside the blob —
  it is an opaque semantic fingerprint, searched by decoding it into the ranking core, not by SQL.
- **Single Table Inheritance** — not in the dated list, but claude-engram has no hierarchy, so it
  is simply not used. Don't introduce one.
