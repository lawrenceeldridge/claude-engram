"""Split a markdown document into heading-delimited sections (pure, stdlib-only).

A section is exactly the span from one heading to the next same-or-higher heading —
never a fixed token window. Content before the first heading becomes a level-0 root
section titled after the file. This mirrors the jdocmunch splitter: a hand-written
CommonMark-aware line scanner, not a library, because the non-obvious value is
*suppressing* heading detection inside fenced code, HTML blocks and frontmatter — a
naive ``re.split(r'^#')`` mis-splits every code sample that contains a ``# comment``.

The scanner tracks a byte cursor per line so each section carries ``byte_start`` /
``byte_end`` offsets into the original UTF-8 bytes; slicing ``content_bytes[start:end]``
reproduces the section body exactly, which is what the content-hash freshness check
relies on. Sections are wired into a hierarchy by a heading-level stack, and each
gets a stable hierarchical slug (``installation/prerequisites``) usable as a
human-readable recall anchor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})")
_SETEXT_UNDERLINE = re.compile(r"^(=+|-+)\s*$")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass
class Section:
    """One heading-delimited span. Body is derived from byte offsets, not stored twice."""

    level: int
    title: str
    slug: str  # hierarchical: "installation/prerequisites"
    heading_path: str  # readable breadcrumb: "Installation › Prerequisites"
    byte_start: int
    byte_end: int
    body: str = ""
    parent_slug: str = ""
    children: list[str] = field(default_factory=list)


def make_slug(title: str) -> str:
    """Lowercase, non-alphanumeric-collapsed slug fragment for one heading."""
    return _SLUG_STRIP.sub("-", title.strip().lower()).strip("-") or "section"


def _fence_token(line: str) -> str | None:
    """The bare fence marker (``` or ~~~) if this line opens/closes a fence, else None."""
    match = _FENCE.match(line)
    return match.group(2)[0] * 3 if match else None


def _frontmatter_end(lines: list[str]) -> int:
    """Index just past a leading YAML (``---``) or TOML (``+++``) frontmatter block, else 0."""
    if not lines:
        return 0
    fence = {"---": "---", "+++": "+++"}.get(lines[0].strip())
    if fence is None:
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == fence:
            return i + 1
    return 0  # unterminated — treat as ordinary content, not frontmatter


def _headings(text: str) -> tuple[list[tuple[int, int, str, int]], int]:
    """Locate every heading as (line_index, level, title, byte_start_of_line).

    Returns the heading list plus the byte offset where real content begins (after
    any frontmatter). Headings inside fenced code blocks are ignored; setext
    underlines (``===`` / ``---``) promote the preceding non-blank paragraph line.
    """
    lines = text.split("\n")
    fm_end = _frontmatter_end(lines)

    # Byte offset of the start of each line (+1 per '\n' the split removed).
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line.encode("utf-8")) + 1

    fm_byte = offsets[fm_end] if fm_end < len(offsets) else len(text.encode("utf-8"))

    headings: list[tuple[int, int, str, int]] = []
    fence: str | None = None
    prev_text_idx: int | None = None
    for i in range(fm_end, len(lines)):
        line = lines[i]
        token = _fence_token(line)
        if fence is not None:
            if token == fence:
                fence = None
            prev_text_idx = None
            continue
        if token is not None:
            fence = token
            prev_text_idx = None
            continue

        atx = _ATX.match(line)
        if atx:
            headings.append((i, len(atx.group(1)), atx.group(2).strip(), offsets[i]))
            prev_text_idx = None
            continue

        # Setext: an === / --- underline promotes the previous non-blank line.
        if prev_text_idx is not None and _SETEXT_UNDERLINE.match(line):
            level = 1 if line.strip().startswith("=") else 2
            title = lines[prev_text_idx].strip()
            headings.append((prev_text_idx, level, title, offsets[prev_text_idx]))
            prev_text_idx = None
            continue

        prev_text_idx = i if line.strip() else None

    return headings, fm_byte


def split_markdown(text: str, doc_stem: str) -> list[Section]:
    """Split markdown into hierarchical sections. ``doc_stem`` titles the root section."""
    content_bytes = text.encode("utf-8")
    total = len(content_bytes)
    headings, fm_byte = _headings(text)

    sections: list[Section] = []

    # Pre-heading content (or a headingless file) becomes a level-0 root.
    first_heading_byte = headings[0][3] if headings else total
    root_body = content_bytes[fm_byte:first_heading_byte].decode("utf-8", "ignore").strip()
    if root_body or not headings:
        sections.append(
            Section(
                level=0,
                title=doc_stem,
                slug=make_slug(doc_stem),
                heading_path=doc_stem,
                byte_start=fm_byte,
                byte_end=first_heading_byte,
                body=root_body,
            )
        )

    # Each heading owns bytes up to the next heading of the same-or-higher level.
    slug_stack: list[tuple[int, str, str]] = []  # (level, slug_fragment, title)
    seen: dict[str, int] = {}
    for n, (_line_idx, level, title, byte_start) in enumerate(headings):
        byte_end = headings[n + 1][3] if n + 1 < len(headings) else total

        while slug_stack and slug_stack[-1][0] >= level:
            slug_stack.pop()
        parent_slug = "/".join(s[1] for s in slug_stack)
        parent_path = " › ".join(s[2] for s in slug_stack)

        fragment = make_slug(title)
        hier = f"{parent_slug}/{fragment}" if parent_slug else fragment
        if hier in seen:  # sibling collision — disambiguate deterministically
            seen[hier] += 1
            hier = f"{hier}-{seen[hier]}"
        else:
            seen[hier] = 1

        heading_path = f"{parent_path} › {title}" if parent_path else title
        body = content_bytes[byte_start:byte_end].decode("utf-8", "ignore")
        sections.append(
            Section(
                level=level,
                title=title,
                slug=hier,
                heading_path=heading_path,
                byte_start=byte_start,
                byte_end=byte_end,
                body=body.strip(),
                parent_slug=parent_slug,
            )
        )
        slug_stack.append((level, fragment, title))

    _wire_children(sections)
    return sections


def _wire_children(sections: list[Section]) -> None:
    by_slug = {s.slug: s for s in sections}
    for s in sections:
        if s.parent_slug and s.parent_slug in by_slug:
            by_slug[s.parent_slug].children.append(s.slug)
