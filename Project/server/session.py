"""Per-connection state machine: handshake, transfer, job dispatch, streaming.

Threading model
---------------
Each accepted socket gets **two** threads:

``Session-N/reader``
    Blocks on ``recv_frame``. Owns all protocol parsing and client-facing
    validation. Never runs an encode inline - doing so would stop this socket
    from answering ``PING`` frames while a 40-minute job was in flight.

``Session-N/writer``
    Blocks on an outbound ``queue.Queue``. Routine frames are written by this
    thread and only this thread.

Worker threads (from :class:`~server.job_queue.JobPool`) push ``PROGRESS`` and
``RESULT`` frames into the writer's queue. That is the whole reason progress can
stream while a job runs: the encode never touches the socket.

A bulk download is the one exception - the reader performs it inline, because the
byte stream has to arrive contiguously. ``_wire`` is a lock held by the writer
around every send and by the reader around the whole download, so two threads can
never emit halves of two frames on the same socket.

Connection life-cycle::

    CONNECT -> HELLO / HELLO_ACK -> [PING/PONG]* -> UPLOAD_BEGIN -> <raw bytes>
            -> UPLOAD_END -> UPLOAD_DONE -> JOB_SUBMIT -> JOB_ACK -> PROGRESS*
            -> RESULT -> DOWNLOAD_REQ -> STREAM_BEGIN -> <framed chunks> -> STREAM_END
            -> BYE

The upload body between ``UPLOAD_BEGIN`` and ``UPLOAD_END`` is one contiguous raw
byte run, not a series of frames: it is read with :func:`recv_exact` and hashed as
it lands. Downloads stay framed (``STREAM_CHUNK`` payloads of type
``DOWNLOAD_CHUNK``) so the client can report per-chunk progress and a truncated
transfer is detected immediately.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import queue
import socket
import threading
import time
from pathlib import Path

from common.checksum import sha256_file
from common.messages import (
    Capabilities,
    JobResult,
    JobSpec,
    Progress,
    safe_filename,
)
from common.paths import (
    MAX_UPLOAD_BYTES,
    PROTOCOL_VERSION,
    STREAM_CHUNK,
    UPLOAD_ACK_INTERVAL,
)
from common.protocol import (
    ConnectionTimeout,
    FrameType,
    PeerClosed,
    ProtocolError,
    recv_exact,
    recv_frame,
    send_frame,
    send_json,
    write_stream,
)
from . import storage
from .engines import build_registry
from .engines.base import Engine, EngineResult, JobContext

LOG = logging.getLogger("rdo.session")

OUTBOUND_QUEUE_LIMIT = 512
_PROGRESS_FIELDS = frozenset(Progress.__dataclass_fields__)


def _human_bytes(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(count) < 1024 or unit == "TiB":
            return f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TiB"


def _default_output_name(spec) -> str:
    """Pick a sensible output file name when the client did not choose one.

    A compute job produces a JSON metrics file, not a video, so handing it the
    default ``output.mp4`` would download a text file wearing a video extension.
    """
    if spec.job_type == "compute":
        stem = Path(spec.output_name).stem if spec.output_name else "metrics"
        return f"{stem or 'metrics'}.json"
    return spec.output_name or "output.mp4"


def ip_allowed(address: str, subnets: list[str]) -> bool:
    """Membership test used as the daemon's first line of defence."""
    try:
        peer = ipaddress.ip_address(address)
    except ValueError:
        return False
    for entry in subnets:
        try:
            if peer in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


