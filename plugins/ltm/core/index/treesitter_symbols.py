"""Multi-language symbol extraction via tree-sitter (Python, TypeScript, JS, JSX).

A declarative per-grammar ``Spec`` (node-type → symbol kind, which nodes are
containers to descend, which are transparent wrappers)
drives one generic walker, so adding a language is data, not code. tree-sitter is an
optional dependency provisioned into the managed venv (see requirements.txt); when it
is absent, ``extract_symbols`` returns None and the caller falls back to stdlib ``ast``
for Python and skips other languages. Pinned to the 0.x language-pack line — the 1.x
wrapper ships an incompatible ``parse()`` binding.

Byte spans come straight from tree-sitter (``start_byte`` / ``end_byte``), so a symbol
reproduces exactly for content-hash freshness. The searchable body is the full symbol
source; the summary is its signature (declaration up to the body).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.index.code_symbols import Symbol

_SIG_MAX = 240

_EXT_TO_GRAMMAR = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


@dataclass(frozen=True)
class _Spec:
    symbols: dict[str, str]  # node type -> symbol kind
    containers: frozenset[str]  # symbol nodes whose body holds members (descend into)
    transparent: frozenset[str]  # wrappers to descend without nesting (export/decorated)
    decorator_wrapper: str = ""  # wrapper whose span should absorb the inner symbol
    arrow_consts: bool = False  # emit `const x = () => …` as a function
    docstring: str = ""  # "python" = first string in body; else none


_TS_SYMBOLS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_TS_SPEC = _Spec(
    symbols=_TS_SYMBOLS,
    containers=frozenset({"class_declaration"}),
    transparent=frozenset({"export_statement"}),
    arrow_consts=True,
)

_SPECS = {
    "python": _Spec(
        symbols={"function_definition": "function", "class_definition": "class"},
        containers=frozenset({"class_definition"}),
        transparent=frozenset(),
        decorator_wrapper="decorated_definition",
        docstring="python",
    ),
    "javascript": _Spec(
        symbols={
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
        },
        containers=frozenset({"class_declaration"}),
        transparent=frozenset({"export_statement"}),
        arrow_consts=True,
    ),
    "typescript": _TS_SPEC,
    "tsx": _TS_SPEC,
}

_ARROW_VALUES = {"arrow_function", "function", "function_expression"}


def supported(ext: str) -> bool:
    return ext.lower() in _EXT_TO_GRAMMAR


def extract_symbols(text: str, ext: str) -> list[Symbol] | None:
    """Symbols for a supported extension, or None if tree-sitter/grammar is unavailable."""
    grammar = _EXT_TO_GRAMMAR.get(ext.lower())
    if grammar is None:
        return None
    try:
        from tree_sitter_language_pack import get_parser

        tree = get_parser(grammar).parse(text.encode("utf-8"))
    except Exception:
        return None
    src = text.encode("utf-8")
    out: list[Symbol] = []
    _walk(tree.root_node, src, _SPECS[grammar], out, prefix="", level=0)
    return out


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "ignore")


def _name(node, src: bytes) -> str:
    field_node = node.child_by_field_name("name")
    if field_node is not None:
        return _text(field_node, src)
    for child in node.children:  # fall back to the first identifier-ish child
        if "identifier" in child.type:
            return _text(child, src)
    return ""


def _signature(node, src: bytes, start: int) -> str:
    body = node.child_by_field_name("body")
    sig_end = body.start_byte if body is not None else node.end_byte
    sig = src[start:sig_end].decode("utf-8", "ignore").strip().rstrip("{").strip()
    return " ".join(sig.split())[:_SIG_MAX]


def _python_docstring(node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    if body is None or not body.named_children:
        return ""
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:
        first = first.named_children[0]
    if first.type != "string":
        return ""
    content = next((c for c in first.named_children if c.type == "string_content"), None)
    raw = _text(content, src) if content is not None else _text(first, src).strip("\"'")
    return raw.strip().split("\n")[0].strip()


def _emit(node, src, spec, out, prefix, level, start_override=None) -> None:
    name = _name(node, src)
    if not name:
        return
    qual = f"{prefix}.{name}" if prefix else name
    start = node.start_byte if start_override is None else start_override
    doc = _python_docstring(node, src) if spec.docstring == "python" else ""
    out.append(
        Symbol(
            name=name,
            qualname=qual,
            kind=spec.symbols[node.type],
            signature=_signature(node, src, start),
            docstring=doc,
            level=level,
            byte_start=start,
            byte_end=node.end_byte,
            body=src[start : node.end_byte].decode("utf-8", "ignore").rstrip(),
        )
    )
    if node.type in spec.containers:
        body = node.child_by_field_name("body") or node
        _walk(body, src, spec, out, qual, level + 1)


def _emit_arrows(decl, src, spec, out, prefix, level) -> None:
    """Emit ``const name = (…) => …`` / ``= function(…)`` as a function symbol."""
    for d in decl.children:
        if d.type != "variable_declarator":
            continue
        value = d.child_by_field_name("value")
        name_node = d.child_by_field_name("name")
        if value is None or name_node is None or value.type not in _ARROW_VALUES:
            continue
        name = _text(name_node, src)
        qual = f"{prefix}.{name}" if prefix else name
        params = value.child_by_field_name("parameters")
        sig = f"const {name}{_text(params, src) if params else '()'}"
        out.append(
            Symbol(
                name=name,
                qualname=qual,
                kind="function",
                signature=" ".join(sig.split())[:_SIG_MAX],
                docstring="",
                level=level,
                byte_start=decl.start_byte,
                byte_end=decl.end_byte,
                body=_text(decl, src).rstrip(),
            )
        )


def _walk(node, src, spec, out, prefix, level) -> None:
    for child in node.children:
        ctype = child.type
        if ctype == spec.decorator_wrapper and spec.decorator_wrapper:
            inner = next((c for c in child.children if c.type in spec.symbols), None)
            if inner is not None:
                _emit(inner, src, spec, out, prefix, level, start_override=child.start_byte)
            continue
        if ctype in spec.transparent:
            _walk(child, src, spec, out, prefix, level)
        elif ctype in spec.symbols:
            _emit(child, src, spec, out, prefix, level)
        elif spec.arrow_consts and ctype in ("lexical_declaration", "variable_declaration"):
            _emit_arrows(child, src, spec, out, prefix, level)
        else:
            _walk(child, src, spec, out, prefix, level)
