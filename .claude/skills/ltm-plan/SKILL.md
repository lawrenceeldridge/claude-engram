---
name: ltm-plan
description: Create implementation plans, progress trackers, and execute tracked work for the claude-ltm plugin. Four modes — "plan" creates a deeply-researched, budget-aware implementation plan; "tracker" derives a checkbox progress tracker from a plan; "run" executes tracker phases with verification (ruff + unittest, ltm eval for retrieval changes); "resume" continues from where work left off. Use when the user says "create a plan", "plan this change", "make a tracker", "run the tracker", "continue implementation", "resume <slug>", "execute phase", or references implementation planning for a change to core/, bin/, hooks, adapters, or the MCP/CLI surface. GitHub-only — plans/trackers are local markdown docs, tied to a GitHub issue/PR when one exists.
argument-hint: "plan <slug> | tracker <slug> | run <slug> [Phase N] | resume <slug>"
user-invocable: true
disable-model-invocation: false
metadata:
  author: Lawrence Eldridge
---

# LTM Plan & Tracker

Create implementation plans, build progress trackers, and execute tracked work for
the **claude-ltm** plugin. Plans and trackers are local markdown docs keyed by a
short slug (optionally a GitHub issue number), so the plan, its tracker, and any
GitHub issue/PR stay connected without a ticket system.

> Dev-only. This skill maintains the plugin; it is **not** shipped to installers.

This skill has four modes:
- **plan** — Deep codebase research + a budget-aware implementation plan.
- **tracker** — Progress tracker derived from an existing plan.
- **run** — Execute pending phases from a tracker, with verification.
- **resume** — Pick up where the last session left off.

## Arguments

```
/ltm-plan plan rerank-stage
/ltm-plan tracker rerank-stage
/ltm-plan run rerank-stage
/ltm-plan run rerank-stage Phase 3
/ltm-plan run rerank-stage Phase 3-5
/ltm-plan resume rerank-stage
```

**Format:** `<mode> <slug> [scope]`

- **mode** (required): `plan`, `tracker`, `run`, or `resume`
- **slug** (required): short kebab-case identifier for the work (e.g. `rerank-stage`,
  `issue-42-float-rescore`). Reuse the same slug across all four modes so the plan and
  tracker files match. When a GitHub issue exists, prefer `issue-<N>-<short-desc>`.
- **scope** (optional, run mode only): `Phase N`, `Phase N-M`, or `all`

If the user gives just a slug without a mode, infer from context — if no plan exists,
suggest creating one first; if a plan exists but no tracker, suggest the tracker.

## File Locations

| Artefact | Path |
|----------|------|
| Plans | `docs/generated/plans/plan-<slug>.md` |
| Trackers | `docs/generated/trackers/tracker-<slug>.md` |

These are **local dev artefacts**, not shipped and not part of `plugins/ltm/`. Create
the directories if they do not exist.

---

## Plan Mode

Creating a plan is the most research-intensive mode. The quality of a plan determines
the quality of implementation — rushing this step produces vague phases and missed
edge cases.

### Step P1: Establish Scope & Context

Gather the "what" and "why" before the "how":
- If a GitHub issue exists, read it: `gh issue view <N>` (title, body, acceptance).
- If the work comes from a PR review or discussion, read it: `gh pr view <N>`.
- Otherwise, take the goal from the user's request directly. There is no ticket system
  — a one-line goal statement is enough to anchor the plan.

State up front **which budget the change touches** — interactive **tokens** (injected
text) or hot-path **latency** (hook wall-clock) — since that framing shapes the whole
plan. See [DESIGN.md § The one constraint](../../../DESIGN.md).

### Step P2: Deep Codebase Research (memory & index first)

This is what separates a useful plan from a hand-wavy one. Before writing anything,
investigate the affected code thoroughly — but the *cheap* way first.

**Memory & index first** (the discipline `prefer_memory.py` enforces at runtime):
1. **`recall`** — prior decisions, rationale, "did we already try this", with a
   calibrated verdict (`ok` / `low_confidence` / `no_memory`).
2. **`search_code`** / **`search_docs`** — ranked symbol/section outlines for the
   affected area (qualname + summary + freshness), not file bodies.
3. **`get_symbol`** / **`get_doc_section`** — pull one exact span once search points at it.

**Stop rule:** trust confident results and open only one or two files to confirm.
Widen to Grep/Glob/Read/Explore only when recall is `low_confidence`/`no_memory` or the
index search is weak or empty.

**If the `ltm-analyse` skill is available**, delegate the heavy research:
1. Invoke `/ltm-analyse impact <change>` (and/or `budget`, `risk`, `test`) for the change.
2. It runs Architecture Impact, Token & Latency Budget, Risk Assessment, etc.,
   producing evidence tables with file paths and line numbers.
3. Feed that output into the plan's Current State, Budget & Constraints, and Risk sections.

**Otherwise, fall back to inline research:**
- Read the affected layers: composition roots (`plugins/ltm/bin/*`), core
  (`plugins/ltm/core/*.py`), adapters (`plugins/ltm/core/adapters/*`), hooks
  (`plugins/ltm/hooks/hooks.json`), and `tests/` / `bench/` / `viewer/`.
