# claude-engram — design

Token-first, cross-project long-term memory for Claude Code, packaged as a plugin.

## The one constraint

Claude only consumes **text tokens**; bytes never enter the model — they enter
the *search* layer. So "efficiency" is two separate budgets:

- **Token budget** — tokens that reach the context window (recall injection).
- **Latency budget** — wall-clock added to a turn (query embedding + search).

Every decision below optimises one or both.

## Architecture (CQRS + Hexagonal)

Capture and recall have opposite performance profiles, so they are split:

- **Write side (capture)** — heavy, batch, latency-tolerant. Runs detached at
  `Stop` / `SessionEnd` / `PreCompact`. Zero interactive-token cost.
- **Read side (recall)** — tiny, hot-path, token- and latency-critical.

```
UserPromptSubmit ─► recall (embed → rank → gated inject)      ← hot path, tail of context
SessionStart     ─► core inject (small, stable)               ← joins cached prefix
Stop/SessionEnd/PreCompact ─► spawn detached capture worker   ← fire & forget
                              │ distil → embed → persist (tier=stm) · verbatim exchanges → index
                              │ checkpoint: consolidate  (replay → … → purge → forget; § sleep pass)
                              ▼
              ${CLAUDE_PLUGIN_DATA}/memory.db   ◄── read-only ── localhost viewer
              (facts + int8/binary embeddings, tier + status, rows tagged by project;
               index chunks: docs · code · snapshots · exchanges)
                              ▲
       durable WorkQueue (inproc SQLite) ─► rescue: re-distil degraded deltas
```

**Durable capture queue (built).** Detached capture publishes work items to a durable
**Command queue** (`WorkQueue`) so a dropped connection or an `ENGRAM_DISTILLER` outage
retries rather than degrades — behind a Separated Interface, with a single stdlib
`inproc` backend (SQLite `work_queue` with retry / backoff / dead-letter / lease-recovery),
never on the recall hot path. It is a Command queue (one handler, retry/dead-letter),
**not** an Event bus — the name says so. The port is retained so a future out-of-process
backend could attach without touching the core.
See the [`stm-ltm-membus` design](docs/generated/designs/stm-ltm-consolidation-and-memory-bus.md).

### POEAA / Cosmic Python patterns

| Role | Pattern | File |
|---|---|---|
| Overall shape | CQRS + Hexagonal (Ports & Adapters) | whole plugin |
| Capture pipeline | Command/Handler, idempotent per fact | `core/service.py` |
| Distil/rank/quantise/consolidate | Functional Core / Imperative Shell | `core/distill.py`, `recall/`, `domain/quantize.py`, `consolidation/{replay,refine,scoring}.py` |
| Memory access + STM/LTM tiers | Repository over Data Mapper (never Active Record); tiers = a `tier` column + `Store` methods, **not** a second Repository | `core/store.py` |
| Query params | Query Object | `core/recall.py::search` |
| Embedding provider | Gateway + Separated Interface | `core/ports/embedding.py`, `core/adapters/` |
| Durable per-memory work (rescue/consolidate) | Command queue (`WorkQueue`) behind a Separated Interface — **not** Events; single stdlib `inproc` backend; port retained for future backends | `core/ports/workqueue.py`, `core/adapters/inproc_queue.py` |
| Injected payload | DTO (deliberately one line/fact) | `core/recall.py::render_block` |
| Empty recall | Special Case / Null Object (inject nothing) | `render_block` returns `""` |
| Wiring | Composition Root | `bin/*` entry points |

## Token efficiency

1. **Hooks, not always-on tools.** Recall is a hook (zero standing cost, zero
   model agency). An MCP `recall` tool would only add value as an optional
   deep-search escape hatch — deferred tool schemas make its standing cost ~zero
   in Claude Code v2.1+, but it still needs the model to decide → search → call.
2. **Just-in-time + threshold-gated.** `UserPromptSubmit` injects only when a
   fact clears `min_sim`, capped at `top_k` / `max_chars`. Irrelevant turns cost
   nothing.
