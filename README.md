# claude-ltm

Token-first, cross-project **long-term memory** for Claude Code, packaged as a
plugin. It captures your sessions off the interactive path, distils them into
atomic facts, embeds those compactly, and injects the *relevant* ones back into
context — automatically, via hooks. Local-first: no API key and no network in the
default configuration, no telemetry. The core runs on the Python standard library
alone; real semantic recall and LLM distillation are opt-in.

## Why it's efficient

Two budgets are optimised separately (see [DESIGN.md](DESIGN.md)):

- **Tokens** — recall is threshold-gated and byte-capped, so you pay tokens only
  in proportion to relevance. A stable per-project *core* is injected once at
  `SessionStart` (joins the prompt-cache prefix → cheap on every later turn); a
  tiny *just-in-time* block is injected per prompt only when something matches.
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

## Layout

```
claude-ltm/
├── .claude-plugin/marketplace.json     # marketplace catalogue (lists the plugin)
└── plugins/ltm/
    ├── .claude-plugin/plugin.json      # plugin manifest + userConfig
    ├── hooks/hooks.json                # SessionStart, UserPromptSubmit, SessionEnd, PreCompact
    ├── commands/memory-viewer.md       # /ltm:memory-viewer
    ├── core/                           # pure-Python core (Ports & Adapters); adapters/ holds fastembed
    ├── bin/                            # hook entry points, CLI (ltm), daemon
    ├── bench/                          # labelled recall benchmark + dataset
    ├── viewer/                         # localhost browser (stdlib http.server)
    └── tests/                          # stdlib unittest smoke tests
```

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

Hooks then run automatically: memory is captured at session end / pre-compact and
recalled at session start and on each prompt.

## CLI

```
ltm doctor              show resolved config, project identity and fact count
ltm capture             capture memory from stdin / --file / --transcript
ltm recall <query>      run a just-in-time recall query for the current project
ltm core                show the stable session-start memory block
ltm projects            list every project in the global store
ltm prune               delete all memory for the current project
ltm sweep [--all]       archive stale facts (TTL expiry; --days N to override)
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
| `embedding` | `hash` | `hash` (lexical stub, zero deps) or `fastembed` (real semantic model) |
| `embedding_model` | *(blank)* | fastembed model id; blank = `BAAI/bge-base-en-v1.5` (best measured recall) |
| `distiller` | `heuristic` | `heuristic` (line extraction), `claude` (headless `claude -p`, Haiku), or `ollama` (local, zero-token) |
| `distiller_model` | *(blank)* | claude: model alias (blank = `haiku`); ollama: model name (blank = `qwen2.5:3b`) |
| `distiller_base_url` | `http://localhost:11434/v1` | OpenAI-compatible endpoint for the `ollama`/`http` distiller |
| `top_k` | `3` | facts injected per prompt |
| `min_sim` | `0.12` | similarity threshold to inject |
| `core_size` | `5` | stable facts injected at session start (0 disables) |
| `max_chars` | `800` | hard cap on injected characters (token guard) |
| `cross_project` | `false` | fall back to other projects when in-project recall is weak |
| `half_life_days` | `30` | recency half-life; lower = forgets faster |
| `supersede_threshold` | `0.85` | new-fact similarity that retires an older one (1.0 disables) |
| `ttl_days` | `0` | archive facts unseen this long on capture (0 disables hard expiry) |
| `ttl_keep_frequency` | `3` | facts reinforced this often are never expired |

Advanced ranking weights (`w_sim`, `w_recency`, `w_freq`) are tunable via `LTM_*`
env vars; defaults `1.0 / 0.3 / 0.2`.

## Real semantic recall (recommended)

The lexical `hash` stub only matches shared vocabulary. For recall that
generalises across wording, use `fastembed`:

```bash
pip install fastembed                  # pulls onnxruntime; downloads a model once
# set embedding=fastembed  (bge-base is the default model)
python3 bin/ltm daemon                 # keep the model warm
export LTM_DAEMON=1                     # recall hook uses the daemon, else in-process
```

## Better distillation (atomic facts + explicit supersedes)

The heuristic distiller just splits lines. An LLM distiller produces genuinely
atomic facts and explicit `supersedes` links (the fix for vocabulary-disjoint
conflicts). It runs in the detached capture worker — off the interactive path —
and falls back to the heuristic on any failure, so capture never breaks. Two
backends:

**Local, zero-token (recommended for cost):** any OpenAI-compatible local server.
Distillation is simple extraction, so a small model suffices.

```bash
ollama pull qwen2.5:3b        # or llama3.2:3b
# set distiller=ollama  (defaults: base_url http://localhost:11434/v1, model qwen2.5:3b)
```

**Claude, using an efficient model:** distillation is a cheap task, so it defaults
to **Haiku**, not Opus/Sonnet.

```bash
# set distiller=claude   (distiller_model defaults to "haiku")
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

Memory is keyed by a **marker-walk**: from the working directory we walk up to the
nearest `.git` / `pyproject.toml` / `package.json` (configurable via `markers`)
and key on that directory's path. This avoids the `basename(cwd)` fragmentation
that mis-files memory in monorepos and subdirectory launches.

## Status

Working end to end (15 stdlib tests). Defaults are local-first and zero-dependency
(`hash` + `heuristic`); real recall is opt-in via `fastembed` (bge-base) and, for
best quality, an LLM distiller (`distiller=ollama` for zero-token local, or
`distiller=claude` on Haiku). See [DESIGN.md](DESIGN.md) for the full
architecture, POEAA pattern choices, caching analysis, memory-lifecycle model,
benchmark, and risk register.
