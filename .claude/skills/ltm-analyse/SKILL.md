---
name: ltm-analyse
description: Deep research and analysis for the claude-ltm plugin. Performs layer/architecture impact tracing, POEAA & layering audits, token/latency budget analysis, dependency mapping, risk assessment, and test/benchmark strategy across core/, bin/, adapters, hooks, and the MCP surface. Consults the ltm-memory recall/search_code/search_docs tools for prior decisions and indexed structure before widening to search. Use when tracing the impact of a change across layers, auditing code against the POEAA pattern map, reasoning about the token or latency budget, mapping dependencies before a PR, assessing risk, or planning tests. Do NOT use for simple file lookups, single-module questions, or reading one file — use recall/search_code then Read directly for those.
argument-hint: "impact <change> | audit <module> | budget <change> | deps <change|PR> | risk <change> | test <feature>"
user-invocable: true
disable-model-invocation: false
---

# LTM Analyse

Deep research and structured analysis for **claude-ltm** — the plugin in this repo.
Acts as a subject-matter analyst, combining codebase investigation with the plugin's
own long-term memory (`recall`) and code/docs index (`search_code` / `search_docs`)
to produce evidence-based findings.

> Dev-only. This skill maintains the plugin; it is **not** shipped to installers.

This skill provides 6 analysis workflows:
- **Architecture Impact** — trace how a change ripples across layers (bin composition roots → core domain/ports → adapters), hooks, the two retrieval surfaces (memory + index), and the MCP/CLI surface.
- **POEAA & Layering Audit** — check code against the POEAA pattern map and the layer-seam / stdlib-first-core contract.
- **Token & Latency Budget** — analyse a change against the two budgets (interactive tokens, hot-path latency) that the whole project optimises.
- **Dependency Mapping** — map prerequisite relationships between modules and PRs.
- **Risk Assessment** — evaluate risks (against the DESIGN.md risk register) before implementation.
- **Test & Benchmark Strategy** — plan the stdlib test approach and when to A/B with `ltm eval`.

## When to Use This Skill

Use when you need structured, evidence-based analysis that goes beyond reading one file:
- Tracing cross-layer impact of a proposed change
- Auditing a module against the POEAA pattern map and layering rules
- Reasoning about whether a change adds interactive tokens or hot-path latency
- Mapping dependencies between modules or PRs before starting work
- Assessing risk before a change to the hot path, the store, or a hook
- Planning what to test and whether a retrieval change needs `ltm eval`

**Do NOT use** for simple file lookups, single-module questions answered by one file,
or basic navigation. Consult `recall` / `search_code`, then `Read` directly.

## Arguments

```
/ltm-analyse impact Add a rerank stage to core/recall.py
/ltm-analyse audit core/store.py
/ltm-analyse budget Inject a second JIT block per prompt
/ltm-analyse deps PR #42
/ltm-analyse risk Change supersede_threshold default
/ltm-analyse test New drift-detection path in indexer
```

**Format:** `[workflow] <subject>`

### Invocation Patterns

| Pattern | Behaviour |
|---------|-----------|
| Workflow keyword + subject | Targeted: run the specified workflow on the given subject |
| PR reference (`PR #N`) | Fetch the PR/diff via `gh`, auto-select workflow(s) from its content |
| Free-form description | Inferred: skill selects the best workflow(s) for the request |

If the user gives just a description, infer the most appropriate workflow(s). Some
requests need several — a hot-path change usually needs both **Token & Latency
Budget** and **Risk Assessment**.

---

## The 7-Step Workflow Sequence

Every workflow follows these 7 steps, defined once here and applied to all 6 below.

### Step 1: Consult Memory & Index First

Before any broad search, apply the project's own memory-first discipline (the rule
`prefer_memory.py` enforces at runtime):

1. **`recall`** — prior decisions/facts for this project, with a calibrated verdict
   (`ok` / `low_confidence` / `no_memory`).
2. **`search_code`** / **`search_docs`** — ranked symbol/section outlines for the
   affected area (qualname + summary + freshness), not file contents.
3. **`get_symbol`** / **`get_doc_section`** — pull one exact span once search points
   at it, instead of reading whole files.

**Stop rule:** trust confident results and open only one or two files they point at
to confirm. Widen to Grep/Glob/Read/Explore only when recall is
`low_confidence`/`no_memory` or the index search is weak or empty. See
`references/knowledge-sources.md`.

### Step 2: Gather Context