3. **Distil for injection; keep verbatim for on-demand detail.** What is *injected* is atomic
   facts (~15 tokens) instead of transcript chunks (hundreds) — lossy compression tuned for
   relevance. What is *kept* also includes each exchange verbatim (redacted, retention-bounded,
   index kind `exchange`), so the detail a fact dropped is one `search_history` call away and
   never injected. Measured on LongMemEval session retrieval (470 questions, fastembed, R@5):
   facts alone 0.891, verbatim exchanges 0.983 (p<0.001) — distillation drops what users state
   in passing (preferences, single-session details). Through the shipped tools end to end
   (transcript → capture → `recall` / `search_history`, hash, 470): `recall` alone 0.864,
   `recall` + `search_history` 0.913. Pulling the top 5 costs ~850 tokens of exchanges vs ~175
   of facts, so it is a pull, not a push. (The harness neutralises LongMemEval's session ids,
   which label the answer; the leak was measured negligible — 0 of 250 paired fastembed questions
   changed R@5.)

## Cache efficiency

Hook `additionalContext` is wrapped in a system-reminder and inserted into the
`messages` array **at the point the hook fired**:

- **SessionStart** → near the head → stable all session → joins the prompt-cache
  prefix → read at ~0.1× on every later turn. Used for the **stable project core**.
- **UserPromptSubmit** → tail → does *not* bust the earlier cached prefix, but is
  never a same-turn cache hit and varies per turn. Used for **JIT relevance** recall
  (distilled facts) only, kept tiny.

This is why recall is a **hybrid**: cache-friendly core + relevance-driven JIT.

## Latency efficiency

- Capture is fully **detached** — the hook spawns a worker and returns.
- Recall is brute-force cosine over **int8** vectors — sub-10ms for a personal
  store; no ANN index needed until ~500k facts.
- Hooks are **short-lived processes**, so a real embedding model would reload
  every turn. The optional **resident daemon** holds it warm; the hook is a thin
  client that **falls back to in-process** on any failure (fail-open).

## Embedding backend — measured, not assumed

`engram eval` runs a labelled paraphrase benchmark (Recall@1/@3, MRR@10) through the
real quantised search path. Findings that drove the defaults, measured on the original
64-fact / 77-query set (frozen as `bench/dataset-v1.json` so these figures reproduce; current
numbers on the larger mined set are in [README § Benchmarking](README.md)):

| backend | Recall@1 | Recall@3 | MRR@10 | bytes/fact |
|---|---|---|---|---|
| hash (lexical stub) | 0.07 | 0.36 | 0.27 | 288 |
| fastembed bge-small int8 | 0.36 | 0.71 | 0.57 | 432 |
| fastembed bge-small float | 0.36 | 0.71 | 0.57 | 1536 |
| **fastembed bge-base int8 (default)** | **0.79** | **0.86** | **0.85** | 864 |

- **int8 ≈ float** — quantization loss is negligible, so the compact int8 store
  stays and float-rescore was measured *not* worth building.
- **Model size is the lever** — bge-base ~2.2× bge-small's Recall@1 here for ~5ms/query
  (absorbed by the warm daemon); on the harder mined set the gap is smaller (0.463 vs 0.398)
  but still significant (paired McNemar p=0.033). Hence bge-base is the default; bge-small remains
  available via `embedding_model` for constrained environments.

Recall@k is one axis; the same harness measures the others a default rests on — `--confidence`
(does the `recall` verdict mean what it says; § Recall confidence below), `--aged` (does age
override relevance on either ranker), `--longmemeval` (verbatim vs distilled session retrieval;
§ Token efficiency), plus `--stm` / `--antipatterns` / `--integrate`. `--distractors N` pads the
store with real facts mined at runtime to reproduce density. Commands and output columns:
[README § Benchmarking](README.md) and `.claude/skills/engram-test/references/benchmark.md`.

## Recall confidence — measured

The `recall` MCP tool returns a `confidence` score and a verdict; `ok` tells the model to trust the
facts and skip a wider search, so it is only worth having if `ok` is usually right. It wasn't:

- **Ordering defect (fixed first).** Confidence read the *fused* first hit, which fusion's recency
  and frequency channels often make a newer, weaker fact — the gap to the "runner-up" (really the
  best match) went to zero. On 120 replayed real queries, 28% collapsed that way and 42% had the
  best cosine match demoted. Confidence now judges the best-matching hit (`best_match`), and the
  recall ledger's `recall_events.top_sim` records that hit's cosine — **rows written before this
  change hold the fused first hit's**, so compare old and new rows with care.
- **Formula defect.** The old gap × strength × identity score barely separated right from wrong
  recalls (AUROC 0.54 / 0.58 / 0.55 at 0 / 1,788 / 20,000 extra facts), so `ok` was right about
  half the time (precision 0.49 / 0.57 / 0.42).

