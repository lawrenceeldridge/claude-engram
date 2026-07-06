# Statistical Rigour Reference

This reference is the spine of the "scientific and statistical significance" the skill
promises. It defines what counts as a measured claim, how to report it honestly at
claude-engram's sample sizes, and how to run and read the `engram eval` benchmark that
produces the numbers. A paper that follows this reference reports evidence; one that
skips it reports vibes.

The governing fact: **the eval set is modest.** At the time of writing it is 34 facts
and 29 queries (plus an STM scenario), and DESIGN.md says so explicitly ("Eval set is
modest — widen for tighter numbers"). Every rule below flows from that.

---

## Measured vs asserted

A **measured claim** is a number you produced this session from `engram eval` (or read
verbatim from source at HEAD) and can reproduce. An **asserted claim** is anything else.
Asserted quantitative claims do not belong in a claude-engram paper. If you cannot run the
harness, mark the section pending; do not fill it with plausible figures.

Every measured claim carries three things:

1. The **metric**, defined (Recall@1, MRR@10, bytes/fact).
2. The **sample size `n`** it was computed over.
3. Either a **dispersion / interval / effect size**, or an explicit statement that the
   sample is too small for one.

---

## Reporting a proportion (Recall@k)

Recall@k is a proportion of queries. At n = 29 the sampling error is large, so a bare
point estimate overstates precision.

- **Always attach `n`.** "Recall@1 = 0.79" → "Recall@1 = 0.79 (n = 29)".
- **Give a 95% interval where it matters.** Use the **Wilson score interval** (it
  behaves near 0 and 1, unlike the normal approximation). At n = 29, a point estimate of
  0.79 has a Wilson 95% interval of roughly [0.61, 0.90] — a spread of about ±0.15. State
  the interval for headline numbers; you may omit it for a table if you state the typical
  margin once in the text.
- **Consequence for comparisons.** Two backends whose Recall@1 differ by less than
  roughly 0.15 are **not distinguishable** at this sample. Report such differences as
  *indicative*, not as one backend "beating" another.

A small helper for the appendix (stdlib only):

```python
# Pseudocode — Wilson 95% interval for a proportion
from math import sqrt
def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k / n
    d = 1 + z*z/n
    centre = (p + z*z/(2*n)) / d
    half = z*sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return (centre - half, centre + half)
```

---

## Effect sizes over bare deltas

A percentage-point delta hides how large a change really is. Prefer a **ratio** or a
named effect size.

- "bge-base improves Recall@1 by 0.43" is weaker than "bge-base reaches 2.2× the lexical
  stub's Recall@1 (0.79 vs 0.36 for bge-small; 0.07 for hash)".
- For MRR, report the absolute value and the ratio; MRR is already a mean, so its spread
  can be summarised with a standard error if the per-query reciprocal ranks are to hand.
- When comparing bytes, report the ratio directly: "int8 uses roughly one-third the bytes
  of float (864 vs 1536 per fact for bge-base / bge-small-float baselines)".

The rule of thumb: a claim survives the small sample when the **effect is large relative
to the interval**. hash (0.07) vs bge-base (0.79) is a 0.72 gap against a ±0.15 margin —
robust. bge-small-int8 (0.36) vs bge-small-float (0.36) is 0.00 — identical within the
sample, which is exactly the H1 (int8 ≈ float) finding.

---

## When a significance test is, and is not, warranted

At n = 29, most between-condition tests are underpowered and a p-value would imply more
than the data supports. The honest default is to **report the delta, its direction, its
interval, and the caveat**, not a test.

A test is warranted only when:

- the comparison is **paired per query** (McNemar's test for two backends' hit/miss on
  the same 29 queries is the right tool, not a two-sample proportion test), and
- the effect is large enough that the test adds information beyond the interval.

If you run a test, name it, its assumptions, and the exact statistic. Never report
"p < 0.05" without the test that produced it. Never imply significance you did not test.

---

## The small-sample honesty rule

State the sample's limits **in the Results and again in the Limitations**, in the
paper's own voice:

> The eval set is modest (34 facts, 29 queries), so 95% intervals on Recall@1 span
> roughly ±0.15 and between-condition differences below that margin are not
> distinguishable from sampling noise. The headline gaps reported here (the lexical stub
> against the semantic backends, and the bge-small → bge-base model-size change) exceed
> that margin and are treated as robust; the finer comparisons are labelled indicative
> and would need a wider corpus to settle.

This is not a weakness to bury. A paper that names its sample limits is more credible,
not less — it is the difference between the Bitcoin whitepaper's measured claims and a
marketing page.

---

## Running and reading `engram eval`

From `plugins/engram/`:

```bash
# Compare backends through the real quantised search path
python3 bin/engram eval --backends "hash,fastembed"

# Single backend
python3 bin/engram eval --backends hash
```

The harness (`bench/run_eval.py`, dataset `bench/dataset.json`) runs each labelled
paraphrase query through embed → quantise → cosine → gate → rank, scores the gold fact's
position, and reports **Recall@1**, **Recall@3**, **MRR@10**, and **bytes/fact** per
backend. `fastembed` requires the semantic model (self-provisioned venv or a pinned
interpreter); `hash` is the dependency-free stub and always runs.

Read the output as: Recall@1 = did the single top hit contain the gold fact; Recall@3 =
was it in the top three; MRR@10 = how high on average within the top ten; bytes/fact =
the storage cost of that quality. Copy the **verbatim** output into the appendix with the
command and commit.

---

## The reproducibility block

Every empirical section ends with a block a reader can execute:

> **Reproducibility.** Figures in § 3 were produced by
> `python3 bin/engram eval --backends "hash,fastembed"` from `plugins/engram/` at commit
> `<hash>`, against `bench/dataset.json` (34 facts, 29 queries). The `fastembed` rows
> use `bge-base-en-v1.5` with int8 quantisation (the shipped default). Raw output is in
> Appendix A.

Name the commit (`git rev-parse --short HEAD`), the command, the dataset, and the
backend configuration. A number nobody can reproduce is not a result.

---

## Checklist

Before a paper's Results/Discussion ship:

- [ ] Every number carries its `n`.
- [ ] Headline proportions carry a 95% (Wilson) interval, or the typical margin is stated once.
- [ ] Comparisons below the sample's resolution are labelled *indicative*, not decisive.
- [ ] Effect sizes / ratios accompany bare deltas.
- [ ] No p-value appears without the named test that produced it.
- [ ] The small-sample limit is stated in both Results and Limitations.
- [ ] A reproducibility block names the command, commit, dataset, and backend.
- [ ] Raw `engram eval` output is in an appendix.
