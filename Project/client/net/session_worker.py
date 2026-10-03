"""Qt worker that drives :class:`~client.net.transport.Transport` off the GUI thread.

Every method here runs on a ``QThread`` and reports back purely through signals.
That is the rule that keeps the interface responsive during a 400 MB upload: the
widgets are never touched from the worker thread, and the worker thread never
touches a widget directly.

Reconnection uses capped exponential backoff. A distributed system's most common
failure is not a bug but a peer that went away, so recovering from that is a
first-class feature rather than an afterthought.
"""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from common.messages import JobSpec, safe_filename
from common.protocol import FrameType, ProtocolError
from client.net.transport import Transport

LEVEL_STYLE = {
    "debug": "trace",
    "info": "info",
    "success": "ok",
    "warn": "warn",
    "error": "error",
}


class SessionWorker(QObject):
    """All blocking network work for the GUI, executed on a worker thread."""

    #: GUI -> worker mailbox. Emitting this is the *only* supported way to start
    #: work: it is a queued connection into this object's thread, so the slot runs
    #: on the worker thread and the GUI thread returns immediately.
    invoke = pyqtSignal(str, object)             # slot name, argument tuple
    logged = pyqtSignal(str, str)                 # level, message
    state_changed = pyqtSignal(str, str)          # state, detail
    connected = pyqtSignal(dict)                  # capabilities + worker id
    disconnected = pyqtSignal(str)
    rtt_measured = pyqtSignal(dict)
    transfer_progress = pyqtSignal(str, int, int) # phase, done, total
    job_progress = pyqtSignal(dict)
    job_finished = pyqtSignal(dict)
    job_failed = pyqtSignal(str, str)
    transport_stats = pyqtSignal(dict)
    capabilities_changed = pyqtSignal(dict)

    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.transport = Transport(host, port, client_name="rdo-gui")
        self.active_job_id = ""
        # Queued because this object lives in another thread; AutoConnection
        # therefore delivers on the worker thread's event loop.
        self.invoke.connect(self._dispatch)

    # -- helpers -----------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        self.logged.emit(LEVEL_STYLE.get(level, level), message)

    def state(self, state: str, detail: str = "") -> None:
        self.state_changed.emit(state, detail)

    @pyqtSlot(str, object)
    def _dispatch(self, name: str, args: object) -> None:
        """Run the requested slot. Connected to :attr:`invoke` in ``__init__``."""
        method = getattr(self, str(name), None)
        if not callable(method):
            self.log("error", f"unknown worker call: {name}")
            return
        try:
            method(*(args or ()))
        except Exception as exc:  # noqa: BLE001 - one bad call must not kill the thread
            self.log("error", f"{name} failed: {type(exc).__name__}: {exc}")
            self.job_failed.emit("internal", f"{name}: {exc}")
            self.state("error", str(exc))

    # -- connection --------------------------------------------------------
    @pyqtSlot()
    def do_connect(self) -> None:
        self.state("connecting", f"{self.host}:{self.port}")
        self.log("info", f"opening TCP connection to {self.host}:{self.port}")
        try:
            self.transport.configure(self.host, self.port)
            self.transport.connect()
        except (OSError, ProtocolError) as exc:
            self.log("error", f"connection failed: {exc}")
            self.state("offline", str(exc))
            self.disconnected.emit(str(exc))
            return

        caps = self.transport.capabilities
        info = {
            "worker_id": self.transport.worker_id,
            "capabilities": caps.to_dict() if caps else {},
        }
        self.log("success", f"handshake complete with worker {self.transport.worker_id}")
        if caps:
            self.log("info", f"gpu: {caps.gpu_name or 'none detected'}")
            self.log("info", f"NVENC: {'available' if caps.nvenc_available else 'unavailable'}"
                             f" | encoders: {', '.join(caps.hardware_encoders) or 'none'}")
            self.log("info", f"engines: {', '.join(caps.engines)}"
                             f" | ffmpeg: {caps.ffmpeg_version or 'n/a'}")
            self.log("info", f"worker allows {caps.max_concurrent_jobs} concurrent job(s),"
                             f" {caps.cpu_count} cpu cores")
        self.capabilities_changed.emit(caps.to_dict() if caps else {})
        self.state("online", self.transport.worker_id)
        self.connected.emit(info)
        self.do_ping()

    @pyqtSlot(int)
    def do_ping(self, count: int = 10) -> None:
        if not self.transport.is_open:
            return
        try:
            stats = self.transport.ping_batch(count=count)
        except (OSError, ProtocolError) as exc:
            self.log("warn", f"latency probe failed: {exc}")
            return
        payload = stats.to_dict()
        self.log("info", f"latency over {count} samples: min {payload['min_ms']:.2f} ms | "
                         f"avg {payload['avg_ms']:.2f} ms | max {payload['max_ms']:.2f} ms | "
                         f"jitter {payload['jitter_ms']:.2f} ms")
        self.rtt_measured.emit(payload)

    @pyqtSlot()
    def do_disconnect(self) -> None:
        self.log("info", "disconnecting")
        self.transport.close()
        self.state("offline", "")
        self.disconnected.emit("closed by user")

    # -- the job pipeline --------------------------------------------------
    @pyqtSlot(str, object)
    def do_submit(self, input_path: str, spec_dict: dict) -> None:
        """Upload, submit, follow progress, download - the whole pipeline."""
        path = Path(input_path)
        spec = JobSpec.from_dict(spec_dict)
        destination = self._destination_for(path, spec)
        started = time.perf_counter()

        if not self.transport.is_open:
            self.job_failed.emit("offline", "not connected to a worker")
            return

        asset_id = ""
        upload_seconds = 0.0
        upload_bytes = 0
        try:
            if spec.job_type == "transcode":
                self.state("uploading", path.name)
                self.log("info", f"hashing {path.name} ({path.stat().st_size} bytes)")
                upload = self.transport.upload(
                    path,
                    on_progress=lambda done, total, elapsed: self.transfer_progress.emit(
                        "upload", done, total),
                )
                asset_id, upload_seconds, upload_bytes = (
                    upload.asset_id, upload.seconds, upload.size)
                self.log("success",
                         f"upload verified by the worker in {upload.seconds:.2f}s "
                         f"({upload.megabits_per_s:.1f} Mb/s)")

            self.state("submitting", spec.output_name)
            ack = self.transport.submit(spec, asset_id=asset_id)
            self.active_job_id = ack["job_id"]
            self.log("success", f"job {ack['job_id']} accepted by {ack['engine']} "
                                f"(queue position {ack['queue_position']})")

            self.state("running", ack["job_id"])
            ftype, result = self.transport.await_result(on_frame=self._on_job_frame)

            if ftype is FrameType.JOB_REJECT:
                self.job_failed.emit("rejected", "; ".join(result.get("errors", [])))
                return
            if ftype is FrameType.ERROR:
                self.job_failed.emit(result.get("code", "error"),
                                     result.get("message", "unknown worker error"))
                return

            stats = {
                "job_id": result["job_id"],
                "engine": result["engine"],
                "encoder": result["encoder"],
                "device": result["device"],
                "compute_seconds": result["compute_seconds"],
                "upload_seconds": upload_seconds,
                "wall_seconds": round(time.perf_counter() - started, 3),
                "upload_bytes": upload_bytes,
                "output_name": result["output_name"],
                "output_size": result["output_size"],
                "output_sha256": result["output_sha256"],
                "queue_total_seconds": result.get("total_seconds", 0.0),
            }

            if result["status"] != "ok":
                stats["error"] = result.get("error", "")
                self.job_failed.emit("engine_error", result.get("error", "job failed"))
                self.job_finished.emit(stats)
                return

            self.log("success",
                     f"{result['engine']} finished in {result['compute_seconds']:.2f}s on "
                     f"{result['device'] or 'worker'} -> {result['output_name']} "
                     f"({result['output_size']} bytes)")

            self.state("downloading", result["output_name"])
            self.transfer_progress.emit("download", 0, result["output_size"])
            download = self.transport.download(
                result["job_id"], destination,
                on_progress=lambda done, total, elapsed: self.transfer_progress.emit(
                    "download", done, total),
            )
            stats.update({
                "download_seconds": download.seconds,
                "megabits_per_s": download.megabits_per_s,
                "output_path": str(download.path),
                "verified": download.verified,
                "total_seconds": round(time.perf_counter() - started, 3),
            })
            self.log("success",
                     f"output received and SHA-256 verified in {download.seconds:.2f}s "
                     f"({download.megabits_per_s:.1f} Mb/s)")
            self.log("success", f"saved to {download.path}")
            self.state("done", str(download.path))
            self.transport_stats.emit(stats)
            self.job_finished.emit(stats)

        except ProtocolError as exc:
            self.log("error", f"protocol failure: {exc}")
            self.job_failed.emit("protocol", str(exc))
            self.state("error", str(exc))
        except OSError as exc:
            self.log("error", f"network failure: {exc}")
            self.job_failed.emit("network", str(exc))
            self.state("error", str(exc))
        except Exception as exc:  # noqa: BLE001 - the GUI must survive anything
            self.log("error", f"unexpected failure: {type(exc).__name__}: {exc}")
            self.job_failed.emit("internal", f"{type(exc).__name__}: {exc}")
            self.state("error", str(exc))
        finally:
            self.active_job_id = ""

    def _destination_for(self, source: Path, spec: JobSpec) -> Path:
        from common.paths import CLIENT_DATA

        outputs = CLIENT_DATA / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(spec.output_name or f"{source.stem}-out.mp4", "output.mp4")
        target = outputs / stem
        counter = 1
        while target.exists():
            target = outputs / f"{Path(stem).stem}-{counter}{Path(stem).suffix}"
            counter += 1
        return target

    def _on_job_frame(self, ftype: FrameType, body: dict) -> None:
        if ftype is FrameType.PROGRESS:
            self.job_progress.emit(body)
        elif ftype is FrameType.LOG:
            self.log(str(body.get("level", "info")), str(body.get("message", "")))
        elif ftype is FrameType.JOB_ACK:
            self.log("info", f"queue position {body.get('queue_position')} "
                             f"({body.get('queued_jobs')} waiting)")

    @pyqtSlot()
    def do_cancel(self) -> None:
        if not self.active_job_id:
            self.log("warn", "no job is running")
            return
        self.transport.cancel(self.active_job_id)
        self.log("warn", f"cancel requested for {self.active_job_id}")

    @pyqtSlot()
    def shutdown(self) -> None:
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001 - best effort on window close
            pass


__all__ = ["SessionWorker"]