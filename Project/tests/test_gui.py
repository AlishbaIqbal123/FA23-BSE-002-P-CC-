"""GUI and worker-thread behaviour, exercised headlessly.

Two things are worth proving here and neither is visible in a unit test of the
transport:

* the window builds and its widgets are wired to real signals, and
* blocking work really happens on the worker thread. That second point is the
  one that regresses silently - calling ``worker.do_submit()`` directly instead of
  posting to its event loop still passes every functional test while freezing the
  interface for the whole transfer.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt, QTimer  # noqa: E402

from client.ui.main_window import MainWindow  # noqa: E402
from server import storage  # noqa: E402
from server.capabilities import build_capabilities, find_ffmpeg  # noqa: E402
from server.job_queue import JobPool, JobQueue  # noqa: E402
from server.listener import Listener  # noqa: E402
from tools.make_sample_media import make_clip  # noqa: E402

FFMPEG = find_ffmpeg()


def pump(app, predicate, timeout: float = 20.0, step: float = 0.01) -> bool:
    """Spin the Qt event loop until ``predicate`` is true or time runs out."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(step)
    app.processEvents()
    return bool(predicate())


@pytest.fixture
def daemon(tmp_path, request):
    """A loopback worker with isolated storage, or skip without ffmpeg."""
    if not FFMPEG:
        pytest.skip("ffmpeg is required for the GUI integration tests")
    original = storage.DATA_ROOT
    storage.DATA_ROOT = tmp_path / "jobs"
    caps = build_capabilities(max_concurrent_jobs=1, ffmpeg_path=FFMPEG)
    pool = JobPool(JobQueue(), workers=1)
    pool.start()
    listener = Listener("127.0.0.1", 0, caps, pool, ["127.0.0.0/8"])
    listener.bind()
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.15)
    try:
        yield listener
    finally:
        listener.stop()
        pool.shutdown()
        storage.DATA_ROOT = original


@pytest.fixture
def window(qapp, daemon):
    win = MainWindow()
    try:
        yield win
    finally:
        win.shutdown_worker()
        win.deleteLater()
        qapp.processEvents()


class TestWindowConstruction:
    def test_the_window_builds_without_a_worker(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("RDO_CLIENT_DATA", str(tmp_path / "client"))
        win = MainWindow()
        assert win.windowTitle()
        assert win.terminal is not None
        win.shutdown_worker()
        win.deleteLater()
        qapp.processEvents()

    def test_progress_bars_start_empty_and_nothing_can_be_submitted_yet(self, window):
        for bar in (window.transfer_bar, window.job_bar):
            assert bar.minimum() == 0
            assert bar.maximum() == 100
            assert bar.value() == 0
        assert window.submit_btn.isEnabled() is False
        assert window.cancel_btn.isEnabled() is False
        assert window.disconnect_btn.isEnabled() is False

    def test_the_quality_controls_map_onto_the_spec(self, window):
        window.tabs.setCurrentIndex(0)
        window.crf_check.setChecked(False)
        window.crf_box.setValue(18)
        spec = window._current_spec()
        assert spec.job_type == "transcode"
        assert spec.crf == 18
        assert spec.bitrate_kbps == 0

        window.crf_check.setChecked(True)
        window.bitrate_box.setValue(4500)
        spec = window._current_spec()
        assert spec.bitrate_kbps == 4500
        assert spec.crf == 0

        # The widget keeps CRF inside the legal range by itself.
        window.crf_box.setValue(99)
        assert window.crf_box.value() == 51

    def test_a_missing_input_file_is_refused(self, window):
        window.file_edit.setText("C:/definitely/not/here.mp4")
        window._on_submit()
        assert "does not exist" in window.terminal.toPlainText()

    def test_submitting_while_offline_is_refused(self, window, qapp, daemon):
        window.file_edit.setText("C:/tmp/clip.mp4")
        window._on_submit()
        assert window.worker.transport.is_open is False
        assert window.transfer_bar.value() == 0


class TestWorkerThreading:
    def test_the_worker_lives_in_its_own_thread(self, window, qapp):
        assert window.worker is not None
        assert window.worker.thread() is window.thread
        assert window.thread is not qapp.thread()

    def test_connecting_uses_the_address_typed_into_the_window(self, window, qapp,
                                                               daemon):
        # Regression guard: the worker used to dial whatever address its transport
        # was constructed with, silently ignoring the GUI fields.
        window.host_edit.setText("127.0.0.1")
        window.port_edit.setValue(daemon.port)
        assert window.worker.transport.port != daemon.port, "test is not meaningful"

        window._on_connect()
        assert pump(qapp, lambda: window.worker.transport.is_open, timeout=15.0)
        assert window.worker.transport.port == daemon.port
        # Submitting only unlocks once the handshake has landed.
        assert pump(qapp, window.submit_btn.isEnabled, timeout=15.0)

    def test_a_scheduled_call_runs_on_the_worker_thread(self, window, qapp, daemon):
        seen: dict[str, object] = {}
        # DirectConnection, so this really runs in the *emitting* thread. A plain
        # connection would be queued to the GUI thread and prove nothing.
        window.worker.state_changed.connect(
            lambda state, detail: seen.update(state=state,
                                              thread=threading.get_ident()),
            Qt.ConnectionType.DirectConnection,
        )
        window.host_edit.setText("127.0.0.1")
        window.port_edit.setValue(daemon.port)

        gui_thread = threading.get_ident()
        window._on_connect()

        assert pump(qapp, lambda: seen.get("state") == "online", timeout=15.0)
        assert seen["thread"] != gui_thread

    def test_the_gui_event_loop_keeps_running_during_a_job(self, window, qapp, daemon,
                                                          tmp_path):
        clip = make_clip(FFMPEG, "360p", 2, 15, tmp_path / "media")
        window.host_edit.setText("127.0.0.1")
        window.port_edit.setValue(daemon.port)
        window._on_connect()
        assert pump(qapp, window.submit_btn.isEnabled, timeout=15.0)

        window.file_edit.setText(str(clip))
        window.preset_box.setCurrentIndex(window.preset_box.findData("ultrafast"))
        window.res_box.setCurrentIndex(window.res_box.findData("360p"))
        # Capabilities must have moved the encoder selection off the disabled
        # NVENC entry, otherwise this job is rejected by the worker.
        assert window.encoder_box.currentData() == "libx264"

        ticks: list[int] = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(5)

        finished: dict[str, object] = {}
        window.worker.job_finished.connect(lambda stats: finished.update(stats=stats))
        window._on_submit()

        ok = pump(qapp, lambda: bool(finished), timeout=90.0)
        timer.stop()
        assert ok, f"job never finished:\n{window.terminal.toPlainText()[-800:]}"

        stats = finished["stats"]
        assert stats["engine"] == "ffmpeg"
        assert stats["verified"] is True
        # A frozen interface would starve this timer while the job ran.
        assert len(ticks) > 5, "the GUI event loop was blocked during the job"