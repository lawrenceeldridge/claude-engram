"""The compact "bytes" layer — float embeddings -> int8 / binary.

A fact's semantic fingerprint is its embedding. We never store float32: int8
scalar quantisation is ~4x smaller with negligible recall loss, and binary
(sign bits) is 32x smaller for a fast Hamming pre-filter. int8 is the primary
search representation here; binary is kept for the pre-filter / demonstration.
"""

from __future__ import annotations

import math
from array import array


def quantize_int8(vec: list[float]) -> tuple[bytes, float]:
    """Return (int8 bytes, scale). Dequantise with ``dequantize_int8``."""
    scale = max((abs(x) for x in vec), default=0.0)
    if scale == 0.0:
        return bytes(len(vec)), 1.0
    q = array("b", (max(-127, min(127, round(x / scale * 127))) for x in vec))
    return q.tobytes(), scale


def dequantize_int8(blob: bytes, scale: float) -> list[float]:
    q = array("b")
    q.frombytes(blob)
    return [x / 127.0 * scale for x in q]


def pack_bits(vec: list[float]) -> bytes:
    """Sign-bit binary embedding, MSB-first. 1 bit per dimension."""
    out = bytearray((len(vec) + 7) // 8)
    for i, x in enumerate(vec):
        if x >= 0:
            out[i >> 3] |= 1 << (7 - (i & 7))
    return bytes(out)


def hamming(a: bytes, b: bytes) -> int:
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
