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
| `service.py` | Capture Command/Handler — `add_facts`, consolidation, `_find_superseded`; idempotent per fact. Durable-queue handlers `rescue` (re-distil a degraded delta) and `reformat_exchanges` (the `exchange_format` rewrite of pre-footer exchanges), both drained at the head of incremental capture. |
| `recall.py` | Read side — Query Object `search`, hybrid re-rank, `render_block` DTO (Null Object on empty). |
| `scoring.py` | Recency decay `e^(-λt)` + Priority Score `sim·Ws + decay·Wr + freq·Wf`. |
| `domain/confidence.py` | Pure score behind the `recall` verdict: `pool_stats` / `pool_z` (the best match against every fact scanned), `Calibration` VO + `calibrate` / `calibrated_confidence` (Platt), `sigmoid` (the one logistic — bench `platt_fit` uses it). A ranked score, not a probability; `core.recall.get_calibration` selects the calibration (`None` for the `hash` stub). |
| `distill.py` | Distiller Strategy — Claude-CLI (default, Haiku) + HTTP/Ollama + heuristic (zero-dep fallback and test stub; salience-ranked `heuristic_facts`); atomic facts + `supersedes` links. |
| `transcript.py` | Parse Claude Code transcripts into capturable text: typed lines (conversation `text` / tool `action`, rendered through `ingest.action_line`), the distiller's text, verbatim prompts, and `(role, text)` turns with each action its own `action` turn. |
| `domain/ingest.py` | Pure capture-time policy: harness stripping, the ask / narration / status / trivial-prompt gates, the tool-action vocabulary (`ACTION_VERBS`, `action_line`, strict `parse_action` / `is_action_line`, `ACTION_PREFIXES`), and `line_salience` (the tier the heuristic distiller's 12-fact cap ranks by: first-person cue > plain > action). |

### Embedding + storage layer
| File | Role |
|---|---|
| `embedding.py` | Gateway + Separated Interface for embedding providers. |
| `adapters/fastembed_gw.py` | fastembed Gateway adapter (opt-in, real semantic model). |
| `adapters/__init__.py` | Adapter package init. |
| `domain/episodes.py` | Pure episodic pipeline: `exchange_units` (user turn + the assistant turns answering it, verbatim, ~800-char split; its tool actions folded into one `action_footer` on the first part — grouped by verb, capped at 1,024 chars; an exchange of actions alone forms no unit), `should_keep_exchange` (length gate), `prepare_exchanges` (redact → gate; shared by capture and the LongMemEval bench), `refold_exchanges` / `legacy_turns` (the one-off rewrite of pre-footer exchanges), `episode_key` (the `<session>:<delta start>` key shared by a delta's exchanges and the `facts.episode` provenance link). |
| `domain/privacy.py` | Pure `redact` (credentials, emails, non-project paths → `«redacted»`) for verbatim storage, and `privacy_flags` (the bench's human-gate detector). |
| `domain/lexical.py` | Pure tokenisation (`tokenize`, `token_set`) for the fusion lexical channel. The zero-dep `hash` embedding is `HashEmbedding` in `ports/embedding.py`. |
| `quantize.py` | int8 (primary search rep) + binary sign-bit quantisation. |
| `provision.py` | Self-provisions the private fastembed venv (no manual pip). |
| `daemon_client.py` | Thin client to the resident daemon; falls back in-process (fail-open). |
| `drift.py` | Embedding-drift canary (pin/check). |

### Code & docs index
| File | Role |
|---|---|
| `indexer.py` | Parses source→symbols and docs→sections, embeds, persists; freshness tracking. `index_nonfile` / `exchange_chunk_units` (with `exchange_anchor` / `exchange_position`, the `<episode>:<turn>.<part>` anchor and its inverse) for snapshots and exchanges. |
| `code_symbols.py` | Python symbol extraction via stdlib `ast`. |
| `treesitter_symbols.py` | TS/JS symbol extraction via `tree-sitter-language-pack`. |
| `chunking.py` | Markdown/doc chunking by heading structure. |
| `index_recall.py` | Ranked index search backing `search_code` / `search_docs` / `search_history` (scoped by kind and optionally one source/episode; cosine via the shared `VectorScorer`). |
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
| `mcp_server.py` | `engram-memory` MCP server (`recall`, `search_code`, `get_symbol`, `code_outline`, `search_docs`, `get_doc_section`, `doc_outline`, `search_history`, `index_docs`, `list_projects`, `invalidate_memory`, `review_memory`); `TOOLS` is the one registry — dispatch is by name to the `_Engine` method. |
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
| `run_eval.py` | Runs the labelled paraphrase set through the real quantised search path (Recall@1/@3, MRR@10, bytes/fact); owns the shared `add_eval_arguments` flag set used by `bin/engram eval`. |
| `confidence_eval.py` | `--confidence`: calibration of the `recall` verdict (AUROC, Brier/ECE, ok-precision/recall) over answerable + unanswerable queries on the production `search_fused_with_stats` path — Platt fitted on the dev half, every metric on the test half. |
| `longmemeval.py` | `--longmemeval`: LongMemEval session retrieval — parity / verbatim-exchange / distilled / hybrid arms, plus `--lme-shipped` (transcript → capture → `recall` / `search_history`), session metrics + chars@5; `--lme-split dev|test|all` (20 / 80 hold-out). |
| `age_eval.py` | `--aged`: old- vs new-gold Recall@k on both production rankers (`search`, `search_fused`) across recency weights, against the age-blind (recency-off) ranking. |
| `retrieval.py` / `stores.py` | Shared rankers over the real paths + Recall@k/MRR scorer; throwaway eval stores with explicit timestamps. |
| `replay_ledger.py` | Replays the last N real `recall_events` queries on a snapshot of the live store (unlabelled reality check). |
| `distractors.py` / `snapshot.py` | Runtime-only distractor mining (contamination/privacy-filtered) from a `sqlite3.backup` snapshot; never written to the repo. |
| `stats.py` / `report.py` / `backends.py` | Pure seeded statistics + `stable_split` (the one dev/test hold-out every harness uses); table printing; backend spec parsing + embedder construction. |
| `mine_corpus.py` | Dev tool: mines dataset *candidates* from the live store for the human review gate. |
| `replay.py` / `run_ab.py` / `eval_code_index.py` | Transcript counterfactual replay; paired live A/B; code-index model scoping. |
| `dataset.json` | The labelled facts + paraphrased queries, plus scenario keys (`stm_`/`antipattern_`/`duplicate_cluster_`/`confidence_scenario`). |

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
