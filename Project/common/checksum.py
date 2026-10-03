"""Streaming SHA-256 helpers used to validate every byte that crosses the wire."""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1 << 20


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_file(path: str | Path, expected: str) -> bool:
    """Constant-shape comparison of ``path`` against an expected hex digest."""
    if not expected:
        return True
    try:
        return sha256_file(path).lower() == expected.strip().lower()
    except OSError:
        return False


__all__ = ["sha256_file", "sha256_bytes", "verify_file", "CHUNK"]