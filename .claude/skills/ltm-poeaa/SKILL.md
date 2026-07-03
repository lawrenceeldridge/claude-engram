---
name: ltm-poeaa
description: Applies Fowler's Patterns of Enterprise Application Architecture plus Cosmic Python's Python-specific extensions to the claude-ltm plugin, and prevents anti-pattern mixing. Four modes — recommend (pick a pattern), audit (find anti-patterns in code), compare (e.g. Repository vs Active Record, Gateway vs direct import), apply (implement a chosen pattern). Use when the user says "poeaa", "Fowler", "Cosmic", "enterprise pattern", "design pattern", "what pattern should I use", "is this an anti-pattern", "Repository vs Data Mapper", "Gateway", "Query Object", "Functional Core", "Null Object", "CQRS", "Hexagonal", "Composition Root", "/ltm-poeaa", or when planning, refactoring, or auditing architecture anywhere in core/, bin/, core/adapters/, the hooks, the MCP surface, or the viewer. Also trigger when choosing how to structure domain logic, data access, the embedding/distiller seams, recall ranking, capture, or the daemon boundary — even without naming a pattern. Do NOT use for syntax help or framework documentation lookups.
argument-hint: "recommend [problem] | audit [module or file] | compare [patternA] vs [patternB] | apply [pattern] to [subject] | catalog [pattern-name]"
metadata:
  author: claude-ltm
user-invocable: true
disable-model-invocation: false
---

# claude-ltm POEAA — Patterns of Enterprise Application Architecture

Authoritative guidance on applying Martin Fowler's *Patterns of Enterprise Application Architecture* (POEAA, 2003) and Harry Percival & Bob Gregory's *Architecture Patterns with Python* (2020, "Cosmic Python") within **claude-ltm** — the token-first, cross-project long-term-memory + code/docs-index plugin in this repo. The catalog covers Fowler's full POEAA pattern set across 10 categories plus the Cosmic Python additions for patterns claude-ltm uses but Fowler doesn't catalog (Command, Handler, CQRS, Hexagonal / Ports & Adapters, Functional Core / Imperative Shell, Composition Root). This skill ensures claude-ltm applies these patterns consistently, never mixes incompatible ones, and locks in pre-decided defaults so that engineers don't reinvent fundamental architectural choices change by change.

