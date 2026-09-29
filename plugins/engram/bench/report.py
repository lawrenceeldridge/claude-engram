"""Plain-text table output shared by the bench scripts."""

from __future__ import annotations


def fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 100 else f"{value:.1f}"
    return str(value)


def print_rows(rows: list[dict], cols: list[str]) -> None:
    """Left-aligned columns sized to the widest cell, header + rule + one line per row."""
    if not rows:
        print("no rows")
        return
    widths = {c: max(len(c), *(len(fmt(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(fmt(r[c]).ljust(widths[c]) for c in cols))
