---
alwaysApply: true
---

# Hooks & Budgets

The hooks are where claude-engram touches the live turn, so they carry the strictest
contracts. Wiring is in `plugins/engram/hooks/hooks.json`; entry points are in
`plugins/engram/bin/`.

## The hooks

| Event | Entry point | Job | Budget profile |
|---|---|---|---|
| `SessionStart` | `recall_session_start.py` | Inject stable project **core** + orientation + memory-first directive | Joins the cached prefix — read at ~0.1× on later turns |
| `SessionStart` | `index_docs.py` | Auto-index the project (single-flight, file-capped) | Off the hot path |
| `UserPromptSubmit` | `recall_prompt.py` | JIT recall — inject only facts clearing `min_sim` | Tail of context; tiny, per-prompt |
| `PreToolUse` | `prefer_memory.py` | Memory-first guard (`ENGRAM_ENFORCE`) | Must be cheap; fail-open |
| `PostToolUse` | `mark_consulted.py`, `index_edit.py`, `credit_read.py` | Record that memory was consulted; re-index edited files; credit bounded reads of indexed files in the ledger | Off the hot path; fail-open |
| `Stop` / `SessionEnd` / `PreCompact` | `capture.py` | Spawn **detached** capture worker | Fire-and-forget; zero interactive cost |

## Fail-open contract (non-negotiable)

**Every hook exits 0 on any error and injects nothing.** A hook must never raise
into, block, or slow the interactive turn:

- Wrap the body so *any* exception → exit 0, no output.
- The recall hook has a **5s timeout ceiling**; overrunning it must degrade to
  injecting nothing, not to a stall.
- Optional infrastructure (the resident daemon, `fastembed`, an LLM distiller) may be
  absent or dead — the hook **falls back in-process / to the heuristic**, never errors.
- The memory-first guard is fail-open by design: any error in it lets the tool through.

## Detached-capture rule

Capture (transcript read → distil → embed → persist, plus the verbatim exchanges → index) is
heavy and latency-tolerant,
so it **never runs inline**. The Stop/SessionEnd/PreCompact hook spawns a worker and
returns immediately (single-flight, so worker/daemon pileup can't happen). LLM
distillation, when enabled, runs *inside* that detached worker — off the interactive
path — and falls back to the heuristic on failure, flagging the fact for later
re-distillation. Never move distillation or embedding onto the interactive path.

## Token / latency budget discipline

The read side is governed by hard caps, all configurable (see
[README.md § Configuration](../../../README.md)):

- **`min_sim`** — similarity gate; a fact must clear it to be injected.
- **`top_k`** — max facts per prompt.
- **`max_chars`** — hard character cap per hook injection (the token guard).
- **`core_size`** — stable facts injected once at `SessionStart` (cache-friendly).

When you change hook behaviour, state the budget impact: does injected text grow?
Does hot-path wall-clock grow? An increase must justify itself against recall quality
measured by `engram eval`.

## Cache placement (why SessionStart vs UserPromptSubmit)

`SessionStart` `additionalContext` lands near the head → stable all session → joins
the prompt-cache prefix → cheap on every later turn: use it for the **stable core**.
`UserPromptSubmit` lands at the tail, varies per turn, never a same-turn cache hit:
use it for **tiny JIT** recall only. Don't put per-turn-varying content at
SessionStart (busts nothing but wastes the cache benefit) or bulky content in
UserPromptSubmit (pays full price every turn). See [DESIGN.md § Cache efficiency](../../../DESIGN.md).

## See also

- [01-poeaa-and-layers.md](./01-poeaa-and-layers.md) — composition roots (`bin/*`) wire these hooks.
- [DESIGN.md § Latency efficiency / Risks](../../../DESIGN.md) — the daemon, timeout, and fail-open mitigations.
- [README.md § Memory-first enforcement](../../../README.md) — `ENGRAM_ENFORCE` modes.
