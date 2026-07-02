"""Extract Python symbols (functions, classes, methods) from source via stdlib ``ast``.

Phase 2 of the index is deliberately Python-only: the portability analysis found
tree-sitter (jcodemunch's multi-language parser) too heavy for a zero-dependency
plugin, and stdlib ``ast`` covers Python precisely for free. Each symbol becomes a
chunk whose body is its full source span (so ``get_symbol`` returns the real
implementation and FTS can match any identifier inside it), while the searchable
summary is the signature + docstring first line — the token-cheap outline view.

Only top-level functions/classes and one level of class members are walked; nested
function bodies are skipped as recall noise. Byte spans are line-based (from the
first decorator to the node's end line), which sidesteps the char-vs-byte ambiguity
of ``col_offset`` while still reproducing the symbol exactly for freshness hashing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str  # function | async_function | method | async_method | class
    signature: str
    docstring: str
    level: int
    byte_start: int
    byte_end: int
    body: str


def _line_byte_offsets(text: str) -> list[int]:
    """Byte offset of the start of each line; ``offs[n]`` is the start of 1-based line n+1."""
    offs = [0]
    for line in text.splitlines(keepends=True):
        offs.append(offs[-1] + len(line.encode("utf-8")))
    return offs


def _signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        sig = f"{prefix} {node.name}({ast.unparse(node.args)})"
        if node.returns is not None:
            sig += f" -> {ast.unparse(node.returns)}"
        return sig + ":"
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases] + [f"{k.arg}={ast.unparse(k.value)}" for k in node.keywords]
        return f"class {node.name}" + (f"({', '.join(bases)})" if bases else "") + ":"
    return ""


def _kind(node: ast.AST, in_class: bool) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    is_async = isinstance(node, ast.AsyncFunctionDef)
    if in_class:
        return "async_method" if is_async else "method"
    return "async_function" if is_async else "function"


def _start_line(node: ast.AST) -> int:
    """First source line of a symbol, counting any decorators above the def/class line."""
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(d.lineno for d in decorators)])


def extract_symbols(text: str, module_stem: str) -> list[Symbol]:
    """Top-level functions/classes plus one level of class members, in source order."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    offs = _line_byte_offsets(text)
    content_bytes = text.encode("utf-8")
    symbols: list[Symbol] = []

    def emit(node: ast.AST, qual_prefix: str, level: int, in_class: bool) -> None:
        name = getattr(node, "name", "")
        qualname = f"{qual_prefix}.{name}" if qual_prefix else name
        start = _start_line(node)
        end = getattr(node, "end_lineno", start)
        byte_start = offs[start - 1] if start - 1 < len(offs) else 0
        byte_end = offs[end] if end < len(offs) else len(content_bytes)
        doc = ast.get_docstring(node) or ""
        symbols.append(
            Symbol(
                name=name,
                qualname=qualname,
                kind=_kind(node, in_class),
                signature=_signature(node),
                docstring=doc.strip().split("\n")[0] if doc else "",
                level=level,
                byte_start=byte_start,
                byte_end=byte_end,
                body=content_bytes[byte_start:byte_end].decode("utf-8", "ignore").rstrip(),
            )
        )
        if isinstance(node, ast.ClassDef):  # descend one level for members
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    emit(child, qualname, level + 1, in_class=True)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            emit(node, "", 0, in_class=False)
    return symbols