**The score now** is the best match's **pool z-score** — how many standard deviations its cosine
sits above the similarity of *every* comparable fact recall scanned — Platt-scaled to 0-1
(`sigmoid(0.6341·z − 3.2706)`, `core/domain/confidence.py`, fitted in `core/recall`). Of the
candidates measured it was the only one better than the old formula at every density, and its fit
barely moves between embedding models. Measured on `engram eval --confidence` (fastembed bge-base,
244 answerable + 89 unanswerable queries), `ok` at the shipped `recall_min_confidence` 0.40:

| store (extra facts) | AUROC (old → new) | `ok` precision (old → new) | `ok` recall (old → new) |
|---|---|---|---|
| 0 | 0.54 → 0.71 | 0.49 → 0.73 | 0.42 → 0.12 |
| 1,788 on-topic | 0.58 → 0.64 | 0.57 → 0.63 | 0.37 → 0.24 |
| 20,000 off-topic | 0.55 → 0.68 | 0.42 → 0.55 | 0.31 → 0.49 |

- **A ranked score, not a probability.** The best Platt fit drifts with store size (b = −2.27 at 0
  extra facts, −3.27 at 20,000), so no fixed calibration holds a promised precision everywhere —
  the old 0.90 target is unreachable with any single signal. The constants are fitted where real
  stores live (tens of thousands of facts); on a small store the same match scores lower, so `ok`
  is rarer there but more often right — the safe error, since a missed `ok` only costs a search.
- **A store floor.** `ok` needs z ≥ 4.5, and one outlier among n values can sit at most √(n−1)
  standard deviations out, so a project with fewer than ~22 facts can never say `ok`.
- **The `hash` stub never says `ok`.** On its lexical vectors the same signal is near or below
  chance (pool z-score AUROC 0.59 / 0.56 / 0.44 at the three densities; no candidate exceeds 0.61), so
  a gateway that isn't `semantic` gets no calibration: `confidence` is `null`, the verdict is
  `low_confidence`, and the guidance says why (the honest Special Case, not a made-up number).
- **Ledger continuity.** `engram stats`' estimated savings count `ok` recalls; on real queries the
  `ok` rate moved from 15% to 62% with this change, so the estimate steps up without any real
  saving. It is excluded from the headline figure for exactly this kind of reason.

Reproduce: `engram eval --backends hash,fastembed --confidence [--distractors N
--distractor-project <key>] --confidence-out obs.jsonl` — the `platt (a, b)` column is where the
shipped constants come from; `bench/replay_ledger.py` replays real ledger queries unlabelled.

## Distillation — heuristic vs LLM

Retrieval quality is capped by *what is stored*, so the distiller is the largest
quality lever. Strategy pattern behind one interface:

- **HeuristicDistiller** (default) — dependency-free line extraction. Cannot detect
  conflicts, so it leans on similarity-based supersession.
- **ClaudeCliDistiller** (`distiller=claude`) — headless `claude -p`, defaulting to
  **Haiku** (the right tier for cheap extraction). Spawned inside a tight isolation
  envelope so a model reading the transcript-in-prompt cannot act on it: `--tools ""`
  disables the **built-in** tools, `--strict-mcp-config` (with no `--mcp-config`) loads
  **zero MCP servers** — without the latter the nested session would inherit ambient MCP
  servers (browser, tracker, …) *and* the project allow-list and perform side-effecting
  "ghost actions" — and `ENGRAM_DISABLE=1` no-ops engram's own hooks (recursion guard).
- **HTTPDistiller** (`distiller=ollama`) — POSTs to any OpenAI-compatible endpoint
  via stdlib urllib; point it at a local Ollama / LM Studio / llama.cpp / vLLM
  server for **zero-token, offline** distillation.

Both LLM backends run in the detached capture worker (off the interactive path),
produce genuinely atomic facts *and* explicit `supersedes` links — fixing the
vocabulary-disjoint conflict case (Paris → London) that similarity cannot — and
fall back to the heuristic on any failure so capture never breaks.

## Hard expiry (TTL sweep)

Recency decay only *de-ranks* old facts; a TTL sweep *retires* them. On capture (if
`ttl_days > 0`, off the interactive path) or via `engram sweep`, active facts unseen for
longer than the TTL are marked `expired` — unless reinforced past `ttl_keep_frequency`
(consolidation protects durable facts). Expiry is reversible (status flag, not delete),
and recall already filters to `status='active'`.

