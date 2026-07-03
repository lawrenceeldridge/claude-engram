# Git Conventions (claude-ltm)

Git and GitHub conventions for the **claude-ltm** repo. This expands on the
always-applied rule [`.claude/rules/01-general/02-commit-conventions.md`](../../../rules/01-general/02-commit-conventions.md),
which is the source of truth — this file adds the branch, PR, pre-push, and release
detail the skill needs. This repo is **GitHub-only**: no Linear ticket ID in the
subject, no Slack step, no release-please.

## Contents

- [Repository Context](#repository-context)
- [Conventional Commits](#conventional-commits)
- [Scopes](#scopes)
- [Version Intent](#version-intent)
- [Branch Naming](#branch-naming)
- [Pull Requests](#pull-requests)
- [Pre-Push Checks](#pre-push-checks)
- [Merge Strategy](#merge-strategy)
- [Releasing (version bump + tag)](#releasing-version-bump--tag)
- [Commit Rules](#commit-rules)
- [Attribution Footer](#attribution-footer)

---

## Repository Context

Single repo: `lawrenceeldridge/claude-ltm`. It is a **Python plugin** — only
`plugins/ltm/**` ships to installers; `.claude/` + `CLAUDE.md` are dev-only tooling.
Use the GitHub CLI (`gh`) for all GitHub operations.

---

## Conventional Commits

All commits and PR titles follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

- `<scope>` is optional (`docs:` and `ci:` may omit it).
- Subject in imperative mood, no trailing period, lower-case after the colon.
- History is the style source of truth, e.g.
  `feat(ltm): consult-first gate — hold Grep/Glob until memory/index was checked`.

### Types

| Type | Use for | Version intent |
|------|---------|----------------|
| `feat` | New capability | minor |
| `fix` | Bug fix | patch |
| `perf` | Performance / token or latency win | patch |
| `docs` | Documentation only (README, DESIGN, `.claude/`, CLAUDE.md) | none |
| `refactor` | Neither feature nor fix | none |
| `test` | Adding or correcting tests / the benchmark | none |
| `chore` | Build, deps, tooling | none |
| `ci` | CI/CD pipeline | none |

Append `!` (e.g. `feat!:`) or a `BREAKING CHANGE` footer for a major bump.

---

## Scopes

Keep scopes to real areas of the tree. `ltm` is the dominant scope; use a narrower one
only when it adds real signal. **No package/monorepo scopes** — this is a single plugin.

| Scope | Maps to |
|-------|---------|
| `ltm` | `plugins/ltm/` — the plugin itself (**default**) |
| `core` | `plugins/ltm/core/` when the change is core-only and worth distinguishing |
| `viewer` | `plugins/ltm/viewer/` |
| `bench` | `plugins/ltm/bench/` |
| `docs` | README, DESIGN, `.claude/`, CLAUDE.md |
| `ci` | CI configuration |

Don't invent deep sub-scopes; prefer `ltm`.

### Examples

```bash
git commit -m "feat(ltm): add resident daemon fallback for warm embeddings"
git commit -m "fix(ltm): single-flight capture to stop worker/daemon pileup"
git commit -m "perf(core): int8 pre-filter before float cosine rescore"
git commit -m "test(ltm): assert recall hook fails open when daemon is dead"
git commit -m "docs: shape .claude rules/skills for the plugin dev workflow"
```

---

## Version Intent

There is **no automated release bot** here. Commit types describe *intent* only — a
`feat` does not bump a version by itself. The actual bump is a manual edit (see
[Releasing](#releasing-version-bump--tag)). When composing a message, note the intent:
`feat` → minor, `fix`/`perf` → patch, `!` / `BREAKING CHANGE` → major. Use `refactor`
for changes that shouldn't imply any bump.

---

## Branch Naming

There is **no ticket ID** (no Linear). Use the commit type plus a short slug:

```bash
git checkout -b feat/consult-first-gate
git checkout -b fix/single-flight-capture
git checkout -b docs/shape-claude-rules
```

**Branch strategy:** GitHub Flow — feature branches merge to `main` via PR.
**Never commit or push directly to `main`** (CLAUDE.md Hard Prohibition #1).

---

## Pull Requests

### PR Title Format

`<type>(<scope>): <description>` — same conventional form as a commit subject, with
**no** ticket token.

| Flavour | Example |
|---------|---------|
| Feature | `feat(ltm): consult-first gate` |
| Bug fix | `fix(ltm): single-flight capture to stop worker pileup` |
| Perf | `perf(core): int8 pre-filter before float cosine rescore` |
| Docs / no bump | `docs: document LTM_ENFORCE guard and MCP tools` |

### PR Body Template

```markdown
### What is the purpose of this PR?

<Brief description of the change and why it's needed.>

### How was this tested?

<ruff + unittest results; any new/updated tests or `ltm eval` benchmark deltas.>

### Token / latency impact

<Which budget it touches and why it is still a net win — or "none".>

---
Generated with [Claude Code](https://claude.com/claude-code)
```

The **Token / latency impact** section replaces a service-style "Prod Impact" — this is
a plugin, so state the budget impact per [CLAUDE.md § The one constraint](../../../../CLAUDE.md).
For retrieval-affecting changes (embeddings, ranking, quantisation, distillation),
include the `ltm eval` before/after numbers.

### Creating a PR

```bash
gh pr create \
  --title "feat(ltm): add resident daemon fallback for warm embeddings" \
  --body "$(cat <<'EOF'
### What is the purpose of this PR?

Keeps the fastembed model warm across short-lived hook processes via an optional
resident daemon, so recall latency doesn't pay a cold-load per hook.

### How was this tested?

ruff clean; `python3 -m unittest discover -s tests` green. Added a test asserting the
recall hook falls open to in-process embedding when the daemon socket is dead.

### Token / latency impact

Latency budget: removes model-load from the hot path when the daemon is up. Zero token
impact. Fails open, so no correctness risk when the daemon is absent.

---
Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Add `--draft` for a draft PR.

---

## Pre-Push Checks

No `just` and no CI workflow in this repo — checks are plain commands, run from
`plugins/ltm/` (see [`.claude/rules/01-general/01-tooling.md`](../../../rules/01-general/01-tooling.md)):

```bash
cd plugins/ltm
ruff check . && ruff format --check .        # lint + format (non-mutating)
python3 -m unittest discover -s tests        # test suite (all stdlib)
```

Fix everything reported — do not push failing code. Retrieval-affecting changes also run:

```bash
python3 bin/ltm eval --backends hash         # add fastembed to compare backends
```

**Never `--no-verify`** (CLAUDE.md Hard Prohibition #2) — fix what the check flags.

---

## Merge Strategy

PRs merge via **squash merge** for a linear history:

```bash
gh pr merge <number> --squash --delete-branch
```

---

## Releasing (version bump + tag)

There is **no release-please**. A release is a small manual step:

1. Bump `version` in **both**
   [`plugins/ltm/.claude-plugin/plugin.json`](../../../../plugins/ltm/.claude-plugin/plugin.json)
   and [`.claude-plugin/marketplace.json`](../../../../.claude-plugin/marketplace.json) —
   keep them in sync (semver: `feat` → minor, `fix`/`perf` → patch, breaking → major).
2. Commit: `chore(ltm): release vX.Y.Z` (or fold into the feature PR).
3. After merge to `main`, tag and push the tag:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
4. Optionally cut a GitHub release: `gh release create vX.Y.Z --generate-notes`.

Keep README's config table in sync with `plugin.json` when defaults change.

---

## Commit Rules

- **DO** end messages with the Co-Authored-By trailer when committing on the user's
  behalf (see [Attribution Footer](#attribution-footer)).
- **DO NOT** add ticket IDs or Slack references — this repo has neither.
- **DO NOT** skip hooks with `--no-verify` / `--no-gpg-sign`.
- **DO NOT** push or force-push to `main`.
- **DO NOT** amend published commits — create new commits to fix issues.
- Prefer adding named files over `git add -A` / `git add .`; never `git add -f` an
  ignored file (e.g. anything under a `.gitignore`-listed dir).

---

## Attribution Footer

End commit messages made on the user's behalf with the trailer the harness specifies:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

Do not add any other attribution, ticket link, or Slack reference.