Each catalog entry cites its canonical source (Fowler's eaaCatalog at <https://martinfowler.com/eaaCatalog/> for POEAA patterns, Percival & Gregory at <https://www.cosmicpython.com/> for Cosmic Python additions). Both sources are summarised under `references/catalog/` so the skill operates without network access. Patterns from sources other than these two are **not** added — see `references/ltm-defaults.md` § Update procedure for the policy that governs catalog extensions.

claude-ltm is a **single Python package** (one bounded context), so there is no per-package framing — see `references/single-package.md`.

---

## Usage

```
/ltm-poeaa recommend [problem]
/ltm-poeaa audit [module or file]
/ltm-poeaa compare [Pattern A] vs [Pattern B]
/ltm-poeaa apply [Pattern] to [target]
/ltm-poeaa catalog [pattern-name]
```

**Format:** `<mode> <argument>`

- **mode** (required): `recommend`, `audit`, `compare`, or `apply` for the four full modes documented in `## Modes` below; `catalog` for a quick lookup of a single pattern's entry without running the full pattern-selection flow.
- **argument** (required): see the per-mode input column in `## Modes` below; for `catalog`, pass the pattern name (e.g. `Repository`, `Gateway`).

If the user provides only a problem statement without naming a mode, default to `recommend`. If they name a single pattern with no mode, default to `catalog`.

---

## Why this skill exists

POEAA is not a list of equally-valid options — many of the catalog's patterns are **mutually exclusive at the same scope**. Mixing them produces well-known anti-patterns:

- A fact persisted by both Active Record (`fact.save()`) **and** a Repository/Data-Mapper layer (`Store.add(...)`) creates two truths about who owns persistence.
- Pure ranking/distillation logic that reaches out to do I/O (DB read, model load, subprocess) breaks the Functional Core / Imperative Shell split that keeps the core stdlib-testable.
- Importing `fastembed` (or shelling out to `claude`) inside `core/` instead of behind the `EmbeddingGateway` / `Distiller` interface breaks Hexagonal's inward-dependency rule.
- Injecting a placeholder or an error on empty recall instead of the Null Object (`render_block` → `""`) spends tokens on irrelevant turns.

claude-ltm adds a project-specific class of mistake on top of Fowler's catalog: **breaking the token or latency budget on the hot path**. Recall runs inside `UserPromptSubmit`; anything that puts embedding work, a synchronous model load, or an un-capped payload on that path is a first-class architectural violation — treated here alongside Fowler's anti-patterns. Capture must stay **detached and fail-open**: a capture or embedding failure must never break a turn.

Once claude-ltm has chosen a pattern for a seam (e.g. Repository over Data Mapper in `store.py`; Gateway + Separated Interface for embeddings), every change in that seam must follow it unless we deliberately migrate the whole seam. This skill enforces that discipline.

The complementary rule: many patterns **only work in combination** (a Gateway is only testable with a Separated Interface + Service Stub; a Query Object needs the Repository/mapper underneath). The skill knows the natural pairings and the forbidden ones — see `references/combinations.md` and `references/anti-patterns.md`.

---

## When to Use This Skill

Trigger this skill whenever an architectural decision touches one of the ten POEAA categories:

| Category | Decision-shape that should trigger ltm-poeaa |
|----------|----------------------------------------------|
| Domain Logic | "Where should this logic live?", new distillation/ranking/capture logic, pure-vs-impure boundary questions |
| Data Source | New persistence concern, a schema/migration change, adding a `Store` method, "should this be Active Record?" |
| O-R Behavioral | Anything proposing an ORM, a session/identity-map abstraction, or lazy loading (usually a smell here) |
| O-R Structural | Fact/chunk identity, embedding blob storage, project tagging, "should this be its own table?" |
| O-R Metadata | New recall/search parameters, query construction, fusion/ranking knobs, a proposed Repository class |
| Web Presentation | Viewer changes; MCP request-dispatch shape |
| Distribution | The injected payload (`render_block`), MCP tool responses, the daemon-client boundary |
| Offline Concurrency | Capture worker lifecycle, single-flight, idempotency, TTL sweep, supersession |
| Session State | Any proposal to hold cross-turn state outside the store (usually a smell) |
| Base | The embedding/distiller Gateways, Separated Interfaces, Plugin selection, Service Stubs, Null Object, Value Objects |

**Use when** the user is planning, refactoring, reviewing, or auditing — not for pure syntax questions.

**Do NOT use** for: general Python syntax, library API lookups, build/test failures, or git operations. Those are not architectural decisions.

---

## Modes

The skill has four modes. Pick based on the user's intent.

### `recommend` — Pattern selection for a new problem

Input: a problem statement or feature ask.
Output: the POEAA pattern(s) that fit, why, and how to wire them into claude-ltm's locked-in defaults.

Workflow:
1. Identify which POEAA category the problem touches (see table above) and which seam it lives in (`store.py`, `service.py`, `recall.py`, the embedding/distiller Gateway, `bin/*`, the index pipeline, the viewer).
2. Read `references/ltm-defaults.md` — if claude-ltm has already chosen a pattern for that category, **that is the answer**. Do not propose alternatives unless the user has explicitly framed this as a re-architecture.
3. If no default applies (genuinely new territory), read the relevant `references/catalog/*.md` file and shortlist 2–3 candidate patterns.
4. Filter the shortlist through `references/dated-patterns.md` — drop any pattern marked dated unless the user has explicitly asked for it. Surface the modern equivalent instead.
5. Cross-check `references/anti-patterns.md` to ensure no candidate clashes with patterns already in use in adjacent seams, and that no budget/fail-open rule is about to be violated.
6. Recommend one primary pattern + the natural pairings it brings. Justify with citations from the catalog.

### `audit` — Detect patterns and anti-pattern combinations in existing code

Input: a file, module, or feature area.
Output: an inventory of POEAA patterns currently in use, a list of any anti-pattern combinations found, dated patterns flagged, and remediation suggestions.

Workflow:
1. Read the target code (use `Read`; consult `recall` / `search_code` first per the memory-first rule).
2. Map observed structures to POEAA patterns using `references/ltm-defaults.md` as the baseline expectation.
3. For each observed pattern, check `references/combinations.md` and `references/anti-patterns.md` for clashes with other patterns also present.
4. Cross-check the claude-ltm–specific rules: heavy deps imported outside `core/adapters/`; I/O inside a pure core function; embedding work or an un-capped payload on the recall hot path; capture that isn't detached or isn't fail-open; Active Record creep on a row/dataclass.
5. Cross-check each observed pattern against `references/dated-patterns.md`. Flag dated patterns as findings with severity per that file.
6. Report findings using the **Audit Report Template** below.
7. If anti-pattern combinations or dated patterns are found, propose the minimum-viable migration to a single coherent (non-dated) pattern.

### `compare` — Side-by-side comparison of alternative patterns

Input: two or more pattern names that compete for the same role.
Output: a comparison covering intent, when to use, when to avoid, force-of-fit for claude-ltm, and the conventional choice.

Workflow:
1. Read the catalog entry for each pattern from `references/catalog/`.
2. Check each pattern against `references/dated-patterns.md`. Mark dated entries with **🪦 Dated** in the column header so the asymmetry is visible.
3. Build a comparison table covering: intent, where logic lives, complexity cost, testability (stdlib, no mocks?), token/latency-budget impact, dated status, conflict with claude-ltm defaults.
4. Conclude with the claude-ltm-conventional choice (cite `references/ltm-defaults.md`) and the conditions under which the other(s) would be preferred. Dated patterns are shown for context, not as even-handed alternatives.

### `apply` — Implement a chosen pattern in claude-ltm

Input: a pattern name + a target (file, module).
Output: a step-by-step implementation guide that respects both the canonical Fowler definition and claude-ltm's CQRS / Hexagonal / stdlib-first conventions.

Workflow:
1. Read the pattern's catalog entry under `references/catalog/`.
2. Check `references/dated-patterns.md`. If the requested pattern is dated, **stop and confirm** with the user — quote the dated reason and the modern equivalent, and require an explicit override before proceeding. Do not silently apply a dated pattern.
3. Read claude-ltm's project rules: `.claude/rules/00-quality/`, `.claude/rules/01-general/`, and especially `.claude/rules/02-architecture/01-poeaa-and-layers.md` and `02-hooks-and-budgets.md`.
4. Read `references/ltm-defaults.md` to confirm the pattern is allowed in the target seam.
5. Read `references/combinations.md` for natural pairings the pattern requires (e.g. Gateway requires Separated Interface + Service Stub; Query Object requires the Repository/mapper).
6. Produce an implementation checklist that maps Fowler's pattern roles onto claude-ltm's layout (`core/`, `core/adapters/`, `bin/*`, `viewer/`).
7. Flag any prerequisite patterns that must be present first (e.g. a new embedding backend → a new adapter behind `EmbeddingGateway`, wired by `get_embedder`, with a fail-open fallback).
8. If the user overrode a dated-pattern warning, update `references/ltm-defaults.md` (and the architecture rule) with the deviation note in the same change so future audits don't re-flag it.

---

## claude-ltm's Locked-in Pattern Defaults (one-page summary)

Full justifications and citations live in `references/ltm-defaults.md`. The summary (this is the same map as `DESIGN.md` § POEAA / Cosmic Python and `.claude/rules/02-architecture/01-poeaa-and-layers.md`):

| Layer | Chosen Pattern | Why this and not the alternative |
|-------|----------------|----------------------------------|
| Overall shape | **CQRS + Hexagonal (Ports & Adapters)** | Capture (write) is heavy/batch/detached; recall (read) is tiny/hot-path. Splitting by performance profile is the top-level decision the whole plugin serves. |
| Domain Logic | **Functional Core / Imperative Shell + function-style Service Layer** | No rich domain — a fact is data. Distil/rank/score/quantise/fuse are pure functions (`distill`, `scoring`, `quantize`, `fusion`); `service.py` + `bin/*` are the shell. Keeps the core stdlib-testable with no mocks. |
| Data Source | **Repository over Data Mapper** (`core/store.py`), never Active Record | All memory + index access goes through `Store`; facts are plain `sqlite3.Row` data with no persistence methods. Lets the store be swept, pruned, migrated, viewed, and benchmarked without coupling logic to the data. |
| O-R Behavioral | **Deliberately minimal** — one short-lived sqlite3 connection, explicit transactions | Hooks are short-lived processes; no ORM, no session graph, so Unit of Work / Identity Map / Lazy Load collapse into "one connection, plain rows". Idempotent batched capture gives the atomicity a UoW would. |
| O-R Structural | **Identity Field (content hash) + Serialized LOB (int8/binary embedding blobs) + Embedded Value (vector fingerprint)** | `fact_id = hash(project_key, text)` makes capture idempotent; the quantised vector is stored whole as an opaque blob inlined on the row; every row is tagged by project key. No inheritance mapping. |
| O-R Metadata | **Query Object** (`recall.search` / `search_fused` params + `Config`) + **lightweight Repository** (`Store` methods) | Bundle search knobs on the query/config object, don't grow a positional signature. One store, one set of named query methods — no `<Aggregate>Repository` class needed. |
| Web Presentation | **Read-only localhost viewer** (stdlib `http.server`); otherwise N/A | Not a web app. The viewer is a thin dispatcher over `Store`. No SPA, no server-side templating. (The MCP server is Distribution, not presentation.) |
| Distribution | **DTO** (`render_block` one-line-per-fact; MCP tool responses) + **Gateway** (embedding/distiller) + **thin daemon client** (Remote Facade, fail-open) | Injected payload is shaped for the token budget, not the store. The daemon client is a coarse Remote Facade over the embedder that falls back in-process on any failure. No message bus / events. |
| Offline Concurrency | **Single-flight capture + idempotent-per-fact capture + TTL sweep** (no offline locks) | No contended interactive edit — so Optimistic/Pessimistic Offline Lock don't apply. Single-flight stops worker/daemon pileup; `fact_id` idempotency makes re-capture safe; `Store.sweep` retires stale facts (reversibly). |
| Session State | **N/A** — short-lived hook processes hold no session state | The only cross-turn state is the durable SQLite store (Domain/index data, not session) and the prompt-cache prefix (a caching optimisation). Anything session-shaped is a smell. |
| Base | **Gateway + Separated Interface + Plugin + Service Stub** (embeddings/distillers) · **Null Object** (`render_block` → `""`) · **Value Object** (`DistilledFact`, `Observation`, `Hit`, `Config`) · **Layer Supertype** (the ABCs) · **Mapper** (row↔dict, quantize) | Heavy/optional deps live only behind Gateways in `core/adapters/`; `HashEmbedding` / `HeuristicDistiller` are the zero-dep defaults *and* the test stubs; every selection is fail-open. Registry avoided — dependencies are passed explicitly. |

---

## Output Templates

### Recommend Report Template

```markdown
## POEAA Recommendation: <problem-summary>

**Category**: <Domain Logic | Data Source | ...>
**Seam**: <store.py | service.py | recall.py | embedding/distiller Gateway | bin/* | index | viewer>
**claude-ltm default applies?**: <yes — <pattern> | no — open territory>

### Recommended Pattern: <Pattern Name>
- **Intent (Fowler / Cosmic)**: <one-line>
- **Why this pattern fits**: <2–3 sentences tying problem to pattern>
- **Required pairings**: <patterns it brings; e.g., Gateway → Separated Interface + Service Stub>
- **Forbidden in adjacent seams**: <patterns that must not also be introduced>
- **Budget / fail-open check**: <hot-path token/latency impact; detached + fail-open if it touches capture or a Gateway>

### Alternatives considered
| Pattern | Why rejected |
|---------|-------------|
| ... | ... |

### Implementation map (claude-ltm)
- `core/<file>.py` — <what>
- `core/adapters/<adapter>.py` — <what, if a new driven adapter>
- `bin/<entry>.py` — <wiring in the Composition Root>

### Citations
- Fowler / Cosmic: `references/catalog/<file>.md`
- claude-ltm default: `references/ltm-defaults.md` § <section>
- Project rule: `.claude/rules/02-architecture/<file>.md`
```

### Audit Report Template

```markdown
## POEAA Audit: <target>

### Patterns Detected
| Pattern | Evidence (file:line) | Status |
|---------|----------------------|--------|
| Repository over Data Mapper | core/store.py:266 | ✅ aligned with claude-ltm default |
| Active Record | core/foo.py:45 — `fact.save()` | ⚠️ violates claude-ltm default (Repository) |
| I/O in pure core | core/scoring.py:88 — DB read inside ranking fn | 🐌 breaks Functional Core / Imperative Shell |
| Heavy dep outside adapters | core/recall.py:5 — `import fastembed` | ⚠️ breaks Hexagonal inward-dependency rule |

### Dated Patterns Found
| Pattern | Severity | Modern Equivalent |
|---------|----------|-------------------|
| Active Record | high (forbidden — Repository is the default) | Repository over Data Mapper (`Store`) |

### Anti-Pattern Combinations Found
1. **<combination>** — why it's an anti-pattern, where, suggested fix.

### Budget / fail-open Findings
- Embedding work / synchronous model load on the recall hot path: <list>
- Capture that isn't detached, or a Gateway with no fail-open fallback: <list>
- Injected payload not capped / not one-line-per-fact: <list>

### Remediation
- <ordered list of minimal-viable changes>

### Citations
- Fowler / Cosmic: `references/catalog/<file>.md`
- claude-ltm default: `references/ltm-defaults.md` § <section>
- Anti-pattern reference: `references/anti-patterns.md` § <section>
- Dated reference: `references/dated-patterns.md` § <pattern>
```

### Compare Report Template

```markdown
## POEAA Compare: <Pattern A> vs <Pattern B> [vs <Pattern C>]

| Dimension | <A> | <B> |
|-----------|-----|-----|
| Intent | ... | ... |
| Where logic lives | ... | ... |
| Persistence ownership | ... | ... |
| Test isolation (stdlib, no mocks?) | ... | ... |
| Complexity cost | ... | ... |
| Token / latency budget impact | ... | ... |
| Conflicts with claude-ltm defaults? | ... | ... |

### claude-ltm-conventional choice: <Pattern>
Reason: <citation to ltm-defaults.md>

### When the other(s) would be preferred
<conditions>
```

### Apply Report Template

```markdown
## POEAA Apply: <Pattern> → <Target>

### Pattern Definition (Fowler / Cosmic)
<intent + how it works>

### Prerequisite Patterns Already in claude-ltm
- <list — e.g., for a new embedding backend: Gateway ✅ (EmbeddingGateway), Plugin ✅ (get_embedder)>

### Implementation Steps
1. <step>
2. <step>
...

### File Changes
- Create `<path>` (a new driven adapter goes in core/adapters/)
- Modify `<path>` (wire it in the bin/* Composition Root)

### Pairings to verify
- <natural pairing> — already in place? <yes/no>
- fail-open fallback: <what it degrades to>

### Anti-pattern guard-rails
- Do NOT also <forbidden combination>.
- Do NOT import the heavy dep outside core/adapters/.
- Do NOT put I/O in a pure core function, or embedding work on the recall hot path.

### Tests
- <what to assert — core logic should be testable on the stdlib without mocks>
```

---

## Reference Files

Read these on demand based on the mode and category in play. Each file is small and category-scoped.

### Pattern catalog (Fowler's POEAA + Cosmic Python additions)

- `references/catalog/01-domain-logic.md` — Transaction Script, Domain Model, Table Module, Service Layer (+ Cosmic: Domain Event, Command, Handler)
- `references/catalog/02-data-source.md` — Table Data Gateway, Row Data Gateway, Active Record, Data Mapper
- `references/catalog/03-or-behavioral.md` — Unit of Work, Identity Map, Lazy Load
- `references/catalog/04-or-structural.md` — Identity Field, Foreign Key Mapping, Association Table Mapping, Dependent Mapping, Embedded Value, Serialized LOB, Single/Class/Concrete Table Inheritance, Inheritance Mappers
- `references/catalog/05-or-metadata.md` — Metadata Mapping, Query Object, Repository
- `references/catalog/06-web-presentation.md` — MVC, Page Controller, Front Controller, Template View, Transform View, Two Step View, Application Controller
- `references/catalog/07-distribution.md` — Remote Facade, Data Transfer Object (+ Cosmic: Message Bus)
- `references/catalog/08-offline-concurrency.md` — Optimistic Offline Lock, Pessimistic Offline Lock, Coarse-Grained Lock, Implicit Lock
- `references/catalog/09-session-state.md` — Client / Server / Database Session State
- `references/catalog/10-base.md` — Gateway, Service Stub, Record Set, Mapper, Layer Supertype, Separated Interface, Registry, Value Object, Money, Special Case, Plugin
- `references/catalog/11-architectural-style.md` — **Cosmic Python additions:** Hexagonal Architecture (Ports and Adapters), CQRS, Functional Core / Imperative Shell, Composition Root. These are architectural-shape patterns that don't fit Fowler's 10 categories — they apply across every category at once, and they are claude-ltm's structural baseline.

### Cross-references and compatibility

- `references/architectural-layers.md` — **Cosmic Appendix A's hexagonal four-layer view** (Domain / Service Layer / Secondary Adapters / Primary Adapters) mapped onto claude-ltm's `core/`, `core/adapters/`, and `bin/*` with concrete file examples per layer. Use this when the question is "where does this code live in claude-ltm?" rather than "which Fowler category does this pattern belong to?".
- `references/combinations.md` — Natural pairings (which patterns belong together), including claude-ltm's adopted bundles (the Gateway/Stub/Plugin testability bundle, the recall read bundle, the capture write bundle).
- `references/anti-patterns.md` — Forbidden combinations and how to spot them in code, including claude-ltm–specific ones (heavy dep outside adapters, I/O in the pure core, hot-path embedding, capture that isn't fail-open).
- `references/dated-patterns.md` — Patterns aged out by tech evolution (XML/SOAP, RecordSet, server-rendered HTML, stateful sessions, hand-rolled pre-ORM data access). The skill skips these unless the user explicitly overrides.
- `references/ltm-defaults.md` — claude-ltm's chosen pattern per layer, with rationale and citations. **The most important file** — read it in every mode.
- `references/decision-trees.md` — Quick decision flowcharts for the most common choice points (Repository or Active Record? Gateway or direct import? pure core or shell? inject or stay silent?).
- `references/single-package.md` — Why claude-ltm is one bounded context and what that means for the modes (there is no per-package divergence to track).

---

## Operating Principles

1. **Defer to claude-ltm defaults.** Never introduce a pattern that competes with one claude-ltm has already chosen for that seam. If the user's request seems to require a competing pattern, treat it as a re-architecture proposal — surface the conflict, don't silently adopt the new pattern.

2. **Pattern names are precise.** Use Fowler's / Cosmic's exact name (e.g. "Data Mapper", not "DAO"; "Separated Interface", not "just an interface"). The catalog entries are the source of truth.

3. **Cite from the catalog.** Every recommendation references the relevant catalog file. The user should be able to click through to the canonical definition.

4. **Pairings are mandatory, not optional.** If you recommend a Gateway, you have implicitly recommended Separated Interface + Service Stub (+ Plugin selection). If you recommend a Query Object, you have implicitly recommended the Repository/mapper underneath. State this explicitly.

5. **Anti-patterns are blocking.** If an audit finds an anti-pattern combination, do not propose new features that would inherit the inconsistency — fix the inconsistency first or scope the new feature to avoid touching it.

6. **Skip dated patterns silently.** Several POEAA patterns are tied to obsolete tech context (XML/SOAP, RecordSet, server-rendered HTML, stateful sessions, hand-rolled pre-ORM gateways). The skill **does not recommend or apply these by default** — see `references/dated-patterns.md`. The user can override by naming a dated pattern explicitly with a justifying context; ask for confirmation before applying and document the deviation in `ltm-defaults.md`. Never silently apply a dated pattern.

7. **One bounded context.** claude-ltm is a single package — patterns must be consistent across all of `core/`, `core/adapters/`, `bin/*`, and `viewer/`. There is no per-package escape hatch. See `references/single-package.md`.

8. **Stdlib-first, deps behind Gateways.** The core runs on the Python standard library. Heavy or optional dependencies (`fastembed`, an HTTP distiller, a subprocess to `claude`) import **only** inside `core/adapters/` (or a Gateway impl), never in `core/` proper. Adding a backend means a new adapter behind the existing interface — not an `if backend == …` in the core.

9. **Functional core stays pure.** Distillation, ranking, scoring, quantisation, and fusion are pure functions over data. No DB reads, model loads, subprocesses, clock reads, or randomness inside them — that all belongs in the imperative shell (`service.py`, `bin/*`). This is what makes the core stdlib-testable without mocks.

10. **Never break the budget or fail closed.** Recall runs on the hot path: no synchronous model load, no un-capped payload, no embedding work that isn't daemon-backed-with-fallback there. Capture runs detached and **fail-open**: every hook exits 0 on any error and injects nothing; every Gateway/Plugin selection degrades safely (fastembed → hash, daemon → in-process, LLM distiller → heuristic). A failure must never break a turn. See `.claude/rules/02-architecture/02-hooks-and-budgets.md`.

---

## Troubleshooting

### Audit target path does not exist

**Symptom:** `audit <path>` is invoked but the path resolves to nothing under the plugin root.
**Cause:** Typo, or a path given relative to the repo root rather than `plugins/ltm/`.
**Fix:** Re-run with the plugin-relative path (e.g. `core/store.py`, `core/recall.py`, `core/adapters/`, `bin/recall_prompt.py`). Prefer auditing a whole seam (`core/`, or `bin/*` as the Composition Roots) over a single file when the concern is cross-cutting (fail-open, budget, dependency direction).

### Catalog reference file not found

**Symptom:** A mode needs to cite from `references/catalog/0X-*.md` (or `11-architectural-style.md`) and the file is missing.
**Cause:** Skill was partially copied, or a category was renamed.
**Fix:** List the catalog files actually present (`ls .claude/skills/ltm-poeaa/references/catalog/`). If a citation can't be sourced, say so explicitly rather than inventing one — Operating Principle 3 ("Cite from the catalog") binds.

### User names a dated pattern as input

**Symptom:** `apply Active Record to ...`, `apply Server Session State to ...` — the named pattern appears in `references/dated-patterns.md`.
**Cause:** User may not realise the pattern is dated for claude-ltm, or may have a justifying context.
**Fix:** Per Operating Principle 6, **do not silently apply**. Surface the dated status, cite `dated-patterns.md`, ask for the justifying context, and treat any override as a re-architecture proposal that updates `ltm-defaults.md` in the same PR.

### A recommendation would put work on the recall hot path or make capture fail closed

**Symptom:** A pattern recommendation loads a model synchronously in `UserPromptSubmit`, injects an un-capped payload, or lets a capture/embedding error propagate out of a hook.
**Cause:** The recommendation was driven by Fowler-pattern shape but didn't account for Operating Principle 10 (budget + fail-open).
**Fix:** Re-derive it with the budget as a first-class constraint — daemon-backed embedding with in-process fallback, a `max_chars`-capped `render_block` DTO, detached capture, and exit-0-on-error. Treat budget/fail-open violations as findings on par with anti-pattern combinations — see `references/anti-patterns.md`.

---

## Notes

### Performance

- Take your time to do this thoroughly. Architectural decisions outlive the change that introduces them.
- Quality is more important than speed. A wrong pattern recommendation will be paid for in every subsequent change in that seam.
- Do not skip validation steps — always cross-check against `anti-patterns.md` before concluding.
- If you cannot map an observation cleanly to a Fowler / Cosmic pattern, say so. Inventing pattern names is worse than admitting the structure is bespoke.

### Related skills

- `/ltm-git` (PR-review mode) — invokes `/ltm-poeaa audit` when reviewing PRs that touch architectural code (`core/`, `core/adapters/`, `bin/*`, the hooks, the MCP surface).
- `/ltm-analyse` and its planning workflows — invoke `ltm-poeaa` organically when they surface architectural decisions; no explicit cross-reference required, the model picks `ltm-poeaa` from the trigger phrases in this skill's description.
- `.claude/rules/02-architecture/01-poeaa-and-layers.md` — the always-on rule that summarises the same pattern map; this skill carries the depth behind it.

### Section structure

This SKILL.md uses a **mode-heavy variant** of the standard skill template — `## Modes` substitutes for `## Workflow`, with each mode (`recommend`, `audit`, `compare`, `apply`) carrying its own input / output / workflow steps inline; `## Output Templates` provides one report template per mode in place of a top-level `## Examples` section. Resulting section order: `Frontmatter → Title → Usage → Why this skill exists → When to Use → Modes → claude-ltm's Locked-in Pattern Defaults → Output Templates → Reference Files → Operating Principles → Troubleshooting → Notes`. This shape is appropriate for skills with four or more distinct modes that each carry a self-contained mini-workflow.