## Compact storage — the "bytes" layer

Per fact: the text (must stay text — it is what gets injected) + a quantised
embedding. int8 (~4× smaller, primary search rep) + binary sign-bits (32×, fast
Hamming pre-filter). The embedding *is* the compact semantic fingerprint.

## Memory lifecycle (cognitive model)

Standard vector similarity recalls stale and irrelevant facts. Three ideas from
memory research are layered on top, split cleanly by responsibility:

| Concept | Implementation | Where |
|---|---|---|
| Forgetting curve | exponential recency decay `e^(-λt)` (λ from `half_life_days`) | `core/domain/scoring.py` |
| Rehearsal (Atkinson–Shiffrin) | frequency boost — a fact seen again reinforces (freq++, recency refreshed) instead of duplicating, and transfers STM→LTM once rehearsed past `promote_after_freq` | `store.reinforce`, `store.promote`, `service.add_records` |
| Context-dependent retrieval | similarity gate (`min_sim`) suppresses facts whose cue doesn't match | `recall.search` |
| Retroactive interference | **hard supersession** — a near-identical newer fact archives older ones (`status='superseded'`, filtered at SQL) | `store.supersede`, `service._find_superseded` |

**Retrieval is a hybrid re-rank, not raw similarity.** Each candidate that clears
the similarity gate gets a Priority Score `sim·Ws + decay·Wr + freq·Wf`, and the
top-k by priority are injected.

**Conflicts vs ordering are deliberately separate.** Genuine conflicts are removed
by *hard supersession* (a superseded fact can never resurface); soft recency decay
mostly *orders* non-conflicting facts — its weight is kept small (`w_recency` 0.05) so it
breaks near-ties rather than overriding relevance. Measured with `engram eval --aged`: at
the former 0.3, relevant 90–240-day-old facts reached the injected top-3 in ~1% of queries
(vs 62% age-blind); at 0.05, 44% with no loss for recent facts (fastembed, bundled set).
Folding conflict-resolution into the score
(as a single weighted formula would) lets a stale-but-frequent fact leak — the
hard filter prevents that.

**Reaching vocabulary-disjoint conflicts.** The cosine path (`_find_superseded` at
`supersede_threshold`) only catches near-duplicates ("deploy target is X" → "…is Y"),
not semantically-conflicting rewrites that share little vocabulary ("deploy via Jenkins"
→ "deploys run on GitHub Actions"). Two mechanisms close that gap:
- **LLM `supersedes` links over similarity-selected candidates.** The distiller is shown
  the facts most *similar* to the captured session (any age), not just the newest, so it
  can emit a `supersedes` link against an old fact a change contradicts even when the
  wording is disjoint — "invalidated by a highly correlated memory"
  (`service._supersede_candidates`).
- **Demand-driven curation.** When a stale fact is actively misleading a session, it can be
  retired on the spot: `invalidate_memory` (MCP) / `engram forget` by id, or the
  distiller-assisted `engram review` / `review_memory` audit, which proposes stale /
  contradicted / redundant entries for keep/update/delete. All are reversible archives
  (`status='expired'` / `'superseded'`), never deletes (`service.invalidate_facts`,
  `review_memories`, `apply_review`).

**Age-based maturation.** STM→LTM promotion is no longer activity-only: alongside rehearsal
(`promote_after_freq`) and replay (recalled), a short-term fact older than `stm_max_age_days`
(default 1 day) *matures* into LTM regardless of activity, so STM stays a genuinely
short-term buffer instead of accumulating one-off facts forever. Recall-neutral at the
default `stm_recall_weight` — it only bounds STM growth (`consolidation/mature.py`).

**The forgetting curve — fades unless recalled.** The retention score (`consolidation/scoring.py`)
composes recency decay, recall (`use`), reinforcement (`frequency`), encoding depth,
novelty (`surprise`) and **importance** (`salience`, from the observation type via
`domain/scoring.salience_of` — a decision/bugfix outlasts a passing discovery). The `refine`
stage prunes (reversibly, `status='pruned'`) facts whose retention has decayed below
`refine_min_retention` — so a fact *fades over time unless it is recalled, reinforced, or
important*. It ships **off** (destructive, and a safe floor is store-size-dependent); note
`engram eval` is a recall-only benchmark and does **not** exercise consolidation, so the
floor is validated by unit tests + reasoning, not the benchmark.

