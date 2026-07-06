# Recall-Quality Benchmark (`engram eval`)

claude-engram has a second test surface beyond correctness: a labelled **recall
benchmark** that measures retrieval quality through the *real* quantised search
path. It exists because retrieval quality cannot be reasoned about — it has to be
measured. This is the "measured, not assumed" ethos from
[DESIGN.md § Embedding backend](../../../../DESIGN.md).

---

## The gate rule

> Any change to embeddings, ranking, quantisation, fusion, or distillation is
> A/B'd with `engram eval` **before** it ships.

Quantization loss, model choice, and fusion weights are all decisions the harness
settled. A retrieval change that isn't benchmarked isn't finished. Report the
before/after numbers in the change description.

---

## Running it

```bash
cd plugins/engram
python3 bin/engram eval --backends hash                          # zero-dep default
python3 bin/engram eval --backends "hash,fastembed"              # stub vs real model
python3 bin/engram eval --backends "fastembed,fastembed+float"   # isolate int8 loss
python3 bin/engram eval --backends "fastembed@BAAI/bge-small-en-v1.5,fastembed@BAAI/bge-base-en-v1.5"
python3 bench/run_eval.py --backends hash,fastembed           # equivalent, direct
```

### Backend spec: `name[@model][+float]`

- `name` — `hash` (lexical stub, zero-dep) or `fastembed` (real model).
- `@model` — optional fastembed model id (blank ⇒ `BAAI/bge-base-en-v1.5`).
- `+float` — rank on raw full-precision vectors in memory instead of the quantised
  store. The gap between a backend and its `+float` twin is **exactly the int8
  quantization loss** — that is how "int8 ≈ float" was established.

---

## What it reports

Per backend, over the bundled labelled set:

| Metric | Meaning |
|---|---|
| **Recall@1** | fraction of queries whose top hit is relevant |
| **Recall@3** | fraction with a relevant hit in the top 3 |
| **MRR@10** | mean reciprocal rank of the first relevant hit (top 10) |
| **bytes/fact** | storage cost of one fact's embedding (the "bytes" budget) |
| corpus embed time / per-query latency | operational cost (the "latency" budget) |

Reference numbers (bundled set — 18 facts, 14 paraphrased queries):

| backend | Recall@1 | Recall@3 | MRR@10 | bytes/fact |
|---|---|---|---|---|
| hash (lexical stub) | 0.07 | 0.36 | 0.27 | 288 |
| fastembed bge-small int8 | 0.36 | 0.71 | 0.57 | 432 |
| **fastembed bge-base int8 (default)** | **0.79** | **0.86** | **0.85** | 864 |

Reading them: the `hash` stub only matches shared vocabulary, so its recall is
floor-level and it exists as the zero-dep default, not as a quality target. Model
size is the real lever (bge-base ≈ 2.2× bge-small's Recall@1); int8 vs float is
noise, so the compact int8 store stays.

---

## The dataset

`bench/dataset.json` — a small labelled set, deliberately adversarial:

```json
{
  "facts": ["The project deploys to AWS Lambda ...", "..."],
  "queries": [{"q": "how is the service shipped to production", "relevant": [0]}]
}
```

- **Queries are paraphrased away from the fact wording** so lexical matching is
  stressed and semantic recall is what's actually measured.
- The last facts are **hard-negative distractors** — plausible but irrelevant, to
  catch a backend that retrieves on surface features.
- `relevant` is a list of indices into `facts`.

The set is small (14 queries) — treat single-query swings as noise; widening it is
listed under "Remaining" in DESIGN.md. When adding a fact/query, keep the
paraphrase gap (don't echo the fact's vocabulary in its query) or the benchmark
stops measuring what it's for.

---

## Adding a metric or backend

- A new **backend** is a new `EmbeddingGateway` implementation wired into
  `make_embedder()` in `bench/run_eval.py`; it then runs through the same store
  path, so its numbers are comparable.
- A new **metric** goes in the per-backend result dict; keep the existing columns
  so historical comparisons still line up.
- Always report the `+float` twin when touching quantisation, so the int8-loss
  claim stays honest.

---

## CI note

There is one stdlib suite and no 3-tier / infrastructure CI. A useful CI shape:

1. **Always:** `python3 -m unittest discover -s tests` (zero-dep; the 5 optional
   skips are expected).
2. **Optional retrieval smoke:** `python3 bin/engram eval --backends hash` — cheap,
   dependency-free, catches a search path that regressed to all-zeros. A full
   `fastembed` A/B is a heavier, opt-in job (it provisions a model), best run on
   PRs that touch the retrieval path rather than every push.

Do not gate CI on absolute recall numbers on a 14-query set — use the benchmark to
compare *a change against its baseline*, not against a fixed threshold.
