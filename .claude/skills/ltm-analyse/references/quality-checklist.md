# Quality Checklist

Process checklists for ensuring analysis quality. Every workflow runs these in
sequence: pre-analysis, during-analysis, post-analysis.

## Contents

- [Pre-Analysis Checklist](#pre-analysis-checklist)
- [During-Analysis Checklist](#during-analysis-checklist)
- [Post-Analysis Checklist](#post-analysis-checklist)
- [Quality Principles](#quality-principles)
- [Common Quality Failures](#common-quality-failures)

---

## Pre-Analysis Checklist

Complete before starting any workflow's research phase.

- [ ] **Identify scope** — Which layers are affected? Composition roots (`bin/`), core (`core/`), adapters (`core/adapters/`), hooks, viewer, tests?
- [ ] **Consult memory & index first** — Run `recall` (note the verdict) and `search_code`/`search_docs` for the affected area before any broad search. (See `knowledge-sources.md`.)
- [ ] **Check the index is fresh** — If `search_code`/`search_docs` matches read `edited`/`stale`/`gone`, run `index_docs` before trusting the outline.
- [ ] **Identify the right workflow(s)** — Match the request to one or more of the 6 workflows. A hot-path change usually needs both Token & Latency Budget and Risk Assessment.
- [ ] **Read the change** — If PR-linked, `gh pr view` / `gh pr diff` to understand the "what" and "why" before assessing "how deep is the impact". GitHub only — no ticket system.
- [ ] **Select knowledge sources** — Pick the README/DESIGN sections and rules files relevant to the scope. Don't read everything.

---

## During-Analysis Checklist

Follow throughout the research and findings phase of every workflow.

### Evidence Requirements

- [ ] **Every finding has a file path** — No finding references "the store" without the file (e.g. `plugins/ltm/core/store.py`).
- [ ] **Every finding has a line number** — When citing code, include the line. Use `get_symbol` or `Read` to verify it is current.
- [ ] **Claims are verified against actual code** — Don't assume from the index summary, a symbol name, or memory. Read the source and confirm. Code changes between sessions.
- [ ] **Quotes are exact** — Copy signatures, names, and messages exactly from the source; don't paraphrase code.

### Cross-Referencing

- [ ] **Multiple sources consulted** — For any Medium/High finding, verify against at least 2 sources (e.g. code + a rules file, or code + DESIGN.md).
- [ ] **Contradictions flagged** — If DESIGN.md/rules say one thing and the code does another, flag it explicitly as a finding rather than silently choosing one.
- [ ] **Index/memory validated against code** — `search_code` outlines and `recall` facts are starting points, not ground truth. Confirm specifics by reading the source.

### Memory (ltm-memory) Integration

- [ ] **recall performed** — For every workflow, run at least one `recall` relevant to the topic and record its verdict (`ok`/`low_confidence`/`no_memory`).
- [ ] **Narrowed before reading** — Go `recall` -> `search_code`/`search_docs` -> `get_symbol`/`get_doc_section`. Don't read whole files when an outline + one span will do.
- [ ] **Historical decisions cited** — If `recall` surfaces a prior decision relevant to the analysis, cite it: "Per memory: {fact}."
- [ ] **Honest about provenance** — Say when an answer came from memory/index vs. a fresh search. Never present a fresh search as if it came from memory. If the MCP server is unavailable, note it and proceed with a direct search.

---

## Post-Analysis Checklist

Complete before presenting the analysis output.

### Completeness

- [ ] **All evidence-table entries have file paths and line numbers** — Every row must have a specific path and line, not a layer-level reference.
- [ ] **Recommendations are actionable** — Concrete ("Move ranking from `bin/recall_prompt.py:40` into `core/recall.py`"), not abstract ("improve layering").
- [ ] **Open questions are genuine** — Each needs human input or further investigation the analysis couldn't resolve. Don't pad.

### Budget Validation

- [ ] **Token-budget check** — For any change to recall injection, confirm the caps still hold (`min_sim`, `top_k`, `max_chars`) and irrelevant turns still cost zero.
- [ ] **Cache-prefix check** — Confirm per-turn-varying content is not injected at `SessionStart` (it would bust the cached prefix).
- [ ] **Latency / detachment check** — Confirm no embedding/distillation lands on the interactive path (must be detached or daemon-served) and the hook still fails open within the 5s ceiling.
- [ ] **Benchmark trigger** — If the change touches embeddings, ranking, quantisation, fusion, or distillation, state the `ltm eval` A/B and the metric that must not regress.

### Cross-Layer Impact

- [ ] **Downstream consumers identified** — If a `core/` signature, config key, MCP tool, or CLI subcommand changes, identify every caller across `bin/`, the MCP server, and the CLI.
- [ ] **Layer-seam check** — Confirm the change doesn't make `core/` import from `bin/` or pull a heavy dep into `core/` import time (deps import only in `core/adapters/`).
- [ ] **Import dependency check** — If a symbol is moved or renamed, find all references with `search_code` + `Grep`.

### Output Quality

- [ ] **Summary is accurate** — Re-read it after finishing. Does it reflect the findings? Update if needed.
- [ ] **Severity ratings are consistent** — A "Low" finding shouldn't be more impactful than a "Medium" one.
- [ ] **Template followed** — Output matches the right template from `analysis-templates.md`; all required sections present, no placeholder text left.

---

## Quality Principles

These guide all analysis work. When in doubt, favour thoroughness over speed.

### 1. Thoroughness Over Speed

A shallow analysis that misses a critical finding is worse than a slower one that
catches everything. Read the files, verify claims, cross-reference. Depth is the point.

### 2. Never Skip Validation

Every claim must be backed by evidence from the current state of the code. Don't rely
on memory of what the code looked like last session — verify.

### 3. Verify Against Actual Code

The index outline, rules files, DESIGN.md, and `recall` facts are starting points, not
ground truth. They may be stale or incomplete. When a finding matters (Medium/High),
read the actual source (`get_symbol` / `Read`) to confirm.

### 4. Evidence Over Opinion

Findings are statements of fact with evidence, not subjective assessments. Instead of
"the layering is sloppy", say "`core/recall.py:88` imports `fastembed` directly,
violating the stdlib-only core contract in `.claude/rules/02-architecture/00-overview.md`."

### 5. Scope Appropriately

Match depth to the request. A single-module audit doesn't need a full cross-layer
impact trace; a hot-path change does. Don't pad with irrelevant sections.

### 6. State the Budget Impact

For any hot-path, recall, or capture change, say which budget it touches (tokens
and/or latency) and why it is still a net win — the project's central discipline.

### 7. Flag Uncertainty

If a finding is uncertain or rests on an assumption, say so. "This appears to run on
the interactive path, but the spawn in `bin/capture.py` would need to be confirmed" is
more useful than a confident claim that might be wrong.

---

## Common Quality Failures

Patterns to avoid in analysis output.

| Failure | Example | How to Avoid |
|---------|---------|-------------|
| Stale references | "The recall logic is in `search.py`" (it's `core/recall.py`) | Confirm with `search_code`/`Read`, not memory |
| Layer-level findings | "The core has layering issues" | Be specific: which file, which pattern, what violation |
| Missing line numbers | "`recall.py` skips the similarity gate" | Read the file, cite the line: `core/recall.py:88` |
| Assumed code state | "The distiller emits `supersedes` links" (from a prior session) | Read `core/distill.py` now to confirm |
| Placeholder recommendations | "Consider more tests" | Be specific: "Add a fail-open test for a dead daemon in `tests/test_daemon_client.py`" |
| Ignoring the budget | Proposing a hot-path embed with no latency note | State the token/latency impact for every hot-path change |
| Unmeasured retrieval change | Changing fusion weights without `ltm eval` | Require the A/B before the finding recommends shipping |
| Inventing tools | Citing `ltm index` or a non-existent MCP tool | Use only the tools in README.md / the MCP list |
| Over-scoping | Audit requested for `core/store.py`, analysis covers all of `core/` | Stick to the requested scope unless cross-layer impact is discovered |
| Ignoring memory | Skipping `recall` because it seems unnecessary | Always run it — prior decisions carry non-obvious rationale |
