# Implementation Plan Template

This reference defines the plan structure for the **claude-ltm** plugin. Every plan
created by the `ltm-plan` skill should follow this template. Plans are local dev
artefacts — not shipped and not part of `plugins/ltm/`.

---

## File Naming

Plans are saved to `docs/generated/plans/` using the work's slug:

- **Format:** `plan-<slug>.md` (e.g. `plan-rerank-stage.md`, `plan-issue-42-float-rescore.md`)
- Reuse the same slug for the plan, the tracker, and (when one exists) the GitHub issue.
- When a GitHub issue anchors the work, prefer `issue-<N>-<short-desc>`.

---

## Plan Template

```markdown
# Plan: <slug> — <Short Title>

**Goal:** <One-line description>
**GitHub:** <#issue / PR link, or "none — direct request">
**Status:** Draft
**Budget touched:** <tokens (injected text) | latency (hook wall-clock) | both | neither>
**Affected layers:** <bin/ (composition roots) | core/ | core/adapters/ | hooks | tests/bench/viewer>
**Progress Tracker:** [tracker-<slug>.md](../trackers/tracker-<slug>.md)

---

## Overview

<2-3 paragraphs: the goal, which budget it touches and why it stays a net win, and why
this work matters. If a GitHub issue exists, reference it for background.>

---

## Current State Analysis

<What exists today. Reference specific files, the layer each lives in, and current
patterns. Use tables to summarise before/after where helpful. Note which retrieval
surface is affected — memory (store/service/recall) or the code/docs index
(indexer/index_recall/code_symbols).>

---

## Recommended Approach

<High-level strategy with at least one alternative considered. Explain why this approach
was chosen. Reference project conventions from `.claude/rules/` and the POEAA map in
DESIGN.md.>

**Phases:**
1. Phase 1: <name> — <one-line description>
2. Phase 2: <name> — <one-line description>
3. ...

---

## Budget & Constraints

The claude-ltm spine of the plan — how it satisfies the Development Workflow Checklist in
`.claude/rules/00-quality/01-code-refinement.md`:

| Constraint | This change |
|------------|-------------|
| **Budget touched** | Tokens (injected text) / latency (hook wall-clock)? Still a net win because… |
| **Behaviour parity** | Does it preserve recall/capture/index behaviour, injected text, and ranking order? |
| **Stdlib-first core** | Does `core/**` still import with the standard library alone? Any dep behind an adapter? |
| **Fail-open** | If it touches a hook, does it exit 0 / inject nothing on error, within the 5s ceiling? |
| **CQRS side** | Write-side (capture, detached) or read-side (recall, hot-path)? Kept on the correct side? |
| **POEAA layering** | Repository (not Active Record), Gateway + Separated Interface for deps, layer seams intact? |
| **Benchmark** | Does it touch embeddings/ranking/quantisation/fusion/distillation? Then `ltm eval` A/B is required. |

---

## Detailed Implementation

### Phase 1: <Phase Name>

<What this phase accomplishes and which layer(s) it touches.>

#### 1.1 <Task Group>
- Step-by-step implementation details
- Reference specific files to create/modify (e.g. `core/recall.py`, `bin/recall_prompt.py`)
- Include code examples for non-obvious patterns

#### 1.2 <Task Group>
...

### Phase 2: <Phase Name>
...

---

## File Change Summary

| Action | File | Layer | Description |
|--------|------|-------|-------------|
| Create | `core/adapters/new_gw.py` | adapters | Description |
| Modify | `core/recall.py` | core | What changes |
| Delete | `bin/old_hook.py` | bin | Why removed |

---

## Risk Assessment

Map onto the DESIGN.md risk register (hot-path latency, hook fail-open, recall pollution,
cross-project leakage, store growth / over-eager supersession, distillation quality).

| Risk | Severity | Mitigation |
|------|----------|------------|
| <Risk description> | High/Medium/Low | <How to mitigate> |

---

## Open Questions

- [ ] <Question that needs answering before or during implementation>
- [ ] <Another question>
```

---

## Section Guide

### Overview
State the goal and, up front, **which budget the change touches** — tokens (injected text)
or latency (hook wall-clock). See [DESIGN.md § The one constraint](../../../../DESIGN.md).
If a GitHub issue exists, reference it for background.

### Current State Analysis
Demonstrates codebase research was done. Include file paths, the layer each file lives in
(`bin/` / `core/` / `core/adapters/`), current patterns, and which retrieval surface
(memory vs the code/docs index) is affected. Prefer `search_code` → `get_symbol` over
reading whole files.

### Recommended Approach
Always consider at least one alternative. Explain trade-offs. Reference the POEAA map in
[DESIGN.md § POEAA / Cosmic Python](../../../../DESIGN.md) and the layer seams in
`.claude/rules/02-architecture/01-poeaa-and-layers.md`.

### Budget & Constraints
The section that makes a plan *this project's*. Fill in every row of the table honestly —
if a change adds interactive tokens or hot-path latency, justify it; if it touches
retrieval, commit to an `ltm eval` A/B before shipping.

### Detailed Implementation
The core of the plan — enough detail that a developer (or the tracker) can execute
step-by-step. Each phase should be independently completable and verifiable. Include
specific file paths, code examples for non-obvious patterns, and verification steps
(`ruff check .`, `python3 -m unittest discover -s tests`, and `python3 bin/ltm eval` for
retrieval changes — all run from `plugins/ltm/`).

### File Change Summary
A quick-reference table with the layer column. Useful for review and for the tracker to
verify completeness against the layer seams.

### Risk Assessment
Focus on risks that could block or delay implementation, mapped onto the
[DESIGN.md § Risks](../../../../DESIGN.md) register. Include severity and concrete mitigation.

### Open Questions
Things that need human input before proceeding — often surfaced during research
(e.g. "should this default change require a store migration?").

---

## Research Checklist

Before writing a plan, investigate — **memory & index first, then widen**:

1. **`recall`** — prior decisions/rationale for this area, with a verdict.
2. **`search_code` / `search_docs`** — ranked outlines for the affected symbols/sections.
3. **`get_symbol` / `get_doc_section`** — pull the exact spans that matter.
4. **GitHub** — read the issue/PR (`gh issue view` / `gh pr view`) if one anchors the work.
5. **Affected layers** — read the code in `bin/` / `core/` / `core/adapters/` / hooks it touches.
6. **Rules files** — `.claude/rules/02-architecture/` (POEAA, layers, fail-open, budgets)
   and `.claude/rules/00-quality/01-code-refinement.md` (the workflow checklist).
7. **Existing plans** — check `docs/generated/plans/` for related prior work.
8. **Tests / benchmark** — `plugins/ltm/tests/` for the test pattern; decide whether
   `ltm eval` is required.

Widen to Grep/Glob/Read/Explore only when recall is `low_confidence`/`no_memory` or the
index search is weak or empty. Consider delegating the heavy research to `/ltm-analyse`.
