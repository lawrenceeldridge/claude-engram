"""claude-engram core — token-first, cross-project long-term memory for Claude Code.

Hexagonal layout (Ports & Adapters):
  - config      : configuration resolved from CLAUDE_PLUGIN_OPTION_* / ENGRAM_* / defaults
  - project     : marker-walk project identity (fixes basename(cwd) fragmentation)
  - embedding   : EmbeddingGateway port + HashEmbedding stub (+ optional fastembed adapter)
  - quantize    : float -> int8 / binary packing (the compact "bytes" layer)
  - store       : SQLite repository (Data Mapper, never Active Record)
  - distill     : transcript/text -> atomic facts (heuristic; LLM adapter is the drop-in)
  - transcript  : parse Claude Code JSONL transcripts
  - recall      : embed query -> rank -> render injection block
  - service     : high-level capture/recall operations used by hooks, CLI and daemon
"""

__version__ = "0.1.0"
