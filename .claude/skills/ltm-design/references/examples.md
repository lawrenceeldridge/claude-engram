# Worked Examples

Concrete claude-ltm examples to reach for when a blank page is hard to face. All numbers
are drawn from [DESIGN.md](../../../../DESIGN.md)'s benchmark table and are illustrative —
a real paper reproduces them from `ltm eval` this session and cites the commit.

---

## Title block and abstract

```markdown
# Token-First Long-Term Memory for LLM Coding Agents: A Two-Budget Design and Retrieval Evaluation

**Authors:** Lawrence Eldridge
**Date:** 6 July 2026
**Version:** 1.0 (canonical)
**Status:** Published

## Abstract

Retrieval-augmented memory for LLM coding agents is usually billed in bytes, but the
model consumes only text tokens, so byte-efficiency and token-efficiency are distinct
budgets a naive design conflates. This paper specifies claude-ltm, a local-first memory
that separates the two: a detached capture path that distils sessions into atomic facts,
and a threshold-gated recall path that injects only facts clearing a similarity gate. The
retrieval path was evaluated with a labelled paraphrase benchmark (n = 29 queries over 34
facts) across four embedding backends through the real quantised search path. The default
backend (bge-base, int8) reached Recall@1 = 0.79 and MRR@10 = 0.85, against 0.07 and 0.27
for a dependency-free lexical stub; int8 matched float within the resolution of the sample
at roughly one-third the bytes. These results are indicative rather than conclusive at this
sample size, and the paper treats them as such. Model size, not quantisation precision, is
the dominant recall lever at personal-store scale.
```

Notice: descriptive title with the contribution in it; metadata block; four-beat abstract
(problem, method, results with `n`, honest conclusion); no em dash, no first person.

---

## Hypotheses block (end of § 1)

```markdown
This study tests three hypotheses:

**H1.** int8 quantisation of the embedding vectors does not materially reduce retrieval
quality relative to float, at personal-store scale.

**H2.** Embedding model size is the dominant recall lever: the bge-small to bge-base
change yields a larger Recall@1 gain than the float-to-int8 precision change.

**H3.** A threshold-gated hybrid re-rank (similarity, recency, frequency) injects fewer
irrelevant facts than raw top-k similarity, at no measurable recall cost.
```

Each hypothesis is answerable by the Results section, and the Discussion returns to each.

---

## Descriptive plus inferential results (the pairing that matters)

```markdown
### 3.2 Descriptive statistics

| Backend | Recall@1 | Recall@3 | MRR@10 | Bytes/fact | n |
|---|---|---|---|---|---|
| hash (lexical stub) | 0.07 | 0.36 | 0.27 | 288 | 29 |
| bge-small int8 | 0.36 | 0.71 | 0.57 | 432 | 29 |
| bge-small float | 0.36 | 0.71 | 0.57 | 1536 | 29 |
| **bge-base int8 (default)** | **0.79** | **0.86** | **0.85** | 864 | 29 |

### 3.3 Inferential statistics

At n = 29 the Wilson 95% interval on a point estimate near 0.79 spans roughly [0.61,
0.90], so between-condition differences below about 0.15 are not distinguishable from
sampling noise. Two comparisons exceed that margin and are treated as robust: the lexical
stub against any semantic backend (0.07 vs 0.36–0.79 on Recall@1), and the model-size
change (bge-small 0.36 vs bge-base 0.79, a 2.2x ratio). The int8-versus-float comparison
showed no difference on any metric (Delta = 0.00), which the sample resolves cleanly: a
zero difference needs no interval. The finer gaps are reported as indicative.
```

Notice: the table carries `n`; the inferential paragraph states the margin, names which
comparisons clear it, and treats the identical int8/float result as the clean finding it
is. No invented p-value.

---

## Integration paragraph (§ 4.2)

```markdown
The result that int8 matches float sits against the vector-store convention of storing
full-precision embeddings and rescoring. At personal-store scale the convention buys
nothing measurable here: the quantisation loss is below the sample's resolution, and the
compact int8 representation keeps the brute-force cosine scan sub-10ms without an ANN
index. The finding that model size dominates aligns with the broader retrieval
literature, and it relocates the engineering effort from precision (already saturated) to
model choice and to what is stored, which is why the distiller, not the quantiser, is
named the largest remaining quality lever.
```

---

## References list (plain-Markdown fallback; prefer a .bib + citeproc)

```markdown
## References

**Source at HEAD**
- Recall path: [`plugins/ltm/core/recall/__init__.py`](../../plugins/ltm/core/recall/__init__.py)
- Capture service: [`plugins/ltm/core/service.py`](../../plugins/ltm/core/service.py)
- Store (Repository): [`plugins/ltm/core/store.py`](../../plugins/ltm/core/store.py)
- Benchmark harness: [`plugins/ltm/bench/run_eval.py`](../../plugins/ltm/bench/run_eval.py)

**Prior designs**
- [DESIGN.md](../../DESIGN.md) — the two-budget model and POEAA map.
- STM/LTM consolidation and MemoryBus design.

**External literature**
- Atkinson, R. C., & Shiffrin, R. M. (1968). Human memory: A proposed system.
- Roediger, H. L., & Karpicke, J. D. (2006). The testing effect.
- Fowler, M. (2002). Patterns of Enterprise Application Architecture.
```

With a `references.bib`, these become numbered `[1]` cites and an auto-styled list — see
[`pdf.md`](pdf.md).

---

## A negative example — an opening that does not work

```
# Claude's Memory

So basically claude-ltm is a really powerful memory system that leverages cutting-edge
embeddings to give Claude a robust, production-ready memory. It's significantly better
than the alternatives — let's dive into how it works!
```

Problems: first person implied, "so basically", "leverages", "cutting-edge", "robust",
"production-ready", "significantly better" with no number or `n`, em-dash flourish,
"let's dive in", exclamation mark. This is marketing copy, not a paper.

## The same opening, fixed

```
# Token-First Long-Term Memory for LLM Coding Agents

An LLM coding agent's context window is finite, and every injected token has a cost, so a
memory that "remembers everything" is not an option. This paper specifies claude-ltm, a
local-first memory that treats byte-efficiency and token-efficiency as distinct budgets
and injects only the facts that clear a similarity gate.
```

Named the problem, named the contribution, no marketing, no unfounded comparison, no AI
tells.
