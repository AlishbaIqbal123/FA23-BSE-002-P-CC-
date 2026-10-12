"""Watch folder automation for video editing pipelines (e.g. Microsoft Clipchamp, Premiere, CapCut).

Monitors a folder for newly exported video files, waits until the export
has finished writing to disk, and automatically loads or offloads the file
to the remote GPU worker server.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts"})


class FolderWatcher(QObject):
    """Monitors a directory for newly finished video exports."""

    video_detected = pyqtSignal(str, int)  # file_path, file_size
    watching_state = pyqtSignal(bool, str)  # active, folder_path

    def __init__(self, watch_dir: str | Path | None = None) -> None:
        super().__init__()
        self.watch_dir = Path(watch_dir) if watch_dir else Path.home() / "Downloads"
        self._enabled = False
        self._seen_files: set[Path] = set()
        self._pending_files: dict[Path, tuple[int, float]] = {}  # path -> (last_size, first_seen_time)

        self._timer = QTimer(self)
        self._timer.setInterval(1200)  # poll every 1.2s
        self._timer.timeout.connect(self._scan)

    def set_watch_dir(self, path: str | Path) -> None:
        p = Path(path)
        if p.exists() and p.is_dir():
            self.watch_dir = p
            self._init_seen()
            self.watching_state.emit(self._enabled, str(self.watch_dir))

    def _init_seen(self) -> None:
        """Record all existing files so we only trigger on newly created exports."""
        self._seen_files.clear()
        self._pending_files.clear()
        if not self.watch_dir.exists():
            return
        try:
            for item in self.watch_dir.iterdir():
                if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                    self._seen_files.add(item.resolve())
        except OSError:
            pass

    def start(self) -> None:
        self._enabled = True
        self._init_seen()
        self._timer.start()
        self.watching_state.emit(True, str(self.watch_dir))

    def stop(self) -> None:
        self._enabled = False
        self._timer.stop()
        self.watching_state.emit(False, str(self.watch_dir))

    def is_active(self) -> bool:
        return self._enabled

    @pyqtSlot()
    def _scan(self) -> None:
        if not self._enabled or not self.watch_dir.exists():
            return

        current_candidates: set[Path] = set()
        try:
            for item in self.watch_dir.iterdir():
                if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                    # Ignore temporary / partial download files
                    lower_name = item.name.lower()
                    if (
                        lower_name.endswith(".part")
                        or lower_name.endswith(".crdownload")
                        or lower_name.endswith(".tmp")
                        or lower_name.startswith("~")
                    ):
                        continue
                    current_candidates.add(item.resolve())
        except OSError:
            return

        # Check for new files not previously seen
        now = time.time()
        for cand in current_candidates:
            if cand not in self._seen_files:
                try:
                    stat = cand.stat()
                    current_size = stat.st_size
                except OSError:
                    continue

                if cand not in self._pending_files:
                    # First time seeing this new file
                    self._pending_files[cand] = (current_size, now)
                else:
                    last_size, first_seen = self._pending_files[cand]
                    # Check if file size has stabilized for at least 1.5s
                    if current_size > 0 and current_size == last_size and (now - first_seen >= 1.5):
                        # File write is complete! Test if we can open it for reading
                        try:
                            with open(cand, "rb") as test_f:
                                test_f.read(1024)
                            # Fully written and unlocked by editor!
                            self._seen_files.add(cand)
                            del self._pending_files[cand]
                            self.video_detected.emit(str(cand), current_size)
                        except (OSError, PermissionError):
                            # Still locked by writer, retry next tick
                            self._pending_files[cand] = (current_size, now)
                    else:
                        # File is still growing
                        self._pending_files[cand] = (current_size, now if current_size != last_size else first_seen)
