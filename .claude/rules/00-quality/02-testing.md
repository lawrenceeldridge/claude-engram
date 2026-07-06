---
alwaysApply: true
---

# Testing

claude-engram ships a **stdlib-first test suite** plus a **recall-quality benchmark**.
Operational depth (writing tests, coverage, anti-pattern review, the benchmark
harness) lives in the [`engram-test`](../../skills/engram-test/SKILL.md) skill.

| Surface | What it is | How to run |
|---|---|---|
| **Unit / integration** | `unittest`-discoverable suite (also runs under `pytest`); all stdlib, no network | `cd plugins/engram && python3 -m unittest discover -s tests` |
| **Recall benchmark** | Labelled paraphrase set through the real quantised search path — Recall@1/@3, MRR@10, bytes/fact | `cd plugins/engram && python3 bin/engram eval --backends "hash,fastembed"` |
| **Doctor** | Resolved config, project identity, fact counts | `python3 bin/engram doctor` |

## Rules

1. **Core stays stdlib-testable.** Tests for `core/**` must run without `fastembed`
   or any network. The default `hash` embedding + `heuristic` distiller make this
   possible — keep it that way.
2. **Measure retrieval changes.** Any change to embeddings, ranking, quantisation,
   fusion, or distillation is A/B'd with `engram eval` **before** it ships. Quantization
   loss, model choice, and fusion weights are all decisions the harness settled — see
   [DESIGN.md § Embedding backend — measured, not assumed](../../../DESIGN.md).
3. **Fail-open is a test target.** A hook or adapter given a broken input, a missing
   dep, or a dead daemon must still exit 0 / fall back — assert that, don't assume it.
4. **Fixtures are local.** No live embedding model in the default test path; use the
   `hash` backend or a stub.

## Invoke the skill for depth

- `/engram-test` — scaffold, write, review, and audit tests; run the benchmark and read
  the numbers.

## See also

- [`engram-test` skill](../../skills/engram-test/SKILL.md) — full operational depth.
- [DESIGN.md § Status of the levers](../../../DESIGN.md) — what is measured vs. remaining.
- `plugins/engram/tests/` — the suite. `plugins/engram/bench/` — the benchmark dataset.