**Supersession completes the fade at once — deliberately hard, not soft.** A newer fact that
supersedes an older one archives it immediately (`status='superseded'`, filtered at SQL),
rather than accelerating a gradual decay. This is the same measured choice as "conflicts vs
ordering are deliberately separate": a *soft*-decayed conflicting fact can still out-rank the
fact that replaced it while it fades, leaking stale-but-frequent memory. Hard supersession is
the strongest form of "the old entry's fade advances" — it advances to completion — and it
stays **reversible** (archived, restorable), which is the safety net a soft variant would add
complexity to provide. The soft accelerated-decay variant is therefore **not** adopted.

### Multi-store tiers + the "sleep" pass (built)

The lifecycle above is the *inline, read-side* control process. Layered on top are two
memory-research models, made explicit on the *write side*, off the hot path.

**Atkinson–Shiffrin multi-store model** — memory is staged, not flat:

| Store / process | claude-engram |
|---|---|
| Sensory register (raw, fleeting; *attention* selects; **one register, all modalities**) | the `sensory` table (`core/store.py`) — page snapshots (visual) and conversation deltas (verbal) enter here first and decay on capacity/TTL; *attention* selects what transfers (`core/domain/sensory.py`, `core/service.py`) |
| Short-term store (fresh, capacity-bounded, *displaced* when full) | `tier='stm'` facts; `stm_capacity` bounds the active STM set, `store.displace_stm` sheds the weakest |
| Rehearsal (STM→LTM transfer) | inline `store.reinforce` + `store.promote` (freq ≥ `promote_after_freq`); batch `replay` promotes STM that was *retrieved* |
| Long-term store (durable, semantic) | `tier='engram'` facts + decay + supersession + TTL |
| Long-term store (episodic, verbatim) | `exchange` chunks in the index — each conversation exchange, redacted; on demand only (`search_history`), never recalled or injected; facts link to theirs by `episode`; forgotten past `episodic_ttl_days` / `episodic_max_chunks` |
| Retrieval (LTM→use) | `recall.search` → `render_block` |

**Two distinct promotion signals — kept separate, not unified.** STM→LTM promotion
fires two ways, from two independently-supported mechanisms, and claude-engram keeps both:

- **Rehearsal (repetition)** — inline in `service.add_records`; a fact re-captured
  enough times (`frequency ≥ promote_after_freq`) is promoted. This is Atkinson–Shiffrin
  **maintenance rehearsal**.
- **Retrieval (use)** — batch in `consolidation/replay.py`; any STM fact recalled at
  least once is promoted. This is the **testing effect / retrieval-induced
  consolidation** (Roediger & Karpicke), a *different* driver from repetition.

Repetition and retrieval are complementary, so the two paths are deliberately **not**
folded into one rule — each encodes a distinct memory mechanism.

**One sensory register, all modalities (A-S Fig. 1).** The register is the single intake stage
*every* input enters — page accessibility snapshots (from Chrome DevTools / Playwright MCP, via a
`PostToolUse` hook) and the conversation delta at capture — mirroring Fig. 1's one
modality-columned register feeding a modality-columned long-term store. engram never *takes*
snapshots; it consumes the ones the browser tools already produce. **Attention** (a control
process, *not* rehearsal — it never touches `freq`/`reinforce`) gates two exits:

- **Verbal → SR → STS → LTS.** Conversation is *coded* (distilled) into facts; the STM tier is
  the short-term store and consolidation the LTM transfer — the pipeline above. `sensory_enabled`
  records the raw delta as a `verbal` observation *additively*, so distillation still reads the
  full delta and `facts` output is byte-identical whether or not the register is on. Alongside
  the coded facts (semantic memory), the delta's exchanges are also kept *uncoded* as the
  episodic trace (`exchange` chunks, redacted and length-gated) — an on-demand copy that never
  enters recall, which is why distillation stays the only thing injected. The earlier rule
  "verbal is only ever coded" gave way to measurement: facts alone lose ~9 points of LongMemEval
  session recall that the verbatim trace keeps (§ Token efficiency).
- **Visual → SR → LTS directly.** A page snapshot resists verbal coding, so (A-S §III) it enters
  the *visual* store directly — engram's index, as a `snapshot` chunk (`index_snapshot`), skipping
  the facts pipeline: the SR→LTS dashed path in Fig. 1. Attention here is *re-perception* of the
  same page (`record_visual_perception`, keyed on a normalised URL); promotion — the embedding —
  runs in the detached capture worker, never on the hook. Freshness is age-based (a URL isn't a
  file on disk), and snapshots never enter the `facts`/recall surface (modality isolation).

