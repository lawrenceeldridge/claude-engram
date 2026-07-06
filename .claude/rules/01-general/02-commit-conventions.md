---
alwaysApply: true
---

# Commit & PR Conventions

Every commit message and PR title follows **Conventional Commits**. This repo is
**GitHub-only** — there is no Linear ticket ID in the subject and no Slack
notification step (unlike the sibling projects this tooling was ported from).

## Format

```
<type>(<scope>): <description>
```

- `<scope>` is optional (`docs:` and `ci:` may omit it).
- Subject is imperative mood, no trailing period, lower-case after the colon.
- The existing history is the source of truth for style — e.g.
  `feat(engram): consult-first gate — hold Grep/Glob until memory/index was checked`.

## Allowed types

| Type | Use for | Version bump |
|------|---------|---------------|
| `feat` | New capability | minor |
| `fix` | Bug fix | patch |
| `perf` | Performance / token or latency win | patch |
| `docs` | Documentation only (README, DESIGN, `.claude/`, CLAUDE.md) | none |
| `refactor` | Neither feature nor fix | none |
| `test` | Adding or correcting tests / the benchmark | none |
| `chore` | Build, deps, tooling | none |
| `ci` | CI/CD pipeline | none |

Append `!` (e.g. `feat!:`) or a `BREAKING CHANGE` footer for a major bump.

## Scopes

Keep scopes to real areas of the tree. In practice `engram` is the dominant scope
(the plugin), matching current history (`feat(engram):`, `fix(engram):`). Others as needed:

| Scope | Maps to |
|-------|---------|
| `engram` | `plugins/engram/` — the plugin itself (default) |
| `core` | `plugins/engram/core/` when the change is core-only and worth distinguishing |
| `viewer` | `plugins/engram/viewer/` |
| `bench` | `plugins/engram/bench/` |
| `docs` | README, DESIGN, `.claude/`, CLAUDE.md |
| `ci` | CI configuration |

Don't invent deep sub-scopes; prefer `engram` unless a narrower scope adds real signal.

## Examples

- `feat(engram): add resident daemon fallback for warm embeddings`
- `fix(engram): single-flight capture to stop worker/daemon pileup`
- `perf(engram): int8 pre-filter before float cosine rescore`
- `test(engram): assert recall hook fails open when daemon is dead`
- `docs: shape .claude rules/skills for the plugin dev workflow`

## Branch & PR flow

- **Never push to `main`.** Branch, push the feature branch, open a PR
  (Hard Prohibition #1 in [CLAUDE.md](../../../CLAUDE.md)).
- **Never `--no-verify`.** Fix what the pre-commit / test step flags.
- Full commit body, PR description template, and merge strategy live in the
  [`engram-git`](../../skills/engram-git/SKILL.md) skill.

## Attribution footer

When committing on the user's behalf, end the message with the Co-Authored-By
trailer the harness specifies; do not add ticket links or Slack references.
