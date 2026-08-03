# claude-engram

Token-first, cross-project **long-term memory + code/docs index** for Claude Code,
packaged as a plugin. It captures your sessions off the interactive path, distils
them into atomic facts, embeds those compactly, and injects the *relevant* ones
back into context — automatically, via hooks. Alongside memory it indexes your
codebase and docs into ranked symbol/section outlines, so recall and a
`search_code` / `get_symbol` lookup replace broad Grep/Glob/Read sweeps (measured
~2/3 fewer tokens). Local-first: no API key and no network in the default
configuration, no telemetry. The core runs on the Python standard library alone;
real semantic recall, the index, and LLM distillation are opt-in.

## Why it's efficient

Two budgets are optimised separately (see [DESIGN.md](DESIGN.md)):

- **Tokens** — recall is threshold-gated and byte-capped, so you pay tokens only
  in proportion to relevance. A stable per-project *core* is injected once at
  `SessionStart` (joins the prompt-cache prefix → cheap on every later turn); a
  tiny *just-in-time* block is injected per prompt only when something matches.
  The index returns outlines (qualname + signature + anchor), not file contents —
  you fetch one symbol's body on demand instead of reading whole files.
- **Latency** — capture (and any LLM distillation) is fully detached: zero
  interactive cost. Recall is a brute-force cosine over quantised (int8) vectors —
  sub-10ms for a personal store, and **numpy-vectorised** (numpy ships with the
  `fastembed` extra) so it stays within the recall hook's budget even on a 10⁵-fact
  store; it falls back to a pure-Python scan when numpy is absent. An optional
  resident daemon keeps the embedding model warm across the short-lived hook processes.

## How memory behaves (cognitive model)

Standard vector search recalls stale and irrelevant facts. claude-engram layers a
memory lifecycle on top, drawn from the **Atkinson–Shiffrin multi-store model** and
the **Active Systems Consolidation Hypothesis** (details, and the honest limits of the
mapping, in [DESIGN.md](DESIGN.md)):

- **Sensory register (intake)** — the fleeting first stage every perception enters: page
  accessibility snapshots (from the browser tools, via a `PostToolUse` hook) and the
  conversation delta at capture. It decays fast (capacity + TTL) unless *attention* selects
  it — a re-perceived page, or a distillation-worthy turn. Attention then transfers it onward
  **by modality**: a snapshot into the **index** (a durable `snapshot` chunk), conversation
  into **facts** via distillation. One register for all input, two exits — mirroring the
  Atkinson–Shiffrin structure (a single sensory register feeding a modality-columned store).
  engram never *takes* snapshots; it consumes the ones the browser tools already produce.
- **Recency decay** — a fact's rank score decays exponentially with age
  (`half_life_days`) unless reinforced.
- **Rehearsal, retrieval & maturation** — three complementary ways a fact promotes from the
  short-term to the long-term tier: *rehearsal* (re-captured past `promote_after_freq` —
  repetition), *retrieval* (recalled at least once — the testing effect, applied in the sleep
  pass below), and *maturation* (older than `stm_max_age_days` — the time path, on by default,
  so STM drains instead of piling up one-off facts). Reinforced facts (frequency↑, recency
  refreshed) rank higher and resist expiry rather than duplicating.
- **Context gate** — a fact is only injected if it clears a similarity threshold
  against the current prompt.
