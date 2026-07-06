# PDF Build Reference

The Markdown paper is always the source of truth. The PDF is a build artefact produced
by [`../scripts/build_pdf.py`](../scripts/build_pdf.py) via **pandoc + tectonic**. This
reference covers the toolchain, the two build modes, citations, and the publish flow.

---

## Toolchain: pandoc + tectonic

- **pandoc** converts Markdown to LaTeX.
- **tectonic** is a self-contained LaTeX engine that auto-fetches the packages a document
  needs — no full TeX Live install, no manual package management.

Install once (macOS):

```bash
brew install pandoc tectonic
```

The **first** build downloads LaTeX packages, so it needs network access; later builds
are offline and fast. If either tool is missing, `build_pdf.py` prints this install line
and exits without touching the source.

Why this engine (over the alternatives): it gives true academic LaTeX typography (the
`bitcoin.pdf` register) with the least setup — tectonic removes the "install a 4 GB TeX
distribution" barrier, and pandoc is already the lingua franca for Markdown conversion.
A pure-Python HTML-to-PDF path (WeasyPrint) was the runner-up; it needs no system install
but produces CSS-styled output rather than LaTeX typesetting.

---

## Build modes

From anywhere in the repo:

```bash
# Build <paper>.pdf beside the source — for iterating on a draft
python3 .claude/skills/ltm-design/scripts/build_pdf.py docs/generated/design-drafts/<slug>/<slug>.md

# Explicit output path
python3 .claude/skills/ltm-design/scripts/build_pdf.py <paper>.md -o /tmp/preview.pdf

# Publish — archive + canonical root copy (see below)
python3 .claude/skills/ltm-design/scripts/build_pdf.py docs/whitepaper-src/<slug>.md --publish
```

The script resolves its bundled config (`assets/pandoc/paper.yaml`, `assets/pandoc/header.tex`)
relative to itself, so it works from any working directory, and sets pandoc's
`--resource-path` to the paper's own directory so figures referenced relatively resolve.

---

## Styling

- **`assets/pandoc/paper.yaml`** — pandoc *defaults*: `pdf-engine: tectonic`, A4,
  11pt `article`, 1-inch margins, a table of contents, numbered sections, coloured links.
- **`assets/pandoc/header.tex`** — a minimal `header-includes` (booktabs tables, tighter
  lists, long-identifier splitting), deliberately limited to widely-available packages so
  tectonic needs nothing exotic.

The paper's own YAML metadata block (`title`, `author`, `date`, `abstract`) drives the
title block and abstract — see [`../assets/paper_template.md`](../assets/paper_template.md).

To restyle a single paper without editing the shared assets, add LaTeX to that paper's
metadata (`header-includes:`); to change the house style for all papers, edit the two
`assets/pandoc/` files.

---

## Citations (optional `.bib` + citeproc)

Citation handling is **conditional on a `references.bib` beside the source**:

- **With `references.bib`** — the build adds `--citeproc --bibliography references.bib`,
  producing numbered in-text citations and an auto-formatted, consistently-styled
  reference list. Cite in the Markdown with `[@atkinson1968]`. This is the academic path.
- **Without it** — the build omits citeproc and the author's plain Markdown `## References`
  list renders as written. No configuration change needed.

To switch a paper to managed citations: drop a `references.bib` next to it and add
`bibliography: references.bib` to the paper's metadata block (or rely on the script's
auto-detection). A CSL style file can be added per paper via `csl: <style>.csl` in the
metadata if a specific journal style is wanted; the default numbered style needs nothing.

---

## The publish flow (`--publish`)

`--publish` promotes a review-ready draft to a published artefact in three steps:

1. Build the PDF.
2. Write a datetime-stamped copy pair into `docs/whitepaper/`:
   `<YYYY-MM-DD-HHMM>-<slug>.md` and `<YYYY-MM-DD-HHMM>-<slug>.pdf`. Every published and
   transitional version is retained here and sorts chronologically.
3. Copy the fresh PDF to the **repo root** as `<slug>.pdf` — the canonical published
   version, alongside `README.md` and `DESIGN.md`.

The slug is the source filename stem; the repo root is located by walking up to the
`.git` marker. Commit the `docs/whitepaper/` archive pair and the root `<slug>.pdf`
together so the canonical PDF and its dated source always match.

---

## Failure behaviour

- **Missing pandoc/tectonic** → install guidance, exit 1, source untouched.
- **pandoc/tectonic error** (e.g. a LaTeX error in the document) → the error is surfaced,
  exit non-zero, source untouched. Fix the Markdown and rebuild.
- **Publish step fails** (e.g. no `.git` marker) → the PDF is still built beside the
  source; only the archive/promotion is skipped, and the reason is reported.

The invariant: a build never modifies or corrupts the Markdown source.
