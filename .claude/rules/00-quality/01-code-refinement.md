---
alwaysApply: true
---

# Code Refinement

These principles govern how code in this repo is written and modified.

## 1. Preserve Functionality

**Never change what code does — only how it does it.** All original features,
outputs, and behaviours must remain intact. Recall/capture are load-bearing on a
live hook path: a refactor that changes injected text, ranking order, or the
fail-open contract is a behaviour change, not a refinement.

## 2. Apply Project Standards

Follow the patterns already established here:

- **Stdlib-first core** — `plugins/ltm/core/**` imports cleanly with the standard
  library alone. Optional deps (`fastembed`) live behind adapters and self-provision.
- **Error handling** — hooks and adapters **fail open**: prefer explicit checks and a
  graceful fallback over an exception that could reach the interactive turn.
- **Type hints** — full annotations on public functions; the core is typed.
- **Naming** — follow the POEAA roles already in the tree (`*Repository`, `*Distiller`,
  `*Gateway`, Query Objects). See [../02-architecture/01-poeaa-and-layers.md](../02-architecture/01-poeaa-and-layers.md).
- **Formatting** — `ruff` for linting and formatting.

## 3. Enhance Clarity

Simplify structure by:

- Reducing unnecessary complexity and nesting
- Eliminating redundant code and abstractions
- Using clear names; consolidating related logic
- Removing comments that merely restate obvious code

**Avoid nested ternaries.** Prefer `match` or `if`/`else` chains for multiple
conditions. Choose clarity over brevity.

## 4. Maintain Balance

Avoid over-simplification that would reduce clarity, produce clever-but-opaque
code, combine too many concerns into one function, remove helpful abstractions
(the Ports & Adapters seams exist for a reason — don't collapse `core/` into
`bin/`), or prioritise "fewer lines" over readability, debuggability, or extensibility.

## 5. Focus Scope

Only refine code touched in the current session, unless explicitly asked to review
a broader scope.

---

## Development Workflow Checklist

Before writing code, verify your approach addresses:

| Criterion | Question to ask |
|-----------|-----------------|
| **Behaviour parity** | Does this preserve recall/capture/index behaviour exactly? |
| **Budget impact** | Which budget does it touch — tokens (injected text) or latency (hook wall-clock)? Is it still a net win? |
| **Stdlib-first** | Does `core/` still import with the standard library alone? Are optional deps behind adapters? |
| **Fail-open** | If this runs in a hook, does it exit 0 and inject nothing on any error? |
| **Layering** | Does it respect the Ports & Adapters seams (core vs adapters vs composition root in `bin/`)? |
| **Type safety** | Are public functions fully annotated? |
| **Testability** | How is it tested? For retrieval changes, does `ltm eval` still hold or improve? |
| **Measurement** | For embedding/ranking/quantisation/distillation changes — did you A/B with `ltm eval` before shipping? |

Present a short plan and wait for approval before implementing non-trivial changes.
