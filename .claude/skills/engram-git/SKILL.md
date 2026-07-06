---
name: engram-git
description: GitHub and version-control workflows for the claude-engram plugin repo — conventional commits (scoped to engram/core/viewer/bench/docs/ci), branch + PR creation with pre-push ruff and unittest checks, and PR merge with an optional merge report. GitHub-only; no Linear, Slack, or release automation. Use when committing, branching, pushing, creating PRs, or merging.
argument-hint: "commit | pr-create [--draft] | pr-merge [PR#]"
metadata:
  author: Lawrence Eldridge
---

# engram-git

Git and GitHub workflows for the **claude-engram** repo (`lawrenceeldridge/claude-engram`).
This repo is **GitHub-only**: Conventional Commits, feature branches, and PRs — no
Linear, no Slack, no release-please. Versioning is manual (bump `plugin.json` +
`marketplace.json`, then tag). See [`references/conventions.md`](references/conventions.md)
for the authoritative format and [`references/troubleshooting.md`](references/troubleshooting.md)
when something fails.

This skill provides three modes:
- **commit** — stage files and compose a conventional commit message with the right type + scope.
- **pr-create** — run pre-push checks (ruff + tests), push a feature branch, open a PR.
- **pr-merge** — merge a PR (if open) and generate an optional merge report.

## When to Use This Skill

Use for any git or GitHub operation in this repo: committing, branching, pushing,
creating a PR, or merging. **Do NOT use** for reading files, searching code, or
running the plugin — use Read/Grep/Glob or the commands in
[`.claude/rules/01-general/01-tooling.md`](../../rules/01-general/01-tooling.md) directly.

## Two hard rules (from [CLAUDE.md](../../../CLAUDE.md) Hard Prohibitions)

1. **Never push to `main`.** Branch, push the feature branch, open a PR.
   Recovery if you committed on `main`:
   `git branch <feat>; git reset --hard origin/main; git checkout <feat>; git push -u origin <feat>; gh pr create`
2. **Never `--no-verify` / `--no-gpg-sign`.** Fix whatever the check flags instead.

## Arguments

```
/engram-git commit
/engram-git pr-create
/engram-git pr-create --draft
/engram-git pr-merge 42
```

**Format:** `<mode> [arguments]`

| Mode | Arguments | Description |
|------|-----------|-------------|
| `commit` | (none) | Stage and commit with conventional format |
| `pr-create` | `[--draft]` | Pre-push checks, push branch, create PR |
| `pr-merge` | `[PR#]` | Merge PR (if open) + optional merge report |

If the user describes an action without naming a mode, infer the most appropriate one.

---

## Mode 1: commit

Stage files and compose a conventional commit message with the correct type and scope.

### Workflow

1. **Identify changes** — `git status` and `git diff --stat` to see what changed.

