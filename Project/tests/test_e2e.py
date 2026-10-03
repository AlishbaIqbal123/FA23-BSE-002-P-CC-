"""End-to-end: a real daemon, a real client, a real encode over a real socket.

These tests are skipped when no usable ffmpeg is present, so the suite still
passes on a machine that only wants to check the protocol. When ffmpeg *is*
present they exercise the whole pipeline: handshake, latency probe, chunked
upload with SHA-256 verification, job submission, asynchronous progress
streaming, output download with SHA-256 verification, and the error paths.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from common.checksum import sha256_file
from common.messages import JobSpec, PROTOCOL_VERSION
from common.paths import CREATE_NO_WINDOW
from common.protocol import (
    FrameType,
    ProtocolError,
    recv_frame,
    send_json,
)
from client.net.transport import Transport
from server import storage
from server.capabilities import find_ffmpeg, run_ffmpeg
from server.job_queue import JobPool, JobQueue
from server.listener import Listener
from tools.make_sample_media import make_clip

FFMPEG = find_ffmpeg()
pytestmark = pytest.mark.skipif(
    not FFMPEG, reason="ffmpeg is required for the end-to-end suite"
)


class Harness:
    """A daemon bound to an ephemeral loopback port, with isolated storage."""

    def __init__(self, tmp_path: Path, workers: int = 1, allowed=None) -> None:
        from server.capabilities import build_capabilities

        self.tmp_path = tmp_path
        self.data_root = tmp_path / "jobs"
        self._patch_storage()
        self.caps = build_capabilities(max_concurrent_jobs=workers, ffmpeg_path=FFMPEG)
        self.jobs = JobQueue()
        self.pool = JobPool(self.jobs, workers=workers)
        self.pool.start()
        self.listener = Listener("127.0.0.1", 0, self.caps, self.pool,
                                 allowed or ["127.0.0.0/8"])
        self.listener.bind()
        self.port = self.listener.port
        self.thread = threading.Thread(target=self.listener.serve_forever,
                                       name="test-accept", daemon=True)
        self.thread.start()
        time.sleep(0.15)

    def _patch_storage(self) -> None:
        original = storage.DATA_ROOT
        storage.DATA_ROOT = self.data_root
        self._original_storage_root = original

    def client(self, timeout: float = 8.0) -> Transport:
        transport = Transport("127.0.0.1", self.port, client_name="pytest")
        transport.connect(timeout=timeout)
        return transport

    def stop(self) -> None:
        self.listener.stop()
        self.pool.shutdown()
        storage.DATA_ROOT = self._original_storage_root


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("media")
    return make_clip(FFMPEG, "360p", 2, 15, out_dir)


@pytest.fixture
def harness(tmp_path):
    instance = Harness(tmp_path)
    yield instance
    instance.stop()


@pytest.fixture
def harness_isolated(tmp_path):
    instance = Harness(tmp_path, allowed=["10.99.0.0/24"])
    yield instance
    instance.stop()


# ------------------------------------------------------------------ handshake
class TestHandshake:
    def test_handshake_returns_capabilities(self, harness):
        transport = harness.client()
        try:
            assert transport.worker_id
            assert transport.capabilities is not None
            assert transport.capabilities.protocol_version == PROTOCOL_VERSION
            assert "transcode" in transport.capabilities.engines
        finally:
            transport.close()

    def test_goodbye_is_accepted(self, harness):
        transport = harness.client()
        transport.close()
        time.sleep(0.3)
        assert harness.listener.active_connections == 0

    def test_a_non_hello_first_frame_is_refused(self, harness):
        with socket.create_connection(("127.0.0.1", harness.port), timeout=5) as sock:
            sock.settimeout(5)
            send_json(sock, FrameType.PING, {"nonce": "x"})
            ftype, payload = recv_frame(sock)
            assert ftype is FrameType.ERROR
            body = json.loads(payload)
            assert body["code"] == "expected_hello"
            assert "HELLO" in body["message"]

    def test_bad_magic_is_refused(self, harness):
        with socket.create_connection(("127.0.0.1", harness.port), timeout=5) as sock:
            sock.settimeout(5)
            send_json(sock, FrameType.HELLO, {"magic": "NOPE",
                                              "protocol_version": PROTOCOL_VERSION})
            ftype, payload = recv_frame(sock)
            assert ftype is FrameType.ERROR
            assert json.loads(payload)["code"] == "bad_magic"

    def test_incompatible_protocol_version_is_refused(self, harness):
        with socket.create_connection(("127.0.0.1", harness.port), timeout=5) as sock:
            sock.settimeout(5)
            send_json(sock, FrameType.HELLO, {"magic": "RDO1", "protocol_version": "99.0"})
            ftype, payload = recv_frame(sock)
            assert ftype is FrameType.ERROR
            assert json.loads(payload)["code"] == "incompatible_version"

    def test_a_version_patch_difference_is_accepted(self, harness):
        with socket.create_connection(("127.0.0.1", harness.port), timeout=5) as sock:
            sock.settimeout(5)
            send_json(sock, FrameType.HELLO, {"magic": "RDO1", "protocol_version": "1.7",
                                              "client": "compat"})
            ftype, payload = recv_frame(sock)
            assert ftype is FrameType.HELLO_ACK
            assert json.loads(payload)["protocol_version"] == PROTOCOL_VERSION

    def test_a_client_outside_the_allowlist_is_dropped(self, harness_isolated):
        with pytest.raises((OSError, ProtocolError)):
            transport = Transport("127.0.0.1", harness_isolated.port)
            try:
                transport.connect(timeout=4)
            finally:
                transport.close()

    def test_several_clients_are_served_concurrently(self, harness):
        transports = [harness.client() for _ in range(4)]
        try:
            assert harness.listener.active_connections == 4
            assert harness.listener.total_connections == 4
            for transport in transports:
                assert transport.ping_batch(count=3).samples
        finally:
            for transport in transports:
                transport.close()
        time.sleep(0.4)
        assert harness.listener.active_connections == 0


# -------------------------------------------------------------------- latency
class TestLatency:
    def test_ping_samples_are_collected(self, harness):
        transport = harness.client()
        try:
            stats = transport.ping_batch(count=12)
            assert len(stats.samples) == 12
            assert stats.min_ms > 0
            assert stats.min_ms <= stats.avg_ms <= stats.max_ms
            assert stats.jitter_ms >= 0
        finally:
            transport.close()

    def test_ping_works_while_a_job_is_running(self, harness, clip):
        transport = harness.client()
        try:
            upload = transport.upload(clip)
            spec = JobSpec(output_name="busy.mp4", video_codec="libx264",
                           preset="ultrafast", resolution="360p", crf=30)
            transport.submit(spec, asset_id=upload.asset_id)
            stats = transport.ping_batch(count=6)
            assert len(stats.samples) == 6
        finally:
            transport.close()


# ------------------------------------------------------------------ transfers
class TestTransfers:
    def test_upload_is_verified_by_the_worker(self, harness, clip):
        transport = harness.client()
        try:
            result = transport.upload(clip)
            assert result.sha256 == sha256_file(clip)
            assert result.size == clip.stat().st_size
            assert result.megabits_per_s > 0
        finally:
            transport.close()

    def test_a_truncated_upload_is_rejected_and_cleaned_up(self, harness, clip):
        transport = harness.client()
        try:
            digest = sha256_file(clip)
            from common.protocol import send_bytes

            send_json(transport.sock, FrameType.UPLOAD_BEGIN, {
                "asset_id": "forged", "name": clip.name,
                "size": clip.stat().st_size, "sha256": digest,
            })
            # Promise the full size, send a fraction of it, then half-close: the
            # worker must notice the truncated body instead of waiting out its
            # 180 second stall deadline.
            partial = clip.read_bytes()[: max(1, clip.stat().st_size // 3)]
            send_bytes(transport.sock, partial)
            transport.sock.shutdown(socket.SHUT_WR)
            while True:
                ftype, payload = recv_frame(transport.sock)
                if ftype is FrameType.ERROR:
                    assert json.loads(payload)["code"] == "upload_interrupted"
                    break
                assert ftype in (FrameType.LOG, FrameType.UPLOAD_ACK)
        finally:
            transport.close()
        assert not any(p.name.endswith(".part") for p in harness.data_root.rglob("*"))

    def test_a_corrupted_upload_is_rejected_by_the_checksum(self, harness, clip):
        transport = harness.client()
        try:
            payload = clip.read_bytes()
            from common.protocol import send_bytes

            send_json(transport.sock, FrameType.UPLOAD_BEGIN, {
                "asset_id": "bad", "name": "evil.mp4", "size": len(payload),
                "sha256": "b" * 64,
            })
            corrupt = bytearray(payload)
            corrupt[0] ^= 0xFF
            send_bytes(transport.sock, bytes(corrupt))
            send_json(transport.sock, FrameType.UPLOAD_END, {"asset_id": "bad"})

            while True:
                ftype, data = recv_frame(transport.sock)
                if ftype is FrameType.ERROR:
                    assert json.loads(data)["code"] == "checksum_mismatch"
                    break
                assert ftype in (FrameType.LOG, FrameType.UPLOAD_ACK)
        finally:
            transport.close()

    def test_download_is_verified_by_the_client(self, harness, clip, tmp_path):
        transport = harness.client()
        try:
            received = self._run_one(transport, clip, tmp_path)
            assert received.verified is True
            assert received.path.exists()
            assert received.path.stat().st_size == received.size
            assert not list(tmp_path.glob("*.part"))
        finally:
            transport.close()

    @staticmethod
    def _run_one(transport: Transport, clip: Path, tmp_path: Path):
        upload = transport.upload(clip)
        spec = JobSpec(output_name="out.mp4", video_codec="libx264", preset="ultrafast",
                       resolution="360p", crf=30, audio_codec="aac")
        acknowledged = transport.submit(spec, asset_id=upload.asset_id)
        ftype, result = transport.await_result()
        assert ftype is FrameType.RESULT
        assert result["status"] == "ok"
        return transport.download(result["job_id"], tmp_path / "received.mp4")


# ------------------------------------------------------------------------ jobs
class TestJobs:
    def test_full_pipeline_produces_a_playable_output(self, harness, clip, tmp_path):
        transport = harness.client()
        try:
            upload = transport.upload(clip)
            spec = JobSpec(output_name="final.mp4", video_codec="libx264",
                           preset="ultrafast", resolution="360p", crf=28)
            acknowledged = transport.submit(spec, asset_id=upload.asset_id)
            assert acknowledged["accepted"] is True
            assert acknowledged["engine"] == "ffmpeg"
            assert acknowledged["job_id"]

            progress: list[float] = []
            ftype, result = transport.await_result(
                on_frame=lambda t, b: progress.append(b["pct"])
                if t is FrameType.PROGRESS else None)
            assert ftype is FrameType.RESULT
            assert result["status"] == "ok"
            assert result["compute_seconds"] > 0
            assert result["output_size"] > 0
            assert len(result["output_sha256"]) == 64

            assert progress, "no progress frames were streamed"
            assert progress == sorted(progress), "progress went backwards"
            assert progress[-1] == 100.0

            received = transport.download(result["job_id"], tmp_path / "final.mp4")
            assert received.sha256 == result["output_sha256"]
            assert received.verified is True
        finally:
            transport.close()

    def test_compute_job_streams_progress_without_an_upload(self, harness, tmp_path):
        transport = harness.client()
        try:
            spec = JobSpec(job_type="compute", compute_op="matmul", compute_size=256,
                           compute_iters=5)
            acknowledged = transport.submit(spec)
            assert acknowledged["engine"] == "compute"

            seen: list[str] = []
            ftype, result = transport.await_result(
                on_frame=lambda t, b: seen.append(b.get("stage", ""))
                if t is FrameType.PROGRESS else None)
            assert ftype is FrameType.RESULT
            assert result["status"] == "ok"
            assert "allocating" in seen
            assert "computing" in seen

            # A compute job has no video, so the metrics file is the output and it
            # must be downloadable and checksummed like any other result.
            assert result["output_name"].endswith(".json")
            assert result["output_size"] > 0
            assert len(result["output_sha256"]) == 64
            received = transport.download(result["job_id"], tmp_path / "metrics.json")
            assert received.verified is True
            payload = json.loads(received.path.read_text(encoding="utf-8"))
            assert payload["operation"] == "matmul"
            assert payload["metrics"]["iters"] == 5
            assert "compute_s" in payload["metrics"]
        finally:
            transport.close()

    def test_submitting_without_an_upload_is_rejected(self, harness):
        transport = harness.client()
        try:
            with pytest.raises(ProtocolError, match="upload the file first"):
                transport.submit(JobSpec(video_codec="libx264", preset="ultrafast"))
        finally:
            transport.close()

    def test_an_invalid_spec_is_rejected_before_it_is_sent(self, harness):
        transport = harness.client()
        try:
            with pytest.raises(ProtocolError, match="invalid job"):
                transport.submit(JobSpec(crf=99))
        finally:
            transport.close()

    def test_an_unavailable_encoder_is_rejected_by_the_worker(self, harness, clip):
        transport = harness.client()
        try:
            upload = transport.upload(clip)
            spec = JobSpec(video_codec="libx265", preset="medium", crf=23)
            with pytest.raises(ProtocolError, match="not in this build of ffmpeg"):
                transport.submit(spec, asset_id=upload.asset_id)
        finally:
            transport.close()

    def test_a_broken_input_produces_a_failed_result_not_a_crash(self, harness, tmp_path):
        transport = harness.client()
        try:
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(b"this is definitely not an mp4 container")
            upload = transport.upload(broken)
            spec = JobSpec(output_name="x.mp4", video_codec="libx264",
                           preset="ultrafast", resolution="360p")
            transport.submit(spec, asset_id=upload.asset_id)
            ftype, result = transport.await_result()
            assert ftype is FrameType.RESULT
            assert result["status"] == "error"
            assert result["error"]
        finally:
            transport.close()

    def test_the_worker_survives_a_failed_job(self, harness, clip, tmp_path):
        transport = harness.client()
        try:
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(b"nope")
            transport.submit(
                JobSpec(video_codec="libx264", preset="ultrafast"),
                asset_id=transport.upload(broken).asset_id)
            transport.await_result()

            upload = transport.upload(clip)
            transport.submit(JobSpec(output_name="ok.mp4", video_codec="libx264",
                                     preset="ultrafast", resolution="360p"),
                             asset_id=upload.asset_id)
            ftype, result = transport.await_result()
            assert ftype is FrameType.RESULT
            assert result["status"] == "ok"
        finally:
            transport.close()

    def test_a_disconnect_mid_job_does_not_kill_the_daemon(self, harness, clip):
        transport = harness.client()
        upload = transport.upload(clip)
        transport.submit(JobSpec(output_name="x.mp4", video_codec="libx264",
                                 preset="ultrafast", resolution="360p"),
                         asset_id=upload.asset_id)
        transport.sock.close()  # vanish without a BYE
        transport.sock = None
        time.sleep(1.5)

        second = harness.client()
        try:
            assert second.ping_batch(count=3).samples
        finally:
            second.close()

    def test_requesting_an_unknown_job_is_refused(self, harness):
        transport = harness.client()
        try:
            with pytest.raises(ProtocolError):
                transport.download("no-such-job", harness.tmp_path / "x.mp4")
        finally:
            transport.close()


# ------------------------------------------------------------------- latency
class TestImportSideEffects:
    def test_no_state_file_is_written_by_the_test_harness(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RDO_SERVER_DATA", str(tmp_path))
        Harness(tmp_path).stop()