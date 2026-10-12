"""Tests for watch folder automation and Clipchamp integration."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication

from client.watcher import FolderWatcher
from client.ui.main_window import MainWindow


def test_watcher_ignores_existing_files(tmp_path, qapp):
    existing = tmp_path / "old_clip.mp4"
    existing.write_bytes(b"existing video content" * 100)

    watcher = FolderWatcher(tmp_path)
    detected = []
    watcher.video_detected.connect(lambda p, s: detected.append((p, s)))
    watcher.start()

    # Process events and tick
    watcher._scan()
    qapp.processEvents()

    assert len(detected) == 0
    watcher.stop()


def test_watcher_detects_new_stable_video(tmp_path, qapp):
    watcher = FolderWatcher(tmp_path)
    detected = []
    watcher.video_detected.connect(lambda p, s: detected.append((p, s)))
    watcher.start()

    # Create a new video file
    new_video = tmp_path / "clipchamp_export.mp4"
    new_video.write_bytes(b"render frames payload" * 200)

    # First scan registers candidate in _pending_files
    watcher._scan()
    qapp.processEvents()
    assert len(detected) == 0

    # Simulate 1.6s delay for size stabilization
    assert new_video in watcher._pending_files
    size, first_seen = watcher._pending_files[new_video]
    watcher._pending_files[new_video] = (size, first_seen - 2.0)

    # Second scan recognizes file is finished & unlocked
    watcher._scan()
    qapp.processEvents()

    assert len(detected) == 1
    file_path, file_size = detected[0]
    assert Path(file_path).name == "clipchamp_export.mp4"
    assert file_size == new_video.stat().st_size
    watcher.stop()


def test_watcher_ignores_partial_downloads(tmp_path, qapp):
    watcher = FolderWatcher(tmp_path)
    detected = []
    watcher.video_detected.connect(lambda p, s: detected.append((p, s)))
    watcher.start()

    # Temporary files should be skipped
    (tmp_path / "export.mp4.crdownload").write_bytes(b"partial")
    (tmp_path / "export.mp4.part").write_bytes(b"partial")
    (tmp_path / "~temp.mp4").write_bytes(b"partial")

    watcher._scan()
    qapp.processEvents()

    assert len(watcher._pending_files) == 0
    assert len(detected) == 0
    watcher.stop()


def test_main_window_has_watcher_tab_and_responds_to_detection(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("RDO_CLIENT_DATA", str(tmp_path / "client"))
    win = MainWindow()
    try:
        assert win.tabs.count() == 3
        assert "Clipchamp" in win.tabs.tabText(2)
        assert win.watch_folder_edit is not None
        assert win.watcher_status_lbl is not None
        assert win.auto_submit_check.isChecked() is True

        # Simulate video detection
        test_video = tmp_path / "my_project_edit.mp4"
        test_video.write_bytes(b"x" * 1024)

        win._on_video_detected(str(test_video), 1024)
        assert win.file_edit.text() == str(test_video)
        assert "my_project_edit-remote.mp4" in win.out_edit.text()
    finally:
        win.shutdown_worker()
        win.deleteLater()
        qapp.processEvents()
