---
name: engram-test
description: claude-engram test lifecycle — the stdlib unittest suite (write / run / review / audit tests, coverage) plus the recall-quality benchmark (`engram eval`). Use when writing or reviewing tests for `core/**`, hooks, adapters, the CLI or MCP tools; when asked "run the tests", "write a test for X", "review these tests", "can I delete this test", "check coverage"; or when changing embeddings, ranking, quantisation, fusion, or distillation — those must be A/B'd with `engram eval` before shipping. Not for general unittest syntax help.
metadata:
  author: Lawrence Eldridge
---

# engram-test — Test Lifecycle for claude-engram

Operational depth for testing claude-engram. The suite is **stdlib `unittest`**
(discoverable, also runs under `pytest`), all standard-library, no network — the
default `hash` embedding + `heuristic` distiller keep the core testable with zero
dependencies. This skill carries the depth the rule pointer at
[`.claude/rules/00-quality/02-testing.md`](../../rules/00-quality/02-testing.md)
intentionally does not: how to structure a test, what to test per code type, how
to keep the suite lean, and how to run and read the **recall-quality benchmark**.

Two distinct surfaces, both first-class:

1. **The unittest suite** — `python3 -m unittest discover -s tests` from
   `plugins/engram`. Correctness of the core, hooks, adapters, CLI, MCP tools.
2. **The recall benchmark** — `python3 bin/engram eval`. Recall@1/@3, MRR@10,
   bytes/fact through the *real* quantised search path. This is how retrieval
   quality is measured; per DESIGN.md's "measured, not assumed" ethos, any change
   to embeddings / ranking / quantisation / fusion / distillation is A/B'd here
   **before** it ships.

---

## Usage

```
/engram-test run [scope]        run the suite (all, a file, or a TestCase)
/engram-test create <module>    write a stdlib-unittest test for a core module / hook / adapter
/engram-test review [path]      QE-standards + anti-pattern review of a test file
/engram-test audit [path]       advisory leanness audit — deletable / mergeable / redundant tests
/engram-test bench [backends]   run `engram eval`; read Recall/MRR/bytes; compare backends
/engram-test coverage [module]  optional coverage report (stdlib `coverage`, opt-in dep)
```

If the user names no mode, infer from intent: "run the tests" → `run`; "write a
test for `core/scoring.py`" → `create`; "are these tests any good" → `review`;
"can I delete this" → `audit`; "did the ranking change hurt recall" → `bench`.

| Mode | Argument | Behaviour |
|------|----------|-----------|
| `run` | none / file / `File.TestCase` | Discover and run the suite (or a subset); parse failures, suggest fixes |
| `create` | module path or feature | Generate an AAA-structured `unittest.TestCase` using stub embedding/distiller + tempdir fixtures; run it |
| `review` | test file / dir | Check naming, AAA, fail-open coverage, stub hygiene, assertion quality; run the anti-pattern checklist |
| `audit` | none / path | Advisory: surface trivial / duplicate / tautological / parametrisable / dead / redundant tests. Never edits |
| `bench` | backend spec | Run `engram eval`, report the metric table, interpret an A/B against the default |
| `coverage` | none / module | Report line coverage via stdlib `coverage` (opt-in dependency), framed as actionable gaps |

---

## Modes

### 1. `run` — Execute the suite

Input: nothing (whole suite), a file, or a `Module.TestCase[.test_method]` path.

```bash
cd plugins/engram
python3 -m unittest discover -s tests            # whole suite (137 tests, 5 skipped)
python3 -m unittest tests.test_smoke             # one module
python3 -m unittest tests.test_smoke.ScoringTests.test_recency_decay_curve
python3 tests/test_smoke.py                       # modules are runnable directly too
```

Optional deps (`fastembed`, `tree-sitter-language-pack`) are **skipped**, not
failed, when absent — a green run with 5 skips is the expected default. For
failures, match against the table in [`references/standards.md`](references/standards.md)
§ "Common failure patterns" and propose a fix (leaked `ENGRAM_DATA_DIR`, an
un-closed `Store`, a stub whose signature drifted from the real adapter, etc.).

### 2. `create` — Write a test for a module

Input: a `core/*.py` module, a hook in `bin/`, an adapter, or a feature.

