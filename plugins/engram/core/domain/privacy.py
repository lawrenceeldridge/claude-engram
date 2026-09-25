"""Privacy detection and redaction for text engram keeps verbatim (Functional Core — pure).

Distillation naturally drops most sensitive detail; verbatim storage (the episodic exchange
layer) does not, so anything kept word-for-word passes through ``redact`` first. ``privacy_flags``
is the detector the benchmark tooling uses to route mined text to a human gate (bench imports it
from here — one definition). Redaction is deliberately conservative about prose: values are
removed only where they look like credentials (``key=value`` / ``key: value`` assignments,
HTTP ``Bearer`` / ``Basic`` / ``Digest`` credentials, well-known token shapes), so ordinary words like "token budget" survive.
"""

from __future__ import annotations

import re

REDACTED = "«redacted»"

# --- Detection (flags for a human gate) --------------------------------------------------
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
RE_SECRET = re.compile(r"(api[_-]?key|secret|token|password|bearer|aws_|sk-[A-Za-z0-9]{8,})", re.IGNORECASE)
RE_ABS_PATH = re.compile(r"/(?:Users|home)/[\w.-]+/[^\s'\"]*")


def privacy_flags(text: str, repo_path: str) -> list[str]:
    """Reasons ``text`` must not ship unreviewed: email, credential-shaped, non-repo path."""
    flags = []
    if RE_EMAIL.search(text):
        flags.append("email")
    if RE_SECRET.search(text):
        flags.append("credential-shaped")
    for path in RE_ABS_PATH.findall(text):
        if not path.startswith(repo_path):
            flags.append(f"non-repo path: {path[:60]}")
            break
    return flags


# --- Redaction (for verbatim storage) ----------------------------------------------------
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_TOKEN_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}"  # OpenAI/Anthropic-style keys
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"  # Slack tokens
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"  # JWT
)
# An Authorization header's credential is always redacted — the header makes it unambiguous.
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*[:=]\s*[\"']?(?:bearer|basic|digest)\s+)[^\s\"',;]+")
# A scheme word elsewhere redacts only a value that looks like a credential (a digit or base64 punctuation, or 20+ token
# chars), so prose after a scheme word — "basic understanding" — is left alone.
_AUTH_SCHEME = re.compile(
    r"(?i)\b(bearer|basic|digest)\s+"
    r"(?:(?=[A-Za-z0-9._~+/=-]*[0-9=+/])[A-Za-z0-9._~+/=-]{8,}|[A-Za-z0-9._~+/=-]{20,})"
)
_ASSIGNED = re.compile(
    r"(?i)\b((?:api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|token|password|passwd|pwd|auth(?:orization)?)"
    r"\s*[:=]\s*[\"']?)(?!(?:bearer|basic|digest)\b)([^\s\"',;]{4,})"
)


def redact(text: str, repo_path: str) -> str:
    """``text`` with credentials, emails and non-project absolute paths replaced by ``REDACTED``."""
    out = _PRIVATE_KEY.sub(REDACTED, text)
    out = _TOKEN_SHAPES.sub(REDACTED, out)
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _ASSIGNED.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = RE_EMAIL.sub(REDACTED, out)
    return RE_ABS_PATH.sub(lambda m: m.group(0) if repo_path and m.group(0).startswith(repo_path) else REDACTED, out)
