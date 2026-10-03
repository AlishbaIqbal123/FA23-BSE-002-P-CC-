"""Client-side socket transport: connect, handshake, measure, transfer, stream.

This module knows nothing about Qt. It is a synchronous, blocking API that the
GUI drives from a worker thread, which keeps the protocol testable in isolation
and stops any widget code from leaking into the networking layer.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from common.checksum import sha256_file
from common.messages import Capabilities, JobSpec, safe_filename
from common.paths import (
    PROTOCOL_VERSION,
    SOCKET_CONNECT_TIMEOUT,
    STREAM_CHUNK,
)
from common.protocol import (
    ConnectionTimeout,
    FrameType,
    ProtocolError,
    RemoteError,
    recv_frame,
    send_bytes,
    send_json,
)

LOG = logging.getLogger("rdo.client.net")


def _decode(payload: bytes) -> dict:
    """Best-effort JSON decode of a control frame, never raising."""
    try:
        body = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {"value": body}


@dataclass
class RttStats:
    samples: list[float] = field(default_factory=list)

    @property
    def min_ms(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def avg_ms(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def jitter_ms(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        mean = self.avg_ms
        variance = sum((s - mean) ** 2 for s in self.samples) / len(self.samples)
        return variance ** 0.5

    def to_dict(self) -> dict:
        return {
            "count": len(self.samples),
            "min_ms": round(self.min_ms, 3),
            "avg_ms": round(self.avg_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "jitter_ms": round(self.jitter_ms, 3),
        }


@dataclass
class UploadResult:
    asset_id: str
    name: str
    size: int
    sha256: str
    seconds: float
    megabits_per_s: float


@dataclass
class DownloadResult:
    job_id: str
    name: str
    path: Path
    size: int
    sha256: str
    seconds: float
    megabits_per_s: float
    verified: bool


class Transport:
    """One connection to one worker."""

    def __init__(self, host: str, port: int, client_name: str = "rdo-gui") -> None:
        self.host = host
        self.port = port
        self.client_name = client_name
        self.sock: socket.socket | None = None
        self.worker_id = ""
        self.capabilities: Capabilities | None = None
        self.connected_at = 0.0
        self._pending_ack: list[dict] = []

    # -- connection --------------------------------------------------------
    def configure(self, host: str, port: int) -> None:
        """Point this transport at a new worker.

        The session worker owns the current target (the GUI edits it live), and a
        transport created earlier - e.g. with the default address - must follow it
        rather than keep dialling whatever it was built with.
        """
        self.host = (host or "").strip()
        self.port = int(port)

    def connect(self, timeout: float = SOCKET_CONNECT_TIMEOUT) -> None:
        self.close()
        started = time.perf_counter()
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(30.0)
        self.sock = sock
        LOG.info("TCP connect to %s:%d in %.1f ms", self.host, self.port,
                 (time.perf_counter() - started) * 1000)
        self.handshake()

    def handshake(self) -> Capabilities:
        assert self.sock is not None
        send_json(self.sock, FrameType.HELLO, {
            "magic": "RDO1",
            "protocol_version": PROTOCOL_VERSION,
            "client": self.client_name,
            "platform": f"{socket.gethostname()}",
            "sent_at": time.time(),
        })
        ftype, payload = recv_frame(self.sock)
        if ftype is not FrameType.HELLO_ACK:
            raise ProtocolError(f"expected HELLO_ACK, got {ftype.name}")
        body = json.loads(payload.decode("utf-8"))
        self.worker_id = body.get("worker_id", "")
        self.capabilities = Capabilities(**body.get("capabilities", {}))
        self.connected_at = time.time()
        return self.capabilities

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            send_json(self.sock, FrameType.BYE, {"reason": "client closing"})
        except (OSError, ProtocolError):
            pass
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    @property
    def is_open(self) -> bool:
        return self.sock is not None

    # -- latency -----------------------------------------------------------
    def ping_batch(self, count: int = 10, gap: float = 0.02) -> RttStats:
        """Send ``count`` pings and wait for each PONG, measuring the RTT."""
        assert self.sock is not None
        stats = RttStats()
        for index in range(count):
            nonce = uuid.uuid4().hex[:12]
            sent = time.perf_counter()
            send_json(self.sock, FrameType.PING, {"nonce": nonce, "t0": sent * 1000.0,
                                                  "seq": index})
            while True:
                ftype, payload = recv_frame(self.sock)
                if ftype is FrameType.PONG:
                    pong = json.loads(payload.decode("utf-8"))
                    if pong.get("nonce") == nonce:
                        stats.samples.append((time.perf_counter() - sent) * 1000.0)
                        break
                elif ftype is FrameType.LOG:
                    continue
                else:
                    raise ProtocolError(f"unexpected {ftype.name} while waiting for PONG")
            if gap:
                time.sleep(gap)
        return stats

    # -- upload ------------------------------------------------------------
    def upload(self, path: Path, on_progress: Callable[[int, int, float], None] | None = None,
               stall_timeout: float = 30.0) -> UploadResult:
        """Stream a file to the worker and block until it is verified."""
        assert self.sock is not None
        path = Path(path)
        size = path.stat().st_size
        digest = sha256_file(path)
        asset_id = uuid.uuid4().hex
        name = safe_filename(path.name)

        send_json(self.sock, FrameType.UPLOAD_BEGIN, {
            "asset_id": asset_id, "name": name, "size": size, "sha256": digest,
        })
        self.sock.settimeout(stall_timeout)

        started = time.perf_counter()
        sent = 0
        with open(path, "rb") as handle:
            while sent < size:
                block = handle.read(STREAM_CHUNK)
                if not block:
                    break
                send_bytes(self.sock, block)
                sent += len(block)
                if on_progress:
                    on_progress(sent, size, time.perf_counter() - started)
        self.sock.settimeout(stall_timeout)

        # The worker acknowledges every few MiB and interleaves LOG frames. Those
        # stay queued in the socket until now, and the loop below drains them
        # until it reaches the frame it actually wants. Nothing is lost, because
        # the server only ever *enqueues* outbound frames - it never blocks its
        # reader on a send, so a slow reader cannot deadlock the transfer.
        send_json(self.sock, FrameType.UPLOAD_END, {"asset_id": asset_id, "sha256": digest,
                                                    "size": size})
        deadline = time.perf_counter() + stall_timeout
        while True:
            if time.perf_counter() > deadline:
                raise ConnectionTimeout("worker did not confirm the upload in time")
            ftype, payload = recv_frame(self.sock)
            if ftype is FrameType.UPLOAD_ACK:
                # A periodic transfer acknowledgement, queued while we were busy
                # sending. Report it so the progress bar keeps moving.
                try:
                    ack = json.loads(payload.decode("utf-8"))
                    if on_progress:
                        on_progress(int(ack.get("received", 0)), int(ack.get("size", size)),
                                    time.perf_counter() - started)
                except (ValueError, AttributeError):
                    pass
                continue
            body = json.loads(payload.decode("utf-8")) if payload else {}
            if ftype is FrameType.UPLOAD_DONE:
                elapsed = time.perf_counter() - started
                if body.get("sha256", "").lower() != digest:
                    raise ProtocolError(
                        "the worker's checksum for the uploaded file does not match ours"
                    )
                return UploadResult(
                    asset_id=body.get("asset_id", asset_id),
                    name=body.get("name", name),
                    size=int(body.get("size", size)),
                    sha256=body.get("sha256", digest),
                    seconds=elapsed,
                    megabits_per_s=size * 8 / max(elapsed, 1e-6) / 1e6,
                )
            if ftype is FrameType.ERROR:
                raise ProtocolError(
                    f"[{body.get('code')}] {body.get('message')}"
                )
            if ftype is FrameType.LOG:
                LOG.debug("server: %s", body.get("message"))
                continue
            raise ProtocolError(f"unexpected {ftype.name} during upload confirmation")

    # -- job ---------------------------------------------------------------
    def _expect(self, wanted: FrameType, tolerate: tuple[FrameType, ...] = (),
                on_frame: Callable[[FrameType, dict], None] | None = None
                ) -> tuple[FrameType, bytes]:
        """Read frames until ``wanted`` arrives.

        The worker interleaves ``LOG``, ``PROGRESS`` and ``UPLOAD_ACK`` frames with
        whatever the client is waiting for, so every read has to be a small state
        machine rather than a single ``recv_frame``. An ``ERROR`` frame is turned
        into :class:`RemoteError` here, which is what the GUI's error box expects.
        """
        assert self.sock is not None
        while True:
            ftype, payload = recv_frame(self.sock)
            if ftype is wanted:
                return ftype, payload
            if ftype is FrameType.ERROR:
                code, message = "unknown", payload.decode("utf-8", "replace")
                try:
                    body = json.loads(payload.decode("utf-8"))
                    code, message = body.get("code", code), body.get("message", message)
                except ValueError:
                    pass
                raise RemoteError(code, message)
            if on_frame is not None:
                on_frame(ftype, _decode(payload))
            elif ftype is FrameType.LOG:
                LOG.debug("server: %s", _decode(payload).get("message"))
            elif ftype not in tolerate:
                raise ProtocolError(f"expected {wanted.name}, got {ftype.name}")

    def submit(self, spec: JobSpec, asset_id: str = "") -> dict:
        assert self.sock is not None
        errors = spec.validate()
        if errors:
            raise ProtocolError("invalid job: " + "; ".join(errors))
        payload = spec.to_dict()
        payload["asset_id"] = asset_id
        send_json(self.sock, FrameType.JOB_SUBMIT, payload)
        while True:
            ftype, data = recv_frame(self.sock)
            if ftype is FrameType.JOB_ACK:
                return json.loads(data.decode("utf-8"))
            if ftype is FrameType.JOB_REJECT:
                body = json.loads(data.decode("utf-8"))
                raise ProtocolError("job rejected: " + "; ".join(body.get("errors", [])))
            if ftype is FrameType.LOG:
                continue
            raise ProtocolError(f"unexpected {ftype.name} while waiting for JOB_ACK")

    def cancel(self, job_id: str) -> None:
        if self.sock is None:
            return
        try:
            send_json(self.sock, FrameType.CANCEL, {"job_id": job_id})
        except (OSError, ProtocolError) as exc:
            LOG.warning("cancel could not be sent: %s", exc)

    # -- result / download -------------------------------------------------
    def await_result(self, on_frame: Callable[[FrameType, dict], None] | None = None
                     ) -> tuple[FrameType, dict]:
        """Block until the job finishes, forwarding every other frame to ``on_frame``."""
        assert self.sock is not None
        while True:
            ftype, payload = recv_frame(self.sock)
            body: dict = {}
            if payload and payload[:1] in (b"{", b"["):
                try:
                    body = json.loads(payload.decode("utf-8"))
                except ValueError:
                    body = {"raw": payload[:200].decode("utf-8", "replace")}
            if on_frame is not None:
                on_frame(ftype, body)
            if ftype in (FrameType.RESULT, FrameType.JOB_REJECT, FrameType.ERROR):
                return ftype, body

    def download(self, job_id: str, destination: Path,
                 on_progress: Callable[[int, int, float], None] | None = None,
                 stall_timeout: float = 60.0) -> DownloadResult:
        assert self.sock is not None
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")

        send_json(self.sock, FrameType.DOWNLOAD_REQ, {"job_id": job_id})
        _, payload = self._expect(FrameType.STREAM_BEGIN)
        header = json.loads(payload.decode("utf-8"))
        size = int(header.get("size", 0))
        expected = str(header.get("sha256", "")).lower()

        self.sock.settimeout(stall_timeout)
        started = time.perf_counter()
        received = 0
        try:
            with open(part, "wb") as handle:
                while received < size:
                    # LOG and PROGRESS frames may still be queued from the job
                    # itself, so chunks are drained through the same tolerant
                    # reader instead of a bare recv_frame.
                    _, block = self._expect(FrameType.DOWNLOAD_CHUNK)
                    handle.write(block)
                    received += len(block)
                    if on_progress:
                        on_progress(received, size, time.perf_counter() - started)
            self.sock.settimeout(30.0)
            _, payload = self._expect(FrameType.STREAM_END)
            trailer = json.loads(payload.decode("utf-8"))
        except BaseException:
            # Never leave a half-written file behind that looks like a real result.
            part.unlink(missing_ok=True)
            raise

        actual = sha256_file(part)
        if expected and actual.lower() != expected:
            part.unlink(missing_ok=True)
            raise ProtocolError(
                "checksum mismatch on the downloaded file: the transfer was corrupted"
            )
        if part.exists():
            part.replace(destination)

        elapsed = time.perf_counter() - started
        return DownloadResult(
            job_id=job_id,
            name=header.get("name", destination.name),
            path=destination,
            size=size,
            sha256=actual,
            seconds=elapsed,
            megabits_per_s=size * 8 / max(elapsed, 1e-6) / 1e6,
            verified=True,
        )

    def drain_logs(self, timeout: float = 0.4) -> Iterator[dict]:
        """Non-blocking-ish helper used by tests to pick up trailing LOG frames."""
        if self.sock is None:
            return iter(())
        self.sock.settimeout(timeout)
        frames: list[dict] = []
        try:
            while True:
                ftype, payload = recv_frame(self.sock)
                if ftype is FrameType.LOG:
                    frames.append(json.loads(payload.decode("utf-8")))
        except (ConnectionTimeout, ProtocolError, OSError):
            pass
        finally:
            if self.sock is not None:
                self.sock.settimeout(30.0)
        return iter(frames)


__all__ = ["Transport", "RttStats", "UploadResult", "DownloadResult"]