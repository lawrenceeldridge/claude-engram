#!/usr/bin/env python3
"""Live paired A/B: tokens per task with the ltm plugin on vs off.

The gold-standard token-savings experiment. Each task in ``bench/tasks.json``
runs twice in fresh headless sessions (``claude -p --output-format json``):

- **on**  — the current environment (plugin enabled);
- **off** — ``--settings bench/ab_off_settings.json`` (``ltm@claude-ltm``
  disabled), verified by scanning the off-arm session transcript for any ltm
  activity; a contaminated off-run invalidates that task's pair.

Tokens are read from the CLI's JSON result (input, cache creation, cache read,
output) and compared both raw and as a cache-discounted composite. Correctness
is gated per task by ``answer_check`` — a pair where either arm fails the check
is reported separately, never silently dropped, so "cheaper by failing" is
always visible. Significance: exact Wilcoxon signed-rank on the paired
composites.

BURNS REAL API TOKENS. Run on demand, never in CI. ``--dry-run`` prints the
commands without spending anything.

Run (from plugins/ltm/):
    python3 bench/run_ab.py --dry-run
    python3 bench/run_ab.py                # full paired run
    python3 bench/run_ab.py --tasks t01,t05
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE / "tasks.json"
OFF_SETTINGS = HERE / "ab_off_settings.json"

# Cache-discounted composite weights — the Anthropic price ratios for cache
# writes (1.25x input) and cache reads (0.1x input); output priced separately
# but weighted 1.0 here since both arms produce comparable answer lengths.
W_INPUT, W_CACHE_WRITE, W_CACHE_READ, W_OUTPUT = 1.0, 1.25, 0.1, 1.0

LTM_MARKERS = ("mcp__plugin_ltm", "plugins/ltm/bin/")


def composite_tokens(usage: dict) -> float:
    """Single cost figure per run, weighting each token class by its price ratio."""
    return (
        usage.get("input_tokens", 0) * W_INPUT
        + usage.get("cache_creation_input_tokens", 0) * W_CACHE_WRITE
        + usage.get("cache_read_input_tokens", 0) * W_CACHE_READ
        + usage.get("output_tokens", 0) * W_OUTPUT
    )


def wilcoxon_exact(diffs: list[float]) -> tuple[float, float]:
    """Exact two-sided Wilcoxon signed-rank p-value for paired differences.

    Returns ``(w_minus, p)``. Zero differences are dropped (standard practice);
    ties among |diffs| get average ranks. Exact by enumerating the signed-rank
    sum distribution (DP over rank subsets), fine for the n<=25 this bench uses.
    """
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    if n == 0:
        return 0.0, 1.0
    by_abs = sorted(range(n), key=lambda i: abs(nonzero[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(nonzero[by_abs[j + 1]]) == abs(nonzero[by_abs[i]]):
            j += 1
        avg = (i + j) / 2 + 1  # average of 1-based ranks i+1..j+1
        for k in range(i, j + 1):
            ranks[by_abs[k]] = avg
        i = j + 1
    w_minus = sum(r for d, r in zip(nonzero, ranks) if d < 0)
    w_plus = sum(r for d, r in zip(nonzero, ranks) if d > 0)
    w = min(w_minus, w_plus)
    # Distribution of the rank sum over all 2^n sign assignments. Ranks may be
    # half-integers under ties, so work in doubled units to stay integral.
    doubled = [round(2 * r) for r in ranks]
    counts: dict[int, int] = {0: 1}
    for dr in doubled:
        nxt: dict[int, int] = {}
        for s, c in counts.items():
            nxt[s] = nxt.get(s, 0) + c
            nxt[s + dr] = nxt.get(s + dr, 0) + c
        counts = nxt
    total = 2**n
    tail = sum(c for s, c in counts.items() if s <= round(2 * w))
    return w_minus, min(1.0, 2.0 * tail / total)


def transcript_is_clean(session_id: str, project_path: str) -> bool | None:
    """True when the off-arm transcript shows no ltm *activity*; None if not found.

    Activity means the plugin's MCP schemas/tool calls or hook scripts appear in
    conversation lines. Attachment lines (skill listings, file snapshots) are
    ignored — answer content about this repo legitimately mentions ltm paths, and
    a substring scan over those produced 24/24 false positives on the first run.
    """
    slug = project_path.replace("/", "-").replace(".", "-").replace("_", "-")
    path = Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(marker in line for marker in LTM_MARKERS):
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if record.get("type") != "attachment":
                    return False
    except OSError:
        return None
    return True


def run_claude(prompt: str, extra_args: list[str], cwd: str, timeout: int) -> dict | None:
    cmd = ["claude", "-p", prompt, "--output-format", "json", *extra_args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="paired A/B: tokens per task, plugin on vs off")
    parser.add_argument("--tasks", help="comma-separated task ids (default: all)")
    parser.add_argument("--cwd", default=str(HERE.parent.parent.parent), help="repo root to run tasks in")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per claude run")
    parser.add_argument("--dry-run", action="store_true", help="print commands, spend nothing")
    args = parser.parse_args()

    data = json.loads(TASKS.read_text(encoding="utf-8"))
    tasks = data["tasks"]
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in tasks if t["id"] in wanted]
    arms = {"on": [], "off": ["--settings", str(OFF_SETTINGS)]}

    if args.dry_run:
        for task in tasks:
            for arm, extra in arms.items():
                print(f"[{task['id']}/{arm}] claude -p {task['prompt']!r} --output-format json {' '.join(extra)}")
        print(f"\n{len(tasks)} tasks x {len(arms)} arms = {len(tasks) * len(arms)} sessions (REAL tokens)")
        return 0

    pairs, incorrect, contaminated = [], [], []
    for task in tasks:
        row: dict = {"id": task["id"]}
        ok = True
        for arm, extra in arms.items():
            res = run_claude(task["prompt"], extra, args.cwd, args.timeout)
            if res is None:
                print(f"[{task['id']}/{arm}] FAILED to run — pair dropped", file=sys.stderr)
                ok = False
                break
            usage = res.get("usage") or {}
            row[arm] = {
                "tokens": composite_tokens(usage),
                "usage": usage,
                "correct": bool(re.search(task["answer_check"], str(res.get("result") or ""), re.IGNORECASE)),
                "session_id": res.get("session_id") or "",
            }
            print(f"[{task['id']}/{arm}] composite={row[arm]['tokens']:.0f} correct={row[arm]['correct']}")
        if not ok:
            continue
        clean = transcript_is_clean(row["off"]["session_id"], args.cwd)
        if clean is False:
            contaminated.append(row)
            continue
        if not (row["on"]["correct"] and row["off"]["correct"]):
            incorrect.append(row)
            continue
        pairs.append(row)

    print(f"\nvalid pairs : {len(pairs)}  (incorrect: {len(incorrect)}, contaminated off-arm: {len(contaminated)})")
    for row in incorrect:
        print(f"  answer-check failed: {row['id']} (on={row['on']['correct']}, off={row['off']['correct']})")
    if not pairs:
        return 1
    diffs = [row["off"]["tokens"] - row["on"]["tokens"] for row in pairs]  # >0 → plugin saves
    mean_on = sum(r["on"]["tokens"] for r in pairs) / len(pairs)
    mean_off = sum(r["off"]["tokens"] for r in pairs) / len(pairs)
    w_minus, p = wilcoxon_exact(diffs)
    print(f"mean composite tokens/task : on={mean_on:,.0f}  off={mean_off:,.0f}")
    print(f"mean saving                : {mean_off - mean_on:,.0f} tokens/task ({(mean_off - mean_on) / mean_off:.1%})")
    print(f"Wilcoxon signed-rank       : W-={w_minus:.1f}, exact two-sided p={p:.4f} (n={len(diffs)})")
    out = HERE / "ab_results.json"
    out.write_text(json.dumps({"pairs": pairs, "incorrect": incorrect, "p": p}, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