Workflow:
1. Read the module. Identify its seam — is it a pure function (`core/scoring.py`,
   `core/quantize.py`), a stateful component over the store (`core/service.py`,
   `core/recall.py`), a hook (`bin/*.py`, tested as a subprocess), or an adapter
   behind a port (`core/embedding.py`, `core/distill.py`)?
2. Pick the fixture shape from [`references/test-data.md`](references/test-data.md):
   pure functions need nothing; store-touching tests need a `tempfile.TemporaryDirectory`
   + `os.environ["ENGRAM_DATA_DIR"]` in `setUp`/`tearDown`; anything embedding-touching
   uses `HashEmbedding(dim=...)`; anything LLM-touching uses a duck-typed stub
   distiller/summarizer (never a live model).
3. Generate an `unittest.TestCase` with AAA-structured methods named
   `test_<subject>_<condition>_<expected>` — at least happy path, boundary/empty,
   and the fail-open path where relevant. Patterns per code type in
   [`references/patterns-unittest.md`](references/patterns-unittest.md).
4. Guard optional deps with `@unittest.skipUnless(...)` per
   [`references/skip-conventions.md`](references/skip-conventions.md).
5. Write to `plugins/engram/tests/test_<area>.py` (add to an existing file if one
   already covers the area), then run it (`run` mode) and report pass/fail.

### 3. `review` — QE-standards + anti-pattern review

Input: a test file, a test dir, or a diff. Output: severity-rated findings.

QE-standards checklist (from [`references/standards.md`](references/standards.md)):
naming, AAA separation, **fail-open coverage** (a hook/adapter given bad input, a
missing dep, or a dead daemon is asserted to exit 0 / fall back — not assumed to),
stub hygiene (the stub's methods match the real port's signature), assertion
quality (no bare `assertTrue(True)`; meaningful comparisons), and
**stdlib-purity** (a `core/**` test must not import `fastembed` or hit the network).

Anti-pattern scanner (checklist, advisory): outdated stub (a stub whose method
signature drifted from the real adapter), leaked global state (`ENGRAM_DATA_DIR` /
`os.environ` not restored in `tearDown`), un-closed `Store`, network in a core
test, and asserting a stdlib guarantee instead of behaviour. Rules in
[`references/standards.md`](references/standards.md) § "Anti-pattern checklist".

### 4. `audit` — Advisory leanness audit

Input: optional path. Output: candidates for deletion / merge / parametrisation
(`subTest`) / property test, each with file:line + heuristic + proposed action +
reason. **Always advisory** — the skill never edits or deletes a test. Honour the
"what NOT to delete" allowlist (regression tests with a linked issue, `DO NOT
DELETE` comments, non-obvious failure modes, fail-open guards). Eight heuristics
and the allowlist in [`references/test-leanness-heuristics.md`](references/test-leanness-heuristics.md).

### 5. `bench` — Recall-quality benchmark

Input: a backend spec (`name[@model][+float]`, comma-separated). This is the
second test surface and the gate on any retrieval change.

```bash
cd plugins/engram
python3 bin/engram eval --backends hash                                   # zero-dep default
python3 bin/engram eval --backends "hash,fastembed"                       # A/B the stub vs real model
python3 bin/engram eval --backends "fastembed,fastembed+float"            # isolate int8 quantization loss
```

Reports Recall@1, Recall@3, MRR@10, and bytes/fact over the bundled labelled set
(18 facts, 14 paraphrased queries) through the real quantised store path. The
`+float` twin ranks on raw vectors, so the gap to it is exactly the int8 loss.
**The rule:** any change to embeddings, ranking, quantisation, fusion, or
distillation is A/B'd here before it ships — see
[`references/benchmark.md`](references/benchmark.md) and
[DESIGN.md § Embedding backend — measured, not assumed](../../../DESIGN.md).

### 6. `coverage` — Optional coverage report

Coverage is **not** a default dependency (the suite is stdlib-first). When you
want a coverage read, install `coverage` into a throwaway venv and run it:

```bash
cd plugins/engram
python3 -m pip install coverage        # opt-in; not in requirements.txt
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report -m --include="core/*"
```

Report line coverage per `core/` module and frame gaps as actionable next steps
("`core/fusion.py` at 61% — the tie-break branch in `fuse()` is untested"). There
is no hard threshold and no CI coverage gate; prioritise coverage of the retrieval
path and the fail-open branches.

---

## Operating principles

These are non-negotiable and mirror
[`.claude/rules/00-quality/02-testing.md`](../../rules/00-quality/02-testing.md):

