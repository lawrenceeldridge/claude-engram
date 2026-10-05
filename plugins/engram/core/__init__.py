"""claude-engram core — token-first, cross-project long-term memory for Claude Code.

Hexagonal layout (Ports & Adapters) — the map lives in ``.claude/rules/02-architecture/`` and the
module registry; in brief:
  - store / service / config / project / transcript / provision / daemon_client (root): the
    Repository, the capture Command/Handlers, configuration, workspace-rooted project identity,
    transcript parsing, the managed venv, the resident-embedder client
  - domain/        : the pure Functional Core (scoring, fusion, quantize, confidence, ingest,
                     episodes, temporal, privacy, …) — no I/O, no clock
  - ports/         : Separated Interfaces (embedding, distill, scorer, workqueue, memory_source)
  - adapters/      : the driven adapters with optional dependencies (fastembed, numpy, …)
  - recall/        : the read side (rank → render the injection block)
  - index/         : the code/docs index (index, search, outlines)
  - consolidation/ : the sleep pass

The plugin's version lives in ``.claude-plugin/plugin.json`` alone.
"""
