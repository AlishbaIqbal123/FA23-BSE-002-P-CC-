"""Capture real screenshots of the client window for the submission.

The images are not mock-ups: this drives the actual ``MainWindow`` against a real
daemon on a loopback port, with a real ffmpeg encode running, and grabs the widget
as it goes. ``QT_QPA_PLATFORM=offscreen`` means it works over SSH and in CI.

    python tools/screenshots.py --out screenshots
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def build_worker(media: Path):
    """Start a daemon in-process and return ``(listener, pool, restore)``."""
    from server import storage
    from server.capabilities import build_capabilities, find_ffmpeg
    from server.job_queue import JobPool, JobQueue
    from server.listener import Listener

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("error: ffmpeg is required to capture a running-job screenshot")

    original = storage.DATA_ROOT
    storage.DATA_ROOT = ROOT / "server" / "data" / "screenshots"
    caps = build_capabilities(max_concurrent_jobs=1, ffmpeg_path=ffmpeg)
    pool = JobPool(JobQueue(), workers=1)
    pool.start()
    listener = Listener("127.0.0.1", 0, caps, pool, ["127.0.0.0/8"])
    listener.bind()
    threading.Thread(target=listener.serve_forever, name="screenshot-accept",
                     daemon=True).start()
    time.sleep(0.2)

    def stop() -> None:
        listener.stop()
        pool.shutdown()
        storage.DATA_ROOT = original

    return listener, stop


def pump(app, predicate, timeout: float, step: float = 0.01) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def grab(window, app, out: Path, name: str) -> Path:
    """Let the layout settle, then save the window as a PNG."""
    for _ in range(6):
        app.processEvents()
        time.sleep(0.05)
    target = out / f"{name}.png"
    window.grab().save(str(target))
    print(f"[shot] {target.relative_to(ROOT)}  {target.stat().st_size / 1024:.0f} KiB")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture client GUI screenshots")
    parser.add_argument("--out", default=str(ROOT / "screenshots"))
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=940)
    args = parser.parse_args(argv)

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from client.ui.main_window import MainWindow
    from server.capabilities import find_ffmpeg
    from tools.make_sample_media import make_clip

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg not found on PATH", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    media = make_clip(ffmpeg, "720p", 20, 30, ROOT / "benchmarks" / "media")

    listener, stop_worker = build_worker(media)
    app = QApplication(sys.argv[:1])
    window = MainWindow()
    window.resize(args.width, args.height)
    window.show()

    try:
        grab(window, app, out, "01-startup")

        window.host_edit.setText("127.0.0.1")
        window.port_edit.setValue(listener.port)
        window._on_connect()
        if not pump(app, lambda: window.submit_btn.isEnabled(), 15.0):
            print("error: the client never reached the online state", file=sys.stderr)
            return 1

        window._set_file(media)
        window.encoder_box.setCurrentIndex(window.encoder_box.findData("libx264"))
        window.preset_box.setCurrentIndex(window.preset_box.findData("veryfast"))
        window.res_box.setCurrentIndex(window.res_box.findData("720p"))
        window._call("do_ping", 20)
        grab(window, app, out, "02-connected-capabilities")

        finished: dict[str, object] = {}
        window.worker.job_finished.connect(lambda s: finished.update(stats=s))
        window._on_submit()
        if not pump(app, lambda: window.job_bar.value() > 0, 20.0):
            print("error: no progress was reported - 03-job-running.png would be a lie",
                  file=sys.stderr)
            return 1
        grab(window, app, out, "03-job-running")

        if not pump(app, lambda: bool(finished), 180.0):
            print("error: the job never finished", file=sys.stderr)
            return 1
        grab(window, app, out, "04-job-complete")

        window.tabs.setCurrentIndex(1)
        grab(window, app, out, "05-compute-tab")

        window._log("info", "worker capabilities negotiated")
        window._log("success", "job j-000123 completed in 3.42 s")
        window._log("warn", "encoder h264_nvenc unavailable - using libx264")
        window._log("error", "example error line: checksum mismatch on asset.bin")
        window.resizeEvent(None)
        grab(window, app, out, "06-log-console")
        return 0
    finally:
        window.shutdown_worker()
        window.deleteLater()
        app.processEvents()
        stop_worker()


if __name__ == "__main__":
    raise SystemExit(main())
