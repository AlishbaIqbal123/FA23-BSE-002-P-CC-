"""Measure link latency and bulk throughput between the two machines.

This is what separates "the GPU was faster" from "the GPU was faster *after* the
network was accounted for". The report quotes both numbers, and this tool is where
the second one comes from.

    # on the worker machine
    python tools/probe_network.py --listen --port 7600

    # on the client machine
    python tools/probe_network.py --host 192.168.1.1 --port 7600 --mb 256

Latency uses the protocol's own ``PING``/``PONG`` frames, so the number it
reports is the same latency the GUI displays. Throughput pushes a pseudo-random
buffer (incompressible, so no accidental zero-cost from a repeating pattern) and
times the transfer.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.messages import PROTOCOL_VERSION  # noqa: E402
from common.paths import STREAM_CHUNK  # noqa: E402
from common.protocol import (  # noqa: E402
    FrameType,
    recv_exact,
    recv_frame,
    send_bytes,
    send_json,
)

PAYLOAD = bytes(range(256)) * 512  # 128 KiB of incompressible-ish filler


def human(mbps: float) -> str:
    return f"{mbps / 1000:.2f} Gbit/s" if mbps >= 1000 else f"{mbps:.1f} Mbit/s"


# --------------------------------------------------------------------- server
def serve(host: str, port: int) -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(4)
    listener.settimeout(1.0)
    print(f"[probe] listening on {host}:{port} - press Ctrl+C to stop", flush=True)

    def handle(conn: socket.socket, address) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(120.0)
        print(f"[probe] connection from {address[0]}:{address[1]}", flush=True)
        try:
            ftype, _ = recv_frame(conn)
            if ftype is not FrameType.HELLO:
                return
            send_json(conn, FrameType.HELLO_ACK, {
                "protocol_version": PROTOCOL_VERSION,
                "worker_id": "netprobe",
                "capabilities": {},
            })
            while True:
                ftype, payload = recv_frame(conn)
                if ftype is FrameType.PING:
                    request = json.loads(payload.decode())
                    send_json(conn, FrameType.PONG, {
                        "nonce": request.get("nonce", ""),
                        "t0": request.get("t0", 0.0),
                        "server_ms": time.perf_counter() * 1000.0,
                        "ts": time.time(),
                    })
                elif ftype is FrameType.UPLOAD_BEGIN:
                    # Bulk upload: the body is one contiguous raw byte run, so it
                    # is read with recv_exact rather than frame by frame.
                    head = json.loads(payload.decode())
                    conn.settimeout(300.0)
                    remaining = int(head.get("size", 0))
                    received = 0
                    started = time.perf_counter()
                    while remaining > 0:
                        block = recv_exact(conn, min(1 << 20, remaining))
                        received += len(block)
                        remaining -= len(block)
                    while True:
                        ftype, tail = recv_frame(conn)
                        if ftype is FrameType.UPLOAD_END:
                            break
                    elapsed = time.perf_counter() - started
                    mbps = received * 8 / max(elapsed, 1e-9) / 1e6
                    print(f"[probe]   upload {received / 1e6:.1f} MB in {elapsed:.2f}s "
                          f"= {human(mbps)}", flush=True)
                    send_json(conn, FrameType.UPLOAD_DONE, {
                        "asset_id": head.get("asset_id", "probe"), "name": "probe.bin",
                        "size": received, "sha256": "", "seconds": round(elapsed, 4),
                        "megabits_per_s": round(mbps, 2),
                    })
                    conn.settimeout(120.0)
        except Exception as exc:  # noqa: BLE001 - a probe run must not traceback
            print(f"[probe] connection ended: {type(exc).__name__}: {exc}", flush=True)
        finally:
            conn.close()

    try:
        while True:
            try:
                conn, address = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(conn, address), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[probe] stopped", flush=True)
    finally:
        listener.close()
    return 0


# --------------------------------------------------------------------- client
def probe(host: str, port: int, count: int, megabytes: int) -> int:
    samples: list[float] = []
    sock = socket.create_connection((host, port), timeout=8)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(60.0)
    send_json(sock, FrameType.HELLO, {
        "magic": "RDO1", "protocol_version": PROTOCOL_VERSION, "client": "netprobe",
    })
    recv_frame(sock)

    print(f"[probe] latency: {count} round trips")
    for _ in range(count):
        sent = time.perf_counter()
        send_json(sock, FrameType.PING, {"nonce": "x", "t0": sent * 1000.0})
        while True:
            ftype, payload = recv_frame(sock)
            if ftype is FrameType.PONG:
                samples.append((time.perf_counter() - sent) * 1000.0)
                break
        time.sleep(0.01)

    total = megabytes * 1024 * 1024
    send_json(sock, FrameType.UPLOAD_BEGIN, {
        "asset_id": "probe", "name": "probe.bin", "size": total, "sha256": "",
    })
    print(f"[probe] throughput: pushing {megabytes} MiB")
    started = time.perf_counter()
    sent_bytes = 0
    while sent_bytes < total:
        block = PAYLOAD[: min(STREAM_CHUNK, total - sent_bytes)]
        send_bytes(sock, block)
        sent_bytes += len(block)
    client_elapsed = time.perf_counter() - started
    send_json(sock, FrameType.UPLOAD_END, {"asset_id": "probe", "size": total})
    server_seconds = 0.0
    while True:
        ftype, payload = recv_frame(sock)
        if ftype is FrameType.UPLOAD_DONE:
            server_seconds = float(json.loads(payload.decode()).get("seconds", 0.0))
            break
    sock.close()

    client_mbps = total * 8 / max(client_elapsed, 1e-9) / 1e6
    server_mbps = total * 8 / max(server_seconds, 1e-9) / 1e6 if server_seconds else client_mbps
    result = {
        "latency_ms": {
            "min": round(min(samples), 3),
            "avg": round(statistics.fmean(samples), 3),
            "max": round(max(samples), 3),
            "stdev": round(statistics.pstdev(samples), 3),
        },
        "throughput_mbits": {
            "bytes": total,
            "client_seconds": round(client_elapsed, 4),
            "server_seconds": round(server_seconds, 4),
            "client_mbits": round(client_mbps, 2),
            "server_mbits": round(server_mbps, 2),
        },
    }
    print("\n" + json.dumps(result, indent=2))
    print(f"\n  latency  avg {result['latency_ms']['avg']:.2f} ms  "
          f"(min {result['latency_ms']['min']:.2f} / max {result['latency_ms']['max']:.2f})")
    print(f"  uplink   {human(client_mbps)} measured at the sender")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Latency and throughput probe")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7600)
    parser.add_argument("--listen", action="store_true", help="act as the responder")
    parser.add_argument("--count", type=int, default=50, help="latency samples")
    parser.add_argument("--mb", type=int, default=64, help="MiB to push")
    args = parser.parse_args(argv)
    if args.listen:
        return serve(args.host, args.port)
    return probe(args.host, args.port, args.count, args.mb)


if __name__ == "__main__":
    raise SystemExit(main())