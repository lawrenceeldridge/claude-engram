---
alwaysApply: true
---

# Architecture

> **Priority:** these rules protect the shape that makes claude-ltm cheap and safe.
> Read alongside [../00-quality/](../00-quality/). Depth lives in
> [DESIGN.md](../../../DESIGN.md) and the [`ltm-poeaa`](../../skills/ltm-poeaa/SKILL.md) skill.

claude-ltm is **CQRS + Hexagonal (Ports & Adapters)**. The two sides have opposite
performance profiles and are split accordingly:

- **Write side (capture)** — heavy, batch, latency-tolerant. Runs **detached** at
  `Stop` / `SessionEnd` / `PreCompact`. Zero interactive-token cost.
- **Read side (recall)** — tiny, hot-path, token- and latency-critical. Runs in
  `UserPromptSubmit` (JIT, gated) and `SessionStart` (stable core, cache-friendly).

```
UserPromptSubmit ─► recall (embed → rank → gated inject)   ← hot path, tail of context
SessionStart     ─► core inject (small, stable)            ← joins cached prefix
Stop/SessionEnd/PreCompact ─► spawn detached capture worker ← fire & forget
```

## Files in this folder

| File | Covers |
|---|---|
| [01-poeaa-and-layers.md](./01-poeaa-and-layers.md) | The POEAA / Cosmic Python pattern map (Repository over Active Record, Gateway + Separated Interface, Query Object, Functional Core / Imperative Shell), the layer seams, and the stdlib-core boundary. |
| [02-hooks-and-budgets.md](./02-hooks-and-budgets.md) | Hook fail-open contract, the detached-capture rule, and the token/latency budget discipline that governs the hot path. |

## The non-negotiables

1. **Stdlib-first core** — `core/**` imports with the standard library alone; heavy
   deps live behind adapters and self-provision (see [../01-general/01-tooling.md](../01-general/01-tooling.md)).
2. **Hooks fail open** — exit 0 and inject nothing on any error; a 5s timeout is the ceiling.
3. **Keep the interactive path near-zero** — capture is detached; recall is
   threshold-gated (`min_sim`) and byte-capped (`max_chars`, `top_k`).
4. **Don't collapse the seams** — `core/` (domain + ports), `core/adapters/`
   (driven adapters), `bin/*` (composition roots / driving adapters) stay distinct.

## See also

- [DESIGN.md](../../../DESIGN.md) — the two-budget model, cache analysis, memory lifecycle.
- [`ltm-poeaa`](../../skills/ltm-poeaa/SKILL.md) — pattern catalogue + this project's defaults.
