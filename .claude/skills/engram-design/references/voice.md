# Voice and Grammar Reference

This reference encodes the linguistic choices that distinguish a claude-engram design paper
from generic AI prose. The register is professional British engineering-research prose:
the product of someone who has read the files they cite and run the benchmark they
report, writing for a peer who knows the field.

---

## Person and tense

- **Third person throughout.** "The design specifies…", "It is hypothesised that…", "The
  recall path embeds the query, then…". No first-person ("we find", "I think"); no
  second-person ("you should"). The paper speaks, or the system does.
- **Present tense for the design as it stands.** "Recall is a hook, not an always-on
  tool." Not "Recall will be a hook".
- **Past tense for what the evaluation surfaced.** "The benchmark found int8 matched
  float within the sample." "bge-base reached Recall@1 = 0.79."
- **Future tense sparingly** — for genuinely deferred work ("A wider eval set will
  tighten the intervals").

---

## Hedging — the load-bearing distinction

Hedge the verb of **recommendation or hypothesis**; state a **finding** as fact.

- Findings (measured or cited at HEAD) are not hedged: "int8 uses roughly one-third the
  bytes of float." "Recall is threshold-gated at `min_sim`." "The store is one SQLite
  database under `${CLAUDE_PLUGIN_DATA}`."
- Hypotheses and recommendations are hedged: "It is hypothesised that model size is the
  dominant lever." "The design specifies…" (strong, for a settled choice). "It is
  recommended that the eval set be widened before…" "Reviewers may relax X if Y."

**MUST is reserved.** Bold ALL-CAPS "MUST" only where deviation compromises a safety or
correctness property (a hook that must fail open). Otherwise use "specifies",
"requires", or "is recommended".

Statistical hedging is a special case: a result the sample cannot bear is reported as
**indicative, not conclusive**, never as if it were settled. See
[`statistics.md`](statistics.md).

---

## UK English

| Instead of | Use |
|---|---|
| organization, organize | organisation, organise |
| utilize, utilization | utilise, utilisation |
| behavior | behaviour |
| optimize, optimization | optimise, optimisation |
| analyze, analyzer | analyse, analyser |
| center | centre |
| favor | favour |
| realize, finalize | realise, finalise |
| license (noun) | licence (noun); license (verb) |

- **Date format:** `6 July 2026`, never `July 6, 2026` or `06/07/2026`.
- `program` is acceptable for software; `programme` for an initiative.
- `whilst` and `while` are both acceptable UK usage.

---

## Adverb discipline

Cut every adverb that does not carry load. "Significantly", "substantially",
"particularly", "essentially", "clearly" are almost always removable and, in a paper
that reports statistics, "significantly" is actively dangerous — it has a technical
meaning. Reserve it for "statistically significant (p < …)" and never as a synonym for
"a lot".

- "significantly faster" → "faster by ~5 ms/query" (name the number)
- "clearly the better backend" → state the metric that makes it better
- "essentially identical" → "identical within the sample (Δ = 0.00)"

Adverbs that may remain: "approximately" (a rounded number), "deterministically" /
"atomically" (technical distinctions), "roughly" (an honest estimate).

---

## Dashes and punctuation

- **No em dash (—) as default punctuation.** It is the single most reliable AI tell.
  Replace with a comma, a colon, a semicolon, or a new sentence in almost every case.
- **En dash (–) for ranges only:** "1 October 2025 to 30 April 2026" is preferred prose;
  "0.79–0.86" is acceptable in a table.
- **Semicolons** join two closely related independent clauses; do not overuse.
- **Oxford comma** is standard.
- **Backtick all identifiers** in body prose: `min_sim`, `core/store.py`, `Recall@1`,
  `bge-base`.

---

## Words and phrases to cut on sight (the stop-slop pass)

### AI throat-clearing
"Here's what…", "Here is the thing…", "So, …" / "Now, …" as openers, "In essence…", "In
other words…", "It's worth noting that…" (at most once), "Interestingly…" (never).

### Fake contrasts
"It's not just X, it's Y" → state Y. "Not only X but also Y" → state both.

### Corporate / research filler
"Leverage" (verb), "robust" (say what makes it robust), "holistic", "mission-critical",
"first-class citizen", "battle-tested", "production-ready", "state-of-the-art" (name the
baseline you beat), "novel" (let the reader judge novelty).

### Exhaustion-era clichés
"Delve into", "deep dive", "unpack", "navigate the complexities of", "harness the power
of", "unlock the potential of".

### Rigour-specific tells
- Percentages without a denominator ("79% better" — 79% of what, over what `n`?).
- "Proven" / "guarantees" for an empirical result (evidence supports; it does not prove).
- Three-item parallel lists where two items would do.
- A run of three or more sentences of identical length and shape.

---

## Sentence rhythm

Vary sentence length within every paragraph; a paragraph of equal-length sentences reads
mechanical, the most reliable tell of generated prose. A dependable rhythm: open with a
medium declarative sentence naming the fact, follow with a short sentence that sharpens
it, close with a longer sentence that names the implication or the caveat.

---

## Before / after

**Decision statement.**
Before: "So we basically went with bge-base since it's clearly the best and int8 is
basically free."
After: "The default backend is bge-base with int8 quantisation. int8 matched float on
every retrieval metric within the sample while using roughly one-third the bytes, so the
compact representation is kept; model size, not precision, is where the recall gain
lives."

**A statistical claim.**
Before: "bge-base is significantly better than the stub."
After: "bge-base reached Recall@1 = 0.79 against the lexical stub's 0.07 (n = 29). The
gap is large relative to the sample, so it is treated as robust; the smaller
between-model differences are reported as indicative."

**A limitation.**
Before: "The eval set is a bit small but the results are still solid."
After: "The eval set is modest (34 facts, 29 queries), so the confidence intervals are
wide and between-condition deltas below roughly 0.15 are not distinguishable from noise.
The headline gaps reported here exceed that margin; the finer comparisons do not, and are
labelled indicative."

---

## A final test

- Would a peer who knows the field read this as the work of someone who ran the
  benchmark, or of a summariser who did not?
- Is there a sentence that reads as chatty, apologetic, or hedging without purpose?
- Is there a sentence that claims more than the evidence supports?
- Does every paragraph earn its place?

If any answer is uncomfortable, revise.