- Read `.claude/rules/` for the affected area — especially
  [02-architecture/01-poeaa-and-layers.md](../../rules/02-architecture/01-poeaa-and-layers.md)
  (POEAA map + layer seams) and
  [02-architecture/02-hooks-and-budgets.md](../../rules/02-architecture/02-hooks-and-budgets.md)
  (fail-open + budgets).
- Check `plugins/ltm/tests/` for the test patterns the change will need.
- Check `docs/generated/plans/` for related prior plans.

**Spend real time here.** A plan that references specific file paths, the layer a change
lives in, and the budget it touches is far more useful than one that speaks in abstractions.

### Step P3: Draft the Plan

Consult [`references/plan-template.md`](references/plan-template.md) for the full template.
Key sections:

1. **Overview** — goal, the budget it touches, why it matters.
2. **Current State Analysis** — specific files, layers, patterns.
3. **Recommended Approach** — with at least one alternative considered.
4. **Budget & Constraints** — tokens vs latency, stdlib-first, fail-open, POEAA layering,
   write-side/read-side split, whether `ltm eval` must run. This section is the claude-ltm
   spine of the plan (mirrors the Development Workflow Checklist in
   [00-quality/01-code-refinement.md](../../rules/00-quality/01-code-refinement.md)).
5. **Detailed Implementation** — phased, step-by-step, with code examples.
6. **File Change Summary** — create/modify/delete table.
7. **Risk Assessment** — against the [DESIGN.md § Risks](../../../DESIGN.md) register.
8. **Open Questions** — unknowns that need human input.

### Step P4: Save and Present

Save to `docs/generated/plans/plan-<slug>.md`. Present the plan summary to the user and
wait for approval before creating a tracker or implementing.

---

## Tracker Mode

A tracker is derived from a plan. It converts the plan's phases and implementation steps
into discrete, checkable tasks.

### Step T1: Read the Plan

Read `docs/generated/plans/plan-<slug>.md`. If it doesn't exist, tell the user and
suggest creating one first: `/ltm-plan plan <slug>`.

### Step T2: Build the Tracker

Consult [`references/tracker-template.md`](references/tracker-template.md). Convert the plan:

- Each plan phase → tracker phase with a status row.
- Each implementation step → checkbox task.
- Add verification tasks at the end of each phase: `ruff check .`, `python3 -m unittest
  discover -s tests`, and — for any retrieval change — `python3 bin/ltm eval` (all from
  `plugins/ltm/`).
- Include a Notes & Decisions table for runtime decisions.
- Link back to the plan and to the GitHub issue/PR if one exists.

### Step T3: Save

Save to `docs/generated/trackers/tracker-<slug>.md`.

---

## Run Mode

Execute pending phases from a tracker, implementing the actual code changes.

### Step E1: Read Tracker and Plan

Read both `docs/generated/trackers/tracker-<slug>.md` and
`docs/generated/plans/plan-<slug>.md`. If either is missing, tell the user and suggest
the appropriate creation step.

### Step E2: Determine Scope

- **Explicit scope:** `Phase N` or `Phase N-M` — execute those.
- **No scope:** find the next pending phase and execute it.
- **`all`:** execute all pending phases sequentially.

### Step E3: Gather Context

Before implementing, gather context — cheap sources first:

**Memory & index (always, first):** `recall` for prior decisions and gotchas on the
affected modules; `search_code` / `search_docs` for the exact symbols/sections the phase
touches. This is cross-session continuity: capture from earlier sessions surfaces here.
If the `ltm-memory` MCP server is unavailable, note it and proceed with a normal search.

**Analysis (best-effort):** if `ltm-analyse` is available and the phase touches
unfamiliar code, invoke it for targeted impact/budget analysis. If unavailable, log
"analyse skill unavailable — proceeding with manual code reading." and continue.

**Read the code (always):** read the files the phase will touch. Understand the existing
patterns and the layer they live in before modifying them.

### Step E4: Execute Phase

For each phase in scope:

1. Mark the phase **In Progress** in the tracker's Status Summary.
2. For each task:
   - Implement the change (respect the stdlib-first core, fail-open hooks, and POEAA
     layer seams — see Budget & Constraints in the plan).
   - Mark the task complete (`- [x]`) and update the tracker file after each task
     (real-time progress).
   - **Log decisions:** on any non-obvious choice (deviated from the plan, chose between
     alternatives, discovered something unexpected, adjusted a target, a budget trade-off),
     add a dated row to the **Notes & Decisions** table immediately — don't batch.
3. Run verification from `plugins/ltm/`: `ruff check . && ruff format .`, then
   `python3 -m unittest discover -s tests`. For any change to embeddings, ranking,
   quantisation, fusion, or distillation, also run `python3 bin/ltm eval` and record the
   Recall@1/@3 / MRR@10 delta — a retrieval change must not ship without this A/B.
