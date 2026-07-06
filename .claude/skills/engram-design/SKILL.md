---
name: engram-design
description: Produces empirical, statistically-rigorous research-paper-structured design documents for claude-engram (systems designs, evaluation write-ups, and whitepapers). Use when the user asks to "write a design", "draft a design doc", "write a paper", "write the whitepaper", "turn this into a paper", "document the architecture properly", "write up the benchmark", "make this publication-ready", or any request for a long-form, evidence-grounded document with an abstract, hypotheses, a method, measured results, and a discussion. Enforces UK English, third-person hedged voice, a scientific-paper structure (Abstract, Introduction, Method, Results, Discussion, References/Appendices) mapped to systems research, statistical rigour (report n, effect sizes, confidence intervals, honest small-sample caveats), grounding in source at HEAD and reproducible `engram eval` output, and no AI tells. Optional PDF via pandoc + tectonic. Do NOT use for implementation planning (use engram-plan) or code-impact analysis (use engram-analyse).
argument-hint: "<paper-type> <subject> | <subject> (auto-detect type)"
user-invocable: true
disable-model-invocation: false
metadata:
  author: Lawrence Eldridge
---

# LTM Design — scientific design-document builder

This skill produces long-form, evidence-grounded design documents for **claude-engram**
in the register of an empirical research paper: an abstract, a literature-anchored
introduction with explicit hypotheses, a method, measured results with statistical
rigour, a discussion that evaluates the hypotheses and states honest limits, and
references plus appendices. The target is a document a serious reader would treat as a
whitepaper — the claude-engram equivalent of `bitcoin.org/bitcoin.pdf` — not a blog post
wearing a lab coat.

> **Dev-only.** This skill maintains the plugin's *documentation*; it is **not**
> shipped to installers. It touches neither the token budget nor the latency budget —
> it produces markdown (and, optionally, a PDF), never runtime code.

## Output locations

Three tiers, resolved deliberately (see [`references/pdf.md`](references/pdf.md)):

| Tier | Location | Committed? |
|---|---|---|
| Working drafts / research notes | `docs/generated/design-drafts/<slug>/` | No (gitignored) |
| Versioned archive (`.md` + `.pdf`) | `docs/whitepaper/<YYYY-MM-DD-HHMM>-<slug>.md` (+ `.pdf`) | Yes |
| Canonical published PDF | **repo root**, alongside `README.md` / `DESIGN.md` | Yes |

"Publish" = build the PDF, write the datetime-stamped `.md`+`.pdf` pair into
`docs/whitepaper/`, then copy the current `.pdf` to the repo root as the authoritative
version. `scripts/build_pdf.py --publish` does all three.

## When this skill applies

Triggers include, but are not limited to:

- "Write the claude-engram whitepaper" / "write a paper on X"
- "Write me a design for X" / "draft a design doc for X"
- "Turn this analysis (or benchmark) into a paper"
- "Document the architecture properly" / "write this up rigorously"
- "Write up the embedding-backend evaluation"
- "Make this publication-ready"

**Do NOT use** for:

- **Implementation planning** — a phased plan + tracker to *execute*. That is
  [`engram-plan`](../engram-plan/SKILL.md).
- **Code-impact / budget / risk analysis** — evidence-gathering across layers. That is
  [`engram-analyse`](../engram-analyse/SKILL.md) (this skill *consumes* its output).
- **Informal notes, READMEs, or short explainers** — plain markdown covers those. This
  skill is reserved for the finished, evidence-grounded, review-ready document.

## Coordination with other skills and tools

A design document is only as good as the evidence under it. Gather that evidence the
cheap way first, then widen. This skill *orchestrates* the following; it does not
duplicate them.

| Source | Role in a paper |
|---|---|
| **`recall`** (engram-memory MCP) | Prior decisions and rationale — feeds Introduction (the gap, related work) and Discussion. Start here. |
| **`search_code` / `search_docs` → `get_symbol` / `get_doc_section`** | The exact symbols/sections the Method describes, grounded at HEAD without reading whole files. |
| [`/engram-analyse`](../engram-analyse/SKILL.md) | Architecture-impact, budget, and risk evidence with file-path/line-number citations — feeds Method and Discussion (threats to validity). Run it before drafting a non-trivial systems paper. |
| [`/engram-poeaa`](../engram-poeaa/SKILL.md) | The canonical POEAA pattern map — so the Method's description of the system's shape is accurate, not approximate. |
| **`engram eval`** (`python3 bin/engram eval --backends …` from `plugins/engram/`) | The measured Results. Any empirical claim in a paper is cited to this harness's output. See [`references/statistics.md`](references/statistics.md). |

## The non-negotiables