- **Supersession** — a newer fact retires conflicting older ones. Similarity catches
  near-duplicates; the LLM distiller adds explicit `supersedes` links for vocabulary-disjoint
  conflicts — and because it is shown the facts most *similar* to the session (any age, not
  just the newest), it can retire an OLD fact a change contradicts ("deploys run on GitHub
  Actions" → retires "deploy via Jenkins"). Supersession is hard and reversible.
- **On-demand curation** — when a stale fact is misleading a session, retire it directly
  (`invalidate_memory` MCP tool / `engram forget`) or run the distiller-assisted audit
  (`engram review` / `review_memory`) that proposes stale/contradicted/redundant entries for
  keep/update/delete. Reversible archives, never deletes.
- **Hard expiry** — an optional TTL sweep archives facts unseen past `ttl_days`,
  protecting ones reinforced past `ttl_keep_frequency`.
- **Consolidation ("sleep") pass** — at session checkpoints an offline pass *replays*
  recalled short-term facts into long-term, *matures* aged short-term facts, *displaces*
  short-term overflow, *integrates* near-duplicates (a stdlib dedup floor, or an opt-in LLM
  tier that merges/abstracts a cluster into one fact), and *refines* the store — **the
  forgetting curve**: pruning facts whose retention (recency + recall + reinforcement +
  importance) has decayed, so a fact fades over time unless recalled, reinforced, or important.
  Non-destructive backstops ship on (`integrate_threshold`, `refine_keep_max`, `stm_capacity`,
  `stm_max_age_days`); the levers that forget or destroy (`refine_prune_percentile`,
  `refine_min_retention`, `purge_horizon_days`) stay off. Archival is reversible; note the
  consolidation levers are not covered by the recall-only `engram eval`.
- **Recovery (rescue)** — when the LLM distiller is unavailable, capture falls back to a
  heuristic, flags the fact `degraded`, and parks the delta on a durable queue. A later
  healthy session re-distils it automatically, so a transient outage doesn't leave
  low-quality facts behind permanently.

## Code & docs index

The index is a second retrieval surface, keyed by the same project identity as
memory. It parses source into symbols and docs into sections, embeds them, and
serves ranked outlines:

- **Languages** — Python (stdlib `ast`) and TypeScript/JavaScript
  (`.ts/.tsx/.js/.jsx/.mjs/.cjs`) via `tree-sitter-language-pack`; Markdown/docs
  by heading structure.
- **Outlines, not dumps** — a match returns the qualified name, a
  signature/docstring summary, an anchor, and a **freshness** verdict
  (`fresh` / `edited` / `stale` / `gone`) checked against the live file. You then
  fetch one symbol's body with `get_symbol`.
- **Hybrid ranking** — FTS5 (bm25) fused with fastembed cosine via reciprocal-rank
  fusion, then diversity-budget packed so one file can't dominate results.
- **Kept current** — `SessionStart` auto-indexes (single-flight, file-capped), and
  a `PostToolUse` hook re-indexes each file you Edit/Write so the outline never
  drifts from disk.
- **Snapshots too** — a third kind alongside doc sections and code symbols: page
  accessibility snapshots the browser tools produce, promoted from the sensory register
  and searchable like the rest (freshness is age-based — a URL isn't a file on disk). Scope
  a search by kind (`docs` / `code` / `snapshots`).

## Memory-first enforcement (`ENGRAM_ENFORCE`)

A `PreToolUse` guard steers you to the cheap path before an expensive one: consult
`recall` / `search_code` / `search_docs` *first*, then Grep/Glob/Read to widen if
those come back weak or empty. A `PostToolUse` marker records the moment any engram
lookup tool runs (a maintenance `index_docs` doesn't count), so the guard can
enforce *ordering* rather than nagging blindly.

| `ENGRAM_ENFORCE` | Behaviour |
|---|---|
| `off` | Guard disabled. |
| `advisory` *(default)* | Reminders only, never blocks. One nudge per session to check memory/index before a search — Grep/Glob **or a filesystem search via Bash** (`rg`, `grep -r`, `find -name`); a nudge before reading a large code file whole. |
| `strict` | Grep/Glob **and Bash searches** (`rg`/`grep -r`/`find -name` — not `… \| grep` pipe filters) are **denied** until memory has been consulted this session (one `recall` — even an empty one — unlocks them). Reading a large **indexed** code file whole is denied (a `search_code` + `get_symbol`, or an `offset`/`limit` pre-edit peek, is expected instead). |

Independently, the code/docs **index** is surfaced *passively* on every prompt (`index_top_k`),
so relevant symbols/sections appear even when the model never calls `search_code` — see
[Configuration](#configuration).

The gate is deliberately cheap to satisfy: a single `recall` call clears it for the
rest of the session. It's fail-open — any error in the hook lets the tool through.

## Layout

```
claude-engram/
├── .claude-plugin/marketplace.json     # marketplace catalogue (lists the plugin)
└── plugins/engram/
    ├── .claude-plugin/plugin.json      # plugin manifest + userConfig
    ├── hooks/hooks.json                # SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd, PreCompact
    ├── commands/memory-viewer.md       # /engram:memory-viewer
    ├── core/                           # pure-Python core (Ports & Adapters): store, service, distill, indexer, chunking, symbol extractors
    ├── bin/                            # hook entry points, CLI (engram), MCP server, daemon
    │   ├── recall_session_start.py     #   SessionStart — core memory + orientation + memory-first directive
    │   ├── recall_prompt.py            #   UserPromptSubmit — just-in-time recall
    │   ├── prefer_memory.py            #   PreToolUse — memory-first guard (ENGRAM_ENFORCE)
    │   ├── mark_consulted.py           #   PostToolUse — records that memory was consulted
    │   ├── index_edit.py               #   PostToolUse — re-index edited files
    │   ├── index_snapshot.py           #   PostToolUse — record page snapshots into the sensory register
    │   ├── credit_read.py              #   PostToolUse — credit bounded reads of indexed files (ledger)
    │   ├── index_docs.py               #   SessionStart — auto-index the project
    │   ├── capture.py                  #   Stop/SessionEnd/PreCompact — detached capture + summary
    │   ├── mcp_server.py               #   MCP tools (recall, search_code, get_symbol, …)
    │   └── daemon.py                   #   optional resident embedder
    ├── bench/                          # labelled recall benchmark + dataset
    ├── viewer/                         # localhost browser (stdlib http.server) — STM / LTM / RnR / index tabs
    └── tests/                          # stdlib unittest / pytest suite
```

## MCP tools

The plugin exposes an `engram-memory` MCP server so the model can query memory and the
index on demand (these are what the memory-first guard steers toward):

| Tool | Returns |
|---|---|
| `recall` | Distilled facts for the current project with a calibrated verdict (`ok` / `low_confidence` / `no_memory`). |
| `search_code` | Ranked code-symbol outlines (qualname + signature + anchor + freshness). |
| `get_symbol` | One symbol's full source by anchor, with a symbol-precise freshness check. |
| `code_outline` | Whole-file / project symbol outline. |
| `search_docs` | Ranked doc-section outlines. |
| `get_doc_section` | One doc section's body by anchor. |
| `doc_outline` | Document/heading outline. |
| `index_docs` | (Re)index the current project's code + docs. |
| `list_projects` | Every project in the global store with its active-fact count. |
| `invalidate_memory` | Retire stale/wrong facts by `id` (from `recall`) — `delete` (archive) or `update` (replace with corrected text). Reversible. |
| `review_memory` | Distiller-assisted audit: proposes stale/contradicted/redundant entries (delete/update, with reasons) for you to act on with `invalidate_memory`. |

## Try it without installing

```bash
cd plugins/engram
python3 -m unittest discover -s tests        # smoke tests (all stdlib)
python3 bin/engram demo                         # capture sample facts, then recall
python3 bin/engram doctor                       # show config, project, counts
python3 bin/engram eval --backends hash         # recall-quality benchmark (add fastembed to compare)
python3 bin/engram viewer                       # browse at http://127.0.0.1:7801/
```

## Install as a plugin (dev)

```bash
claude --plugin-dir ./plugins/engram            # session-scoped, for iterating
```

Or add the marketplace and install:

```bash
/plugin marketplace add /path/to/claude-engram
/plugin install engram@claude-engram
```

Hooks then run automatically: memory is recalled and the project auto-indexed at
session start, relevant facts injected per prompt, the memory-first guard steers
tool use, edited files are re-indexed, and memory is captured (with a throttled
session summary) at stop / session end / pre-compact.

## CLI

```
engram doctor              show resolved config, project identity and fact count
engram capture             capture memory from stdin / --file / --transcript
engram recall <query>      run a just-in-time recall query for the current project
engram core                show the stable session-start memory block
engram review [--query Q]  distiller-assisted audit — propose stale/invalid memories to retire (--apply to enact)
engram forget <id>...      retire specific facts by id (reversible archive)
engram projects            list every project in the global store
engram prune               delete all memory for the current project
engram uninstall           uninstall the plugin, KEEPING memory (--purge-data to also remove it; --dry-run to preview)
engram import claude-mem   migrate memory from a claude-mem store into engram (--db/--project/--map/--dry-run/--batch/--yes)
engram sweep [--all]       archive stale facts (TTL expiry; --days N to override)
engram consolidate [--all] run the sleep pass: promote recalled STM, integrate near-duplicates + prune (if enabled)
engram queue [--all]       inspect the durable work queue (rescue backlog + dead-letter); --purge-dead/--purge-stage/--purge-all to clear
engram daemon              run the resident daemon (keeps the embedder warm)
engram viewer              launch the localhost viewer (STM / LTM / RnR / index tabs; delete a project via the 🗑 button)
engram stats [--all]       token-savings ledger: injected (cost) vs saved (targeted + bounded reads + recall shortcuts), net
engram eval --backends … [--stm]  benchmark embedding backends (paired stats when ≥2); --stm adds the STM-tier lever scenario
engram replay [--transcript-dir …]  counterfactual token savings from past session transcripts (trace-driven, conservative)
engram drift               pin/check the embedding-drift canary
engram setup               provision the private fastembed venv (one-time)
engram demo                capture sample facts then recall (end-to-end proof)
```

`engram stats` is the effectiveness dashboard. It accounts both sides of the token
budget from a local usage ledger: **cost** = bytes injected per prompt / at session
start; **saved** = *measured* (a `get_symbol` / `get_doc_section` read of one unit —
or a bounded `offset`/`limit` `Read` of an indexed file — instead of the whole file,
real file-minus-span bytes) plus *estimated* (each `ok` recall verdict scored as one
avoided grep+read, heuristic). The **net** is saved − cost. Passive injection that
merely *might* have saved a search isn't credited, so net is a conservative floor,
not a marketing number.

### Importing from claude-mem

`engram import claude-mem` migrates an existing **claude-mem** store into engram's own
store — one-way, **idempotent**, and **read-only** on the source. Each claude-mem
*observation* fans out to one fact per entry in its `facts[]` (carrying
title/subtitle/narrative/type/files), and each non-empty *session-summary* field becomes a
fact; original timestamps are preserved. The source DB path is resolved from claude-mem's
own settings/env (override with `--db`).

claude-mem records only a project *label*, so you map it to an engram project **path**
(which derives the key). Precedence: `--map LABEL=/abs/path` > an existing engram project
with that label > **skipped** (a label is never written under a guessed key).

```bash
# 1. Preview — no writes; resolves the target key and counts per project
engram import claude-mem --project ukh-world --map ukh-world=/path/to/ukh-world --dry-run
# 2. Import (--yes is required in a non-interactive shell; otherwise it prompts)
engram import claude-mem --project ukh-world --map ukh-world=/path/to/ukh-world --yes
# 3. Compress the raw import — collapse near-duplicates, displace/purge weak STM
engram consolidate          # run in the target workspace (or --all for every project)
```

Re-runs are safe: fact IDs are content hashes, so an already-imported fact reinforces
instead of duplicating. Facts land in **STM**; superseding and spread-activation edges are
deferred to `consolidate` (a raw import can be large — ~10⁵ facts — so the first
consolidation is the expensive one; run it per-project first to bound blast radius). A store
that large stays recallable within the hook budget via the numpy-vectorised scan (see
`scorer` in [Configuration](#configuration)).

> **TTL caveat.** Because original timestamps are preserved, if you have set `ttl_days > 0`
> a `sweep` will archive imported facts older than that window immediately. `ttl_days` is
> `0` (off) by default — keep it off during and after a historical import.

### Uninstalling (your memory is kept by default)

`claude plugin uninstall <plugin>` **deletes the plugin's data directory by default** —
for engram that is the SQLite memory store *and* the provisioned venv (pass `--keep-data`
to the raw command to preserve it). To make destruction opt-in rather than the default,
uninstall through the plugin instead:

```bash
engram uninstall               # uninstalls but KEEPS your memory (passes --keep-data)
engram uninstall --purge-data  # also removes the store + venv — explicit opt-in
engram uninstall --dry-run     # print exactly what would happen; change nothing
```

The memory store lives at `~/.claude/plugins/data/engram-<marketplace>/`; removing that
directory is the only way to erase it. A `PreToolUse` guard also warns before a raw
`claude plugin uninstall` that lacks `--keep-data`, so an accidental wipe is caught first.

## Configuration

Set via the plugin's `userConfig` (exposed to scripts as `CLAUDE_PLUGIN_OPTION_*`)
or `ENGRAM_*` env vars for standalone use:

| Key | Default | Meaning |
|---|---|---|
| `embedding` | `hash` | `hash` (lexical stub, zero deps) or `fastembed` (real semantic model, self-provisions a venv) |
| `embedding_model` | *(blank)* | fastembed model id; blank = `BAAI/bge-base-en-v1.5` (best measured recall) |
| `embedding_truncate_dim` | `0` | Matryoshka truncation: keep the first N dims and re-normalise (0 = off). Only for Matryoshka-trained models (e.g. `nomic-ai/nomic-embed-text-v1.5`); changing it invalidates stored vectors — re-capture/re-index |
| `scorer` | `auto` | recall similarity-scan backend: `auto` (numpy if importable, else pure-Python) / `python` / `numpy`. numpy vectorises the cosine scan (~100× faster) so large stores stay under the recall-hook budget; ships with the `fastembed` extra. Ranking is identical to the pure-Python scan |
| `distiller` | `claude` | `claude` (headless `claude -p`, Haiku), `ollama` (local, zero-token), or `heuristic` (line extraction, no LLM) |
| `distiller_model` | *(blank)* | claude: model alias (blank = `haiku`); ollama: model name (blank = `qwen2.5:3b`) |
| `distiller_base_url` | `http://localhost:11434/v1` | OpenAI-compatible endpoint for the `ollama`/`http` distiller (ignored under `claude`) |
| `antipatterns` | `true` | mine admitted mistakes into durable `antipattern` memories (a strict rule + do/don't), surfaced to prevent repeats; gated by an admission-marker scan, runs in the detached worker. No-op without an LLM distiller |
| `review_enabled` | `true` | enable the distiller-assisted curation aid (`engram review` / `review_memory` MCP tool): the LLM proposes stale/contradicted/redundant memories to retire (keep/update/delete). No-op without an LLM distiller; direct `invalidate_memory` / `engram forget` always works |
| `top_k` | `3` | facts injected per prompt — the small injected focus (Cowan ~4) |
| `activated_k` | `0` | breadth the on-demand `recall` MCP tool searches (0 = use `top_k`); the broader "activated LTM" beyond the injected focus, no per-prompt token cost |
| `core_scaffold` | `false` | render the session core as a titled scaffold (facts grouped by card title) instead of a flat list — an LT-WM retrieval structure; same char budget |
| `spread_weight` | `0` | associative spreading activation (ACT-R): 0 = off (no edges, hot path untouched); >0 records co-occurrence/shared-entity edges at capture and boosts co-activated candidates at recall — `engram eval`-tune before enabling |
| `min_sim` | `0.12` | similarity threshold to inject |
| `core_size` | `5` | stable facts injected at session start (0 disables) |
| `max_chars` | `800` | hard cap on injected characters (token guard) |
| `index_top_k` | `2` | code/docs **index** hits injected per prompt (0 disables) — FTS-prefiltered then cosine-reranked, so it's hot-path-cheap regardless of index size |
| `index_min_sim` | `0.18` | similarity threshold for a passive index hit |
| `index_max_chars` | `400` | hard cap on the passive index block per prompt |
| `cross_project` | `false` | fall back to other projects when in-project recall is weak |
| `identity` | `workspace` | how a project is keyed: `workspace` = the folder you opened (`CLAUDE_PROJECT_DIR`, else cwd); `marker` = walk up to the nearest project marker. `.engram-root` overrides both |
| `half_life_days` | `30` | recency half-life; lower = forgets faster |
| `supersede_threshold` | `0.85` | new-fact similarity that retires an older one (1.0 disables) |
| `supersede_candidates` | `60` | how many existing facts the LLM distiller is shown as supersede candidates — most-similar first (any age), then most-recent, so it can retire an old fact a change contradicts even when the wording is disjoint. Capture-path only |
| `supersede_candidate_min_sim` | `0.3` | minimum cosine for a fact to be offered as a supersede candidate (a loose gate — the LLM makes the final call) |
| `ttl_days` | `0` | archive facts unseen this long on capture (0 disables hard expiry) |
| `ttl_keep_frequency` | `3` | facts reinforced this often are never expired |
| `recall_min_confidence` | `0.35` | confidence the `recall` tool needs to report verdict `ok` |
| `recall_max_chars` | `1200` | character budget for facts returned by the `recall` tool |
| `viewer_autostart` | `true` | start the localhost viewer detached at session start |
| `viewer_port` | `7801` | port for the always-on memory/index viewer |

Env-only knobs (no `userConfig` entry):

| Env var | Default | Meaning |
|---|---|---|
| `ENGRAM_ENFORCE` | `advisory` | memory-first guard strength — `off` / `advisory` / `strict` (see above) |
| `ENGRAM_DAEMON` | *(unset)* | `1` makes the recall hook use the resident daemon instead of loading the model in-process |
| `ENGRAM_PYTHON` / `python` userConfig | *(blank)* | pin an interpreter that already has fastembed; blank = auto-provisioned managed venv |

Advanced ranking weights (`w_sim`, `w_recency`, `w_freq`) are tunable via `ENGRAM_*`
env vars; defaults `1.0 / 0.3 / 0.2`.

### Memory lifecycle — STM/LTM tiers & consolidation

Fresh facts enter a short-term tier and promote to long-term on rehearsal, on recall,
or on **age** (`stm_max_age_days`, on by default); a consolidation ("sleep") pass runs
at session checkpoints (or `engram consolidate`). Recall is tier-agnostic and **destructive
pruning is off by default** — turn the forgetting curve on deliberately. Set via
`userConfig` (or `ENGRAM_*` env):

| Key | Default | Meaning |
|---|---|---|
| `promote_after_freq` | `2` | reinforcement count that promotes an STM fact to LTM (rehearsal) |
| `stm_max_age_days` | `1` | age at which a short-term fact matures into LTM regardless of activity — the time-based promotion path, so STM stays short-term instead of piling up one-off facts. On by default; recall-neutral at the default STM recall weight (0 = off) |
| `stm_capacity` | `2000` | max active STM facts before the weakest are displaced — a generous, reversible backstop against runaway STM growth (0 = unbounded/off). With maturation on, STM is bounded by age; this is only a burst cap |
| `stm_recall_weight` | `1.0` | recall weight for STM facts (1.0 = tier-agnostic; `<1` down-ranks STM) |
| `integrate_threshold` | `0.92` | consolidation merges near-duplicate short-term facts at/above this cosine similarity, keeping one survivor (reversible) — ships on as a low-risk near-identical mop-up above `supersede_threshold` (0 = off) |
| `refine_keep_max` | `20000` | keep only the top-N facts by retention score, prune the rest (reversible) — ships on as a generous idempotent growth ceiling (0 = off) |
| `refine_prune_percentile` | `0` | prune the weakest fraction of active facts by retention each pass — a self-limiting cohort percentile in `(0,1)` (`0.1` = drop the weakest 10%). Off by default: it forgets every pass and a good rate is store-dependent (0 = off) |
| `refine_min_retention` | `0` | **the forgetting curve** — prune (reversibly) any fact whose retention score has decayed below this floor, so a fact fades over time *unless* recalled, reinforced, or important. Idempotent; anti-patterns exempt. Off by default (destructive; a safe floor is store-size-dependent and `engram eval` can't measure it) — start around `0.1`–`0.2` (0 = off) |
| `purge_horizon_days` | `0` | hard-delete facts archived longer than this, then `VACUUM`. Off by default: the only irreversible lever (0 = off) |

### Sensory register — intake

The fleeting first stage all perception enters (page snapshots + conversation), before
attention transfers the worthy parts onward (snapshots → index, conversation → facts). A
separate, capacity- and TTL-bounded table that never touches recall. Set via `userConfig`
(or `ENGRAM_*` env):

| Key | Default | Meaning |
|---|---|---|
| `sensory_enabled` | `true` | record perception into the register — page snapshots via a `PostToolUse` hook + the conversation delta at capture; promotion (embedding) runs in the detached worker |
| `attention_window_seconds` | `300` | window within which re-perceiving the same page counts as *attention* (which promotes it) — the A-S selective read-out, not rehearsal |
| `sensory_capacity` | `64` | max live perceptions per project before the oldest unattended ones decay (0 = unbounded) |
| `sensory_ttl_seconds` | `900` | how long an unattended perception lives before it decays; decayed tombstones are purged past this age (0 = no TTL decay) |

### Durable work queue — WorkQueue

Detached capture and recovery run through a durable **Command** queue (one handler per
item, at-least-once with retry + dead-letter) — **not** an event bus. It is a
zero-dependency SQLite queue (`work_queue`): idempotent publish, retry + backoff,
dead-letter past `queue_max_deliver`, and crash recovery via lease expiry. It never runs
on the recall hot path. Inspect it with `engram queue`. Tune via `userConfig` (or
`ENGRAM_*` env):

| Key | Default | Meaning |
|---|---|---|
| `queue_max_deliver` | `5` | delivery attempts before a work item is dead-lettered |
| `queue_backoff` | `5,30,120,600` | retry backoff schedule, seconds (comma-separated) |
| `lease_ttl` | `300` | seconds a claimed item is leased before reclaim (crash recovery) |
| `queue_dead_after` | `604800` | dead-letter a pending item unprocessed this long (7 days); 0 disables. Inspect/clear with `engram queue` |

The queue sits behind a Separated Interface (`core/ports/workqueue.py`), so a future
out-of-process backend could attach without touching the core; today the sole backend
is the in-process SQLite queue.

## Real semantic recall (recommended)

The lexical `hash` stub only matches shared vocabulary. For recall that
generalises across wording, use `fastembed`. On first use it **self-provisions a
private venv** under the plugin data dir and downloads the model once — no manual
`pip` needed; set `embedding=fastembed` and go. To keep the model warm across the
short-lived hook processes:

```bash
python3 bin/engram daemon                 # keep the model warm
export ENGRAM_DAEMON=1                     # recall hook uses the daemon, else in-process
```

## Better distillation (atomic facts + explicit supersedes)

The heuristic distiller just splits lines. An LLM distiller produces genuinely
atomic facts and explicit `supersedes` links (the fix for vocabulary-disjoint
conflicts). It runs in the detached capture worker — off the interactive path —
and falls back to the heuristic on any failure (flagging the fact for later
re-distillation), so capture never breaks. Two backends:

**Claude, using an efficient model (default):** distillation is a cheap task, so it
defaults to **Haiku**, not Opus/Sonnet.

```bash
# distiller=claude is the default; distiller_model defaults to "haiku"
```

**Local, zero-token:** any OpenAI-compatible local server. Distillation is simple
extraction, so a small model suffices.

```bash
ollama pull qwen2.5:3b        # or llama3.2:3b
# set distiller=ollama  (defaults: base_url http://localhost:11434/v1, model qwen2.5:3b)
```

## Benchmarking retrieval quality

`engram eval` runs a labelled paraphrase set through the real quantised search path
and reports Recall@1/@3, MRR@10, and operational cost. Backend spec is
`name[@model][+float]`:

```bash
python3 bin/engram eval --backends "hash,fastembed,fastembed@BAAI/bge-small-en-v1.5,fastembed+float"
python3 bin/engram eval --backends hash --stm    # add the STM-tier lever scenario
```

The `--stm` scenario reports how `stm_recall_weight` trades off recall of fresh
short-term facts against their older long-term competitors — the measurable check
before changing any STM-ranking default (short-term is a *state*, not a faster clock).

Measured on the bundled set (297 facts, 244 paraphrased queries — mined from real
sessions, with 50 hard negatives; the earlier 64/77 set is frozen as
`bench/dataset-v1.json` for reproducibility of published figures):

| backend | Recall@1 | Recall@3 | MRR@10 | bytes/fact |
|---|---|---|---|---|
| hash (lexical stub) | 0.148 | 0.234 | 0.210 | 288 |
| fastembed bge-small | 0.398 | 0.611 | 0.518 | 432 |
| **fastembed bge-base (default)** | **0.463** | **0.656** | **0.574** | 864 |

The harness also prints paired comparisons (McNemar exact on Recall@k, seeded
bootstrap on MRR), which resolve smaller between-backend deltas than the
per-backend intervals: bge-base beats bge-small at p=0.033 on this set, while
retrieval-specialised and larger alternatives (arctic, mxbai-large) failed to
beat it. int8 quantization loss is negligible (int8 ≈ float), so the store stays
compact. For Matryoshka-trained models, `embedding_truncate_dim` trades ~3×
smaller vectors for a statistically insignificant recall drop (measured on
nomic-v1.5 at 256 dims). Use the harness to A/B any future change before shipping it.

## Project identity

Memory and the index are keyed by **the folder you opened** (`identity=workspace`, the
default): the directory Claude Code was started in (`CLAUDE_PROJECT_DIR`, stable even if
the terminal `cd`s), falling back to the working directory. That folder's absolute path
is hashed into a stable key and its basename becomes the label. This matches the human's
chosen boundary — a monorepo subfolder opened as a workspace
(`…/ips-applications/applications/dune/moj-sak`) stays its own `moj-sak` project rather
than being folded into the monorepo root, and a repo opened at its top (`…/claude-engram`)
does not fragment into a nested package (`plugins/engram`). Hashing the path (not using the
basename as the *key*) keeps it collision-free: two different `backend/` folders get
distinct keys though they share a label.

Two escape hatches:

- **`identity=marker`** restores the legacy behaviour — walk up from the working directory
  to the nearest `.git` / `pyproject.toml` / `package.json` / `go.mod` / `Cargo.toml` /
  `pom.xml` (configurable via `markers`) and key on that. Useful when sessions are launched
  from deep subdirectories and should consolidate upward.
- An empty **`.engram-root`** file **overrides both modes**: drop one in a directory to pin
  it as the project root (the nearest `.engram-root` ancestor wins). Use it to consolidate a
  monorepo subtree, or to pin a root when neither mode fits.

## Status

Working end to end (240 tests, 10 skipped). Defaults are local-first and
zero-dependency (`hash` embedding + `heuristic` fallback); real recall is opt-in
via `fastembed` (bge-base, self-provisioning venv) and, for best quality, an LLM
distiller (`distiller=claude` on Haiku by default, or `distiller=ollama` for
zero-token local). The memory lifecycle adds explicit STM/LTM tiers with
rehearsal- and retrieval-based promotion and a consolidation ("sleep") pass (replay →
displace → integrate near-duplicates → refine/forget → purge); capture and recovery
run through a durable, zero-dependency SQLite **Command** queue (retry + dead-letter +
crash recovery), off the recall hot path. See
[DESIGN.md](DESIGN.md) for the full architecture, POEAA pattern choices, caching
analysis, memory-lifecycle model, benchmark, and risk register.