2. **Determine scope** — map changed paths to a scope (full table in
   [`references/conventions.md`](references/conventions.md#scopes)):

   | Changed path | Scope |
   |---|---|
   | `plugins/engram/**` (general) | `engram` *(default)* |
   | `plugins/engram/core/**` (core-only, worth distinguishing) | `core` |
   | `plugins/engram/viewer/**` | `viewer` |
   | `plugins/engram/bench/**` | `bench` |
   | `README.md`, `DESIGN.md`, `.claude/**`, `CLAUDE.md` | `docs` |
   | CI config | `ci` |

   Prefer `engram` unless a narrower scope adds real signal. Don't invent deep sub-scopes.

3. **Select the type** — `feat` (new capability), `fix` (bug), `perf` (token/latency
   win), `refactor`, `test`, `chore`, `docs`, `ci`. Append `!` or a `BREAKING CHANGE`
   footer for a breaking change. See the type table in `references/conventions.md`.

4. **Note version intent (informational only)** — this repo has **no** automated
   release bot, so a commit type never bumps a version by itself. `feat` → minor,
   `fix`/`perf` → patch, `!`/`BREAKING CHANGE` → major describe *intent*; the actual
   bump is a manual edit to `plugin.json` + `marketplace.json` (see
   `references/conventions.md#releasing-version-bump--tag`).

5. **Stage and commit** — prefer named files over `git add -A`. **Never `git add -f`**
   an ignored file. When committing on the user's behalf, end the message with the
   Co-Authored-By trailer the harness specifies (see `references/conventions.md#attribution-footer`).
   Never add ticket links or Slack references.

Consult `references/conventions.md` for the full format, scope table, and rules.

---

## Mode 2: pr-create

Push a feature branch and open a PR, after running the pre-push checks.

### Step 1: Ensure you are on a feature branch

Never push to `main`. If `git branch --show-current` reports `main`, create a branch
first: `git checkout -b <type>/<short-description>` (e.g. `feat/consult-first-gate`).
Branch names use the commit type + a short slug — **no** ticket ID (there is no Linear).

### Step 2: Gather PR body context

Analyse the branch's commits and diff:

```bash
git log main..HEAD --oneline
git diff main...HEAD --stat
```

If a plan artifact exists under `docs/generated/` for this work, use it as the primary
source for purpose/testing/impact. Otherwise draft those sections from the commits and
changed files.

### Step 3: Bump the version if this PR ships

**Part of every shipping PR — not a separate release chore.** If the branch changes
shipped plugin behaviour (a `feat` / `fix` / `perf` touching `plugins/engram/**`), bump
`version` in **both** `plugins/engram/.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json`, kept in sync (`feat` → minor, `fix`/`perf` → patch,
breaking → major). Docs-only, `test`, `chore`, `ci`, and dev-tooling (`.claude/**`,
`CLAUDE.md`) PRs skip this.

Edit the manifests **by hand — never `ruff format` them** (it rewrites JSON with trailing
commas → invalid). Why it's mandatory, not optional: the `claude-engram` marketplace source
is a **local directory**, so an installed plugin only re-fetches its code when the
advertised version changes — skip the bump and the feature ships *dead* (it did: the
version froze at `0.13.1` across #10–#16). Full rationale + tag/`/plugin`-update flow:
[`references/conventions.md` § Releasing](references/conventions.md#releasing-version-bump--tag).

### Step 4: Pre-push checks

There is no `just` here — checks are plain commands, run from `plugins/engram/`:

```bash
cd plugins/engram
ruff check .                                 # lint (tree-wide)
ruff format --check $(cd .. && git diff main...HEAD --name-only -- 'plugins/engram/**' | sed 's#plugins/engram/##')
python3 -m unittest discover -s tests        # test suite (all stdlib)
```

Format-check the files **your branch changed**, not the whole tree: `ruff format --check .`
can flag pre-existing drift you didn't touch, and it false-positives on the JSON manifests
(see Step 3) — neither is a reason to reformat. If anything fails: fix it, commit the fix
(`fix(engram): resolve lint errors` etc.), and re-run. **Do NOT push until ruff and the tests
pass.** If a change touches embeddings, ranking, quantisation, or distillation, also run
`python3 bin/engram eval --backends hash` and confirm no regression (see
[`.claude/rules`](../../rules/) and README § Benchmarking). See
`references/troubleshooting.md` for common failures.

### Step 5: Push the branch

```bash
git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null   # upstream?
git push -u origin "$(git branch --show-current)"                  # first push
git push                                                           # subsequent pushes
```

### Step 6: Create the PR

Derive `<type>` and `<scope>` from the diff the same way commit mode does. Use the body
template from [`references/conventions.md`](references/conventions.md#pr-body-template)
(no Linear line):

```bash
gh pr create \
  --title "<type>(<scope>): <description>" \
  --body "$(cat <<'EOF'
### What is the purpose of this PR?

<from Step 2>

### How was this tested?

<ruff + unittest results; any new/updated tests or benchmark deltas>

### Token / latency impact

<which budget it touches and why it is still a net win — or "none">

---
Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

For draft PRs add `--draft`. The "Token / latency impact" section replaces the old
"Prod Impact" section — this is a plugin, not a service; state the budget impact per
[CLAUDE.md § The one constraint](../../../CLAUDE.md).

---

## Mode 3: pr-merge

Merge a PR (if still open) and, on request, generate a short merge report.

### Step 1: Resolve the PR number

If provided, use it. Otherwise:

```bash
gh pr list --author @me --state open --limit 5      # open PRs
gh pr list --state merged --author @me --limit 5     # else recently merged
```

Confirm with the user which PR to act on.

### Step 2: Merge (if the PR is still open)

```bash
gh pr view <PR#> --json state --jq '.state'
```

**If open:**

1. Check readiness:
   ```bash
   gh pr checks <PR#>
   gh pr view <PR#> --json mergeable,reviewDecision --jq '{mergeable, reviewDecision}'
   ```
2. If checks pass, ask: "PR #N is open and ready. Shall I squash and merge it?"
3. **Wait for confirmation**, then:
   ```bash
   gh pr merge <PR#> --squash --delete-branch
   ```
4. If checks are failing or there are conflicts, report them and ask how to proceed.
   Do NOT merge a PR with failing checks unless the user explicitly confirms.

**If already merged:** skip to Step 3.

### Step 3: Merge report (optional)

If the user wants a wrap-up, fetch the lifecycle data and follow
[`references/merge-report-template.md`](references/merge-report-template.md):

```bash
gh pr view <PR#> --json title,author,mergedBy,mergedAt,headRefName,baseRefName,reviews
gh pr view <PR#> --json comments --jq '.comments[] | {author: .author.login, body: .body[0:200], createdAt}'
gh api repos/{owner}/{repo}/pulls/<PR#>/comments \
  --jq '.[] | {user: .user.login, path, body: .body[0:200], createdAt: .created_at}'
```

Present the report **inline**. There is no Linear ticket to post it to; if a permanent
record is wanted, post it as a PR comment with `gh pr comment <PR#> --body-file -`.
Any follow-up work identified becomes a GitHub issue (`gh issue create`), not a ticket.

---

## Project context

**Repo:** `lawrenceeldridge/claude-engram` — a Python plugin (see [README.md](../../../README.md)).
Only `plugins/engram/**` ships to installers; `.claude/` + `CLAUDE.md` are dev-only.
The core is **stdlib-only** by contract; hooks **fail open**; capture is detached.
Every change should state which budget (token or latency) it touches.

| Area | Path | Typical scope |
|---|---|---|
| Plugin (default) | `plugins/engram/` | `engram` |
| Pure-Python core | `plugins/engram/core/` | `core` |
| Localhost viewer | `plugins/engram/viewer/` | `viewer` |
| Recall benchmark | `plugins/engram/bench/` | `bench` |
| Docs / dev config | README, DESIGN, `.claude/`, CLAUDE.md | `docs` |

## Examples

### Example 1: Conventional commit

**User says:** `/engram-git commit`

**Result:** Runs `git status`, sees changes under `plugins/engram/core/recall.py`, picks
scope `core` and type `perf` for an int8 pre-filter, commits
`perf(core): int8 pre-filter before float cosine rescore` with the Co-Authored-By trailer.

### Example 2: PR creation

**User says:** `/engram-git pr-create`

**Result:** Confirms the branch is `feat/consult-first-gate` (not `main`); since it's a
`feat` touching `plugins/engram/**`, bumps `plugin.json` + `marketplace.json` (e.g. `0.14.0`
→ `0.15.0`) in the same PR; runs `ruff check`, `ruff format --check` on the branch's
changed files, and the unittest suite from `plugins/engram/`; pushes with `-u`; opens a PR
titled `feat(engram): consult-first gate` with the purpose / testing / token-impact body —
no Linear line.

### Example 3: Merge and report

**User says:** `/engram-git pr-merge 12`

**Result:** Finds PR #12 open with green checks, asks to squash-merge, runs
`gh pr merge 12 --squash --delete-branch`, then prints a short inline merge report and
offers to open a follow-up GitHub issue for a deferred cleanup noted in review.
