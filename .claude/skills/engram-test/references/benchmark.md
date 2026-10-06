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
(`bench/cli_args.add_eval_arguments`), so every scenario flag works from both: `--stm`,
`--antipatterns`, `--integrate`, `--confidence` (+ `--ok-precision`), `--aged`, `--longmemeval`
(+ `--lme-path` / `--lme-download` / `--lme-split` / `--lme-limit` / `--lme-out` / `--lme-shipped` / `--lme-llm`); `--latency` / `--latency-consolidation` (+ `--latency-n` / `--latency-python-n` / `--latency-out`);
`--distractors` pads the store for `--confidence` / `--aged`; `--store-project` / `--store-db` name the
real store that `--distractors` mines and `--latency` times (always on a snapshot).

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

## Tune on dev, report on test

A number tuned on the rows it is reported on overstates itself. The harnesses that **fit or
choose** something therefore share one fixed hold-out split, `bench/stats.py::stable_split`:
within each stratum the items with the lowest salted SHA-256 of their key go to dev, the rest to
test. Membership depends only on the keys (not on order, not on a seed), each stratum keeps its
share exactly, and an added item moves at most one existing item across the boundary. The salt
(`SPLIT_SALT`) is pinned by a known-value test — changing it re-deals every hold-out.

| Harness | Key · stratum | Dev / test | Fitted or chosen on dev | Reported on test |
|---|---|---|---|---|
| `--confidence` | query text · answerable | 50 / 50 | Platt `(a, b)` (the `platt (a, b) [dev]` column) | every other column, incl. the shipped gate |
| `--longmemeval` | question id · question type | 20 / 80 (93 / 377 of 470) | anything a change tunes (`--lme-split dev`, the default) | once, `--lme-split test` |

Tune with as many dev runs as you like; run test **once** per decision and report it as measured.
`--lme-split all` reproduces the earlier full-set figures. The 244-query paraphrase set is a
regression gate: a change must hold parity on it, never be tuned to it.

## Recall-verdict calibration (`--confidence`)

Measures whether the `recall` tool's `ok` verdict means "the returned facts contain the
answer" (`bench/confidence_eval.py`). Answerable `queries` + `confidence_scenario.unanswerable`
run through the production on-demand path (`search_fused_with_stats` at `activated_k`); a
query is positive only if a gold fact is **returned**. Per candidate score (`current` is
production's `recall_confidence` under the backend's `get_calibration` — the calibrated `pool_z`,
or never-`ok` for the `hash` stub; `top1` / `pool_z` / `topk_z` are the raw signals) it reports:

| Output | Meaning |
|---|---|
| AUROC [CI], ΔAUROC vs current [paired CI] | discrimination — rank-based, invariant to rescaling (test) |
| Brier, ECE | calibration of the dev-fitted Platt probabilities (test) |
| ok precision / recall at p ≥ `--ok-precision` | the gate at a dev-fitted Platt probability (default 0.90; test) |
| platt (a, b) [dev] | the dev Platt fit — where a shipped `Calibration`'s constants come from |
| shipped gate | production's `is_trusted(current, recall_min_confidence)` — the verdict as it ships (test) |

`--confidence-out` records carry `answerable`, so an offline fit or threshold sweep can reuse the
same split (`split()` keys on `q` and stratifies on `answerable`). Only the `ok` boundary
`z* = (logit(threshold) − b) / a` decides the verdict; `a` and `b` individually are poorly
identified at this sample size (a held-out re-check found half-size fits spread `a` over
0.39–0.90 while `z*` stayed centred on the shipped 4.52).

Density matters (the failure mode is many near-neighbours), so `--distractors N
--store-project <key|label>` pads the store with facts mined **at runtime** from a
snapshot of a real engram DB (`bench/distractors.py`, `bench/snapshot.py`) — never written
to the repo; contamination/privacy-flagged and dataset-near-duplicate facts are dropped.
`bench/replay_ledger.py` is the unlabelled reality check: it replays the last N real ledger
queries on a snapshot of the live store.


## Recall latency (`--latency`, `--latency-consolidation`)

