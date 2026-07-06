# Paper Structure — Detailed Reference

This reference explains each of the six sections of a claude-engram design paper in depth,
with the systems-research mapping and worked examples grounded in the plugin's own
evidence ([DESIGN.md](../../../../DESIGN.md), the `engram eval` benchmark, the POEAA map).

The template is an empirical research paper. claude-engram is a systems project, so the
psychology-paper vocabulary (Participants, ANOVAs) is mapped to systems-research
equivalents. This is a real genre: systems and ML evaluation papers state hypotheses,
describe a methodology, report measured results with effect sizes, and name threats to
validity. For which paper type demands which emphasis, read this file then
[`paper-types.md`](paper-types.md).

---

## Title & Abstract

The first heading is the paper title — descriptive, not rhetorical. "Token-First
Long-Term Memory for LLM Coding Agents: A Two-Budget Design and Retrieval Evaluation"
is appropriate. "Giving Claude a Memory" is not.

Immediately under the title, a metadata block (authors, date, version, and — for a
whitepaper — a one-line status). Then the abstract.

**The abstract is a 150–250-word standalone summary in four beats:**

1. **Problem** — the specific problem, in one or two sentences. Not the general topic.
2. **Method** — what the design/system does and how it was evaluated.
3. **Key results** — the headline numbers, each with its sample size.
4. **Conclusion** — what the results mean and the honest scope of the claim.

The abstract is written **last**, once the paper it summarises exists. A reader decides
whether to continue on the strength of these 200 words; they earn the most care.

### Worked abstract (empirical-evaluation paper)

> Retrieval-augmented memory for LLM coding agents is usually billed in bytes, but the
> model only ever consumes text tokens, so byte-efficiency and token-efficiency are
> distinct budgets that a naive design conflates. This paper specifies claude-engram, a
> local-first memory that separates the two: a detached capture path that distils
> sessions into atomic facts, and a threshold-gated recall path that injects only the
> facts clearing a similarity gate. The retrieval path was evaluated with a labelled
> paraphrase benchmark (n = 29 queries over 34 facts) across four embedding backends
> through the real quantised search path. The default backend (bge-base, int8) reached
> Recall@1 = 0.79 and MRR@10 = 0.85, against 0.07 and 0.27 for a dependency-free lexical
> stub; int8 quantisation matched float within the resolution of the sample while using
> roughly one-third the bytes. These results are indicative rather than conclusive at
> this sample size, and the limitations section treats them as such. The design shows
> that model size, not quantisation precision, is the dominant recall lever for a
> personal-scale memory.

Notice: named the problem (two budgets conflated), the method (labelled benchmark, real
search path), the headline numbers each carrying `n`, and an honest conclusion that does
not outrun the sample.

---

## 1 Introduction

The introduction moves from the world to the specific claims this paper makes. It has
five beats, and the hypotheses come **last**.

1. **Opening / context.** The problem in the world. For claude-engram: an agent's context
   window is finite and every injected token has a cost, so "remember everything" is not
   an option; memory has to be selective and cheap.

2. **Related work.** What is already known and built. Synthesise, do not list. Three
   strands are load-bearing for claude-engram:
   - *Memory research* — the forgetting curve, Atkinson–Shiffrin's multi-store model,
     rehearsal, the testing effect (Roediger & Karpicke), Active Systems Consolidation.
   - *Retrieval systems* — RAG, vector stores, ANN indexes, quantised embeddings.
   - *Software architecture* — POEAA / Cosmic Python (Repository, Gateway + Separated
     Interface, Functional Core / Imperative Shell, CQRS).

3. **The gap.** What prior approaches miss. For claude-engram: always-on retrieval tools
   carry a standing token cost and hand the model an agency decision every turn; raw
   vector similarity recalls stale and conflicting facts; neither optimises the token
   budget as distinct from storage.

4. **The present study.** What this design/system does about the gap, in one paragraph.
   Name the contribution precisely.