4. Mark the phase **Complete** with 100% progress and update overall progress.
5. **Write the Phase Report** — mandatory; see Step E4a.

### Step E4a: Phase Report (Mandatory)

A phase is **not finished** until its phase report is recorded. Do this immediately after
updating the tracker — before asking the user about the next phase.

1. **Build the report** following [`references/phase-report-template.md`](references/phase-report-template.md):
   - Phase number and name.
   - Actions performed (files created/modified/deleted).
   - Budget impact (which budget the phase touched; benchmark delta if retrieval changed).
   - Deviations from plan, each with Reason, Impact, and Downstream effect.
   - Verification results (`ruff`, `unittest`, `ltm eval` pass/fail).
2. **Record it (GitHub-only):**
   - If a GitHub issue exists for the slug: `gh issue comment <N> --body-file <tmpfile>`.
   - If the work is on a PR: `gh pr comment <N> --body-file <tmpfile>`.
   - Otherwise, present the report **inline** to the user and note it wasn't posted
     anywhere. There is no Linear or Slack — GitHub or inline are the only targets.
3. Append a one-line summary to the tracker's Notes & Decisions table so the report is
   also discoverable from the tracker.

### Step E5: Handle Blockers

If a task is blocked:
1. Note it in the task: `- [ ] Task — **BLOCKED:** reason`
2. Add a Notes & Decisions entry.
3. Skip to the next unblocked task.
4. Report the blocker to the user; mark the phase **Blocked** in the Status Summary.
5. If a GitHub issue exists, note the blocker with `gh issue comment <N>` so it's visible.

---

## Resume Mode

Resume is a convenience wrapper around run mode. It reads the tracker, finds where work
stopped, and continues.

### Step R1: Read Tracker

Read `docs/generated/trackers/tracker-<slug>.md`. Parse the Status Summary to find:
- Phases marked **In Progress** (continue from the first incomplete task).
- The first **Pending** phase after all **Complete** phases (start it next).
- Any phase marked **Blocked** — surface the recorded blocker and ask the user whether
  it's resolved before continuing.

### Step R2: Report Status

Tell the user which phases are complete, where work will resume, and any blockers noted.

### Step R3: Execute

Follow the Run Mode workflow (Steps E1–E5) from the identified resume point.

---

## claude-ltm Context

**Layers (respect the seams — dependencies point inward):**

| Layer | Path | Role |
|-------|------|------|
| Composition roots | `plugins/ltm/bin/` | Hook entry points, `ltm` CLI, MCP server, daemon — wire config → adapters → core |
| Core (domain + ports) | `plugins/ltm/core/` | `store`, `service`, `recall`, `distill`, `indexer`, `index_recall`, `scoring`, `fusion`, `embedding`, `quantize`, `project` — **stdlib-only** |
| Driven adapters | `plugins/ltm/core/adapters/` | Heavy optional deps (`fastembed`) — the only place they import |
| Tests / bench / viewer | `plugins/ltm/tests`, `bench`, `viewer` | stdlib suite, `ltm eval` benchmark, localhost browser |

**Key conventions (a plan phase must honour these):**
- **Two budgets, stated explicitly** — tokens (injected text) vs latency (hook wall-clock);
  each change says which it touches and why it's still a net win.
- **Stdlib-first core** — `core/**` imports the standard library alone; `fastembed` / LLM
  distillation are opt-in adapters that self-provision a venv (never `import fastembed` at core import).
- **Hooks fail open** — exit 0, inject nothing on any error, 5s ceiling. **Capture is detached** — distillation/embedding never on the interactive path.
- **CQRS split** — capture (write) is heavy/batch/detached; recall (read) is tiny/hot-path/gated. Keep changes on the correct side.
- **POEAA layering** — Repository over Active Record (`core/store.py`), Gateway + Separated
  Interface for embeddings, Query Object, Functional Core / Imperative Shell.
- **Measure retrieval changes** — embeddings/ranking/quantisation/fusion/distillation are
  A/B'd with `python3 bin/ltm eval` before shipping.
- **Verification** (from `plugins/ltm/`): `ruff check . && ruff format .`, `python3 -m unittest discover -s tests`.
- **GitHub-only** — conventional commits scoped `feat(ltm):`, `fix(core):`, etc.; no Linear/Slack.

## Examples

- **`/ltm-plan plan rerank-stage`** — Consults `recall` + `search_code` for prior ranking
  decisions, delegates the latency check to `/ltm-analyse budget`, saves a phased,
  budget-aware plan to `docs/generated/plans/plan-rerank-stage.md`.
- **`/ltm-plan tracker rerank-stage`** — Reads the plan, converts phases into checkbox
  tasks with `ruff` / `unittest` / `ltm eval` verification, saves the tracker.
- **`/ltm-plan run rerank-stage Phase 1`** — Marks Phase 1 In Progress, implements each
  task, runs verification (including `ltm eval` for the ranking change), marks it Complete,
  records a phase report.
- **`/ltm-plan resume rerank-stage`** — Finds Phase 2 In Progress at 3/8 tasks, reports
  status, continues from task 4.