The labelled set measures *quality* on a few hundred facts; `--latency` measures *cost* at the
size real stores reach (10⁵ facts), where recall is scan-bound (`bench/latency_eval.py`):

```bash
python3 bin/engram eval --backends fastembed --latency [--latency-consolidation] \
  --store-project <key|label> [--store-db <frozen copy>] [--latency-n 40] [--latency-python-n 3] \
  [--latency-out run.json]
```

- **What runs:** the project's last `--latency-n` distinct answered ledger questions, on a snapshot,
  through the two production read paths — the hook's `search` and the `recall` tool's
  `search_fused_with_stats` at `activated_k` — with the numpy scorer and (on the first
  `--latency-python-n`) the pure-Python one. Query embedding is done once up front and excluded;
  `now` is pinned to the snapshot's newest fact, so the run is deterministic. A backend whose
  dim isn't in the store (`stored_dims`) or a `+float` spec is skipped.
- **What it reports:** per path × scorer, p50 / p90 / max ms per query (clean runs); the median
  per-stage ms (load / scan / lexical / FTS / pool / fusion / other) from a separate instrumented
  pass that wraps the production callables listed in `STAGES` (update that table, not the
  harness, when a hot-path refactor renames a stage); and a **parity digest** — a hash of every
  query's ranked ids, exact score `repr`s and pool.
- **Proving a refactor is exact:** freeze one copy of the store (`sqlite3` online backup) and pass it
  as `--store-db` to both the before and the after run; equal digests per path × scorer mean
  byte-identical rankings (and so an unchanged calibrated confidence). The live DB changes with
  every capture, so two runs against it are not comparable.
- **`--latency-consolidation`** times one `consolidate()` pass per stage (`CONSOLIDATION_STAGES`,
  with each stage's changed-row count) on its own snapshot — consolidation writes. The whole run
  pins `distiller="heuristic"`: integrate's LLM tier would otherwise call `claude -p` per cluster,
  so the timings are the store-side cost only.
- Run it with the interpreter that serves recall (the managed fastembed venv), so numpy and the
  store's embedding model are present.


## Age-aware ranking (`--aged`)

DESIGN.md's contract is that recency decay *mostly orders* non-conflicting facts (conflicts are
removed by supersession) — it breaks near-ties, so an old relevant fact must not lose its rank
for being old. This benchmark is what set the hook's `w_recency` to 0.05 (DESIGN.md § Memory
lifecycle has the numbers).
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
question type) plus engram's token axis (chars in the top-5 units). `--lme-split` picks the hold-out
first (see *Tune on dev, report on test*), then `--lme-limit` samples within it, stratified over
*scoreable* questions (abstention questions have no evidence) — a sample never crosses the split. **D always uses the offline
heuristic distiller** — never the configured one, which may be an LLM; an LLM-distilled run needs
explicit `--lme-llm N` (external calls, capped to N questions). ~40 s/question on fastembed.
Session ids are replaced by neutral per-question ordinals at parse time: LongMemEval's own ids
label the evidence (`answer_…` vs `sharegpt_…` / `ultrachat_…`), and an exchange's title and
episode key are embedded and FTS-indexed, so a raw id would leak the answer to the ranker.
`--lme-out` appends each question's record as it finishes, so a long run stopped early keeps every
completed question (the stratified sample is round-robin by type, so any prefix stays balanced).

`--lme-shipped` adds three arms that run the **shipped** path end to end instead of the bench's
direct calls: each session is written as a Claude Code transcript and captured by
`capture_transcript_incremental` (verbatim prompts, distilled facts, exchanges, the `episode`
link), then read back through the model's two surfaces at their default `k` / character budget —
**Vs** `search_history` exchanges, **Ds** `recall` facts mapped to a session by their `episode`
only (an unlinked fact credits no session), **Hs** both fused. Paired rows V→Vs, D→Ds, H→Hs show
what the shipped surfaces lose or gain against the direct arms (~2× runtime). The distiller for
every arm comes from the run's config alone (`distiller_runs` pins the heuristic), and the harness
tests fail if any test reaches an LLM distiller subprocess.
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
