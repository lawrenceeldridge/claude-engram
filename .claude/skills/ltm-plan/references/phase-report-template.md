# Phase Report Template

Template for the progress report recorded at phase completion during `ltm-plan` run mode.
claude-ltm is **GitHub-only** — a report is posted to a GitHub issue/PR comment when one
exists, otherwise presented inline. There is no Linear or Slack.

---

## When to Use

Build and record a report following this template at the end of each completed phase
(Step E4a, after marking the phase Complete in the tracker). A phase is not finished
until its report is recorded.

**Where to record it:**
- GitHub issue for the slug → `gh issue comment <N> --body-file <tmpfile>`
- Work is on a PR → `gh pr comment <N> --body-file <tmpfile>`
- Neither exists → present inline to the user and note it wasn't posted.

Also append a one-line summary to the tracker's Notes & Decisions table so the report is
discoverable from the tracker.

---

## Report Structure

```markdown
### Phase N: <Phase Name> — Complete

**Status:** Complete | **Duration:** <time if known>

#### Actions Performed
- Created: `core/adapters/new_gw.py` — <description>
- Modified: `core/recall.py` — <what changed>
- Deleted: `bin/old_hook.py` — <why removed>

#### Budget Impact
- **Budget touched:** <tokens (injected text) | latency (hook wall-clock) | both | neither>
- **Net win because:** <one line>
- **Benchmark (retrieval changes only):** `ltm eval` — Recall@1 X→Y, Recall@3 X→Y, MRR@10 X→Y

#### Deviations from Plan
<see below>

#### Verification
- `ruff check . && ruff format .`: Pass/Fail
- `python3 -m unittest discover -s tests`: Pass/Fail (N passed, M skipped)
- `python3 bin/ltm eval`: Pass/Fail (retrieval changes only)
- Fail-open check (hook changes only): <broken input / dead daemon still exits 0?>

#### Notes
<Any additional context — decisions made, edge cases discovered, etc.>
```

---

## Budget Impact Section

Always state which budget the phase touched — this is the claude-ltm framing that makes
the report meaningful. If a change added interactive tokens or hot-path latency, justify
why it's still a net win. If the phase touched embeddings, ranking, quantisation, fusion,
or distillation, the benchmark line is **required** — a retrieval change does not ship
without an `ltm eval` A/B.

If the phase touched neither budget (e.g. a test-only or viewer-only change), say so:
"Neither budget touched — <reason>."

---

## Deviations Section

**If no deviations:**

```markdown
#### Deviations from Plan
No deviations — phase executed as planned.
```

**If deviations occurred:**

```markdown
#### Deviations from Plan
- **Deviation:** <what changed from the plan>
  - **Reason:** <why the deviation was necessary>
  - **Impact:** <does this affect downstream phases, the budget, or a layer seam?>
  - **Downstream effect:** <phases, files, or the benchmark that may need updating>
```

Each deviation must include all three fields (Reason, Impact, Downstream effect). If the
impact is contained within the current phase, state "No downstream impact." If it affects
future phases, flag specifically which and how. Flag any deviation that shifted a budget
(added tokens/latency), crossed a layer seam (e.g. a dep leaked toward `core/`), or changed
the fail-open contract.

---

## Guidelines

- Keep the report concise — a reader should understand the phase outcome in 30 seconds.
- Use file paths relative to `plugins/ltm/` (e.g. `core/recall.py`, `bin/capture.py`) or
  the repo root for dev files (e.g. `.claude/skills/ltm-plan/SKILL.md`).
- Group related actions (e.g. "Created 3 adapter tests" rather than listing trivial creates).
- For a failed verification, include the actual output truncated to the key error lines.
- The Notes section is optional — omit it if there's nothing noteworthy beyond actions,
  budget, and deviations.
