"""Per-connection transfer state and the on-disk job staging area.

Layout under ``server/data``::

    jobs/<job_id>/meta.json      job metadata as submitted
    jobs/<job_id>/input/<name>   bytes received from the client
    jobs/<job_id>/output/<name>  bytes produced by the engine

Uploads land in ``*.part`` and are renamed only after the SHA-256 matches, so a
truncated transfer can never be mistaken for a complete asset. The same rule
applies to downloads on the client side.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from common.checksum import sha256_file, verify_file
from common.messages import JobSpec, safe_filename
from common.paths import SERVER_DATA

DATA_ROOT = SERVER_DATA / "jobs"

#: Client-supplied identifiers must look like ``uuid4().hex`` before they are
#: allowed anywhere near a path.
_HEX_ID = re.compile(r"[0-9a-f]{1,64}")


@dataclass(slots=True)
class StagedUpload:
    asset_id: str
    name: str
    path: Path
    size: int
    sha256: str
    received_bytes: int = 0

    @property
    def complete(self) -> bool:
        return self.path.exists() and self.path.stat().st_size == self.size


def new_job_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def job_dir(job_id: str) -> Path:
    return DATA_ROOT / job_id


def ensure_dirs() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)


def allocate_upload(asset_name: str, asset_id: str | None = None) -> tuple[Path, StagedUpload]:
    """Reserve a staging path and an upload record for ``asset_name``.

    ``asset_id`` is the caller's identifier, kept when it is a plain lowercase
    hex token so the client can correlate its own request with our reply. Anything
    else is replaced by a fresh uuid, because the value ends up in a directory
    name and must never carry a path separator or a traversal sequence.
    """
    ensure_dirs()
    if not asset_id or len(asset_id) > 64 or not _HEX_ID.fullmatch(asset_id):
        asset_id = uuid.uuid4().hex
    name = safe_filename(asset_name, "asset.bin")
    holder = DATA_ROOT / f"upload-{asset_id}"
    holder.mkdir(parents=True, exist_ok=True)
    part = holder / f"{name}.part"
    part.touch()
    return part, StagedUpload(asset_id=asset_id, name=name, path=part, size=0, sha256="")


def finalize_upload(upload: StagedUpload, declared_sha: str) -> None:
    """Verify then promote ``*.part`` to its final name.

    Raises :class:`ValueError` on a digest or size mismatch; the caller is
    responsible for discarding the staging directory.
    """
    actual_size = upload.path.stat().st_size
    if actual_size != upload.size:
        raise ValueError(
            f"incomplete transfer: received {actual_size} of {upload.size} bytes"
        )
    actual_sha = sha256_file(upload.path)
    if declared_sha and actual_sha.lower() != declared_sha.lower():
        raise ValueError(
            f"checksum mismatch: client declared {declared_sha[:16]}..., "
            f"server computed {actual_sha[:16]}..."
        )
    upload.sha256 = actual_sha
    final = upload.path.with_suffix("")
    if final.exists():
        final.unlink()
    upload.path.rename(final)
    upload.path = final


def commit_upload_to_job(upload: StagedUpload, job_id: str) -> Path:
    destination = job_dir(job_id) / "input"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / upload.path.name
    shutil.move(str(upload.path), str(target))
    upload.path = target
    parent = upload.path.parent
    if parent.name.startswith("upload-") and not any(parent.iterdir()):
        shutil.rmtree(parent, ignore_errors=True)
    return target


def discard_upload(upload: StagedUpload | None) -> None:
    if upload is None:
        return
    parent = upload.path.parent
    if parent.name.startswith("upload-"):
        shutil.rmtree(parent, ignore_errors=True)


def output_path(job_id: str, name: str) -> Path:
    directory = job_dir(job_id) / "output"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / safe_filename(name, "output.mp4")


def write_meta(job_id: str, spec: JobSpec, extra: dict | None = None) -> None:
    payload = spec.to_dict()
    payload["job_id"] = job_id
    payload["created_at"] = time.time()
    if extra:
        payload.update(extra)
    path = job_dir(job_id) / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def cleanup_job(job_id: str, keep_output: bool = False) -> None:
    directory = job_dir(job_id)
    if keep_output and directory.exists():
        shutil.rmtree(directory / "input", ignore_errors=True)
        shutil.rmtree(directory / "meta.json", ignore_errors=True)
        return
    shutil.rmtree(directory, ignore_errors=True)


def prune(keep_jobs: int = 25) -> int:
    """Delete the oldest job directories beyond ``keep_jobs``. Returns count removed."""
    if not DATA_ROOT.exists():
        return 0
    entries = sorted(
        (p for p in DATA_ROOT.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    removed = 0
    for directory in entries[: max(0, len(entries) - keep_jobs)]:
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
    return removed


def verify_output(path: Path, expected_sha: str) -> bool:
    return verify_file(path, expected_sha)


__all__ = [
    "DATA_ROOT", "StagedUpload", "new_job_id", "job_dir", "ensure_dirs",
    "allocate_upload", "finalize_upload", "commit_upload_to_job",
    "discard_upload", "output_path", "write_meta", "cleanup_job", "prune",
    "verify_output", "sha256_file",
]