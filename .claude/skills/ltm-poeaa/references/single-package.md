# Single Package — one bounded context

claude-ltm is **one Python package** (`plugins/ltm/`), not a monorepo. There is a single
bounded context, so there are **no per-package pattern divergences to track**. This file
exists to say that explicitly and to explain what "single package" means for the four
skill modes.

The source project this skill was adapted from had five packages with different shapes;
that framing is deliberately **not** carried over. Do not invent bounded contexts
("the recall context", "the index context") — the plugin is one coherent package with
one set of locked-in defaults (`ltm-defaults.md`).

---

## The seams (not bounded contexts)

Within the one package, patterns are organised by **seam** — a layer/responsibility
boundary, not a separate context. The defaults in `ltm-defaults.md` apply uniformly
across all of them:

| Seam | Directory | Primary patterns |
|------|-----------|------------------|
| Domain / functional core | `core/distill.py`, `scoring.py`, `quantize.py`, `fusion.py`, `confidence.py`, `lexical.py` | Functional Core, Value Object |
| Service layer (write) | `core/service.py` | Command / Handler, function-style Service Layer |
| Service layer (read) | `core/recall.py` | Query Object, DTO / Null Object |
| Persistence | `core/store.py` | Repository over Data Mapper |
| Driven adapters | `core/embedding.py`, `core/distill.py` interfaces, `core/adapters/`, `core/daemon_client.py` | Gateway, Separated Interface, Plugin, Service Stub, Remote Facade |
| Index pipeline | `core/indexer.py`, `chunking.py`, `code_symbols.py`, `treesitter_symbols.py`, `index_recall.py` | Functional Core parsers over the same Repository |
| Composition roots | `bin/*`, `bin/_bootstrap.py` | Composition Root (per entry-point type) |
| Presentation | `viewer/` | thin Front-Controller-shaped dispatcher (mostly N/A) |

**Consistency is required across all seams.** A pattern chosen in one seam binds the
whole package — e.g. Repository-over-Data-Mapper in `store.py` means no seam gets to use
Active Record; stdlib-first means no seam imports a heavy dep outside `core/adapters/`.
There is no per-package escape hatch, because there is one package.

---

## How the modes treat "single package"

- **`recommend`** — after identifying the POEAA category, go straight to
  `ltm-defaults.md`. There is no "which package?" step. Identify the **seam** instead
  (from the table above) and confirm the default for that category.
- **`audit`** — the whole package shares one expectation set. Anything outside
  `ltm-defaults.md` (plus the base/architectural patterns) is a finding — there are no
  documented per-package exceptions to tolerate.
- **`compare`** — weight the choice toward the single package's committed default; the
  alternative is second-class regardless of seam.
- **`apply`** — the implementation map comes straight from `ltm-defaults.md` and the
  file map; adjust only by which seam/file the change lands in.

---

## When a second package would appear

If claude-ltm ever grew a genuinely separate deployable (e.g. a standalone server, a
second plugin), *that* would be a new bounded context and this file would be replaced
with a per-context table. Until then: one package, one context, one set of defaults.
Read `ltm-defaults.md` for the defaults and `architectural-layers.md` for where code
lives within the package.
