# Merge Report Template

A short wrap-up generated after a PR is merged, covering the PR lifecycle. Used by the
`ltm-git` **pr-merge** mode. Optional — generate it only when the user wants a summary.

---

## Where it goes

This repo has **no Linear ticket** to post to. The report is:

1. **Presented inline** in Claude Code for the user to read, and
2. optionally posted as a **PR comment** if a permanent record is wanted:
   `gh pr comment <PR#> --body-file -`.

Follow-up work identified during review becomes a **GitHub issue**
(`gh issue create`), not a ticket.

---

## Data Sources

Fetch via `gh` before generating the report (`{owner}/{repo}` expand from the repo context):

| Data | Command |
|------|---------|
| PR metadata | `gh pr view <PR#> --json title,author,mergedBy,mergedAt,headRefName,baseRefName,reviews` |
| PR comments | `gh pr view <PR#> --json comments --jq '.comments[]'` |
| Line-specific review comments | `gh api repos/{owner}/{repo}/pulls/<PR#>/comments` |

If a fetch fails, note the gap in the report rather than failing entirely. For a
self-merged PR with no review activity, most sections collapse to a one-line summary —
don't manufacture discussion that didn't happen.

---

## Report Structure

```markdown
# PR Merge Report: #<number> — <title>

**PR:** #<number> — <title>
**Author:** <author>
**Reviewers:** <reviewer list, or "none — self-merged">
**Merged:** <date>
**Branch:** <branch> → main

---

## Summary

<2-3 sentences: what was delivered and why. Name the token/latency budget it moved.>

---

## Review Timeline

| Date | Actor | Action | Detail |
|------|-------|--------|--------|
| <date> | <user> | PR Created | <initial description summary> |
| <date> | <user> | Review | <N comments> |
| <date> | <user> | Merged | Squash merge to main |

---

## Discussion Summary

<Synthesise the review threads by theme — decisions made, notable feedback. If the PR
was self-merged with no comments, write: "No review comments — PR was self-merged.">

---

## Follow-up Suggestions

<Optional GitHub issues worth opening from deferred items or review discussion.>

| Type | Priority | Description |
|------|----------|-------------|
| <feature/bug/improvement> | <high/normal/low> | <one-line description for `gh issue create`> |

If none: "No follow-up issues identified."
```

---

## Guidelines

- Keep the Summary to ~30 seconds of reading; name the budget the change touched.
- The Review Timeline is chronological and factual — no editorialising.
- The Discussion Summary synthesises by theme, it does not transcribe.
- Follow-up Suggestions must be specific enough to run `gh issue create` from.
- Self-merged / zero-comment PRs are the common case here — use the defaults above
  rather than padding the report.