Every document produced by this skill satisfies each of the following. Do not ship a
draft that fails any of them.

1. **UK English throughout.** "Organisation", "utilise", "behaviour", "analyse",
   "centralise", "whilst". No "leverage" as a verb for "use". See
   [`references/voice.md`](references/voice.md).
2. **Third-person, hedged-but-direct voice.** Findings are stated as fact; hypotheses
   and recommendations are hedged ("The design specifies…", "It is hypothesised
   that…"). No first-person ("we find"), no second-person ("you should").
3. **The scientific-paper structure** — the six sections below, in order, with the
   systems-research term mapping. Depth in [`references/structure.md`](references/structure.md).
4. **Title and Abstract at the top.** The abstract is a 150–250-word standalone
   summary in four beats: Problem, Method, Key Results, Conclusion. It is written last.
5. **Every claim is either a finding or a hypothesis.** A *finding* is measured or
   cited to source at HEAD or to `engram eval` output. A *hypothesis / recommendation* is
   hedged and marked as such. There are no unsupported assertions dressed as facts.
6. **Statistical rigour.** Every quantitative claim reports its sample size `n`; where
   the sample permits, a confidence interval or a significance test and an effect size;
   where it does not, the number is reported as *indicative, not conclusive*, and the
   small sample is named. Effect sizes beat bare percentage deltas. See
   [`references/statistics.md`](references/statistics.md).
7. **No AI tells.** Apply the stop-slop pass in [`references/voice.md`](references/voice.md):
   no em dash as default punctuation, no "not X but Y" contrasts, no "here is what…"
   throat-clearing, no unearned adverbs, no three-item lists where two will do.
8. **No emojis, ever.** Not in headings, callouts, or tables.
9. **No Claude Code boilerplate.** No "Generated with Claude Code" footer, no
   Co-Authored-By trailer, in the paper itself.
10. **Facts grounded in source at HEAD.** File paths, line numbers, function names, and
    config keys are copied verbatim from the source. If the paper cites
    `core/recall/__init__.py::search`, that symbol exists there at HEAD and does what
    the paper says.
11. **Reproducibility.** The Method and an appendix let a reader re-run the evidence —
    the exact `engram eval` command, the backends, the dataset, and the commit — and
    obtain the reported numbers. A result nobody can reproduce is not a result.
12. **The design does not implement.** It specifies decisions, contracts, and claims;
    pseudocode is acceptable where it makes a contract concrete, runnable code is not.

## The structure (systems-research mapping)

The reference structure is a psychology-paper template; claude-engram is a systems
project. Keep every section and the full apparatus, but map the terms to systems
research (a real genre — ML/systems evaluation papers have hypotheses, a methodology,
measured results, and threats to validity). Section numbers appear in the output.

| Section | Sub-parts (systems-research mapping) |
|---|---|
| **Title & Abstract** | Title, authors, date/version; a 150–250-word abstract (Problem · Method · Key Results · Conclusion). |
| **1. Introduction** | Opening / context (the problem in the world) · Related work (memory research, RAG / vector stores, POEAA) · The gap (what prior approaches miss) · The present study (what this system/design does) · **Hypotheses / claims** (formal, testable — stated at the end of the section). |
| **2. Method** | *Participants* → **Corpus & environment** (the eval dataset and its `n`, the runtime, the backends under test) · *Design* → **Variables & conditions** (independent: backend / quantisation / distiller; dependent: Recall@1/@3, MRR@10, bytes/fact, latency; controlled: search path, seed) · *Materials / measures* → **System & instruments** (the architecture, the `engram eval` harness, the metric definitions) · *Procedure* → **Protocol** (how a query is embedded, ranked, gated, injected; how the benchmark runs end to end). |
| **3. Results** | Data cleaning (exclusions, failures, missing) · Descriptive statistics (the metric table) · **Inferential statistics** (significance / effect size / CI, with the honest small-sample caveat) · Visuals (tables and figures the reader is pointed to). |
| **4. Discussion** | Hypothesis evaluation (supported / rejected, plainly) · Integration (compared to the related work from § 1) · **Limitations / threats to validity** · Implications & future directions. |
| **References & Appendices** | References (source files at HEAD, prior designs, external literature) · Appendices (raw `engram eval` output, the full config table, the dataset description, algorithms / pseudocode). |

Use a horizontal rule (`---`) between major sections. Do not put one between
subsections — the numbered headings carry the structure.

## The statistical-significance contract

This is what separates this skill's output from a well-written essay. A number in a
claude-engram paper is a measurement, not a decoration:

- **Name the sample.** "Recall@1 = 0.79" is incomplete; "Recall@1 = 0.79 (n = 29
  queries)" is a measurement. The current eval set is modest (34 facts / 29 queries);
  a paper says so and treats between-backend deltas as **indicative, not conclusive**,
  unless the effect is large relative to the sample.
