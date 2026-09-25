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
python3 bin/engram eval --backends "hash,fastembed" --confidence  # recall-verdict calibration
```

`bin/engram eval` and `bench/run_eval.py` share one flag definition
(`run_eval.add_eval_arguments`), so every scenario flag works from both: `--stm`,
`--antipatterns`, `--integrate`, `--confidence` (+ `--ok-precision`), `--aged`, `--longmemeval`
(+ `--lme-path` / `--lme-download` / `--lme-limit` / `--lme-llm`); `--distractors`,
`--distractor-project`, `--distractor-db` pad the store for `--confidence` / `--aged`.

### Backend spec: `name[@model][%dim][+float]`

- `name` — `hash` (lexical stub, zero-dep) or `fastembed` (real model).
- `@model` — optional fastembed model id (blank ⇒ `BAAI/bge-base-en-v1.5`).
- `%dim` — truncate a Matryoshka-trained model's vectors to `dim`.
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

Reference numbers (bundled set — 297 facts, 244 paraphrased queries; same table as
README § Benchmarking retrieval quality — keep the two in sync):

| backend | Recall@1 | Recall@3 | MRR@10 | bytes/fact |
|---|---|---|---|---|
| hash (lexical stub) | 0.148 | 0.234 | 0.210 | 288 |
| fastembed bge-small int8 | 0.398 | 0.611 | 0.518 | 432 |
| **fastembed bge-base int8 (default)** | **0.463** | **0.656** | **0.574** | 864 |

Reading them: the `hash` stub only matches shared vocabulary, so its recall is
floor-level and it exists as the zero-dep default, not as a quality target. Model
size is the real lever; int8 vs float is noise, so the compact int8 store stays.
Paired comparisons (McNemar exact, seeded bootstrap) are printed below the table.

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
- Facts no query targets are **hard-negative distractors** — plausible but irrelevant, to
  catch a backend that retrieves on surface features.
- `relevant` is a list of indices into `facts`.
- Scenario keys (`stm_scenario`, `antipattern_scenario`, `duplicate_cluster_scenario`,
  `confidence_scenario`) feed the flag-gated scenarios and never touch the headline Recall@k.
  In particular, **unanswerable queries live in `confidence_scenario.unanswerable`, never in
  `queries`** — `bench.retrieval.score_queries` would count them as misses and move the published numbers.

At 244 queries, deltas of ~0.1 clear the Wilson interval; smaller ones need the paired
tests. When adding a fact/query, keep the paraphrase gap (don't echo the fact's vocabulary
in its query) or the benchmark stops measuring what it's for. `bench/dataset-v1.json` is
frozen for reproducibility of earlier published figures.

---

## Recall-verdict calibration (`--confidence`)

Measures whether the `recall` tool's `ok` verdict means "the returned facts contain the
answer" (`bench/confidence_eval.py`). Answerable `queries` + `confidence_scenario.unanswerable`
run through the production on-demand path (`search_fused_with_stats` at `activated_k`); a
query is positive only if a gold fact is **returned**. Per candidate score (`current` is
production's `recall_confidence`; the others are alternatives under evaluation) it reports:

| Output | Meaning |
|---|---|
| AUROC [CI], ΔAUROC vs current [paired CI] | discrimination — rank-based, invariant to rescaling |
| Brier, ECE | calibration of 2-fold cross-fitted Platt probabilities |
| ok precision / recall at p ≥ `--ok-precision` | the gate as a calibrated score would ship (default 0.90) |
| shipped gate | `current ≥ recall_min_confidence` — the verdict as it ships today |

Density matters (the failure mode is many near-neighbours), so `--distractors N
--distractor-project <key|label>` pads the store with facts mined **at runtime** from a
snapshot of a real engram DB (`bench/distractors.py`, `bench/snapshot.py`) — never written
to the repo; contamination/privacy-flagged and dataset-near-duplicate facts are dropped.
`bench/replay_ledger.py` is the unlabelled reality check: it replays the last N real ledger
queries on a snapshot of the live store.


## Age-aware ranking (`--aged`)

DESIGN.md's contract is that recency decay only *orders* non-conflicting facts (conflicts are
removed by supersession), so an old relevant fact must not lose its rank for being old.
`bench/age_eval.py` stamps each dataset fact old (90–240 d) or new (0–14 d) by a seeded coin,
splits queries by the age of their gold, and scores **both** production rankers — `search`
(the per-prompt hook's priority score) and `search_fused` (the `recall` tool's rank fusion) —
at their shipped weights and weaker recency settings. Old-gold hit@3 is compared with the same
ranker with recency off (*age-blind* — on one store that is exactly "age carries no weight"; an
all-stamped-now store is not neutral for fusion, whose recency channel then ranks by insertion
order), and new-gold hit@3 with the shipped weights (what weakening recency costs recent facts);
both paired McNemar exact. The dataset cannot reward
recency (nothing in it is "newer and therefore truer"), so it measures one thing: whether age
overrides relevance. Fusion weights have no config knob; variants override them with a scoped
`mock.patch.dict` inside the bench only.

## LongMemEval session retrieval (`--longmemeval`)

`bench/longmemeval.py` scores four arms on LongMemEval-S (HF `xiaowu0162/longmemeval-cleaned`,
MIT — fetched at runtime with `--lme-download` into `<data dir>/bench-cache/`, never committed;
the run prints the file's sha256): **P** one document per session (user turns), pure cosine —
mempalace's 96.6% "raw" configuration, the only number comparable with theirs; **V** ~800-char
verbatim exchanges prepared exactly as capture stores them (`core/domain/episodes.prepare_exchanges`:
split → redact → length gate; `indexer.exchange_chunk_units`) through engram's hybrid chunk search;
**D** facts distilled per session through the `recall` tool path; **H** rank fusion of V and D
units. Metrics are LongMemEval's own at session level (recall_any@k, recall_all@5, NDCG@5, per
question type) plus engram's token axis (chars in the top-5 units). The sample is stratified over
*scoreable* questions (abstention questions have no evidence). **D always uses the offline
heuristic distiller** — never the configured one, which may be an LLM; an LLM-distilled run needs
explicit `--lme-llm N` (external calls, capped to N questions). ~40 s/question on fastembed.
---

## Adding a metric or backend

- A new **backend** is a new `EmbeddingGateway` implementation wired into
  `make_embedder()` in `bench/backends.py`; it then runs through the same store
  path, so its numbers are comparable.
- New **statistics** go in `bench/stats.py` (pure, seeded) with a known-value test in
  `tests/test_bench_stats.py`; tables print through `bench/report.print_rows`; ranking + Recall@k/MRR scoring live in
  `bench/retrieval.py` (`search_ranker`, `fused_ranker`, `score_queries`), throwaway stores in
  `bench/stores.build_store`.
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
