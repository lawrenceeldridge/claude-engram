# Troubleshooting

Common failures across the `ltm-git` modes, in Symptom / Cause / Fix form.

## Contents

- [commit Mode](#commit-mode)
- [pr-create Mode](#pr-create-mode)
- [pr-merge Mode](#pr-merge-mode)
- [General GitHub CLI](#general-github-cli)

---

## commit Mode

### Wrong scope in commit message

**Symptom:** The scope doesn't match the area that changed (e.g. `viewer` for a change
under `core/`).

**Cause:** Scope typed without checking the mapping.

**Fix:** See the scope table in `conventions.md`. Prefer `ltm` unless a narrower scope
(`core` / `viewer` / `bench` / `docs` / `ci`) adds real signal. Amend with
`git commit --amend` if not yet pushed.

### Breaking change not flagged

**Symptom:** A breaking change was committed as a plain `feat`.

**Cause:** Breaking changes need `!` after the type (`feat(ltm)!:`) or a
`BREAKING CHANGE` footer.

**Fix:** If not yet pushed, `git commit --amend`. This repo has no auto-release, so the
practical impact is only the manual version bump — bump the **major** in `plugin.json`
+ `marketplace.json` when you release (see `conventions.md#releasing-version-bump--tag`).

### Accidentally committed on `main`

**Symptom:** `git branch --show-current` shows `main` with unpushed commits.

**Cause:** Forgot to branch first.

**Fix (do NOT push):**
```bash
git branch <feat>
git reset --hard origin/main
git checkout <feat>
git push -u origin <feat>
gh pr create
```

---

## pr-create Mode

### `ruff` reports lint or format errors

**Symptom:** `ruff check .` or `ruff format --check .` fails.

**Cause:** Code doesn't pass the quality gate.

**Fix:**
1. Auto-fix what ruff can: `ruff check --fix .` and `ruff format .`
2. Fix remaining issues by hand.
3. Commit: `fix(ltm): resolve lint errors` (or fold into the change).
4. Re-run — do not push until clean. **Never `--no-verify`.**

### Tests fail

**Symptom:** `python3 -m unittest discover -s tests` fails from `plugins/ltm/`.

**Cause:** A regression, or a hook that no longer fails open.

**Fix:** Read the failing test, fix the cause (not the test), re-run. Remember the
invariants: core imports stdlib-only, hooks exit 0 on any error, capture stays detached.

### Retrieval-affecting change with no benchmark

**Symptom:** A change touches embeddings, ranking, quantisation, or distillation but no
`ltm eval` numbers are recorded.

**Cause:** Skipped the measurement step.

**Fix:** Run `python3 bin/ltm eval --backends hash` (add `fastembed` to compare) and put
the before/after Recall@k / MRR into the PR's Token/latency-impact section.

### Branch has no upstream

**Symptom:** `git push` fails with "The current branch has no upstream branch."

**Cause:** Branch not yet pushed.

**Fix:** `git push -u origin "$(git branch --show-current)"`.

### PR already exists for this branch

**Symptom:** `gh pr create` fails because a PR already exists.

**Cause:** A PR (possibly a draft) was created earlier.

**Fix:** `gh pr view --web` and ask the user whether to update the existing PR instead.

---

## pr-merge Mode

### PR not mergeable

**Symptom:** `gh pr view <PR#> --json mergeable` reports conflicts or a blocked state.

**Cause:** Base branch moved, or checks are red.

**Fix:** Rebase/merge `main` into the branch locally, resolve conflicts, re-run the
pre-push checks, push. Only merge once `mergeable` is clean.

### Empty diff / wrong PR number

**Symptom:** `gh pr diff <PR#>` returns nothing.

**Cause:** PR already merged/closed, or the number is wrong.

**Fix:** `gh pr view <PR#>` to confirm state and number. For a merged PR, use
`git diff` against the merge base instead of `gh pr diff`.

---

## General GitHub CLI

### `gh` not authenticated

**Symptom:** `gh` commands fail with an auth error.

**Cause:** Not logged in or token expired.

**Fix:** `gh auth login` and follow the prompts.

### Wrong repository targeted

**Symptom:** A `gh` command targets the wrong repo (e.g. a fork).

**Cause:** Default repo misconfigured.

**Fix:** `gh repo set-default lawrenceeldridge/claude-ltm`, or pass
`--repo lawrenceeldridge/claude-ltm` explicitly.

### GitHub API rate limit

**Symptom:** `gh api` returns 403 / rate-limit.

**Cause:** Too many calls in a short window.

**Fix:** `gh api rate_limit --jq '.resources.core'`, wait, and retry.
