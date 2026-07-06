#!/usr/bin/env python3
"""Build a claude-engram design paper (Markdown) into an academic PDF via pandoc + tectonic.

The Markdown is always the source of truth; the PDF is a build artefact. This script
never edits the source.

Usage
-----
    build_pdf.py <paper.md>                 # build <paper>.pdf beside the source
    build_pdf.py <paper.md> -o out.pdf      # build to an explicit path
    build_pdf.py <paper.md> --publish       # datetime-stamped archive + canonical root copy

Toolchain
---------
Requires `pandoc` and `tectonic` on PATH (`brew install pandoc tectonic`). If either is
missing the script prints install guidance and exits non-zero without touching anything.
tectonic fetches LaTeX packages on first run, so the first build needs network access.

Citations
---------
Citeproc is conditional: if a `references.bib` sits beside the source, the build passes
`--citeproc --bibliography references.bib` for numbered, CSL-styled references. Without
it, a plain Markdown `## References` list builds unchanged.

Publish flow (--publish)
------------------------
1. Build the PDF.
2. Write a datetime-stamped copy pair into `<repo>/docs/whitepaper/`:
   `<YYYY-MM-DD-HHMM>-<slug>.md` and `<YYYY-MM-DD-HHMM>-<slug>.pdf`.
3. Copy the fresh PDF to the repo root as `<slug>.pdf` (the canonical published version).
The slug is the source filename stem. The repo root is found by walking up to `.git`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PANDOC_DIR = SCRIPT_DIR.parent / "assets" / "pandoc"
DEFAULTS = PANDOC_DIR / "paper.yaml"
HEADER = PANDOC_DIR / "header.tex"


def _err(msg: str) -> None:
    print(f"build_pdf: {msg}", file=sys.stderr)


def check_toolchain() -> list[str]:
    """Return the list of missing required tools (empty when all present)."""
    return [tool for tool in ("pandoc", "tectonic") if shutil.which(tool) is None]


def find_repo_root(start: Path) -> Path | None:
    """Walk up from *start* to the nearest directory containing a `.git` marker."""
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def build(source: Path, output: Path) -> None:
    """Run pandoc → tectonic to produce *output* from *source*. Raises on failure."""
    cmd = [
        "pandoc",
        str(source),
        "--defaults",
        str(DEFAULTS),
        "--include-in-header",
        str(HEADER),
        "--resource-path",
        str(source.parent),
        "-o",
        str(output),
    ]
    bib = source.parent / "references.bib"
    if bib.exists():
        cmd += ["--citeproc", "--bibliography", str(bib)]
        print(f"  citations: {bib.name} (citeproc)")
    else:
        print("  citations: plain Markdown reference list (no references.bib)")
    print(f"  {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def publish(source: Path, built_pdf: Path) -> None:
    """Archive a datetime-stamped copy pair and promote the PDF to the repo root."""
    repo_root = find_repo_root(source.resolve())
    if repo_root is None:
        raise RuntimeError("could not locate repo root (no .git marker found above source)")

    slug = source.stem
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    archive_dir = repo_root / "docs" / "whitepaper"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archive_md = archive_dir / f"{stamp}-{slug}.md"
    archive_pdf = archive_dir / f"{stamp}-{slug}.pdf"
    shutil.copy2(source, archive_md)
    shutil.copy2(built_pdf, archive_pdf)

    canonical = repo_root / f"{slug}.pdf"
    shutil.copy2(built_pdf, canonical)

    print(f"  archived: {archive_md.relative_to(repo_root)}")
    print(f"  archived: {archive_pdf.relative_to(repo_root)}")
    print(f"  canonical: {canonical.relative_to(repo_root)} (repo root)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a claude-engram design paper into a PDF.")
    parser.add_argument("source", type=Path, help="the paper Markdown file")
    parser.add_argument("-o", "--output", type=Path, help="explicit PDF output path")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="datetime-stamp into docs/whitepaper/ and copy the canonical PDF to the repo root",
    )
    args = parser.parse_args(argv)

    source: Path = args.source
    if not source.is_file():
        _err(f"source not found: {source}")
        return 1
    if source.suffix.lower() not in (".md", ".markdown"):
        _err(f"source is not Markdown: {source}")
        return 1

    missing = check_toolchain()
    if missing:
        _err(f"missing required tool(s): {', '.join(missing)}")
        _err("install with:  brew install pandoc tectonic")
        _err("the Markdown source was not touched.")
        return 1

    for asset in (DEFAULTS, HEADER):
        if not asset.exists():
            _err(f"missing bundled asset: {asset}")
            return 1

    output: Path = args.output or source.with_suffix(".pdf")
    print(f"building: {source} -> {output}")
    try:
        build(source, output)
    except subprocess.CalledProcessError as exc:
        _err(f"pandoc failed (exit {exc.returncode}). The source was not modified.")
        return exc.returncode or 1
    except OSError as exc:
        _err(f"build error: {exc}")
        return 1

    if args.publish:
        try:
            publish(source, output)
        except (OSError, RuntimeError) as exc:
            _err(f"publish step failed: {exc}")
            _err(f"the PDF was still built at {output}.")
            return 1

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