1. **Core stays stdlib-testable.** Tests for `core/**` run without `fastembed`
   and without the network. Use `HashEmbedding` and a stub distiller. If a test
   truly needs a real model, gate it with `@unittest.skipUnless`.
2. **Measure retrieval changes.** Embeddings, ranking, quantisation, fusion, and
   distillation changes are A/B'd with `engram eval` before merge. Don't reason about
   recall — measure it.
3. **Fail-open is a test target, not an assumption.** A hook or adapter given a
   broken input / missing dep / dead daemon must still exit 0 or fall back —
   assert it explicitly.
4. **Fixtures are local and self-cleaning.** `tempfile.TemporaryDirectory` +
   `ENGRAM_DATA_DIR` set in `setUp` and restored in `tearDown`; close every `Store`.
   No shared mutable state across tests; no live model in the default path.
5. **AAA structure.** Arrange / Act / Assert, visually separated. `create` and
   `review` enforce it.
6. **Optional deps skip, never fail.** A missing `fastembed` or tree-sitter
   yields a `skip`, not a red run. The zero-dep path is the contract.
7. **Leanness audit is advisory.** The skill proposes deletions/merges with
   reasons; the human decides. Coverage-erosion and regression guards apply.

---

## Reference files

Read on demand based on mode and depth. Each is small and focused.

- [`references/standards.md`](references/standards.md) — naming, AAA, fail-open
  coverage, what-to-test per code type, the anti-pattern checklist, common
  failure-pattern table.
- [`references/patterns-unittest.md`](references/patterns-unittest.md) — copy-paste
  `unittest` patterns per code type (pure function, store round-trip, recall/search,
  hook-as-subprocess, adapter/stub, CLI, MCP tool).
- [`references/test-data.md`](references/test-data.md) — local fixtures, stub
  embeddings (`HashEmbedding`), stub distillers/summarizers, the tempdir +
  `ENGRAM_DATA_DIR` pattern. No faker, no DB, no network.
- [`references/skip-conventions.md`](references/skip-conventions.md) — how to gate
  optional deps (`@unittest.skipUnless`), the `slow` convention, and the
  stdlib-purity rule for `core/**`.
- [`references/test-leanness-heuristics.md`](references/test-leanness-heuristics.md) —
  eight deletion/merge heuristics, `subTest`/property alternatives, the "what NOT
  to delete" allowlist.
- [`references/benchmark.md`](references/benchmark.md) — `engram eval` in depth:
  dataset shape, metrics, backend spec, the int8-loss A/B, and the
  measure-before-shipping rule (plus the optional CI eval smoke).

---

## Troubleshooting

### A `core/**` test fails only when `fastembed` is installed
The test picked up the real adapter instead of `HashEmbedding`. Pin
`HashEmbedding(dim=cfg.dim)` explicitly, or skip-gate the fastembed path.

### Tests pass alone but fail when run together
Leaked global state. Confirm `ENGRAM_DATA_DIR` (and any `ENGRAM_*` env you set) is
restored in `tearDown`, and that every `Store` is `.close()`d. Namespace
per-session markers by PID where hooks write them (see `test_hooks.py`).

### A patched/stubbed adapter test passes but the real path is broken
Outdated stub — the stub's method signature drifted from the real port. Cross-check
the stub against `core/distill.py` / `core/embedding.py`; `review` mode flags this.

### `engram eval` reports 0.0 across the board
Wrong backend spec or an empty dataset load. Confirm the spec parses
(`name[@model][+float]`) and that `bench/dataset.json` is present; run
`python3 bin/engram eval --backends hash` as the zero-dep sanity check.

### `coverage: command not found`
Coverage is opt-in and not in `requirements.txt`. `python3 -m pip install coverage`
into a scratch venv first (see `coverage` mode).

---

## Related

- [`.claude/rules/00-quality/02-testing.md`](../../rules/00-quality/02-testing.md)
  — the canonical rule pointer this skill expands on.
- [`memory-recall` skill](../../../plugins/engram/skills/memory-recall/SKILL.md) —
  consult memory/index before a wide search (shipped to installers; this skill is
  dev-only tooling and is not shipped).
- [DESIGN.md](../../../DESIGN.md) — architecture, the memory-lifecycle model, and
  the measured-not-assumed benchmark ethos the `bench` mode enforces.