Read relevant sources from `references/knowledge-sources.md` for the workflow:
- [README.md](../../../README.md) — layout, MCP tools, config keys, benchmark.
- [DESIGN.md](../../../DESIGN.md) — the two budgets, POEAA map, memory lifecycle, cache analysis, risk register.
- `.claude/rules/` — quality, tooling, and the architecture contract (layering, hooks, budgets).
- Sibling skills (`ltm-poeaa`, `ltm-test`) for pattern and testing depth.

Select by relevance — don't load everything for a single-module audit.

### Step 3: Research Codebase

Investigate the affected code directly with `search_code` → `get_symbol` first, then
`Glob`/`Grep`/`Read`/`Explore` to widen. The index outline is a starting point, not
ground truth — read actual source for findings that matter.

### Step 4: Check Index Freshness

`search_code` / `search_docs` results carry a **freshness** verdict
(`fresh` / `edited` / `stale` / `gone`). If matches for the affected files are
`edited`/`stale`/`gone`, the outline has drifted from disk — re-index before trusting
it:

```
index_docs        # MCP tool — (re)index the current project's code + docs
```

Skip this step when the analysis only touches files you will `Read` directly.

### Step 5: Cross-Reference Sources

Validate findings against multiple sources. For any finding rated Medium or High
severity, verify against at least 2 independent sources (e.g. code + a rules file, or
code + DESIGN.md). Flag contradictions explicitly.

### Step 6: Validate Findings

Every finding must have evidence. Consult `references/quality-checklist.md`. Key bar:
- Every finding has a specific file path (not module-level).
- Every code reference has a line number, verified against the current file.
- Claims are checked against actual code, not the index summary or memory alone.
- Severity ratings are internally consistent.

### Step 7: Produce Structured Output

Use the appropriate template from `references/analysis-templates.md`. Output is
presented inline to the user. This repo has no ticket system and no
`docs/generated/analyses/` — do not auto-save; save to a file only if the user asks.

---

## Workflow 1: Architecture Impact

**When to use:** Tracing how a proposed change affects other layers, hooks, the two
retrieval surfaces, or the MCP/CLI surface. Use for cross-cutting changes, new
adapters, or refactors that move or rename shared code in `core/`.

**Trigger phrases:** "impact", "what does this affect", "cross-layer", "ripple effect", "breaking change", "trace the impact"

### What to Research

1. **Affected layers** — Which composition roots (`bin/*`), core modules
   (`core/*.py`), and adapters (`core/adapters/*`) import or depend on the changed
   code? Use `search_code` then `Grep` for import references.
2. **Layer-seam crossings** — Does the change make `core/` import from `bin/`, or pull
   a heavy third-party dep into `core/` import time? Both violate the seam (see
   [02-architecture/01-poeaa-and-layers.md](../../rules/02-architecture/01-poeaa-and-layers.md)).
3. **Retrieval-surface impact** — Does it change what memory stores/injects
   (`store.py`, `service.py`, `recall.py`) or what the index serves
   (`indexer.py`, `index_recall.py`, `code_symbols.py`)? Trace both surfaces.
4. **Hook & interface impact** — If a hook contract, config key, MCP tool, or CLI
   subcommand changes, identify every consumer (`hooks/hooks.json`, `bin/mcp_server.py`, `bin/ltm`).

**recall / index:** `recall("why does <module> do X")`; `search_code("store repository")`; `search_docs("cache prefix")`

### Output

Use the **Architecture Impact** template. Include: Affected Layers table, Layer-Seam
Check, Retrieval-Surface Trace, Breaking Changes list.

---

## Workflow 2: POEAA & Layering Audit

**When to use:** Checking code against the POEAA pattern map and the layering /
stdlib-first-core contract. Use after adding a module or adapter, before PR review, or
when refactoring `core/`.

**Trigger phrases:** "audit", "pattern check", "does this follow POEAA", "layering", "is this the right pattern", "compliance"

### What to Research

1. **Pattern source** — The canonical map in [DESIGN.md § POEAA / Cosmic Python](../../../DESIGN.md) and [02-architecture/01-poeaa-and-layers.md](../../rules/02-architecture/01-poeaa-and-layers.md). Invoke [`/ltm-poeaa`](../../skills/ltm-poeaa/SKILL.md) for the full catalogue.
2. **Repository, not Active Record** — All memory access goes through `core/store.py`; facts are plain data + a mapper, not self-persisting.
3. **Gateway + Separated Interface** — Heavy deps (`fastembed`) import only in `core/adapters/*`, lazily; the core depends on the interface in `core/embedding.py`, never on `import fastembed`.
4. **Functional Core / Imperative Shell** — Distillation, ranking, scoring, quantisation are pure over data; I/O lives in the shell. Query Object for `search` params. Null Object for empty recall (`render_block` returns `""`).
5. **Composition roots wire, don't compute** — `bin/*` reads config, picks adapters, calls the core; business logic must not live in a hook script.

