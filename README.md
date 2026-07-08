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
  interactive cost. Recall is brute-force cosine over quantised (int8) vectors,
  sub-10ms for a personal store. An optional resident daemon keeps the embedding
  model warm across the short-lived hook processes.

## How memory behaves (cognitive model)

Standard vector search recalls stale and irrelevant facts. claude-engram layers a
memory lifecycle on top, drawn from the **Atkinson–Shiffrin multi-store model** and
the **Active Systems Consolidation Hypothesis** (details, and the honest limits of the
mapping, in [DESIGN.md](DESIGN.md)):

- **Recency decay** — a fact's rank score decays exponentially with age
  (`half_life_days`) unless reinforced.
- **Rehearsal & retrieval** — two complementary ways a fact promotes from the
  short-term to the long-term tier: *rehearsal* (re-captured past `promote_after_freq` —
  repetition) and *retrieval* (recalled at least once — the testing effect, applied in
  the sleep pass below). Reinforced facts (frequency↑, recency refreshed) rank higher and
  resist expiry rather than duplicating.
- **Context gate** — a fact is only injected if it clears a similarity threshold
  against the current prompt.
- **Supersession** — a newer fact retires conflicting older ones. Similarity
  catches near-duplicates; the LLM distiller adds explicit `supersedes` links for
  vocabulary-disjoint conflicts ("I moved to London" → retires "I live in Paris").
- **Hard expiry** — an optional TTL sweep archives facts unseen past `ttl_days`,
  protecting ones reinforced past `ttl_keep_frequency`.
- **Consolidation ("sleep") pass** — at session checkpoints an offline pass *replays*
  recalled short-term facts into long-term, *displaces* short-term overflow, *integrates*
  near-duplicates (a stdlib dedup floor, or an opt-in LLM tier that merges/abstracts a
  cluster into one fact), and *refines* the store by pruning the lowest-retention facts.
  Non-destructive backstops ship on (`integrate_threshold`, `refine_keep_max`, `stm_capacity`);
  the levers that forget or destroy (`refine_prune_percentile`, `purge_horizon_days`) stay off.
  All are eval-gated and archival is reversible.
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

### Prefer a11y snapshots over screenshots (`ENGRAM_PREFER_SNAPSHOT`)

A second, independent `PreToolUse` nudge fires when the model is about to **screenshot**
a page (Chrome DevTools MCP `take_screenshot`, Playwright MCP `browser_take_screenshot`,
BrowserMCP `browser_screenshot`). A screenshot costs ~1,500+ vision tokens; the
accessibility-tree **text** snapshot (`take_snapshot` / `browser_snapshot`, or engram's
own `compact_page_view` tool) is ~10–50× cheaper (measured **~32× median** —
`python3 bench/visual_tokens.py`) and yields stable element refs, enough for most
visual/E2E structure and assertion work. `ENGRAM_PREFER_SNAPSHOT` = `off` / `advisory`
*(default — a once-per-session reminder, never blocks)* / `strict` (denies the screenshot).
It only steers *before* the call — a hook can't reclaim tokens from an image another MCP
server has already returned. Fail-open, like every hook.