- **Prefer effect sizes to bare deltas.** "bge-base roughly doubles bge-small's
  Recall@1 (0.79 vs 0.36, a 2.2× relative gain)" carries more than "43 points better".
- **Only claim significance you can defend.** With n ≈ 29, most between-condition
  differences do not warrant a p-value; report the delta, its direction, and the
  honest caveat. Where a test *is* warranted, name it and its assumptions.
- **Reproducibility block.** Every empirical section ends pointing at the exact command
  (`python3 bin/engram eval --backends "hash,fastembed"`), the dataset, and the commit.

Full guidance, including how to read `engram eval` output, is in
[`references/statistics.md`](references/statistics.md).

## Working method

When the user asks for a paper or design, proceed in this order.

### 1. Establish scope and paper type

Confirm the **subject**, the **paper type** (empirical-evaluation / systems-design /
whitepaper — see [`references/paper-types.md`](references/paper-types.md)), and the
**source material** (which modules, which prior designs, which benchmark). Auto-detect
the type when it is obvious ("write up the benchmark" → empirical-evaluation;
"the claude-engram whitepaper" → whitepaper) and confirm the inference in your first
response.

### 2. Gather evidence (cheap sources first)

`recall` for prior decisions and rationale; `search_code` / `search_docs` →
`get_symbol` / `get_doc_section` for the exact structure the Method describes. For a
non-trivial systems paper, run `/engram-analyse impact|budget|risk <subject>` and fold its
evidence tables into Method and Discussion. Widen to `Grep`/`Glob`/`Read` only when
those come back weak. Read the primary sources (DESIGN.md, the rules, the affected
modules) in full — do not summarise a file you have not read.

### 3. Produce the measured results

For any empirical claim, run `engram eval` yourself (from `plugins/engram/`) and record the
raw output for the appendix. Do not quote a benchmark number you have not reproduced
this session, and do not invent one. If the harness cannot be run, say so and mark the
section as pending rather than fabricating figures.

### 4. Draft in the reliable order

Method and Results first (they are the factual spine), then the Introduction (the gap
and hypotheses read better once the results exist), then the Discussion, then the
Abstract **last** (it summarises a finished document). References and appendices accrue
throughout.

### 5. Apply the stop-slop pass

Run the quick-check in [`references/voice.md`](references/voice.md): remove every
default em dash, cut unearned adverbs, break up runs of equal-length sentences,
rewrite any sentence that reads like a pull-quote.

### 6. Validate every citation and number

Every file path, line number, symbol, and config key resolves at HEAD. Every
quantitative claim carries its `n` and its caveat. Spot-check at least five citations
and every headline number before shipping.

### 7. Write the file, then optionally build the PDF

Save the markdown to `docs/generated/design-drafts/<slug>/<slug>.md` while drafting.
When it is review-ready, the source of truth is the `.md`; generate the PDF with
`python3 scripts/build_pdf.py <paper>.md` (build beside the source) or
`--publish` (datetime-stamped archive in `docs/whitepaper/` + canonical copy to the
repo root). See [`references/pdf.md`](references/pdf.md).

## Bundled references

- [`references/structure.md`](references/structure.md) — each of the six sections in
  depth, with the systems-research mapping and worked claude-engram examples.
- [`references/voice.md`](references/voice.md) — UK English, third-person, hedging, the
  stop-slop / no-AI-tells pass, before/after examples.
- [`references/statistics.md`](references/statistics.md) — the statistical-rigour spine:
  reporting `n`, confidence intervals, effect sizes, small-sample honesty, and how to
  run and read `engram eval`.
- [`references/paper-types.md`](references/paper-types.md) — empirical-evaluation vs
  systems-design vs whitepaper, and the section variants each carries.
- [`references/examples.md`](references/examples.md) — worked openings, an abstract, a
  hypotheses list, a descriptive-plus-inferential results pairing, and a negative
  example with its fix.
- [`references/pdf.md`](references/pdf.md) — the PDF build: pandoc + tectonic, the
  publish flow, and citations.
- [`assets/paper_template.md`](assets/paper_template.md) — a blank six-section skeleton
  ready to fill in.

When drafting a new paper, the fastest path is to copy `assets/paper_template.md` and
replace the bracketed placeholders section by section.

## A note on length and honesty

A claude-engram paper is typically 400 to 1,200 lines of rendered markdown. Longer is not
better. The Abstract, the hypotheses, and the results table together must let a reader
grasp the contribution without reading every appendix. The single fastest way to lose
a serious reader is an overstated number: when the evidence is thin, the paper that
says so is stronger than the one that hides it.