**recall / index:** `recall("why Repository not Active Record")`; `search_code("adapter fastembed")`; `search_docs("POEAA")`

### Output

Use the **POEAA & Layering Audit** template. Include: Violations Table (with fix
each), Compliance Score, Pattern Source used.

---

## Workflow 3: Token & Latency Budget

**When to use:** Reasoning about a change against the two budgets the whole project
optimises. Use for anything touching recall injection, capture, hooks, embedding, or
ranking.

**Trigger phrases:** "token budget", "latency", "hot path", "cache", "interactive cost", "detached", "how many tokens", "performance"

### What to Research

1. **Token budget (injection)** — Does the change add tokens that reach the context
   window? Recall injection is threshold-gated (`min_sim`) and capped
   (`top_k`, `max_chars`); the JIT block is per-prompt, the core block is per-session.
   Verify caps still hold and irrelevant turns still cost zero.
2. **Cache-prefix impact** — `SessionStart` injection joins the cached prefix (cheap
   every later turn); `UserPromptSubmit` injection sits at the tail (never a same-turn
   cache hit). Putting per-turn-varying content at `SessionStart` busts the prefix.
   See [DESIGN.md § Cache efficiency](../../../DESIGN.md).
3. **Latency budget (hot path)** — Recall is brute-force cosine over int8 vectors,
   sub-10ms. Is any embedding/distillation on the interactive path? It must be
   **detached** (capture worker) or served by the resident daemon; the hook must fall
   back in-process and respect the 5s ceiling.
4. **Measure it** — Any change to embeddings, ranking, quantisation, fusion, or
   distillation is A/B'd with `python3 bin/ltm eval` before it ships (Recall@1/@3,
   MRR@10, bytes/fact).

**recall / index:** `recall("cache prefix decision")`; `search_code("render_block min_sim top_k")`; `search_docs("token efficiency")`

### Output

Use the **Token & Latency Budget** template. Include: Token Budget Check,
Cache-Prefix Check, Latency / Detachment Check, Benchmark Requirement.

---

## Workflow 4: Dependency Mapping

**When to use:** Mapping prerequisite relationships between modules or PRs,
identifying blocking work, and verifying implementation sequencing.

**Trigger phrases:** "dependencies", "what blocks this", "sequencing", "prerequisites", "order", "depends on"

### What to Research

1. **Code dependencies** — Import relationships between the affected module and the
   rest of `core/` / `bin/` / `adapters/`. Respect the inward-pointing seam.
2. **PR dependencies (GitHub)** — For a PR reference, fetch it with `gh pr view` /
   `gh pr diff`. Check the description and diff for work it builds on. There is no
   Linear/Slack integration — GitHub only.
3. **Sequencing** — If the change spans phases, verify no earlier step depends on
   work done in a later one (e.g. an adapter that assumes an interface not yet added).
4. **Prior work** — Use `recall` and `git log` for related completed work this builds
   on.

**recall / index:** `recall("did we already do <thing>")`; `recall("prerequisite for <feature>")`; `search_code("<interface the change depends on>")`

### Output

Use the **Dependency Mapping** template. Include: Prerequisite Graph, Blocking Work
table, Sequencing Constraints, Coherence Check.

---

## Workflow 5: Risk Assessment

**When to use:** Evaluating risks before implementation. Use for hot-path changes,
store/schema changes, hook changes, or changes to a default that shifts recall
behaviour.

**Trigger phrases:** "risk", "what could go wrong", "breaking change risk", "rollback plan", "risk assessment"

### What to Research

Map the change onto the [DESIGN.md § Risks](../../../DESIGN.md) register:

1. **Hot-path latency** — Could it add interactive latency? Mitigations: local
   embedding + daemon, 5s timeout, fail open.
2. **Hook fail-open** — Could the change let a hook raise into a turn? Every hook must
   exit 0 and inject nothing on error (see [02-architecture/02-hooks-and-budgets.md](../../rules/02-architecture/02-hooks-and-budgets.md)).
3. **Recall pollution** — Could it inject irrelevant facts? Guarded by `min_sim`,
   `top_k`, `max_chars`, project scoping.
4. **Cross-project leakage** — Does it touch project scoping? Recall is project-scoped
   by default; fallback is penalised and opt-in.
5. **Store growth / stale facts / over-eager supersession** — recency decay +
   supersession + TTL sweep; superseded/expired rows are archived (reversible), not
   deleted. A default change (e.g. `supersede_threshold`) can retire distinct facts.
