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
| Distil / rank / quantise | Functional Core / Imperative Shell | `core/domain/*` (pure: `quantize`, `fusion`, `scoring`, `ingest`, `episodes`, …), called from `core/ports/distill.py` and `core/recall/` |
| Memory access | Repository over Data Mapper (never Active Record) | `core/store.py` |
| Query params | Query Object | `core/recall/` (search) |
| Embedding provider | Gateway + Separated Interface | `core/ports/embedding.py`, `core/adapters/` |
| Injected payload | DTO (one line per fact) | `core/recall/` (render_block) |
| Empty recall | Special Case / Null Object (inject nothing) | `core/recall/` (render_block → "") |
| Durable per-memory processing | Separated Interface — a Command queue (`WorkQueue`), not Events | `core/ports/workqueue.py`, `core/adapters/inproc_queue.py` |
| Wiring | Composition Root | `bin/*` entry points |

---

## `core/` — pure-Python core (stdlib-only)

### Memory (capture + recall)
| File | Role |
|---|---|
| `store.py` | Repository / Data Mapper over the SQLite store (facts + int8/binary embeddings, rows tagged by project); `reinforce`, `supersede`; `scan_rows` — the lean, rowid-ordered columns recall scans (full rows are re-read with `get` for the hits it returns); `_scoped_fts_ids` — the one keyword-channel query (`fts_search`, `chunk_fts_search`): the store-wide FTS `MATCH` filtered to the scope's rowids (`+rowid IN …`, the `+` is load-bearing) before bm25 and any wide-row read; `get_chunk` — primary key first, then the anchor in rowid order; the migration ladder (`_migrate`) resumes from the `user_version` stamp, and every FTS create-and-backfill is one transaction (`_fts_built`); `fts_coverage` / `repair_fts` — indexed docs vs content rows, and the rebuild of an index that lost them. |
| `service.py` | Capture Command/Handler — `add_facts`, consolidation, `_find_superseded`; idempotent per fact. Durable-queue handlers `rescue` (re-distil a degraded delta) and `reformat_exchanges` (the `exchange_format` rewrite of pre-footer exchanges), both drained at the head of incremental capture. Read side: `recall_prompt_block` (the UserPromptSubmit hook — the memory block and `index_prompt_block`, sharing one embedding of the prompt through `QueryMemo`), `recall_structured` (the `recall` tool). |
| `recall/` | Read side — Query Object `search` (exact bounded top-k by default; full rank + sort for cross-project / spreading / STM weight), `search_fused_with_stats` (5-channel fusion over lean rows), `_hydrate` (full rows for what leaves), `render_block` DTO (Null Object on empty). |
| `domain/scoring.py` | Recency decay `e^(-λt)` + Priority Score `sim·Ws + decay·Wr + freq·Wf`; `fact_priority` (one row's score), `top_by_priority` (the exact k best without scoring every row — decay, boost ≤ 1 bound the score). |
| `domain/confidence.py` | Pure score behind the `recall` verdict: `pool_stats` / `pool_z` (the best match against every fact scanned), `Calibration` VO + `calibrate` / `calibrated_confidence` (Platt), `sigmoid` (the one logistic — bench `platt_fit` uses it). A ranked score, not a probability; `core.recall.get_calibration` selects the calibration (`None` for the `hash` stub). |
| `ports/distill.py` | Distiller port (Strategy): the `Distiller` ABC, the `HeuristicDistiller` (zero-dep fallback and test stub; salience-ranked `heuristic_facts`), the `LLMDistiller` template with its pure prompts + parsers (atomic facts + `supersedes` links), `is_distiller_prompt` (derived from those prompts), and `get_distiller` (Plugin selection; imports the adapters on demand). |
| `transcript.py` | Parse Claude Code transcripts into capturable text: typed lines (conversation `text` / tool `action`, rendered through `ingest.action_line`), the distiller's text, verbatim prompts, and `(role, text)` turns with each action its own `action` turn. |
| `domain/ingest.py` | Pure capture-time policy: harness stripping, the ask / narration / status / trivial-prompt gates, the tool-action vocabulary (`ACTION_VERBS`, `action_line`, strict `parse_action` / `is_action_line`, `ACTION_PREFIXES`), and `line_salience` (the tier the heuristic distiller's 12-fact cap ranks by: first-person cue > plain > action). |

### Embedding + storage layer
| File | Role |
|---|---|
| `ports/embedding.py` | Gateway + Separated Interface for embedding providers (every read path embeds its query through `embed_query`; stored text through `embed` / `embed_one`), plus the zero-dep `HashEmbedding` and `QueryMemo` — the per-call wrapper that lets the prompt hook's two blocks share one embedding of the prompt. |
| `adapters/fastembed_gw.py` | fastembed Gateway adapter (opt-in, real semantic model). |
| `adapters/__init__.py` | Adapter package init. |
| `adapters/llm_distillers.py` | The LLM transports behind `LLMDistiller` — `ClaudeCliDistiller` (headless `claude -p`, Haiku, the shipped default, inside its tool/MCP isolation envelope) and `HTTPDistiller` (any OpenAI-compatible endpoint). Only the I/O; stdlib. |
| `adapters/numpy_scorer.py` | Vectorised (numpy) cosine scan — the fast `VectorScorer` for large stores, behind `ports/scorer.py`. |
| `ports/scorer.py` | `VectorScorer` port + the stdlib pure-Python default (`get_scorer` picks numpy when present). |
| `health.py` | Service health — one list of `Check`s rendered by `engram doctor`, the viewer's `/api/health` (warn-only chips for the detached side) and `engram import`: queue / embedding / distiller / recall scan (`scan_check` warns when a numpy-less project's estimated pure-Python scan nears the hook ceiling), then store write lock, capture progress (`.capture-requested` marker vs cursor progress), consolidation, last error, WAL size, keyword-index coverage (`fts_check`). `session_warnings` → the SessionStart `systemMessage` (store lock, capture, errors). |
| `errlog.py` | Fail open, but leave a record — a bounded, rotating JSON-lines `errors.log` in the data dir (`record`, never raises; `last`). Written by the capture worker, consolidation's stage deadline and the long-lived processes' stray-transaction guard. |
| `singleflight.py` | One pid-lock implementation (`held`, `acquire`, `release`, `holder`) for capture, consolidation, the indexer, the edit drain and the daemon; a dead holder's lock is reclaimed. |
| `domain/episodes.py` | Pure episodic pipeline: `exchange_units` (user turn + the assistant turns answering it, verbatim, ~800-char split; its tool actions folded into one `action_footer` on the first part — grouped by verb, capped at 1,024 chars; an exchange of actions alone forms no unit), `should_keep_exchange` (length gate), `prepare_exchanges` (redact → gate; shared by capture and the LongMemEval bench), `refold_exchanges` / `legacy_turns` (the one-off rewrite of pre-footer exchanges), `episode_key` (the `<session>:<delta start>` key shared by a delta's exchanges and the `facts.episode` provenance link). |
| `domain/temporal.py` | Pure `TimeWindow` VO (epoch bounds, either open) with `distance` / `boost` (×1.4 inside, halving every 7 days outside) and `boost_by_window` (re-scores fused results; never adds or drops one) — `search_history`'s `after` / `before`, parsed from ISO dates in `bin/mcp_server.py`. |
| `domain/sensory.py` | Pure sensory-register decisions (attention gate, promotion) for the one modality-columned register. |
| `domain/entities.py` | Lightweight entity extraction for shared-entity association edges. |
| `domain/spreading.py` | Spreading activation over the fact association graph (ACT-R). |
| `domain/privacy.py` | Pure `redact` (credentials, emails, non-project paths → `«redacted»`) for verbatim storage, and `privacy_flags` (the bench's human-gate detector). |
| `domain/lexical.py` | Pure tokenisation (`tokenize`, `token_set`) for the fusion lexical channel; `overlap_counts` — the exact per-text query-token overlap via one `str.find` sweep instead of tokenising every text. The zero-dep `hash` embedding is `HashEmbedding` in `ports/embedding.py`. |
| `domain/quantize.py` | int8 (primary search rep) + binary sign-bit quantisation. |
| `provision.py` | Self-provisions the private fastembed venv (no manual pip). |
| `daemon_client.py` | Thin client to the resident daemon; falls back in-process (fail-open). |
| `index/drift.py` | Embedding-drift canary (pin/check). |

### Code & docs index
| File | Role |
|---|---|
| `index/indexer.py` | Parses source→symbols and docs→sections, embeds, persists; freshness tracking. `index_nonfile` / `exchange_chunk_units` (with `exchange_anchor` / `exchange_position`, the `<episode>:<turn>.<part>` anchor and its inverse) for snapshots and exchanges. |
| `index/code_symbols.py` | Python symbol extraction via stdlib `ast`. |
| `index/treesitter_symbols.py` | TS/JS symbol extraction via `tree-sitter-language-pack`. |
| `index/chunking.py` | Markdown/doc chunking by heading structure. |
| `index/index_recall.py` | Ranked index search backing `search_code` / `search_docs` / `search_history` (scoped by kind and optionally one source/episode; cosine via the shared `VectorScorer`; an optional `TimeWindow` boosts candidates indexed in or near it). |
| `domain/fusion.py` | Weighted Reciprocal Rank Fusion — one `fuse` shared by fact recall (similarity / lexical / fts / recency / frequency) and the index (fts ⊕ cosine); `limit` keeps the top k exactly (plain-float accumulation, `Fused` only for what's returned). The index's diversity-budget packing is `index/index_recall.py::_diverse_pack`. |

### Consolidation (the sleep pass) and durable work
| File | Role |
|---|---|
| `consolidation/__init__.py` | `consolidate()` — the checkpoint pass, in order: replay → mature → displace → integrate → refine → invalidate → purge → forget (exchanges). `stages()` is the one stage table (consolidate runs it, the scale test measures it); each stage runs under `STAGE_DEADLINE_SECONDS` (`Store.deadline`). |
| `consolidation/replay.py` / `mature.py` | Rehearsed STM facts graduate (NREM replay); age-based STM→LTM transfer. |
| `consolidation/integrate.py` / `refine.py` / `invalidate.py` | Collapse near-duplicates (opt-in LLM merge); SHY-style pruning of low-importance facts; retire anti-patterns whose files are gone. |
| `consolidation/scoring.py` | Retention score — how important a fact is, for the sleep pass. |
| `ports/workqueue.py` | The durable Command queue port (`WorkQueue`, `WorkItem`, `Lease`) and the stage names it carries (`RESCUE`, `EXCHANGE_FORMAT`). |
| `adapters/inproc_queue.py` | Its sole backend — the SQLite `work_queue` (idempotent publish, lease, nak/backoff, dead-letter). |

### Import (one-way, from another memory tool)
| File | Role |
|---|---|
| `ports/memory_source.py` | `MemorySource` — a read-only port over an external memory store. |
| `adapters/claude_mem_source.py` | Reads a claude-mem SQLite store for `engram import claude-mem`. |
| `migrate.py` | The import orchestrator: a `MemorySource` → engram's store (dry-run, project mapping). |

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
| `capture.py` | Stop / SessionEnd / PreCompact — detached capture under `.capture.lock`, then (checkpoints) consolidation under its own `.consolidate.lock`; every best-effort step recorded via `errlog`; first it rebuilds a keyword index that lost coverage (`Store.repair_fts`), and records that. |
| `mcp_server.py` | `engram-memory` MCP server (`recall`, `search_code`, `get_symbol`, `code_outline`, `search_docs`, `get_doc_section`, `doc_outline`, `search_history`, `index_docs`, `list_projects`, `invalidate_memory`, `review_memory`); `TOOLS` is the one registry — dispatch is by name to the `_Engine` method. After every request `_Engine.settle()` rolls back a stray transaction and logs it. |
| `daemon.py` | Optional resident embedder (keeps the model warm); serves only the interactive recall hooks, so its Store uses `INTERACTIVE_BUSY_MS`; rolls back a stray transaction after each op. |
| `engram` | The CLI — `doctor`, `capture`, `recall`, `core`, `projects`, `prune`, `sweep`, `setup`, `daemon`, `viewer`, `stats`, `drift`, `eval`, `demo`. |
| `_bootstrap.py` | Shared path/interpreter bootstrap for the entry points; `emit` — the one hook-output envelope (`hookSpecificOutput` + `hookEventName`, top-level `systemMessage`). |

---

## `tests/` — stdlib `unittest` / `pytest`

A selection of the suite's entry points (`ls plugins/engram/tests` for the full set).

| File | Covers |
|---|---|
| `_harness.py` | Imported first by every test module: import paths, a hermetic env (ambient `ENGRAM_*` cleared, heuristic distiller, temp data dir), and guards that make a `claude` spawn, an HTTP request, or a read-write open of the real store raise; `scoped_env` / `temp_data_dir` / `allow_llm_transport` helpers. |
| `test_harness.py` | The harness's guards and helpers, plus the meta-test that every `test_*.py` imports it. |
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
| `cli_args.py` | The `engram eval` flag set (`add_eval_arguments`) — defined once, used by `run_eval.py` and `bin/engram`; import-light by design. |
| `confidence_eval.py` | `--confidence`: calibration of the `recall` verdict (AUROC, Brier/ECE, ok-precision/recall) over answerable + unanswerable queries on the production `search_fused_with_stats` path — Platt fitted on the dev half, every metric on the test half. |
| `longmemeval.py` | `--longmemeval`: LongMemEval session retrieval — parity / verbatim-exchange / distilled / hybrid arms, plus `--lme-shipped` (transcript → capture → `recall` / `search_history`), session metrics + chars@5; `--lme-split dev|test|all` (20 / 80 hold-out). |
| `age_eval.py` | `--aged`: old- vs new-gold Recall@k on both production rankers (`search`, `search_fused`) across recency weights, against the age-blind (recency-off) ranking. |
| `retrieval.py` / `stores.py` | Shared rankers over the real paths + Recall@k/MRR scorer; throwaway eval stores with explicit timestamps. |
| `replay_ledger.py` | Replays the last N real `recall_events` queries on a snapshot of the live store (unlabelled reality check). |
| `latency_eval.py` | `--latency`: the read paths' cost on a snapshot of a real store — the hook's two blocks (`search` + `index_prompt_block`), the `recall` tool's `search_fused_with_stats` and the `search_code` / `search_docs` tools' `search_index` × numpy / pure-Python scorer over the project's own ledger questions (query embedding excluded, `now` pinned): end-to-end p50/p90/max, the token columns (`hit_pct`, the hook blocks' `mean_chars`), an instrumented stage breakdown (wraps the production callables in `STAGES`), and a parity digest per path × scorer. `--latency-consolidation`: one `consolidate()` pass timed per stage on its own snapshot (heuristic distiller pinned). |
| `distractors.py` / `snapshot.py` | Runtime-only distractor mining (contamination/privacy-filtered); `snapshot.py` owns reading a real store safely — the `sqlite3.backup` snapshot, `find_project`, `store_source` (`--store-db` / `--store-project`) and `snapshot_project`, shared by `--distractors` and `--latency`. Never written to the repo. |
| `stats.py` / `report.py` / `backends.py` | Pure seeded statistics + `stable_split` (the one dev/test hold-out every harness uses); table printing; backend specs — `parse_spec` → `Spec` (`name[@model][%dim][+float]`, an unknown flag is an error), `make_embedder(spec, cfg)`, and `store_embedder` for harnesses that rank through the store. |
| `mine_corpus.py` | Dev tool: mines dataset *candidates* from the live store for the human review gate. |
| `replay.py` / `run_ab.py` / `eval_code_index.py` | Transcript counterfactual replay; paired live A/B; code-index model scoping (it indexes its *own* plugin tree, so arms in one run share a corpus, but a before/after across code versions must hold the corpus fixed). |
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