This is the **structures vs control processes** split A-S §III draws: the register, `facts` and
index are the permanent *structures*; attention, coding (distillation) and rehearsal are the
transient, `Config`-tunable *control processes* over them.

**Active Systems Consolidation Hypothesis + the Sequential Hypothesis** — an offline
"sleep" pass (`core/consolidation/`) runs at session checkpoints (not every turn, like
sleep itself), orchestrated by `consolidate()` and exposed as `engram consolidate`. Its
stages run in order `replay → mature → displace → integrate → refine → invalidate → purge →
forget` (the order `consolidate()` runs them); each maps to a mechanism and is individually gated
— every stage but purge and forget archives reversibly:

- **Replay** (`replay.py`) — *active systems consolidation*: short-term facts that were
  actually **recalled** graduate STM→LTM, mirroring selective, prioritized hippocampal
  replay. Additive — it only moves a tier, never removes.
- **Integrate** (`integrate.py`) — *REM-style integration*: cluster near-duplicate STM
  facts by cosine and collapse each cluster to one (reversible `status='merged'`). Two
  tiers, mirroring the embedding/distiller split: a stdlib **heuristic floor** (keep the
  strongest survivor, archive the rest) and an opt-in **LLM tier** (the distiller either
  *abstracts* the cluster into one merged fact or *vetoes* the merge as genuinely distinct;
  fail-open to the floor). Runs before refine so the retention cut scores a deduplicated
  set. Ships **on** at `integrate_threshold=0.92` — a low-risk near-identical mop-up sitting
  above `supersede_threshold` (0.85), which does the bulk of dedup on the write path; still
  `engram eval`-gated for any change.
