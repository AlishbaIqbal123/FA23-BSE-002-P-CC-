"""Checksum helpers and the staging area's two-phase promote."""

from __future__ import annotations

import hashlib

import pytest

from common.checksum import sha256_bytes, sha256_file, verify_file
from common.messages import JobSpec
from server import storage


def test_sha256_matches_hashlib(tmp_path):
    target = tmp_path / "payload.bin"
    data = bytes(range(256)) * 900
    target.write_bytes(data)
    assert sha256_file(target) == hashlib.sha256(data).hexdigest()


def test_sha256_bytes_matches_hashlib():
    data = b"remote task offloading"
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_verify_file_accepts_the_right_digest(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"hello")
    assert verify_file(target, sha256_file(target))
    assert verify_file(target, sha256_file(target).upper())


def test_verify_file_rejects_a_wrong_digest(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"hello")
    assert not verify_file(target, "0" * 64)


def test_verify_file_with_no_expected_digest_is_a_no_op(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"hello")
    assert verify_file(target, "")


def test_verify_file_on_a_missing_path_is_false(tmp_path):
    assert not verify_file(tmp_path / "nope.bin", "0" * 64)


def test_large_file_is_hashed_in_chunks(tmp_path):
    target = tmp_path / "big.bin"
    with open(target, "wb") as handle:
        for _ in range(8):
            handle.write(b"A" * (1 << 20))
    from common.checksum import CHUNK

    assert sha256_file(target) == hashlib.sha256(b"A" * (8 << 20)).hexdigest()
    assert CHUNK == 1 << 20


class TestStaging:
    def test_upload_lands_on_a_part_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        part, record = storage.allocate_upload("clip.mp4")
        part.write_bytes(b"payload")
        assert part.name.endswith(".part")
        assert record.size == 0

    def test_finalize_verifies_then_renames(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        part, record = storage.allocate_upload("clip.mp4")
        data = b"x" * 1000
        part.write_bytes(data)
        record.size = len(data)
        digest = hashlib.sha256(data).hexdigest()
        storage.finalize_upload(record, digest)
        assert record.path.name == "clip.mp4"
        assert not record.path.name.endswith(".part")
        assert record.sha256 == digest

    def test_finalize_rejects_a_truncated_transfer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        part, record = storage.allocate_upload("clip.mp4")
        part.write_bytes(b"x" * 500)
        record.size = 1000
        with pytest.raises(ValueError, match="incomplete transfer"):
            storage.finalize_upload(record, "")

    def test_finalize_rejects_a_checksum_mismatch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        part, record = storage.allocate_upload("clip.mp4")
        data = b"corrupted in transit"
        part.write_bytes(data)
        record.size = len(data)
        with pytest.raises(ValueError, match="checksum mismatch"):
            storage.finalize_upload(record, "a" * 64)

    def test_discard_removes_the_whole_staging_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        part, record = storage.allocate_upload("clip.mp4")
        part.write_bytes(b"junk")
        parent = part.parent
        storage.discard_upload(record)
        assert not parent.exists()

    def test_commit_moves_the_asset_into_the_job_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        part, record = storage.allocate_upload("clip.mp4")
        part.write_bytes(b"payload")
        record.size = 7
        storage.finalize_upload(record, hashlib.sha256(b"payload").hexdigest())
        committed = storage.commit_upload_to_job(record, "job-1")
        assert committed.parent.name == "input"
        assert committed.read_bytes() == b"payload"

    def test_output_path_sanitises_the_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        path = storage.output_path("job-1", "../../escape.mp4")
        assert path.name == "escape.mp4"
        assert path.parent.name == "output"

    def test_job_ids_are_unique(self):
        assert len({storage.new_job_id() for _ in range(500)}) == 500

    def test_prune_keeps_the_newest(self, tmp_path, monkeypatch):
        import os
        import time

        root = tmp_path / "jobs"
        monkeypatch.setattr(storage, "DATA_ROOT", root)
        for index in range(6):
            directory = root / f"job-{index}"
            directory.mkdir(parents=True)
            os.utime(directory, (time.time() + index, time.time() + index))
        removed = storage.prune(keep_jobs=2)
        assert removed == 4
        remaining = sorted(p.name for p in root.iterdir())
        assert remaining == ["job-4", "job-5"]

    def test_cleanup_can_keep_the_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        (storage.job_dir("j1") / "input").mkdir(parents=True)
        (storage.job_dir("j1") / "input" / "in.mp4").write_bytes(b"a")
        (storage.job_dir("j1") / "output").mkdir(parents=True)
        (storage.job_dir("j1") / "output" / "out.mp4").write_bytes(b"b")
        storage.write_meta("j1", JobSpec())
        storage.cleanup_job("j1", keep_output=True)
        assert (storage.job_dir("j1") / "output" / "out.mp4").exists()
        assert not (storage.job_dir("j1") / "input").exists()

    def test_cleanup_without_keep_removes_everything(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DATA_ROOT", tmp_path / "jobs")
        (storage.job_dir("j2") / "output").mkdir(parents=True)
        storage.cleanup_job("j2")
        assert not storage.job_dir("j2").exists()