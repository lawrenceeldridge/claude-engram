# Progress Tracker Template

This reference defines the tracker structure for the **claude-engram** plugin. Trackers are
created from implementation plans and track phase-by-phase progress. They are local dev
artefacts — not shipped and not part of `plugins/engram/`.

---

## File Naming

Trackers are saved to `docs/generated/trackers/` using the work's slug:

- **Format:** `tracker-<slug>.md` (e.g. `tracker-rerank-stage.md`)
- Must match the corresponding plan file: `plan-<slug>.md` ↔ `tracker-<slug>.md`

---

## Tracker Template

```markdown
# Progress Tracker: <slug> — <Short Title>

**Task:** <One-line description from plan>
**GitHub:** <#issue / PR link, or "none — direct request">
**Implementation Plan:** [plan-<slug>.md](../plans/plan-<slug>.md)
**Budget touched:** <tokens | latency | both | neither>
**Affected layers:** <bin/ | core/ | core/adapters/ | hooks | tests/bench/viewer>
**Branch:** `<type>/<slug>`  (type ∈ feat|fix|refactor|test|docs|chore)
**Status:** Planning

---

## Status Summary

| Phase | Status | Progress |
|-------|--------|----------|
| Phase 1: <name> | Pending | 0% |
| Phase 2: <name> | Pending | 0% |
| Phase 3: <name> | Pending | 0% |

**Overall Progress:** 0%

---

## Phase 1: <Phase Name>

<Brief description of what this phase accomplishes and which layer(s) it touches.>

### 1.1 <Task Group>

- [ ] Task description (reference the file, e.g. `core/recall.py`)
- [ ] Another task
- [ ] Verification: `ruff check . && ruff format .` (from `plugins/engram/`)
- [ ] Verification: `python3 -m unittest discover -s tests`
- [ ] Verification (retrieval changes only): `python3 bin/engram eval` — record Recall@1/@3, MRR@10 delta

### 1.2 <Task Group>

- [ ] Task description

---

## Phase 2: <Phase Name>
...

---

## Notes & Decisions

| Date | Decision | Rationale |
|------|----------|-----------|
| | | |

---

## Related Links

- [Implementation Plan](../plans/plan-<slug>.md)
- <GitHub issue/PR link, if any>
```

---

## How to Derive Phases from a Plan

1. Read the plan's **Detailed Implementation** section.
2. Each `### Phase N:` heading becomes a phase in the tracker.
3. Each `#### N.M <Task Group>` becomes a task group with checkboxes.
4. Convert implementation steps into discrete, checkable tasks.
5. Add verification tasks at the end of each phase (see below).

## Verification Tasks (per phase)

Every phase ends with verification, run from `plugins/engram/`:

- `ruff check . && ruff format .` — lint + format.
- `python3 -m unittest discover -s tests` — the stdlib test suite (no network).
- **Retrieval changes only** — `python3 bin/engram eval`. Any change to embeddings, ranking,
  quantisation, fusion, or distillation must be A/B'd; record the Recall@1/@3 and MRR@10
  delta in the task and confirm the target metric did not regress.
- **Hook changes** — confirm fail-open: broken input / missing dep / dead daemon still
  exits 0 and injects nothing.

## Status Values

| Status | Meaning |
|--------|---------|
| **Pending** | Not started |
| **In Progress** | Currently being worked on |
| **Complete** | All tasks checked, verified |
| **Blocked** | Cannot proceed — see Notes & Decisions |
| **Skipped** | Intentionally skipped — see Notes & Decisions |

## Progress Calculation

- Phase progress = (completed tasks / total tasks) × 100, rounded to nearest 5%.
- Overall progress = average of all phase progress values.
- Update both the Status Summary table and the Overall Progress line.

## Task Writing Guidelines

- Each task should be independently verifiable — a developer can check it off.
- Include the specific file path and its layer (`core/` vs `bin/` vs `core/adapters/`) when
  a task creates or modifies a file — the layer seam matters here.
- Group related tasks under numbered headings (1.1, 1.2, …).
- End each phase with the verification tasks above.
- If a task is blocked: `- [ ] Task — **BLOCKED:** reason`

## Notes & Decisions Guidelines

The Notes & Decisions table is a **runtime decision log** — populate it throughout
execution, not just at the end. Add a dated row whenever:

- You **deviate from the plan** (different approach, skipped step, changed scope).
- You **choose between alternatives** (e.g. adapter vs inline, int8 vs float rescore).
- You **discover something unexpected** (a symbol moved, a pattern doesn't match, a dep is missing).
- You **make a budget trade-off** (added tokens / latency and why it's still a net win).
- You **record a benchmark result** (`engram eval` before/after numbers for a retrieval change).
- You **adjust a target**, a task is **blocked** and skipped, or you make a **judgement call**
  a future reader would need to understand.
- You **record a phase report** (link or one-line summary — see `phase-report-template.md`).

Log decisions **as they happen** — don't batch. Each entry has the date, a concise
decision, and the rationale. This table is the permanent record of *why* implementation
diverged from the plan.