- **Refine** (`refine.py`) — *SHY-style forgetting* + the **forgetting curve**: score every
  active fact with a pure **retention score** (`consolidation/scoring.py` — recency, recall,
  frequency, importance/`salience`, depth, novelty) and archive the weakest. Three gated
  knobs, each one meaning: `refine_keep_max` keeps the top-N (an absolute count →
  **idempotent**); `refine_prune_percentile` in `(0,1)` drops the weakest that fraction of the
  live active set (self-limiting as the store grows — the SHY "only the relatively strong
  survive" property, stateless, no persisted running score); and `refine_min_retention` is an
  absolute retention floor — a fact fades once its score decays below it *unless* recall,
  reinforcement, or importance keep it up. This keeps the active set small enough that
  brute-force search stays viable (see § the bytes layer / vector-store decision in the
  STM-LTM design). Split by blast radius: `refine_keep_max` ships **on** at 20000 (a generous,
  idempotent ceiling that only fires on runaway growth), while `refine_prune_percentile` and
  `refine_min_retention` ship **off** (they forget, and a good rate/floor is store-dependent).
  Archival is a reversible status flip (`status='pruned'`), never a delete. Note `engram eval`
  is recall-only and does **not** exercise consolidation, so these are validated by unit tests
  + reasoning, not the benchmark.
- **Forget** (episodic) — verbatim `exchange` chunks past `episodic_ttl_days` or beyond the
  per-project `episodic_max_chunks` cap leave the index (a hard delete: an on-demand trace, not a
  fact), and facts whose whole episode is gone lose their `episode` link. Ships on (180 days /
  20,000) because unbounded verbatim storage is the layer's storage and privacy risk.
- **Rescue** (`service.rescue`) — re-distils degraded deltas parked on the durable queue
  when an LLM distiller was down, so a transient outage doesn't leave low-quality facts
  behind. It needs the embedder + distiller and runs at the head of every capture, so it
  is **co-located with the write path**, not in the checkpoint-only sleep pass.

The **retention score** `R` is a single pure function over features the shell gathers
(use, recency, salience, encoding depth, surprise, capture frequency) — the
functional-core spine of refine, so it stays stdlib-testable and its weights are an
eval-tuned retrieval lever, not a hand-tuned constant.

**Where we deliberately diverge from the biology (honest limits).** The theories are a
principled vocabulary, not a fidelity target — the divergences below are engineering
choices, called out so the mapping isn't over-claimed:

- **No distinct REM *phase* — but integration is built.** Consolidation now does all
  three: **transfer** (replay), **transform** (integrate — dedup + LLM merge/abstraction),
  and **forgetting** (refine). What we deliberately *don't* model is a separate REM sleep
  *phase*: biologically, replay and SHY downscaling are NREM/slow-wave processes and
  REM-style integration follows in a later cycle, whereas here all stages run in one
  checkpoint pass. Entity-level conflict merge across disjoint vocabulary still relies on
  the LLM (`merge_cluster` in integrate, and the `supersedes` path at capture), since the
  cosine clustering only groups lexically/semantically near facts.
- **Pipeline order is engineering-first, not phase-order.** The order above promotes (replay,
  mature) before displacement so a fact about to be promoted leaves the STM overflow set first
  (nothing is lost). This is a data-safety ordering,
  not a claim to reproduce the NREM-then-REM sequence.
- **STM is a promotion-gated state, not a faster clock.** Atkinson–Shiffrin has the
  short-term store decay in *seconds*; that timescale deliberately does **not** transfer
  to a cross-session developer-memory tool, where a fact is "short-term" because it hasn't
  yet earned promotion — not because a timer is expiring. Both tiers share `half_life_days`
  and recall is tier-agnostic by default (`stm_recall_weight=1.0`); rapid short-term loss
  is approximated by **capacity displacement**, not a separate decay constant. The reason
  is domain-specific: a *fresh* fact is often the *most* relevant one (the thing being
  worked on right now), so accelerating STM decay would fight recall rather than help it.
  Any future STM-ranking change (e.g. defaulting `stm_recall_weight < 1`) is gated on first
  extending `engram eval` with a fresh/STM scenario so the effect can be measured.
- **"Rehearsal" and "consolidation" are now distinct terms.** Inline frequency-boost is
  *rehearsal* (Atkinson–Shiffrin maintenance); the offline sleep pass is *consolidation*
  (ASCH). Earlier revisions of this doc used "consolidation" for the inline boost —
  corrected above.

Full design + the durable `WorkQueue` that carries rescue/consolidate work items:
[`docs/generated/designs/stm-ltm-consolidation-and-memory-bus.md`](docs/generated/designs/stm-ltm-consolidation-and-memory-bus.md).

## Cross-project

One **global** store under `${CLAUDE_PLUGIN_DATA}` (survives plugin updates),
every row tagged with a project key. Recall defaults to the current project;
`cross_project` enables a penalised fallback. The viewer is the one component
that intentionally spans all projects.

### Project identity — the workspace root (hashed)

A memory belongs to **the folder the session was opened in** (`identity=workspace`,
default): the directory Claude Code was started in (`CLAUDE_PROJECT_DIR`, stable across a
terminal `cd`), else `cwd`. That folder's absolute path is hashed into the key and its
basename is the display label. This tracks the human's chosen boundary: a monorepo
subfolder opened as a workspace (`…/dune/moj-sak`) stays its own project rather than
folding into the git root (`ips-applications`), and a repo opened at its top
(`…/claude-engram`) does not fragment into a nested package (`plugins/engram`). Hashing the
path — not using the basename as the *key* — keeps identity collision-free where the label
is not (two `backend/` folders → distinct keys, shared label).

Earlier designs walked up from `cwd` to the nearest project marker (`.git`,
`pyproject.toml`, …). That over-corrects in two directions: it folds a workspace subfolder
up into a monorepo root, and it splits a repo whose subpackages carry their own markers
(the `plugins/engram/pyproject.toml` fragmentation observed in this very repo). It survives
as `identity=marker` for sessions launched from deep subdirectories that *should*
consolidate upward. In both modes an explicit `.engram-root` sentinel overrides the choice
(nearest ancestor wins), configurable for monorepo granularity via `markers`.

## Risks

| Risk | Mitigation |
|---|---|
| Hot-path embedding latency | local embedding + resident daemon; 5s hook timeout; fail open |
| Hook error breaks a turn | every hook exits 0 on any error, injects nothing |
| Irrelevant recall pollutes context | `min_sim` threshold + `top_k` + `max_chars` cap + project scoping |
| Cross-project leakage | project-scoped by default; fallback penalised and opt-in |
| Store growth / stale facts | recency decay + supersession de-rank/retire old facts; idempotent capture; viewer prune; verbatim exchanges bounded by `episodic_ttl_days` + `episodic_max_chunks` (reported by `engram doctor`) |
| Verbatim storage keeps secrets a distiller would drop | exchanges are redacted before storage (credentials, auth headers, token shapes, private keys, emails, non-project paths); local-only store; retention horizon; `episodic_enabled=false` turns the layer off |
| Over-eager supersession retires a distinct fact | conservative default threshold (0.85); superseded rows are archived (reversible), not deleted |
| Distillation quality (heuristic) | pluggable distiller; LLM adapter is the drop-in |
| Plugin/hook API drift | thin Claude-Code adapter; core is framework-agnostic |
| Durable queue becomes a de-facto dependency | `WorkQueue` is a stdlib-only SQLite queue behind a Separated Interface; no external backend or broker; core stays importable with the standard library alone |
| Consolidation prunes a still-useful fact | only `refine_keep_max` (a generous idempotent ceiling) ships on; the forgetting levers `refine_prune_percentile` and `refine_min_retention` are default-off; archival is a reversible status flip, not a delete; purge is default-off and only removes rows past a long cold horizon. (`engram eval` is recall-only, so it can't measure these — they're unit-tested instead.) |
| STM leaks low-confidence facts into context | promotion is rehearsal/recall-gated; `stm_recall_weight` can down-rank STM; A/B with `engram eval` |

## Status of the levers

Done and measured:
- **Semantic embeddings** — `fastembed` (bge-base default), benchmarked vs the stub.
- **LLM distiller** — atomic facts + explicit `supersedes` links, via local Ollama
  (`distiller=ollama`, zero-token) or Claude on Haiku (`distiller=claude`).
- **Conflict resolution** — similarity supersession *and* explicit LLM `supersedes` links for
  vocabulary-disjoint conflicts, the latter over **similarity-selected candidates (any age)** so
  an old fact a change contradicts is reachable — plus demand-driven curation
  (`invalidate_memory` / `engram forget`, and the distiller-assisted `review`).
- **Hard expiry** — TTL sweep with frequency protection.
- **Multi-store tiers + sleep pass** — explicit STM/LTM `tier` with promotion by rehearsal,
  recall, **and age** (`stm_max_age_days`, on by default — the time-based maturation path); an
  offline `consolidate()` pass (replay / mature / displace / integrate / refine / purge); and a
  pure retention score that now includes **importance** (`salience` from the observation type).
  The consolidation knobs ship **split by blast radius**: non-destructive, reversible backstops
  default **on** (`integrate_threshold=0.92`, `refine_keep_max=20000`, `stm_capacity=2000`,
  `stm_max_age_days=1`), while the levers that forget or destroy — `refine_prune_percentile`
  (compounds every pass), `refine_min_retention` (the forgetting-curve floor), and
  `purge_horizon_days` (irreversible hard-delete) — stay **off**. `engram eval` is recall-only
  and does not exercise consolidation, so these are validated by unit tests, not the benchmark.
- **REM-style integration** — the `integrate` stage: a stdlib heuristic dedup floor plus an
  opt-in LLM tier (`merge_cluster`) that abstracts a near-duplicate cluster into one fact or
  vetoes the merge. Reversible (`status='merged'`), fail-open; the LLM tier is opt-in, the
  heuristic floor ships on at `integrate_threshold=0.92`.
- **Durable capture queue** — `WorkQueue` Command queue: a stdlib `inproc` SQLite queue
  (retry / backoff / DLQ / lease-recovery), off the recall hot path. Port retained for a
  future out-of-process backend.

Remaining:
- **No separate REM sleep *phase*** — all consolidation stages run in one checkpoint pass,
  not a distinct NREM-then-REM cycle. A deliberate simplification, not a missing capability.
- **`hash`/heuristic remain the zero-dep defaults** — real recall needs
  `embedding=fastembed` (and an LLM distiller for best quality); these cost a
  dependency / tokens (or a local model), so they are opt-in.
- **STM ranking stays default tier-agnostic** — `stm_recall_weight=1.0`; the measurable
  lever exists (`engram eval --stm`) but flipping the default awaits eval tuning.
- **Eval set** is 297 facts / 244 queries (+ the STM scenario) after the 2026-07 mining
  pass — paired tests (McNemar / bootstrap) in the harness resolve ~0.05 deltas; the
  earlier 64/77 set is frozen as `bench/dataset-v1.json`.
- **LLM distiller latency/cost** is unbounded per session — batching / a cheaper
  model for distillation is a future tuning knob.