class Session:
    """Handles one client connection for its entire lifetime."""

    def __init__(
        self,
        sock: socket.socket,
        peer: tuple[str, int],
        caps: Capabilities,
        pool,
        allowed: list[str],
    ) -> None:
        self.sock = sock
        self.peer = peer
        self.caps = caps
        self.pool = pool
        self.allowed = allowed
        self.engines: dict[str, Engine] = build_registry(caps)

        self.tag = f"{peer[0]}:{peer[1]}"
        self.outbound: queue.Queue = queue.Queue(maxsize=OUTBOUND_QUEUE_LIMIT)
        self._wire = threading.RLock()
        self.halt = threading.Event()
        self.closed = threading.Event()
        self.client_id = ""
        self.uploads: dict[str, storage.StagedUpload] = {}
        self.jobs: dict[str, JobContext] = {}
        self.upload_seconds: dict[str, float] = {}
        self.job_upload_seconds: dict[str, float] = {}
        self.engines_by_job: dict[str, Engine] = {}
        # Why the session ended. Set exactly once, then reported in the run()
        # epilogue so a dropped connection can always be explained in the log.
        self.reason = "still running"

    def _end(self, reason: str) -> None:
        """Record the first reason the session is ending and request a stop."""
        if not self.halt.is_set():
            self.reason = reason
        self.halt.set()

    # -- outbound plumbing -------------------------------------------------
    def emit(self, ftype: FrameType, payload: dict) -> None:
        """Queue a control frame. Never blocks, never writes to the socket."""
        if self.halt.is_set():
            return
        try:
            self.outbound.put_nowait((ftype, payload))
        except queue.Full:
            if ftype is FrameType.PROGRESS:
                LOG.warning("[%s] outbound queue full, dropping a progress frame", self.tag)
                return
            LOG.error("[%s] outbound queue full on %s - dropping the connection", self.tag, ftype)
            self._end(f"outbound queue overflow on {ftype.name}")

    def emit_log(self, level: str, message: str) -> None:
        self.emit(FrameType.LOG, {"level": level, "message": message, "ts": time.time()})

    def _writer_loop(self) -> None:
        while not self.halt.is_set() or not self.outbound.empty():
            try:
                ftype, payload = self.outbound.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                with self._wire:
                    if isinstance(payload, (bytes, bytearray)):
                        send_frame(self.sock, ftype, bytes(payload))
                    else:
                        send_json(self.sock, ftype, payload)
            except (OSError, ProtocolError) as exc:
                LOG.warning("[%s] writer stopped: %s", self.tag, exc, exc_info=True)
                self._end(f"writer failed: {exc}")
                return
            finally:
                self.outbound.task_done()

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.writer = threading.Thread(target=self._writer_loop, name=f"Writer-{self.tag}",
                                       daemon=True)
        self.writer.start()
        self.sock.settimeout(30.0)
        try:
            if not ip_allowed(self.peer[0], self.allowed):
                LOG.warning("[%s] rejected: address outside %s", self.tag, self.allowed)
                self._end(f"address {self.peer[0]} is outside {self.allowed}")
                return
            self._handshake()
            while not self.halt.is_set():
                self._dispatch_once()
        except PeerClosed:
            self._end("client closed the connection")
        except ConnectionTimeout as exc:
            self._end(f"idle timeout: {exc}")
        except ProtocolError as exc:
            self._end(f"protocol error: {exc}")
        except OSError as exc:
            self._end(f"socket error: {exc}")
        finally:
            self.shutdown()
            LOG.info("[%s] session finished: %s", self.tag, self.reason)

    def shutdown(self) -> None:
        if self.closed.is_set():
            return
        # Stop the writer first so it stops touching the socket, then join it with
        # a short deadline. Joining (rather than polling the queue) means we never
        # close the socket while the writer is still inside send_frame, and any
        # RESULT written microseconds before a client hangup still reaches it.
        self.halt.set()
        writer = getattr(self, "writer", None)
        if writer is not None and writer.is_alive():
            writer.join(timeout=1.5)
        for ctx in self.jobs.values():
            ctx.cancel()
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # -- handshake ---------------------------------------------------------
    def _handshake(self) -> None:
        ftype, payload = recv_frame(self.sock)
        if ftype is not FrameType.HELLO:
            self._refuse("expected_hello",
                         f"the first frame must be HELLO, not {ftype.name}")
        hello = json.loads(payload.decode("utf-8"))
        if hello.get("magic") != "RDO1":
            self._refuse("bad_magic",
                         f"magic {hello.get('magic')!r} does not identify an RDO client")
        version = str(hello.get("protocol_version", ""))
        if version.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
            self._refuse("incompatible_version",
                         f"protocol {version} cannot be spoken by a "
                         f"{PROTOCOL_VERSION} daemon")
        self.client_id = str(hello.get("client", "unknown"))[:64]

        self.emit(FrameType.HELLO_ACK, {
            "protocol_version": PROTOCOL_VERSION,
            "worker_id": self.caps.worker_id,
            "capabilities": self.caps.to_dict(),
            "queue_depth": self.pool.jobs.depth(),
            "active_jobs": self.pool.jobs.active_count(),
            "server_time": time.time(),
        })
        self.emit_log("info", f"handshake ok: {self.client_id} -> worker {self.caps.worker_id}")

    def _refuse(self, code: str, message: str) -> None:
        """Tell the client why it was rejected, then end the session.

        Silently dropping the socket would leave the GUI showing a bare
        connection reset with no hint that the cause was a version skew.
        """
        self.emit(FrameType.ERROR, {"code": code, "message": message})
        self.emit_log("error", f"handshake refused: {message}")
        raise ProtocolError(f"handshake refused [{code}]: {message}")

    def _read_json(self, wanted: FrameType) -> dict:
        ftype, payload = recv_frame(self.sock)
        if ftype is not wanted:
            raise ProtocolError(f"expected {wanted.name}, got {ftype.name}")
        return json.loads(payload.decode("utf-8"))

    def _fail(self, code: str, message: str, fatal: bool = True) -> None:
        self.emit(FrameType.ERROR, {"code": code, "message": message})
        if fatal:
            self._end(f"error [{code}]: {message}")

    # -- dispatch ----------------------------------------------------------
    def _dispatch_once(self) -> None:
        ftype, payload = recv_frame(self.sock)
        self.sock.settimeout(30.0)

        if ftype in (FrameType.PING, FrameType.PING_BATCH):
            request = json.loads(payload.decode("utf-8"))
            if ftype is FrameType.PING:
                self.emit(FrameType.PONG, {
                    "nonce": request.get("nonce", ""),
                    "t0": request.get("t0", 0.0),
                    "server_ms": time.perf_counter() * 1000.0,
                    "ts": time.time(),
                })
            else:
                count = max(1, min(int(request.get("count", 5)), 50))
                base = time.perf_counter() * 1000.0
                for index in range(count):
                    self.emit(FrameType.PONG, {
                        "nonce": f"{request.get('nonce', '')}-{index}",
                        "t0": base,
                        "batch_index": index,
                        "server_ms": time.perf_counter() * 1000.0,
                        "ts": time.time(),
                    })
        elif ftype is FrameType.UPLOAD_BEGIN:
            self._begin_upload(json.loads(payload.decode("utf-8")))
        elif ftype is FrameType.JOB_SUBMIT:
            self._submit_job(json.loads(payload.decode("utf-8")))
        elif ftype is FrameType.DOWNLOAD_REQ:
            self._download(json.loads(payload.decode("utf-8")))
        elif ftype is FrameType.CANCEL:
            self._cancel(json.loads(payload.decode("utf-8")))
        elif ftype is FrameType.BYE:
            self.emit_log("info", "client said goodbye")
            self._end("client sent BYE")
        else:
            self._fail("unexpected_frame",
                       f"{ftype.name} is not valid at this point in the session")

    # -- upload ------------------------------------------------------------
    def _begin_upload(self, request: dict) -> None:
        name = safe_filename(request.get("name", "asset.bin"), "asset.bin")
        size = int(request.get("size", 0))
        declared = str(request.get("sha256", "")).lower()
        if size < 0 or size > MAX_UPLOAD_BYTES:
            self._fail("upload_too_large",
                       f"{size} bytes exceeds the {MAX_UPLOAD_BYTES} byte limit")
            return

        part, record = storage.allocate_upload(name, request.get("asset_id"))
        record.size = size
        record.sha256 = declared
        self.uploads[record.asset_id] = record
        self.emit_log("info", f"upload accepted: {name} ({_human_bytes(size)})"
                              + (f", sha256 {declared[:16]}..." if declared else ""))

        self.sock.settimeout(180.0)
        started = time.perf_counter()
        received = 0
        next_ack = UPLOAD_ACK_INTERVAL
        try:
            with open(part, "wb") as handle:
                while received < size:
                    block = recv_exact(self.sock, min(STREAM_CHUNK, size - received))
                    handle.write(block)
                    received += len(block)
                    if received >= next_ack or received == size:
                        self.emit(FrameType.UPLOAD_ACK, {
                            "asset_id": record.asset_id,
                            "direction": "upload",
                            "received": received,
                            "size": size,
                            "pct": round(received / max(size, 1) * 100.0, 2),
                        })
                        next_ack += UPLOAD_ACK_INTERVAL
        except (ProtocolError, OSError) as exc:
            storage.discard_upload(record)
            self.uploads.pop(record.asset_id, None)
            self.emit(FrameType.ERROR, {"code": "upload_interrupted",
                                        "message": f"stopped at {received}/{size} bytes: {exc}"})
            self._end(f"upload of {name} interrupted at {received}/{size} bytes")
            return
        finally:
            self.sock.settimeout(30.0)

        try:
            end = self._read_json(FrameType.UPLOAD_END)
        except (ProtocolError, ValueError) as exc:
            storage.discard_upload(record)
            self.uploads.pop(record.asset_id, None)
            self._fail("upload_unterminated", str(exc))
            return

        if end.get("asset_id") != record.asset_id:
            storage.discard_upload(record)
            self.uploads.pop(record.asset_id, None)
            self._fail("asset_mismatch", "UPLOAD_END asset_id does not match")
            return

        elapsed = time.perf_counter() - started
        try:
            storage.finalize_upload(record, declared)
        except (ValueError, OSError) as exc:
            storage.discard_upload(record)
            self.uploads.pop(record.asset_id, None)
            self.emit(FrameType.ERROR, {"code": "checksum_mismatch", "message": str(exc)})
            self.emit_log("error", f"rejected upload: {exc}")
            self._end(f"upload of {name} failed its checksum: {exc}")
            return

        self.upload_seconds[record.asset_id] = elapsed
        throughput = record.size * 8 / max(elapsed, 1e-6) / 1e6
        self.emit(FrameType.UPLOAD_DONE, {
            "asset_id": record.asset_id,
            "name": record.name,
            "size": record.size,
            "sha256": record.sha256,
            "seconds": round(elapsed, 4),
            "megabits_per_s": round(throughput, 2),
        })
        self.emit_log("success", f"upload verified in {elapsed:.2f}s ({throughput:.1f} Mb/s)")

    # -- job submission ----------------------------------------------------
    def _submit_job(self, request: dict) -> None:
        spec = JobSpec.from_dict(request)
        errors = spec.validate()
        asset_id = str(request.get("asset_id", ""))
        record = self.uploads.get(asset_id)

        if spec.job_type not in self.engines:
            errors.append(f"this worker exposes no '{spec.job_type}' engine "
                          f"(available: {sorted(self.engines)})")
        if spec.job_type == "transcode":
            if record is None:
                errors.append("no uploaded asset matches asset_id - upload the file first")
            elif self.caps.hardware_encoders and \
                    spec.video_codec not in self.caps.hardware_encoders:
                errors.append(
                    f"encoder {spec.video_codec} is not in this build of ffmpeg "
                    f"(available: {self.caps.hardware_encoders})"
                )

        if errors:
            self.emit(FrameType.JOB_REJECT, {"errors": errors, "job_type": spec.job_type})
            self.emit_log("error", "job rejected: " + "; ".join(errors))
            return

        if record is not None:
            spec.asset_name, spec.asset_size = record.name, record.size
            spec.asset_sha256 = record.sha256

        job_id = storage.new_job_id()
        engine = self.engines[spec.job_type]
        output = storage.output_path(job_id, _default_output_name(spec))
        input_path = storage.commit_upload_to_job(record, job_id) if record else output.parent

        ctx = JobContext(
            job_id=job_id,
            spec=spec,
            input_path=input_path,
            output_path=output,
            progress=self._progress_adapter(job_id),
            log=self.emit_log,
        )
        self.jobs[job_id] = ctx
        self.engines_by_job[job_id] = engine
        self.job_upload_seconds[job_id] = self.upload_seconds.get(asset_id, 0.0)

        storage.write_meta(job_id, spec, {
            "client": self.client_id,
            "peer": self.tag,
            "engine": engine.name,
            "input_sha256": spec.asset_sha256,
            "upload_seconds": round(self.upload_seconds.get(asset_id, 0.0), 4),
        })

        position = self.pool.submit(job_id, self._execute_job)
        self.emit(FrameType.JOB_ACK, {
            "job_id": job_id,
            "accepted": True,
            "queue_position": position,
            "engine": engine.name,
            "queued_jobs": self.pool.jobs.depth(),
            "active_jobs": self.pool.jobs.active_count(),
        })
        self.emit_log("info", f"job {job_id} accepted on {engine.name} "
                              f"(queue position {position})")

    def _progress_adapter(self, job_id: str):
        """Build the callback an engine uses to report progress.

        The percentage is clamped to be non-decreasing. Progress is produced by two
        threads - the pool worker running the engine and the reader that accepted
        the job - so without this the client can legitimately be shown 2% and then
        0%, which reads as a bug in the GUI even though each report was correct on
        its own.
        """
        state = {"pct": 0.0}
        lock = threading.Lock()

        def adapter(pct: float, stage: str, detail: str = "", **extra) -> None:
            if self.halt.is_set():
                return
            value = max(0.0, min(100.0, float(pct)))
            with lock:
                if value < state["pct"]:
                    value = state["pct"]
                else:
                    state["pct"] = value
            fields = {k: v for k, v in extra.items() if k in _PROGRESS_FIELDS}
            progress = Progress(pct=round(value, 2), stage=stage, detail=detail, **fields)
            self.emit(FrameType.PROGRESS, {"job_id": job_id, **progress.to_dict()})
        return adapter

    # -- execution ---------------------------------------------------------
    def _execute_job(self, job_id: str) -> None:
        ctx = self.jobs.get(job_id)
        engine = self.engines_by_job.get(job_id)
        if ctx is None or engine is None:
            self.pool.jobs.finish(job_id)
            return

        spec = ctx.spec
        result = JobResult(
            job_id=job_id,
            engine=engine.name,
            device=getattr(engine, "device", ""),
            upload_bytes=spec.asset_size,
            upload_seconds=round(self.job_upload_seconds.get(job_id, 0.0), 4),
        )

        started = time.perf_counter()
        self.emit_log("info", f"job {job_id} started on {result.device or 'worker'}")
        outcome: EngineResult
        try:
            outcome = engine.run(ctx)
        except Exception as exc:  # noqa: BLE001 - a crashed engine must not kill the pool
            LOG.exception("engine crash on job %s", job_id)
            outcome = EngineResult(status="error", error=f"{type(exc).__name__}: {exc}")

        result.status = outcome.status or "error"
        result.encoder = outcome.encoder
        result.device = outcome.device or result.device
        result.command = outcome.command
        result.stderr_tail = outcome.stderr_tail
        result.error = outcome.error
        if outcome.metrics:
            self.emit_log("info", "engine metrics: " + json.dumps(outcome.metrics))

        if result.status == "ok" and ctx.output_path.exists():
            result.output_name = ctx.output_path.name
            result.output_size = ctx.output_path.stat().st_size
            result.output_sha256 = sha256_file(ctx.output_path)
        else:
            result.status = result.status or "error"

        result.compute_seconds = round(time.perf_counter() - started, 4)
        result.total_seconds = round(result.compute_seconds + result.upload_seconds, 4)
        result.finished_at = time.time()

        if result.status != "ok":
            ctx.output_path.unlink(missing_ok=True)
            storage.cleanup_job(job_id, keep_output=True)

        self.pool.jobs.record(job_id, result.to_dict())
        self.pool.jobs.finish(job_id)
        self.emit(FrameType.RESULT, result.to_dict())
        suffix = f" - {result.error}" if result.error else ""
        self.emit_log("success" if result.status == "ok" else "error",
                      f"job {job_id} {result.status} in {result.compute_seconds:.2f}s{suffix}")

    def _cancel(self, request: dict) -> None:
        job_id = str(request.get("job_id", ""))
        ctx = self.jobs.get(job_id)
        if ctx is not None and not self.pool.jobs.cancel(job_id):
            ctx.cancel()
            self.emit_log("warn", f"job {job_id} cancellation requested")
        else:
            self.emit_log("warn", f"job {job_id} is unknown or already finished")

    # -- download ----------------------------------------------------------
    def _download(self, request: dict) -> None:
        job_id = str(request.get("job_id", ""))
        record = self.pool.jobs.result(job_id)
        if record is None or record.get("status") != "ok":
            self._fail("no_result", f"job {job_id} has no successful output")
            return
        path = storage.job_dir(job_id) / "output" / record["output_name"]
        if not path.exists():
            self._fail("output_missing", f"{path.name} is no longer on the worker")
            return

        size = path.stat().st_size
        sha = record.get("output_sha256") or sha256_file(path)
        started = time.perf_counter()
        sent = 0

        def ack(done: int, total: int) -> None:
            self.emit(FrameType.UPLOAD_ACK, {
                "job_id": job_id,
                "direction": "download",
                "received": done,
                "size": total,
                "pct": round(done / max(total, 1) * 100.0, 2),
            })

        try:
            # The byte stream must arrive contiguously, so the reader sends it
            # inline while holding the wire lock that the writer respects.
            with self._wire:
                self.sock.settimeout(60.0)
                send_json(self.sock, FrameType.STREAM_BEGIN, {
                    "job_id": job_id,
                    "name": record["output_name"],
                    "size": size,
                    "sha256": sha,
                    "compute_seconds": record.get("compute_seconds", 0.0),
                    "upload_seconds": record.get("upload_seconds", 0.0),
                    "upload_bytes": record.get("upload_bytes", 0),
                })
                with open(path, "rb") as handle:
                    write_stream(self.sock, FrameType.DOWNLOAD_CHUNK, handle, size, on_chunk=ack)
                sent = size
                elapsed = time.perf_counter() - started
                send_json(self.sock, FrameType.STREAM_END, {
                    "job_id": job_id,
                    "sha256": sha,
                    "size": size,
                    "seconds": round(elapsed, 4),
                    "megabits_per_s": round(size * 8 / max(elapsed, 1e-6) / 1e6, 2),
                })
        except (OSError, ProtocolError) as exc:
            self.emit(FrameType.ERROR, {"code": "download_interrupted",
                                        "message": f"sent {sent}/{size} bytes: {exc}"})
            self._end(f"download of {record['output_name']} interrupted at {sent}/{size} bytes")
            return
        finally:
            self.sock.settimeout(30.0)

        self.emit_log("success", f"output sent: {record['output_name']} "
                                 f"({_human_bytes(size)})")


__all__ = ["Session", "ip_allowed"]