The `compact_page_view` tool's `playwright` / `chrome-devtools` backends need the optional
`playwright` package installed in the interpreter engram runs under (its managed venv, or a
pinned `ENGRAM_PYTHON`); without it, the tool fails soft to an empty snapshot.

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
    │   ├── prefer_snapshot.py          #   PreToolUse — prefer a11y snapshot over screenshot (ENGRAM_PREFER_SNAPSHOT)
    │   ├── mark_consulted.py           #   PostToolUse — records that memory was consulted
    │   ├── index_edit.py               #   PostToolUse — re-index edited files
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
| `compact_page_view` | A page's accessibility-tree **text** snapshot — a token-cheap alternative to a screenshot for visual/E2E testing (measured ~32× cheaper). Needs `snapshotter=playwright`/`chrome-devtools` + the optional `playwright` dep; default `stub` returns a sample. |

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
engram projects            list every project in the global store
engram prune               delete all memory for the current project
engram uninstall           uninstall the plugin, KEEPING memory (--purge-data to also remove it; --dry-run to preview)
engram sweep [--all]       archive stale facts (TTL expiry; --days N to override)
engram consolidate [--all] run the sleep pass: promote recalled STM, integrate near-duplicates + prune (if enabled)
engram nats status|start|stop  manage the opt-in NATS server (bus=nats)
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
| `distiller` | `claude` | `claude` (headless `claude -p`, Haiku), `ollama` (local, zero-token), or `heuristic` (line extraction, no LLM) |
| `distiller_model` | *(blank)* | claude: model alias (blank = `haiku`); ollama: model name (blank = `qwen2.5:3b`) |
| `distiller_base_url` | `http://localhost:11434/v1` | OpenAI-compatible endpoint for the `ollama`/`http` distiller (ignored under `claude`) |
| `antipatterns` | `true` | mine admitted mistakes into durable `antipattern` memories (a strict rule + do/don't), surfaced to prevent repeats; gated by an admission-marker scan, runs in the detached worker. No-op without an LLM distiller |
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
| `ttl_days` | `0` | archive facts unseen this long on capture (0 disables hard expiry) |
| `ttl_keep_frequency` | `3` | facts reinforced this often are never expired |
| `recall_min_confidence` | `0.35` | confidence the `recall` tool needs to report verdict `ok` |
| `recall_max_chars` | `1200` | character budget for facts returned by the `recall` tool |
| `viewer_autostart` | `true` | start the localhost viewer detached at session start |
| `viewer_port` | `7801` | port for the always-on memory/index viewer |
| `snapshotter` | `stub` | backend for `compact_page_view`: `stub` (canned sample, zero-dep), `playwright` (own headless chromium), or `chrome-devtools` (attach over CDP). playwright/chrome-devtools need the optional `playwright` package + a browser; both fail soft to empty |
| `visual_max_chars` | `2000` | hard cap on characters returned by `compact_page_view` (token guard) |
| `snapshot_cdp_url` | `http://localhost:9222` | CDP endpoint for `snapshotter=chrome-devtools` (a Chrome started with `--remote-debugging-port`) |
| `snapshot_headless` | `true` | `snapshotter=playwright`: launch chromium headless |
| `snapshot_timeout_ms` | `5000` | per-navigation/attach timeout before failing soft to an empty snapshot |

Env-only knobs (no `userConfig` entry):

| Env var | Default | Meaning |
|---|---|---|
| `ENGRAM_ENFORCE` | `advisory` | memory-first guard strength — `off` / `advisory` / `strict` (see above) |
| `ENGRAM_PREFER_SNAPSHOT` | `advisory` | prefer-a11y-snapshot nudge before a screenshot tool — `off` / `advisory` (once-per-session reminder) / `strict` (deny the screenshot) |
| `ENGRAM_DAEMON` | *(unset)* | `1` makes the recall hook use the resident daemon instead of loading the model in-process |
| `ENGRAM_PYTHON` / `python` userConfig | *(blank)* | pin an interpreter that already has fastembed; blank = auto-provisioned managed venv |

Advanced ranking weights (`w_sim`, `w_recency`, `w_freq`) are tunable via `ENGRAM_*`
env vars; defaults `1.0 / 0.3 / 0.2`.

### Memory lifecycle — STM/LTM tiers & consolidation

Fresh facts enter a short-term tier and promote to long-term on rehearsal; a
consolidation ("sleep") pass runs at session checkpoints (or `engram consolidate`).
Recall is tier-agnostic and **pruning is off by default** — turn it on deliberately.
Set via `userConfig` (or `ENGRAM_*` env):

| Key | Default | Meaning |
|---|---|---|
| `promote_after_freq` | `2` | reinforcement count that promotes an STM fact to LTM |
| `stm_capacity` | `2000` | max active STM facts before the weakest are displaced — a generous, reversible backstop against runaway STM growth (0 = unbounded/off) |
| `stm_recall_weight` | `1.0` | recall weight for STM facts (1.0 = tier-agnostic; `<1` down-ranks STM) |
| `integrate_threshold` | `0.92` | consolidation merges near-duplicate short-term facts at/above this cosine similarity, keeping one survivor (reversible) — ships on as a low-risk near-identical mop-up above `supersede_threshold` (0 = off) |
| `refine_keep_max` | `20000` | keep only the top-N facts by retention score, prune the rest (reversible) — ships on as a generous idempotent growth ceiling (0 = off) |
| `refine_prune_percentile` | `0` | prune the lowest-retention facts each pass — a value in `(0,1)` is a self-limiting percentile of the active set (`0.1` = drop the weakest 10%), a value `≥1` is an absolute score floor. Off by default: it forgets every pass and a good rate is store-dependent (0 = off) |
| `purge_horizon_days` | `0` | hard-delete facts archived longer than this, then `VACUUM`. Off by default: the only irreversible lever (0 = off) |

### Durable work queue — MemoryBus (inproc / NATS)

Detached capture and recovery run through a durable Command queue. The default
`inproc` backend is a zero-dependency SQLite queue (retry + backoff + dead-letter +
crash recovery). Opt into **NATS JetStream** for durable, cross-process processing —
the server is auto-provisioned (a checksum-verified `nats-server` binary, no Docker
required) and it **fails open to `inproc`** whenever NATS is unavailable, so enabling
it is safe. Set via `userConfig` (or `ENGRAM_*` env):

| Key | Default | Meaning |
|---|---|---|
| `bus` | `inproc` | `inproc` (SQLite queue) or `nats` (JetStream) |
| `bus_max_deliver` | `5` | delivery attempts before a work item is dead-lettered |
| `bus_backoff` | `5,30,120,600` | retry backoff schedule, seconds (comma-separated) |
| `lease_ttl` | `300` | seconds a claimed item is leased before reclaim (crash recovery) |
| `bus_dead_after` | `604800` | dead-letter a pending item unprocessed this long (7 days); 0 disables. Inspect/clear with `engram queue` |
| `nats_url` | `nats://localhost:4222` | NATS URL — use a dedicated port so engram doesn't share another server |
| `nats_stream` | `ENGRAM_WORK` | JetStream stream name |
| `nats_provision` | `binary` | auto-start NATS: `binary` (download nats-server), `docker`, or `off` (bring your own) |
| `nats_version` | `2.10.22` | pinned nats-server version for the binary provisioner |

Enable NATS in `settings.json` (a dedicated port keeps it isolated):

```json
"env": { "ENGRAM_BUS": "nats", "ENGRAM_NATS_URL": "nats://localhost:4225" }
```

The `nats-py` client is auto-installed into the managed venv on the next capture, so
activation takes one restart (it stays on `inproc` until ready). Manage the server
with `engram nats status | start | stop`.

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

A separate, standalone measure — **not** `engram eval` — quantifies the visual-snapshot
saving (this feature touches no embedding/ranking/quantisation, so the recall benchmark
doesn't apply):

```bash
python3 bench/visual_tokens.py                     # offline: a11y snapshot vs screenshot tokens
python3 bench/visual_tokens.py --url https://…     # add a live page (needs a browser backend)
```

It reports the per-page token ratio (a screenshot's `ceil(w/28)*ceil(h/28)` visual tokens
vs the a11y text's ~chars/4), which runs ~30–46× on the built-in fixtures.

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
run through a durable work queue — a zero-dependency `inproc` SQLite queue by default,
or opt-in NATS JetStream (auto-provisioned, fail-open to `inproc`). See
[DESIGN.md](DESIGN.md) for the full architecture, POEAA pattern choices, caching
analysis, memory-lifecycle model, benchmark, and risk register.