6. **Rollback** — Can it be reverted with `git revert`? Any irreversible store
   migration?

**recall / index:** `recall("risk of <change>")`; `recall("why fail-open")`; `recall("supersession threshold rationale")`

### Output

Use the **Risk Assessment** template. Include: Risk Matrix (impact × likelihood),
Mitigation Strategies for High/Critical, Rollback Plan, Overall Risk Rating.

---

## Workflow 6: Test & Benchmark Strategy

**When to use:** Planning the testing approach for a feature, refactor, or fix, and
deciding whether a change needs the retrieval benchmark.

**Trigger phrases:** "test strategy", "how to test", "what tests needed", "benchmark", "ltm eval", "coverage"

### What to Research

1. **Existing patterns** — Read `plugins/ltm/tests/` for structure, fixtures, and the
   stdlib `unittest` conventions. Invoke [`/ltm-test`](../../skills/ltm-test/SKILL.md) for depth.
2. **Stdlib-first** — Tests for `core/**` must run without `fastembed` or network. Use
   the `hash` embedding + `heuristic` distiller, or a stub. Flag any test that would
   need a live model in the default path.
3. **Fail-open as a test target** — A hook or adapter given broken input, a missing
   dep, or a dead daemon must still exit 0 / fall back. Assert it.
4. **Benchmark trigger** — Any change to embeddings, ranking, quantisation, fusion, or
   distillation is A/B'd with `python3 bin/ltm eval` before it ships. Define the
   backends to compare and the metric that must not regress (Recall@1/@3, MRR@10).

**recall / index:** `recall("test pattern for <module>")`; `recall("benchmark decision")`; `search_code("test hash embedding stub")`

### Output

Use the **Test & Benchmark Strategy** template. Include: Recommended Test Types, Test
Cases table, Edge Cases, Stdlib/Fail-Open Constraints, Benchmark Requirement (if
applicable).

---

## Output Format

Results are presented inline, following the template from
`references/analysis-templates.md`. This repo has no ticket system — analyses are not
auto-saved; save to a file only if the user asks. Before presenting, run the
post-analysis checklist in `references/quality-checklist.md`: every evidence-table
entry has a file path and line number, recommendations are actionable, severity
ratings are consistent, and the budget impact (tokens and/or latency) is stated for any
hot-path, recall, or capture change.

---

## claude-ltm Project Context

**Layers:**

| Layer | Path | Role |
|-------|------|------|
| Composition roots | `plugins/ltm/bin/` | Hook entry points, `ltm` CLI, MCP server, daemon — wire config → adapters → core |
| Core (domain + ports) | `plugins/ltm/core/` | `store`, `service`, `recall`, `distill`, `indexer`, `index_recall`, `scoring`, `fusion`, `embedding`, `quantize`, `project` — stdlib-only |
| Driven adapters | `plugins/ltm/core/adapters/` | Heavy optional deps (`fastembed`); the only place they import |
| Tests / bench / viewer | `plugins/ltm/tests`, `bench`, `viewer` | stdlib suite, `ltm eval` benchmark, localhost browser |

**The two budgets:** interactive **tokens** (recall injection — gated + capped) and
hot-path **latency** (embed + search — sub-10ms, capture detached). Every change
states which it touches.

**Key contracts:** core imports the standard library only (`fastembed` / LLM
distillation are opt-in adapters); every hook fails open (exit 0, 5s ceiling); capture
/ distillation / embedding never run on the interactive path; memory access goes
through `core/store.py` (Repository, not Active Record); retrieval changes are A/B'd
with `ltm eval` before shipping; project identity is a marker-walk, not
`basename(cwd)`. The two retrieval surfaces — memory (`recall`) and the code/docs index
(`search_code`/`search_docs` → `get_symbol`/`get_doc_section`) — share the project key.

## Examples

### Example 1: Hot-path change

**User says:** `/ltm-analyse budget Add a float-rescore stage after int8 cosine`

**Result:** Runs Token & Latency Budget + Risk Assessment. Reads `core/recall.py` and
`core/quantize.py`, checks rescore stays off the interactive path (or within the 5s
ceiling), notes DESIGN.md already measured int8 ≈ float so rescore was judged not worth
building, and requires an `ltm eval` A/B before shipping. Cites file paths and lines.

### Example 2: Impact trace

**User says:** `/ltm-analyse impact Rename Store.add_facts`

**Result:** `search_code` for `add_facts` callers, confirms with `Grep`, produces an
Affected Layers table (`core/service.py`, `bin/capture.py`), a Layer-Seam Check, and a
Breaking Changes list noting the MCP/CLI paths that reach it.
