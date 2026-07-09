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
                              │ distil → embed → persist (tier=stm)
                              │ checkpoint: consolidate  (replay → displace → refine → purge)
                              ▼
              ${CLAUDE_PLUGIN_DATA}/memory.db   ◄── read-only ── localhost viewer
              (facts + int8/binary embeddings, tier + status, rows tagged by project)
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
3. **Distil, don't store transcripts.** Atomic facts (~15 tokens) instead of
   transcript chunks (hundreds). Lossy compression tuned for relevance.

## Cache efficiency

Hook `additionalContext` is wrapped in a system-reminder and inserted into the
`messages` array **at the point the hook fired**:

- **SessionStart** → near the head → stable all session → joins the prompt-cache
  prefix → read at ~0.1× on every later turn. Used for the **stable project core**.
- **UserPromptSubmit** → tail → does *not* bust the earlier cached prefix, but is
  never a same-turn cache hit and varies per turn. Used for **JIT episodic** recall
  only, kept tiny.

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
real quantised search path. Findings that drove the defaults:

| backend | Recall@1 | Recall@3 | MRR@10 | bytes/fact |
|---|---|---|---|---|
| hash (lexical stub) | 0.07 | 0.36 | 0.27 | 288 |
| fastembed bge-small int8 | 0.36 | 0.71 | 0.57 | 432 |
| fastembed bge-small float | 0.36 | 0.71 | 0.57 | 1536 |
| **fastembed bge-base int8 (default)** | **0.79** | **0.86** | **0.85** | 864 |

- **int8 ≈ float** — quantization loss is negligible, so the compact int8 store
  stays and float-rescore was measured *not* worth building.
- **Model size is the lever** — bge-base ~2.2× bge-small's Recall@1 for ~5ms/query
  (absorbed by the warm daemon). Hence bge-base is the default; bge-small remains
  available via `embedding_model` for constrained environments.

## Distillation — heuristic vs LLM

Retrieval quality is capped by *what is stored*, so the distiller is the largest
quality lever. Strategy pattern behind one interface:

- **HeuristicDistiller** (default) — dependency-free line extraction. Cannot detect
  conflicts, so it leans on similarity-based supersession.
- **ClaudeCliDistiller** (`distiller=claude`) — headless `claude -p`, defaulting to
  **Haiku** (the right tier for cheap extraction).
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
only *orders* non-conflicting facts. Folding conflict-resolution into the score
(as a single weighted formula would) lets a stale-but-frequent fact leak — the
hard filter prevents that.

**Honest limit on conflict detection.** Supersession fires on embedding
*similarity*, so it catches near-duplicates ("deploy target is X" → "deploy target
is Y") but not semantically-conflicting rewrites that share little vocabulary
("I live in Paris" vs "I moved to London"). Precise conflict detection needs
entity/attribute extraction — the LLM-distiller drop-in, which can emit explicit
`supersedes` links.

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
  full delta and `facts` output is byte-identical whether or not the register is on.
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
stages run in order `replay → displace → integrate → refine → purge`; each maps to a
mechanism and is individually gated and reversible:

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
- **Refine** (`refine.py`) — *SHY-style forgetting*: score every active fact with a pure
  **retention score** (`consolidation/scoring.py`) and archive the weakest. Two gated
  knobs make the cut *relative*, so it self-limits as the store grows (the SHY "only the
  relatively strong survive" property, achieved statelessly — no persisted running score):
  `refine_keep_max` keeps the top-N (an absolute count → **idempotent**), and
  `refine_prune_percentile` in `(0,1)` drops the weakest that fraction of the live active
  set (`≥1` = an absolute score floor). This keeps the active set small enough that
  brute-force search stays viable (see § the bytes layer / vector-store decision in the
  STM-LTM design). Split by blast radius: `refine_keep_max` ships **on** at 20000 (a generous,
  idempotent ceiling that only fires on runaway growth), while `refine_prune_percentile` ships
  **off** (it forgets every pass and a good rate is store-dependent). Both are **`engram
  eval`-gated** (they change what is injected); archival is a reversible status flip
  (`status='pruned'`), never a delete.
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
- **Pipeline order is engineering-first, not phase-order.** We run
  replay → displace → integrate → refine → purge so a fact about to be promoted leaves the STM
  overflow set *before* displacement (nothing is lost). This is a data-safety ordering,
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
| Store growth / stale facts | recency decay + supersession de-rank/retire old facts; idempotent capture; viewer prune |
| Over-eager supersession retires a distinct fact | conservative default threshold (0.85); superseded rows are archived (reversible), not deleted |
| Distillation quality (heuristic) | pluggable distiller; LLM adapter is the drop-in |
| Plugin/hook API drift | thin Claude-Code adapter; core is framework-agnostic |
| Durable queue becomes a de-facto dependency | `WorkQueue` is a stdlib-only SQLite queue behind a Separated Interface; no external backend or broker; core stays importable with the standard library alone |
| Consolidation prunes a still-useful fact | only `refine_keep_max` (a generous idempotent ceiling) ships on; the forgetting lever `refine_prune_percentile` is default-off; all are `engram eval`-gated; archival is a reversible status flip, not a delete; purge is default-off and only removes rows past a long cold horizon |
| STM leaks low-confidence facts into context | promotion is rehearsal/recall-gated; `stm_recall_weight` can down-rank STM; A/B with `engram eval` |

## Status of the levers

Done and measured:
- **Semantic embeddings** — `fastembed` (bge-base default), benchmarked vs the stub.
- **LLM distiller** — atomic facts + explicit `supersedes` links, via local Ollama
  (`distiller=ollama`, zero-token) or Claude on Haiku (`distiller=claude`).
- **Conflict resolution** — similarity supersession *and* explicit LLM links for
  vocabulary-disjoint conflicts.
- **Hard expiry** — TTL sweep with frequency protection.
- **Multi-store tiers + sleep pass** — explicit STM/LTM `tier` with rehearsal/recall
  promotion, an offline `consolidate()` pass (replay / displace / integrate / refine /
  purge), and a pure retention score. The consolidation knobs ship **split by blast radius**:
  non-destructive, reversible backstops default **on** (`integrate_threshold=0.92`,
  `refine_keep_max=20000`, `stm_capacity=2000`), while the levers that forget or destroy —
  `refine_prune_percentile` (compounds every pass) and `purge_horizon_days` (irreversible
  hard-delete) — stay **off**. All remain `engram eval`-gated.
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
