---
title: "[Paper title — descriptive, with the contribution in it]"
author: "[Author name]"
date: "[e.g. 6 July 2026]"
abstract: |
  [150–250 words, four beats. Problem: the specific problem in one or two sentences.
  Method: what the design/system does and how it was evaluated. Key results: the
  headline numbers, each with its sample size n. Conclusion: what the results mean and
  the honest scope of the claim. Write this LAST.]
# Optional — uncomment when a references.bib sits beside this file:
# bibliography: references.bib
---

<!--
  Working draft location: docs/generated/design-drafts/<slug>/<slug>.md (gitignored).
  Build:   python3 .claude/skills/engram-design/scripts/build_pdf.py <this-file>.md
  Publish: python3 .claude/skills/engram-design/scripts/build_pdf.py <this-file>.md --publish
  Voice/structure/statistics rules: ../references/*.md. Delete this comment before publishing.
-->

# 1 Introduction

[Opening / context — the problem in the world.]

[Related work — synthesise the prior art this builds on: memory research, retrieval
systems, software architecture. Do not list; connect.]

[The gap — what prior approaches miss.]

[The present study — what this design/system does about the gap, in one paragraph.]

This study tests the following [hypotheses | design claims]:

**H1.** [Formal, testable, answerable by § 3.]

**H2.** [...]

**H3.** [...]

---

# 2 Method

## 2.1 Corpus and environment

[What was studied: the eval dataset and its sample size n, the runtime, the backends
under test. State n once, prominently.]

## 2.2 Variables and conditions

- **Independent** — [what was varied.]
- **Dependent** — [what was measured: Recall@1/@3, MRR@10, bytes/fact, latency.]
- **Controlled** — [what was held constant.]

## 2.3 System and instruments

[The architecture at the level the claims need, cited at HEAD. The measuring
instrument: the `engram eval` harness and precise metric definitions.]

## 2.4 Protocol

[The chronological path: capture (distil, embed, quantise, persist) and recall (embed,
cosine, gate, re-rank, render); how the benchmark scores each query. Mark any pseudocode
as pseudocode.]

---

# 3 Results

## 3.1 Data cleaning

[Excluded / failed / missing cases, or "none excluded".]

## 3.2 Descriptive statistics

| Backend | Recall@1 | Recall@3 | MRR@10 | Bytes/fact | n |
|---|---|---|---|---|---|
| [...] | | | | | |

## 3.3 Inferential statistics

[Intervals / effect sizes / the small-sample caveat. State the margin; label
sub-margin comparisons indicative. No p-value without its named test.]

## 3.4 Visuals

[Point to the tables/figures. Keep figures honest.]

**Reproducibility.** [Command, commit hash, dataset, backend configuration. Raw output
in Appendix A.]

---

# 4 Discussion

## 4.1 Hypothesis evaluation

[Return to each H from § 1; state supported/rejected plainly.]

## 4.2 Integration

[Compare to the related work in § 1.]

## 4.3 Limitations and threats to validity

[Name the weaknesses before a reviewer does; each names what it threatens.]

## 4.4 Implications and future directions

[What the results mean; what to measure next. Hedge where the evidence is thin.]

---

# References

[Source at HEAD; prior designs; external literature. Prefer a references.bib + citeproc;
a plain Markdown list is the fallback.]

---

# Appendix A — Raw benchmark output

```
[Verbatim engram eval output, with the command and commit.]
```

# Appendix B — Configuration

[The userConfig keys and defaults relevant to the study.]
