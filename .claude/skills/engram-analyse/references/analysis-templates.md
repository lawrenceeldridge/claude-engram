# Analysis Output Templates

Structured output templates for each of the 6 analysis workflows. Every template
follows the same skeleton: **Summary, Evidence Table, Detailed Findings,
Recommendations, Open Questions**. Adapt section depth to scope — a single-module
audit needs less than a cross-layer impact trace.

## Contents

- [Common Structure](#common-structure)
- [Architecture Impact](#architecture-impact)
- [POEAA and Layering Audit](#poeaa-and-layering-audit)
- [Token and Latency Budget](#token-and-latency-budget)
- [Dependency Mapping](#dependency-mapping)
- [Risk Assessment](#risk-assessment)
- [Test and Benchmark Strategy](#test-and-benchmark-strategy)

---

## Common Structure

All analysis outputs share this skeleton. Workflow-specific sections are inserted
between Detailed Findings and Recommendations.

```markdown
# Analysis: {Workflow} — {Subject}

**PR:** #{N} (if PR-linked) · **Date:** {YYYY-MM-DD} · **Scope:** {layers / modules} · **Analyst:** Claude (engram-analyse)

## Summary
{2-3 sentence executive summary of findings and recommendation}

## Evidence Table
| # | Finding | File | Line | Severity |
|---|---------|------|------|----------|
| 1 | {description} | {path} | {line} | {High/Medium/Low} |

## Detailed Findings
### Finding 1: {title}
{Description with code references. Every claim must have a file path and line number.}

## {Workflow-Specific Sections}
{See individual templates below}

## Recommendations
| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| 1 | {what to do} | {S/M/L} | {description} |

## Open Questions

- [ ] {Question that needs human input or further investigation}
```

---

## Architecture Impact

Use when tracing how a change ripples across layers, hooks, or the two retrieval
surfaces.

### Workflow-Specific Sections

#### Affected Layers

| Layer | Module(s) | Impact Type | Severity |
|-------|-----------|-------------|----------|
| Core | `core/service.py`, `core/store.py` | Signature change | High |
| Composition roots | `bin/capture.py` | Caller update | Medium |
| Adapters | `core/adapters/fastembed_gw.py` | No impact | None |

#### Layer-Seam Check

Confirm the change respects the inward-pointing seam (`bin/ -> core/ -> adapters/`).

| Check | Result | Evidence |
|-------|--------|----------|
| `core/` does not import from `bin/` | Pass | — |
| No heavy dep at `core/` import time | Pass | `fastembed` still lazy in `core/adapters/` |
| Composition roots wire, don't compute | Pass | logic stays in `core/` |

#### Retrieval-Surface Trace

Trace the change across the two surfaces (memory + index). Use ASCII for portability.

```
[capture]  bin/capture.py -> core/service.add_facts -> core/store.py (memory.db)
[recall]   bin/recall_prompt.py -> core/recall.search -> render_block (inject)
[index]    bin/index_edit.py -> core/indexer -> core/index_recall.search_code
```

#### Breaking Changes

| Change | Affected Consumers | Migration Required | Backwards Compatible |
|--------|-------------------|--------------------|----------------------|
| {description} | {bin / MCP / CLI callers} | Yes/No | Yes/No |

---

## POEAA and Layering Audit

Use when checking code against the POEAA pattern map (DESIGN.md) and the layering
contract (`.claude/rules/02-architecture/`).

### Workflow-Specific Sections

#### Violations Table

| # | File | Rule / Pattern | Violation | Fix |
|---|------|----------------|-----------|-----|
| 1 | `core/adapters/foo.py` | Separated Interface | Leaks `fastembed` type into `core/` signature | Return a plain vector; keep the dep in the adapter |
| 2 | `bin/recall_prompt.py` | Composition roots wire, don't compute | Ranking logic inline in the hook | Move to `core/recall.py` |

#### Compliance Score

| Category | Total Rules | Pass | Fail | Score |
|----------|-------------|------|------|-------|
| Repository (no Active Record) | 3 | 3 | 0 | 100% |
| Separated Interface (deps in adapters) | 4 | 3 | 1 | 75% |
| Functional core / imperative shell | 5 | 4 | 1 | 80% |
| **Overall** | **12** | **10** | **2** | **83%** |

#### Pattern Source

Document which source was used for the audit:
- Primary: [DESIGN.md POEAA / Cosmic Python](../../../../DESIGN.md) and `.claude/rules/02-architecture/01-poeaa-and-layers.md`
- Depth: the [`engram-poeaa`](../../engram-poeaa/SKILL.md) skill (catalogue, decision trees, anti-patterns)

---

## Token and Latency Budget

Use when analysing a change against the two budgets the project optimises: interactive
tokens and hot-path latency.

### Workflow-Specific Sections

#### Token Budget Check

| Injection point | Gated by | Capped by | Verdict |
|-----------------|----------|-----------|---------|
| `SessionStart` core | stable per project | `core_size` | Joins cache prefix — cheap |
| `UserPromptSubmit` JIT | `min_sim` | `top_k`, `max_chars` | Zero on irrelevant turns |

#### Cache-Prefix Check

Confirm per-turn-varying content is not injected at `SessionStart` (would bust the
cached prefix). See [DESIGN.md Cache efficiency](../../../../DESIGN.md).

| Content | Injected at | Varies per turn | Cache-safe |
|---------|-------------|-----------------|------------|
| {description} | SessionStart / UserPromptSubmit | Yes/No | Yes/No |

#### Latency / Detachment Check

| Operation | On interactive path | Detached / daemon | Fail-open |
|-----------|---------------------|-------------------|-----------|
| Query embed + cosine search | Yes (must be <10ms / 5s ceiling) | daemon warms model | falls back in-process |
| Distillation + capture | No | spawned worker | heuristic fallback |

#### Benchmark Requirement

If the change touches embeddings, ranking, quantisation, fusion, or distillation,
state the A/B plan.

| Backends to compare | Metric that must not regress | Command |
|---------------------|------------------------------|---------|
| `hash,fastembed` | Recall@3, MRR@10 | `python3 bin/engram eval --backends "hash,fastembed"` |

---

## Dependency Mapping

Use when mapping prerequisite relationships between modules or PRs and checking
sequencing constraints. GitHub only — no ticket system.

### Workflow-Specific Sections

#### Prerequisite Graph

```
Change X (new rerank stage)
  |-- depends-on: Query Object in core/recall.py::search  [present]
  |-- depends-on: fastembed adapter (real vectors)        [present]
  +-- optional: resident daemon — not blocking

Change Y (float-rescore)
  |-- depends-on: Change X (rerank stage) — BLOCKING
  +-- depends-on: engram eval A/B result     — BLOCKING
```

#### Blocking Work

| Item | Description | Status | Blocks | Resolution |
|------|-------------|--------|--------|------------|
| PR #40 | Query Object refactor | Merged | Change X | Resolved |
| PR #41 | Benchmark widening | Open | Change Y | In review |

#### Sequencing Constraints

| Constraint | Reason | Impact if Violated |
|------------|--------|--------------------|
| Interface before adapter | Adapter implements the port in `core/embedding.py` | Adapter references a missing protocol |
| Benchmark before default change | `engram eval` must confirm no regression | Ships an unmeasured recall regression |

#### Coherence Check

| Step | Prerequisites Met | Issues |
|------|-------------------|--------|
| 1 | Yes | — |
| 2 | Yes (step 1 interface) | — |

---

## Risk Assessment

Use when evaluating the risks of a proposed change before implementation, mapped onto
the [DESIGN.md Risks](../../../../DESIGN.md) register.

### Workflow-Specific Sections

#### Risk Matrix

| # | Risk | Likelihood | Impact | Severity | Mitigation |
|---|------|-----------|--------|----------|------------|
| 1 | {description} | High/Med/Low | High/Med/Low | {L x I} | {strategy} |

**Severity:** High x High = Critical, High x Med = High, Med x Med = Medium, anything
with Low = Low.

#### Mitigation Strategies

For each High or Critical risk:

```markdown
### Risk 1: {title}

**Trigger:** {what would cause this risk to materialise}
**Prevention:** {steps to prevent it}
**Detection:** {how to detect it early — e.g. engram eval regression, hook exit code}
**Response:** {what to do if it happens}
```

#### Rollback Plan

| Step | Action | Verification |
|------|--------|-------------|
| 1 | `git revert {commit}` | Tests pass (`python3 -m unittest discover -s tests`) |
| 2 | Confirm no irreversible store migration | Superseded/expired rows are archived, not deleted — reversible |
| 3 | Re-run `engram eval` if retrieval touched | No metric regression |

#### Overall Risk Rating

**Rating:** {Low / Medium / High / Critical}

**Rationale:** {1-2 sentences justifying the rating from the matrix}

---

## Test and Benchmark Strategy

Use when planning the testing approach for a feature, refactor, or fix, and deciding
whether a change needs the retrieval benchmark.

### Workflow-Specific Sections

#### Recommended Test Types

| Type | Scope | Framework | Backend |
|------|-------|-----------|---------|
| Unit | Pure functions (scoring, quantize, distill) | stdlib `unittest` | `hash` / stub |
| Integration | Store + service + recall round-trip | `unittest` | `hash` (no network) |
| Fail-open | Hook / adapter with broken input or dead daemon | `unittest` | stub |
| Benchmark | Recall quality through the quantised search path | `engram eval` | `hash` vs `fastembed` |

#### Test Cases

| # | Test | Type | File | Description |
|---|------|------|------|-------------|
| 1 | test_supersede_retires_older | Integration | `tests/test_service.py` | New near-identical fact archives the old one |
| 2 | test_render_block_empty | Unit | `tests/test_recall.py` | No match injects `""` (Null Object) |

#### Edge Cases

| # | Edge Case | Why It Matters | Test Approach |
|---|-----------|---------------|---------------|
| 1 | Dead daemon | Recall must not break a turn | Assert fall-back to in-process, exit 0 |
| 2 | Missing `fastembed` | Core stays stdlib-only | Assert `hash` path still works |
| 3 | Vocabulary-disjoint conflict | Similarity supersession can't catch it | Assert LLM-distiller `supersedes` link (or documented limit) |

#### Stdlib / Fail-Open Constraints

- Tests for `core/**` must run without `fastembed` or network — use `hash` + `heuristic` or a stub.
- Every hook/adapter test asserts exit 0 / graceful fallback on error.

#### Benchmark Requirement

For any change to embeddings, ranking, quantisation, fusion, or distillation:

| Metric | Target | How to Measure |
|--------|--------|---------------|
| Recall@3 | >= current default (0.86) | `python3 bin/engram eval --backends "hash,fastembed"` |
| MRR@10 | no regression vs baseline | same, compare backends |
| bytes/fact | within storage budget | reported by `engram eval` |