5. **Hypotheses / claims.** Formal and testable, stated as a numbered list. Each must be
   answerable by the Results section. Examples for claude-engram:
   > **H1.** int8 quantisation of the embedding vectors does not materially reduce
   > retrieval quality relative to float, at personal-store scale.
   > **H2.** Embedding model size is the dominant recall lever; a larger model yields a
   > larger Recall@1 gain than the precision change from float to int8.
   > **H3.** A threshold-gated hybrid re-rank (similarity + recency + frequency) injects
   > fewer irrelevant facts than raw top-k similarity.

A systems-design paper (as opposed to empirical-evaluation) may phrase these as **design
claims** rather than statistical hypotheses ("The design claims that a Command queue,
not an Event bus, is the correct abstraction for detached capture, because…"). Either
way they are explicit and the Discussion returns to each.

The introduction is continuous prose with one numbered hypotheses block at the end. No
other bullet lists.

---

## 2 Method

The method is the recipe: enough that a competent reader could rebuild the system and
re-run the evaluation. Four sub-parts, mapped from the psychology template.

### 2.1 Corpus & environment (*Participants*)

Who/what was studied. For claude-engram: the evaluation corpus (the labelled dataset —
its size, how the facts and paraphrase queries were constructed, `bench/dataset.json`),
the runtime (Python version, OS, whether the resident daemon was warm), and the
backends under test (hash, bge-small, bge-base; int8 vs float). State the sample size
`n` here, once, prominently — every later number refers back to it.

### 2.2 Variables & conditions (*Design*)

Name the variables explicitly:

- **Independent** — what was varied: embedding backend, quantisation (int8 / float),
  distiller (heuristic / LLM).
- **Dependent** — what was measured: Recall@1, Recall@3, MRR@10, bytes/fact, and (where
  relevant) per-query latency.
- **Controlled** — what was held constant: the search path (the same quantised cosine
  scan), the dataset, the random seed, the caps (`top_k`, `min_sim`).

### 2.3 System & instruments (*Materials / measures*)

The system under study and the measuring instrument. Describe the architecture at the
level the claims need — CQRS + Hexagonal, the capture pipeline, the recall path, the
POEAA roles — citing the source at HEAD (`core/service.py`, `core/recall/__init__.py`,
`core/store.py`) and the pattern map in
[`.claude/rules/02-architecture/01-poeaa-and-layers.md`](../../../rules/02-architecture/01-poeaa-and-layers.md).
Then the instrument: the `engram eval` harness (`bench/run_eval.py`), and precise
definitions of each metric (Recall@k = fraction of queries whose gold fact appears in
the top k; MRR@10 = mean reciprocal rank of the gold fact within the top 10).

### 2.4 Protocol (*Procedure*)

The chronological path, step by step: how a captured session becomes atomic facts
(distil → embed → quantise → persist, tier=stm), and how a query becomes an injection
(embed → cosine over int8 → similarity gate → hybrid re-rank → top-k render). For the
benchmark: how each paraphrase query is scored against the labelled gold fact, and how
the metrics are aggregated. Name the exact command in the reproducibility block.

Pseudocode is acceptable where it makes a contract concrete; mark it as pseudocode.
Runnable code is not — this is a design paper, not the implementation.

---

## 3 Results

Facts only. Interpretation waits for the Discussion.

### 3.1 Data cleaning

State any excluded or failed cases: queries with no gold fact, backends that failed to
provision, missing measurements. If nothing was excluded, say so in one sentence.

### 3.2 Descriptive statistics

The metric table. This is the centre of an empirical-evaluation paper.

| Backend | Recall@1 | Recall@3 | MRR@10 | Bytes/fact | n (queries) |
|---|---|---|---|---|---|
| hash (lexical stub) | 0.07 | 0.36 | 0.27 | 288 | 29 |
| bge-small int8 | 0.36 | 0.71 | 0.57 | 432 | 29 |
| bge-small float | 0.36 | 0.71 | 0.57 | 1536 | 29 |
| **bge-base int8 (default)** | **0.79** | **0.86** | **0.85** | 864 | 29 |

Every column that is a measurement carries its sample size. Round consistently.

### 3.3 Inferential statistics

Where the sample supports it, report a confidence interval, an effect size, or a
significance test — and where it does not, say so plainly. With n = 29, a proportion
like Recall@1 = 0.79 has a wide Wilson 95% interval (roughly ±0.15), so between-backend
differences are reported as **indicative** and only *large* gaps (hash 0.07 vs bge-base
0.79; a 2.2× ratio between model sizes) are treated as robust to the sample. See
[`statistics.md`](statistics.md) for the exact reporting rules. Do not manufacture a
p-value the sample cannot bear.

### 3.4 Visuals

Point the reader to the tables and any figures. Keep figures honest — a bar chart with a
truncated y-axis is a slop tell in a rigorous paper. Tables are usually enough at this
scale.

---

## 4 Discussion

The meaning. This section is allowed to interpret; it is not allowed to overstate.

### 4.1 Hypothesis evaluation

Return to each hypothesis from § 1 and state plainly whether it was supported. "H1
(int8 ≈ float) is supported: the two conditions were identical on Recall@1/@3 and MRR@10
within the sample, at roughly one-third the bytes. H2 (model size dominates) is
supported: the bge-small → bge-base change moved Recall@1 by 0.43 (a 2.2× ratio),
against a 0.00 change from float → int8." No hedging on a supported finding; honest
rejection of an unsupported one.

### 4.2 Integration

Connect back to the related work in § 1. How does the measured result sit against RAG /
vector-store practice and the memory-research grounding? Where claude-engram diverges from
the biology (no distinct REM phase; STM as a promotion-gated state, not a faster clock),
name the divergence as an engineering choice, per DESIGN.md's "honest limits".

### 4.3 Limitations / threats to validity

The section that earns trust. Name the weaknesses before a reviewer does: the modest
eval set (34 facts / 29 queries), the single-domain corpus, the absence of a separate
REM consolidation phase, the heuristic distiller's inability to detect
vocabulary-disjoint conflicts. Each limitation names what it threatens (external
validity, statistical power, construct validity) and, where relevant, the follow-up that
would address it.

### 4.4 Implications & future directions

What the results mean for the design and what to measure next: widen the eval set for
tighter intervals; add a fresh/STM scenario before flipping `stm_recall_weight`; measure
LLM-distiller latency/cost. Implications are hedged where the evidence is thin.

---

## References & Appendices

### References

Bulleted, grouped:

- **Source at HEAD** — every module the paper cites, with a repo-relative path
  (`plugins/engram/core/recall/__init__.py`). A reader can check each claim against the
  named file.
- **Prior designs** — related design docs (`docs/generated/designs/*.md`, DESIGN.md).
- **External literature** — memory research, retrieval systems, POEAA. Use a
  `references.bib` + citeproc for a numbered, consistently-styled list (see
  [`pdf.md`](pdf.md)); a plain Markdown list is the fallback.

Do not cite gitignored working artefacts (`docs/generated/design-drafts/…`) as evidence
a reader can follow — reproduce the underlying evidence inline instead.

### Appendices

Material that would clutter the main text but is needed for reproducibility:

- **Raw `engram eval` output** — the verbatim benchmark run behind § 3, with the command
  and the commit hash.
- **Full configuration table** — the `userConfig` keys and defaults relevant to the
  study.
- **Dataset description** — how `bench/dataset.json` is structured.
- **Algorithms / pseudocode** — the retention score, the hybrid re-rank, kept out of the
  Method to keep it readable.

---

## A final test

Before shipping, read the paper end to end and ask:

- Could a competent reader rebuild the system and re-run the evaluation from the Method?
- Does every number in the Results carry its `n`, and does every headline number
  reproduce from the appendix command?
- Does the Discussion evaluate each hypothesis from § 1, plainly?
- Does the Limitations section name every weakness a reviewer would raise?
- Is there a single sentence that claims more than the evidence supports? Cut or hedge it.

If any answer is uncomfortable, revise before publishing.
