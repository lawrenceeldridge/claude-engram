# Module & Surface Registry

claude-engram is a **single Python package** (not a monorepo), so this is a
module/surface map rather than a package list. It mirrors DESIGN.md's POEAA table
and is verified against the real directories
(`plugins/engram/core`, `plugins/engram/bin`, `tests/`, `bench/`, `viewer/`). When it
drifts from disk, trust `code_outline` / `ls` over this file.

**Layering (from `.claude/rules/02-architecture/`):** `core/` is pure-Python,
stdlib-only, framework-agnostic (never `import fastembed` at import time — real
embeddings live behind `core/adapters/` and self-provision a venv). `bin/` are
the composition roots (hook entry points, CLI, MCP server, daemon) that wire the
core to Claude Code.

---

## POEAA role → file (mirrors DESIGN.md)

| Role | Pattern | Primary file(s) |
|---|---|---|
| Overall shape | CQRS + Hexagonal (Ports & Adapters) | whole plugin |
| Capture pipeline | Command/Handler, idempotent per fact | `core/service.py` |
| Distil / rank / quantise | Functional Core / Imperative Shell | `core/distill.py`, `core/recall.py`, `core/quantize.py` |
| Memory access | Repository over Data Mapper (never Active Record) | `core/store.py` |
| Query params | Query Object | `core/recall.py` (search) |
| Embedding provider | Gateway + Separated Interface | `core/embedding.py`, `core/adapters/` |
| Injected payload | DTO (one line per fact) | `core/recall.py` (render_block) |
| Empty recall | Special Case / Null Object (inject nothing) | `core/recall.py` (render_block → "") |
| Wiring | Composition Root | `bin/*` entry points |

---

## `core/` — pure-Python core (stdlib-only)

### Memory (capture + recall)
| File | Role |
|---|---|
| `store.py` | Repository / Data Mapper over the SQLite store (facts + int8/binary embeddings, rows tagged by project); `reinforce`, `supersede`. |
| `service.py` | Capture Command/Handler — `add_facts`, consolidation, `_find_superseded`; idempotent per fact. |
| `recall.py` | Read side — Query Object `search`, hybrid re-rank, `render_block` DTO (Null Object on empty). |
| `scoring.py` | Recency decay `e^(-λt)` + Priority Score `sim·Ws + decay·Wr + freq·Wf`. |
| `confidence.py` | Calibrates the `recall` verdict (`ok` / `low_confidence` / `no_memory`). |
| `distill.py` | Distiller Strategy — heuristic (default) + Claude-CLI + HTTP/Ollama; atomic facts + `supersedes` links; heuristic fallback. |
| `transcript.py` | Parse Claude Code transcripts into capturable text. |

### Embedding + storage layer
| File | Role |
|---|---|
| `embedding.py` | Gateway + Separated Interface for embedding providers. |
| `adapters/fastembed_gw.py` | fastembed Gateway adapter (opt-in, real semantic model). |
| `adapters/__init__.py` | Adapter package init. |
| `lexical.py` | `hash` lexical embedding stub (zero-dep default) + lexical/FTS support. |
| `quantize.py` | int8 (primary search rep) + binary sign-bit quantisation. |
| `provision.py` | Self-provisions the private fastembed venv (no manual pip). |
| `daemon_client.py` | Thin client to the resident daemon; falls back in-process (fail-open). |
| `drift.py` | Embedding-drift canary (pin/check). |

### Code & docs index
| File | Role |
|---|---|
| `indexer.py` | Parses source→symbols and docs→sections, embeds, persists; freshness tracking. |
| `code_symbols.py` | Python symbol extraction via stdlib `ast`. |
| `treesitter_symbols.py` | TS/JS symbol extraction via `tree-sitter-language-pack`. |
| `chunking.py` | Markdown/doc chunking by heading structure. |
| `index_recall.py` | Ranked index search backing `search_code` / `search_docs`. |
| `fusion.py` | Reciprocal-rank fusion (FTS5 bm25 ⊕ cosine) + diversity-budget packing. |

### Shared
| File | Role |
|---|---|
| `config.py` | Resolve config from `userConfig` (`CLAUDE_PLUGIN_OPTION_*`) / `ENGRAM_*` env. |
| `project.py` | Project identity — workspace root by default (`CLAUDE_PROJECT_DIR`/cwd, hashed key); `identity=marker` walks up to a marker; `.engram-root` overrides. |
| `__init__.py` | Package init / public surface. |

---

## `bin/` — composition roots (hooks, CLI, MCP, daemon)

| File | Trigger / role |
|---|---|
| `recall_session_start.py` | SessionStart — core memory + orientation + memory-first directive. |
| `recall_prompt.py` | UserPromptSubmit — just-in-time recall injection. |
| `prefer_memory.py` | PreToolUse — memory-first guard (`ENGRAM_ENFORCE`). |
| `mark_consulted.py` | PostToolUse — records that an engram lookup ran (enables ordering). |
| `index_docs.py` | SessionStart — auto-index the project (single-flight, file-capped). |
| `index_edit.py` | PostToolUse — re-index each Edited/Written file. |
| `capture.py` | Stop / SessionEnd / PreCompact — detached capture + throttled summary. |
| `mcp_server.py` | `engram-memory` MCP server (`recall`, `search_code`, `get_symbol`, `code_outline`, `search_docs`, `get_doc_section`, `doc_outline`, `index_docs`, `list_projects`). |
| `daemon.py` | Optional resident embedder (keeps the model warm). |
| `engram` | The CLI — `doctor`, `capture`, `recall`, `core`, `projects`, `prune`, `sweep`, `setup`, `daemon`, `viewer`, `stats`, `drift`, `eval`, `demo`. |
| `_bootstrap.py` | Shared path/interpreter bootstrap for the entry points. |

---

## `tests/` — stdlib `unittest` / `pytest`

| File | Covers |
|---|---|
| `test_smoke.py` | End-to-end smoke (all stdlib). |
| `test_recall_api.py` | `recall` verdict + budget behaviour. |
| `test_capture_content.py` | Capture / distillation output. |
| `test_index.py` | Code/docs indexing + outlines. |
| `test_incremental.py` | Incremental re-index on edit. |
| `test_quality.py` | Recall-quality assertions. |
| `test_recovery.py` | Degraded-fact re-distillation recovery. |
| `test_hooks.py` | Hook fail-open behaviour. |

## `bench/` — labelled recall benchmark (`engram eval`)

| File | Role |
|---|---|
| `run_eval.py` | Runs the labelled paraphrase set through the real quantised search path (Recall@1/@3, MRR@10, bytes/fact). |
| `dataset.json` | The labelled facts + paraphrased queries. |

## `viewer/` — localhost browser (stdlib `http.server`)

| File | Role |
|---|---|
| `serve.py` | Read-only memory + index browser at `http://127.0.0.1:7801/` (spans all projects). |

---

## Adding / renaming a module

Keep the layering contract: new memory/index logic goes in `core/` and must
import cleanly on the standard library alone; any hard third-party dependency
goes behind an interface in `core/adapters/` with a self-provisioned venv, never
at `core/` import time (see
[`.claude/rules/02-architecture/00-overview.md`](../../../../.claude/rules/02-architecture/00-overview.md)).
New wiring (a hook, a CLI subcommand, an MCP tool) goes in `bin/`. Update
DESIGN.md's POEAA table and this registry together.
