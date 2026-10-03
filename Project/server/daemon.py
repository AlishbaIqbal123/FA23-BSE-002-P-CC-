"""Remote GPU worker daemon - entry point.

    python server/daemon.py --host 0.0.0.0 --port 7575 --workers 2

Start this on the machine that owns the GPU. It runs headless: no window, no
console interaction, just a listening socket and a job pool.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.messages import GPU_ENCODERS  # noqa: E402
from common.paths import DEFAULT_ALLOWED_SUBNETS, DEFAULT_HOST, DEFAULT_PORT  # noqa: E402
from common.protocol import PROTOCOL_VERSION  # noqa: E402
from server import storage  # noqa: E402
from server.capabilities import build_capabilities  # noqa: E402
from server.job_queue import JobPool, JobQueue  # noqa: E402
from server.listener import Listener  # noqa: E402

LOG = logging.getLogger("rdo.daemon")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("rdo.session").setLevel(logging.DEBUG if verbose else logging.INFO)


def local_addresses() -> list[str]:
    found = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.168.1.1", 9))
        found.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(found)


def banner(caps, port: int, workers: int, allowed: list[str]) -> str:
    lines = [
        "",
        "=" * 74,
        "  Remote GPU Worker Daemon  -  Distributed Task Offloading System",
        "=" * 74,
        f"  worker id      : {caps.worker_id}",
        f"  hostname       : {caps.hostname}",
        f"  os             : {caps.os}",
        f"  python         : {caps.python}",
        f"  protocol       : {PROTOCOL_VERSION}",
        f"  listening on   : 0.0.0.0:{port}   (all interfaces)",
        f"  local addresses: {', '.join(local_addresses()) or 'none detected'}",
        f"  allowed subnets: {', '.join(allowed)}",
        f"  job workers    : {workers}",
        "-" * 74,
        f"  ffmpeg         : {caps.ffmpeg_path or 'NOT FOUND'}",
        f"  ffmpeg build   : {caps.ffmpeg_version or 'n/a'}",
        f"  gpu            : {caps.gpu_name or 'none detected'}",
        f"  NVENC          : {'available' if caps.nvenc_available else 'unavailable'}",
        f"  encoders       : {', '.join(caps.hardware_encoders) or 'none'}",
        f"  pytorch        : {'yes' if caps.torch_available else 'no'}"
        + (f"  cuda: {caps.cuda_device}" if caps.cuda_available else ""),
        f"  engines        : {', '.join(caps.engines) or 'none'}",
        f"  cpu cores      : {caps.cpu_count}",
        "-" * 74,
        "  Hardware accelerators this build can use:",
    ]
    for name in caps.hardware_encoders:
        lines.append(f"    - {name:<14} {GPU_ENCODERS.get(name, 'unlabelled encoder')}")
    lines += [
        "=" * 74,
        "  Waiting for clients. Press Ctrl+C to stop.",
        "",
    ]
    return "\n".join(lines)


def write_state_file(port: int, caps, workers: int) -> Path:
    path = Path(__file__).resolve().parent / "data" / "daemon.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": os.getpid(),
        "port": port,
        "worker_id": caps.worker_id,
        "gpu": caps.gpu_name,
        "nvenc": caps.nvenc_available,
        "workers": workers,
        "started_at": time.time(),
        "protocol_version": PROTOCOL_VERSION,
    }, indent=2), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daemon.py",
        description="Remote GPU worker daemon for the Distributed Task Offloading System",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP port (default {DEFAULT_PORT})")
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent job workers; 2+ only helps for the "
                             "compute engine, since NVENC is a shared block")
    parser.add_argument("--allow", action="append", metavar="CIDR",
                        help="subnet permitted to connect; repeatable "
                             f"(default {','.join(DEFAULT_ALLOWED_SUBNETS)})")
    parser.add_argument("--any-client", action="store_true",
                        help="accept connections from any address (use only on a "
                             "trusted isolated network)")
    parser.add_argument("--ffmpeg", default=None, help="path to the ffmpeg binary")
    parser.add_argument("--keep-jobs", type=int, default=25,
                        help="how many finished job directories to retain")
    parser.add_argument("--prune-on-start", action="store_true",
                        help="delete stale job directories at start-up")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--no-banner", action="store_true", help="suppress the start-up banner")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    allowed = ["0.0.0.0/0", "::/0"] if args.any_client else list(args.allow or DEFAULT_ALLOWED_SUBNETS)

    caps = build_capabilities(max_concurrent_jobs=args.workers, ffmpeg_path=args.ffmpeg)
    storage.ensure_dirs()
    if args.prune_on_start:
        removed = storage.prune(keep_jobs=0)
        LOG.info("pruned %d stale job directory(ies)", removed)

    if not caps.engines:
        LOG.error("no execution engine is available - install ffmpeg on this machine")
        return 2
    if "transcode" not in caps.engines:
        LOG.warning("no usable video encoder found; the transcode engine is disabled")

    jobs = JobQueue()
    pool = JobPool(jobs, args.workers)
    listener = Listener(args.host, args.port, caps, pool, allowed)

    try:
        listener.bind()
    except OSError as exc:
        LOG.error("cannot bind %s:%d - %s", args.host, args.port, exc)
        if getattr(exc, "errno", None) in (48, 98):
            LOG.error("port %d is already in use; choose another with --port", args.port)
        return 1

    pool.start()
    state = write_state_file(listener.port, caps, args.workers)
    if not args.no_banner:
        print(banner(caps, listener.port, args.workers, allowed), flush=True)
    LOG.info("state written to %s", state)

    stopping = threading.Event()

    def handle_signal(signum, _frame) -> None:
        LOG.info("received %s, shutting down", signal.Signals(signum).name)
        stopping.set()
        listener.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (OSError, ValueError):
            pass

    serve = threading.Thread(target=listener.serve_forever, name="AcceptLoop", daemon=True)
    serve.start()
    try:
        while not stopping.is_set():
            stopping.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        pool.shutdown()
        LOG.info("daemon stopped cleanly (peak %d concurrent connection(s))",
                 listener.total_connections)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())