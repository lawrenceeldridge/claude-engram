# claude-ltm

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

Standard vector search recalls stale and irrelevant facts. claude-ltm layers a
memory lifecycle on top (details in [DESIGN.md](DESIGN.md)):

- **Recency decay** — a fact's rank score decays exponentially with age
  (`half_life_days`) unless reinforced.
- **Consolidation** — a fact seen again is reinforced (frequency↑, recency
  refreshed) instead of duplicated; frequent facts rank higher and resist expiry.
- **Context gate** — a fact is only injected if it clears a similarity threshold
  against the current prompt.
- **Supersession** — a newer fact retires conflicting older ones. Similarity
  catches near-duplicates; the LLM distiller adds explicit `supersedes` links for
  vocabulary-disjoint conflicts ("I moved to London" → retires "I live in Paris").
- **Hard expiry** — an optional TTL sweep archives facts unseen past `ttl_days`,
  protecting ones reinforced past `ttl_keep_frequency`.
- **Recovery** — when the LLM distiller is unavailable, capture falls back to a
  heuristic and flags the fact `degraded`. A later healthy session re-distils the
  queued captures automatically, so a transient outage doesn't leave low-quality
  facts behind permanently.

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

## Memory-first enforcement (`LTM_ENFORCE`)

A `PreToolUse` guard steers you to the cheap path before an expensive one: consult
`recall` / `search_code` / `search_docs` *first*, then Grep/Glob/Read to widen if
those come back weak or empty. A `PostToolUse` marker records the moment any ltm
lookup tool runs (a maintenance `index_docs` doesn't count), so the guard can
enforce *ordering* rather than nagging blindly.

| `LTM_ENFORCE` | Behaviour |
|---|---|
| `off` | Guard disabled. |
| `advisory` *(default)* | Reminders only, never blocks. One nudge per session to check memory/index before Grep/Glob; a nudge before reading a large code file whole. |
| `strict` | Grep/Glob are **denied** until memory has been consulted this session (one `recall` — even an empty one — unlocks them). Reading a large **indexed** code file whole is denied (a `search_code` + `get_symbol`, or an `offset`/`limit` pre-edit peek, is expected instead). |

The gate is deliberately cheap to satisfy: a single `recall` call clears it for the
rest of the session. It's fail-open — any error in the hook lets the tool through.

## Layout

```
claude-ltm/
├── .claude-plugin/marketplace.json     # marketplace catalogue (lists the plugin)
└── plugins/ltm/
    ├── .claude-plugin/plugin.json      # plugin manifest + userConfig
    ├── hooks/hooks.json                # SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd, PreCompact
    ├── commands/memory-viewer.md       # /ltm:memory-viewer
    ├── core/                           # pure-Python core (Ports & Adapters): store, service, distill, indexer, chunking, symbol extractors
    ├── bin/                            # hook entry points, CLI (ltm), MCP server, daemon
    │   ├── recall_session_start.py     #   SessionStart — core memory + orientation + memory-first directive
    │   ├── recall_prompt.py            #   UserPromptSubmit — just-in-time recall
    │   ├── prefer_memory.py            #   PreToolUse — memory-first guard (LTM_ENFORCE)
    │   ├── mark_consulted.py           #   PostToolUse — records that memory was consulted
    │   ├── index_edit.py               #   PostToolUse — re-index edited files
    │   ├── index_docs.py               #   SessionStart — auto-index the project
    │   ├── capture.py                  #   Stop/SessionEnd/PreCompact — detached capture + summary
    │   ├── mcp_server.py               #   MCP tools (recall, search_code, get_symbol, …)
    │   └── daemon.py                   #   optional resident embedder
    ├── bench/                          # labelled recall benchmark + dataset
    ├── viewer/                         # localhost browser (stdlib http.server) — memory + index views
    └── tests/                          # stdlib unittest / pytest suite
```

## MCP tools

The plugin exposes an `ltm-memory` MCP server so the model can query memory and the
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

## Try it without installing

```bash
cd plugins/ltm
python3 -m unittest discover -s tests        # smoke tests (all stdlib)
python3 bin/ltm demo                         # capture sample facts, then recall
python3 bin/ltm doctor                       # show config, project, counts
python3 bin/ltm eval --backends hash         # recall-quality benchmark (add fastembed to compare)
python3 bin/ltm viewer                       # browse at http://127.0.0.1:7801/
```

## Install as a plugin (dev)

```bash
claude --plugin-dir ./plugins/ltm            # session-scoped, for iterating
```

Or add the marketplace and install:

```bash
/plugin marketplace add /path/to/claude-ltm
/plugin install ltm@claude-ltm
```

Hooks then run automatically: memory is recalled and the project auto-indexed at
session start, relevant facts injected per prompt, the memory-first guard steers
tool use, edited files are re-indexed, and memory is captured (with a throttled
session summary) at stop / session end / pre-compact.

## CLI

```
ltm doctor              show resolved config, project identity and fact count
ltm capture             capture memory from stdin / --file / --transcript
ltm recall <query>      run a just-in-time recall query for the current project
ltm core                show the stable session-start memory block
ltm projects            list every project in the global store
ltm prune               delete all memory for the current project
ltm sweep [--all]       archive stale facts (TTL expiry; --days N to override)
ltm consolidate [--all] run the sleep pass: promote recalled STM, prune (if enabled)
ltm nats status|start|stop  manage the opt-in NATS server (bus=nats)
ltm daemon              run the resident daemon (keeps the embedder warm)
ltm viewer              launch the localhost viewer
ltm eval --backends …   benchmark embedding backends (see below)
ltm demo                capture sample facts then recall (end-to-end proof)
```

## Configuration

Set via the plugin's `userConfig` (exposed to scripts as `CLAUDE_PLUGIN_OPTION_*`)
or `LTM_*` env vars for standalone use:

| Key | Default | Meaning |
|---|---|---|
| `embedding` | `hash` | `hash` (lexical stub, zero deps) or `fastembed` (real semantic model, self-provisions a venv) |
| `embedding_model` | *(blank)* | fastembed model id; blank = `BAAI/bge-base-en-v1.5` (best measured recall) |
| `distiller` | `claude` | `claude` (headless `claude -p`, Haiku), `ollama` (local, zero-token), or `heuristic` (line extraction, no LLM) |
| `distiller_model` | *(blank)* | claude: model alias (blank = `haiku`); ollama: model name (blank = `qwen2.5:3b`) |
| `distiller_base_url` | `http://localhost:11434/v1` | OpenAI-compatible endpoint for the `ollama`/`http` distiller (ignored under `claude`) |
| `top_k` | `3` | facts injected per prompt |
| `min_sim` | `0.12` | similarity threshold to inject |
| `core_size` | `5` | stable facts injected at session start (0 disables) |
| `max_chars` | `800` | hard cap on injected characters (token guard) |
| `cross_project` | `false` | fall back to other projects when in-project recall is weak |
| `half_life_days` | `30` | recency half-life; lower = forgets faster |
| `supersede_threshold` | `0.85` | new-fact similarity that retires an older one (1.0 disables) |
| `ttl_days` | `0` | archive facts unseen this long on capture (0 disables hard expiry) |
| `ttl_keep_frequency` | `3` | facts reinforced this often are never expired |
| `recall_min_confidence` | `0.35` | confidence the `recall` tool needs to report verdict `ok` |
| `recall_max_chars` | `1200` | character budget for facts returned by the `recall` tool |
| `viewer_autostart` | `true` | start the localhost viewer detached at session start |
| `viewer_port` | `7801` | port for the always-on memory/index viewer |

Env-only knobs (no `userConfig` entry):

| Env var | Default | Meaning |
|---|---|---|
| `LTM_ENFORCE` | `advisory` | memory-first guard strength — `off` / `advisory` / `strict` (see above) |
| `LTM_DAEMON` | *(unset)* | `1` makes the recall hook use the resident daemon instead of loading the model in-process |
| `LTM_PYTHON` / `python` userConfig | *(blank)* | pin an interpreter that already has fastembed; blank = auto-provisioned managed venv |

Advanced ranking weights (`w_sim`, `w_recency`, `w_freq`) are tunable via `LTM_*`
env vars; defaults `1.0 / 0.3 / 0.2`.

### Memory lifecycle — STM/LTM tiers & consolidation

Fresh facts enter a short-term tier and promote to long-term on rehearsal; a
consolidation ("sleep") pass runs at session checkpoints (or `ltm consolidate`).
Recall is tier-agnostic and **pruning is off by default** — turn it on deliberately.
Set via `userConfig` (or `LTM_*` env):

| Key | Default | Meaning |
|---|---|---|
| `promote_after_freq` | `2` | reinforcement count that promotes an STM fact to LTM |
| `stm_capacity` | `0` | max active STM facts before the weakest are displaced (0 = unbounded/off) |
| `stm_recall_weight` | `1.0` | recall weight for STM facts (1.0 = tier-agnostic; `<1` down-ranks STM) |
| `retention_keep_max` | `0` | keep only the top-N facts by retention score, prune the rest (0 = off) |
| `prune_threshold` | `0` | prune facts whose retention score is below this (0 = off) |
| `purge_horizon_days` | `0` | hard-delete facts archived longer than this, then `VACUUM` (0 = off) |

### Durable work queue — MemoryBus (inproc / NATS)

Detached capture and recovery run through a durable Command queue. The default
`inproc` backend is a zero-dependency SQLite queue (retry + backoff + dead-letter +
crash recovery). Opt into **NATS JetStream** for durable, cross-process processing —
the server is auto-provisioned (a checksum-verified `nats-server` binary, no Docker
required) and it **fails open to `inproc`** whenever NATS is unavailable, so enabling
it is safe. Set via `userConfig` (or `LTM_*` env):

| Key | Default | Meaning |
|---|---|---|
| `bus` | `inproc` | `inproc` (SQLite queue) or `nats` (JetStream) |
| `bus_max_deliver` | `5` | delivery attempts before a work item is dead-lettered |
| `bus_backoff` | `5,30,120,600` | retry backoff schedule, seconds (comma-separated) |
| `lease_ttl` | `300` | seconds a claimed item is leased before reclaim (crash recovery) |
| `nats_url` | `nats://localhost:4222` | NATS URL — use a dedicated port so ltm doesn't share another server |
| `nats_stream` | `LTM_WORK` | JetStream stream name |
| `nats_provision` | `binary` | auto-start NATS: `binary` (download nats-server), `docker`, or `off` (bring your own) |
| `nats_version` | `2.10.22` | pinned nats-server version for the binary provisioner |

Enable NATS in `settings.json` (a dedicated port keeps it isolated):

```json
"env": { "LTM_BUS": "nats", "LTM_NATS_URL": "nats://localhost:4225" }
```

The `nats-py` client is auto-installed into the managed venv on the next capture, so
activation takes one restart (it stays on `inproc` until ready). Manage the server
with `ltm nats status | start | stop`.

## Real semantic recall (recommended)

The lexical `hash` stub only matches shared vocabulary. For recall that
generalises across wording, use `fastembed`. On first use it **self-provisions a
private venv** under the plugin data dir and downloads the model once — no manual
`pip` needed; set `embedding=fastembed` and go. To keep the model warm across the
short-lived hook processes:

```bash
python3 bin/ltm daemon                 # keep the model warm
export LTM_DAEMON=1                     # recall hook uses the daemon, else in-process
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

`ltm eval` runs a labelled paraphrase set through the real quantised search path
and reports Recall@1/@3, MRR@10, and operational cost. Backend spec is
`name[@model][+float]`:

```bash
python3 bin/ltm eval --backends "hash,fastembed,fastembed@BAAI/bge-small-en-v1.5,fastembed+float"
```

Measured on the bundled set (18 facts, 14 paraphrased queries):

| backend | Recall@1 | Recall@3 | MRR@10 | bytes/fact |
|---|---|---|---|---|
| hash (lexical stub) | 0.07 | 0.36 | 0.27 | 288 |
| fastembed bge-small | 0.36 | 0.71 | 0.57 | 432 |
| **fastembed bge-base (default)** | **0.79** | **0.86** | **0.85** | 864 |

int8 quantization loss is negligible (int8 ≈ float), so the store stays compact;
model size is the real lever. Use the harness to A/B any future change before
shipping it.

## Project identity

Memory and the index are keyed by a **marker-walk**: from the working directory we
walk up to the nearest `.git` / `pyproject.toml` / `package.json` / `go.mod` /
`Cargo.toml` / `pom.xml` (configurable via `markers`) and key on that directory's
path. This avoids the `basename(cwd)` fragmentation that mis-files memory in
monorepos and subdirectory launches.

## Status

Working end to end (137 tests, 5 skipped). Defaults are local-first and
zero-dependency (`hash` embedding + `heuristic` fallback); real recall is opt-in
via `fastembed` (bge-base, self-provisioning venv) and, for best quality, an LLM
distiller (`distiller=claude` on Haiku by default, or `distiller=ollama` for
zero-token local). See [DESIGN.md](DESIGN.md) for the full architecture, POEAA
pattern choices, caching analysis, memory-lifecycle model, benchmark, and risk
register.
