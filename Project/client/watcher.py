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
IGNORED_PATTERNS = (".part", ".crdownload", ".tmp", ".download")


class FolderWatcher(QObject):
    """Monitors a directory for newly finished video exports."""

    video_detected = pyqtSignal(str, int)  # file_path, file_size
    watching_state = pyqtSignal(bool, str)  # active, folder_path

    def __init__(self, watch_dir: str | Path | None = None) -> None:
        super().__init__()
        self.watch_dir = Path(watch_dir) if watch_dir else Path.home() / "Downloads"
        self._enabled = False
        # path -> (last_size, last_mtime)
        self._file_snapshots: dict[Path, tuple[int, float]] = {}
        # path -> (last_size, first_seen_time)
        self._pending_files: dict[Path, tuple[int, float]] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(1000)  # poll every 1.0s
        self._timer.timeout.connect(self._scan)

    @property
    def _seen_files(self) -> set[Path]:
        """Backward-compatible view of resolved paths that have been seen."""
        return set(self._file_snapshots.keys())

    def set_watch_dir(self, path: str | Path) -> None:
        p = Path(path)
        if p.exists() and p.is_dir():
            self.watch_dir = p
            self._init_seen()
            self.watching_state.emit(self._enabled, str(self.watch_dir))

    def _init_seen(self) -> None:
        """Record all existing files so we only trigger on newly created or updated exports."""
        self._file_snapshots.clear()
        self._pending_files.clear()
        if not self.watch_dir.exists():
            return
        try:
            for item in self.watch_dir.iterdir():
                if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                    try:
                        st = item.stat()
                        self._file_snapshots[item.resolve()] = (st.st_size, st.st_mtime)
                    except OSError:
                        pass
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

    def scan_latest_video(self, max_age_seconds: float = 0.0) -> Path | None:
        """Find and immediately emit the most recently modified video file in watch_dir.

        If max_age_seconds > 0, ignores files older than max_age_seconds.
        If max_age_seconds <= 0, picks the newest video in the directory regardless of age.
        """
        search_dirs = [self.watch_dir]
        if self.watch_dir == Path.home() / "Downloads":
            vid_dir = Path.home() / "Videos"
            if vid_dir.exists() and vid_dir.is_dir():
                search_dirs.append(vid_dir)

        candidates: list[tuple[Path, int, float]] = []
        for sdir in search_dirs:
            if not sdir.exists():
                continue
            try:
                for item in sdir.iterdir():
                    if not item.is_file():
                        continue
                    lower_name = item.name.lower()
                    if (
                        any(lower_name.endswith(ext) for ext in IGNORED_PATTERNS)
                        or lower_name.startswith("~")
                        or lower_name.startswith(".")
                    ):
                        continue
                    if item.suffix.lower() in VIDEO_EXTENSIONS:
                        try:
                            st = item.stat()
                            if st.st_size > 0:
                                candidates.append((item.resolve(), st.st_size, st.st_mtime))
                        except OSError:
                            pass
            except OSError:
                continue

        if not candidates:
            return None

        # Sort by mtime descending (most recently modified/exported first)
        candidates.sort(key=lambda x: x[2], reverse=True)
        newest, size, mtime = candidates[0]

        now = time.time()
        if max_age_seconds > 0 and (now - mtime) > max_age_seconds:
            return None

        try:
            with open(newest, "rb") as f:
                f.read(4096)
        except (OSError, PermissionError):
            return None

        self._file_snapshots[newest] = (size, mtime)
        self._pending_files.pop(newest, None)
        self.video_detected.emit(str(newest), size)
        return newest

    @pyqtSlot()
    def _scan(self) -> None:
        if not self._enabled or not self.watch_dir.exists():
            return

        current_candidates: dict[Path, tuple[int, float]] = {}
        try:
            for item in self.watch_dir.iterdir():
                if not item.is_file():
                    continue
                lower_name = item.name.lower()
                # Ignore temporary / partial download files
                if (
                    any(lower_name.endswith(ext) for ext in IGNORED_PATTERNS)
                    or lower_name.startswith("~")
                    or lower_name.startswith(".")
                ):
                    continue
                if item.suffix.lower() in VIDEO_EXTENSIONS:
                    try:
                        st = item.stat()
                        current_candidates[item.resolve()] = (st.st_size, st.st_mtime)
                    except OSError:
                        continue
        except OSError:
            return

        now = time.time()
        for cand, (current_size, current_mtime) in current_candidates.items():
            prev = self._file_snapshots.get(cand)
            is_new = prev is None
            is_modified = prev is not None and (
                current_size != prev[0] or abs(current_mtime - prev[1]) > 0.05
            )

            if not (is_new or is_modified):
                continue

            if cand not in self._pending_files:
                # First time seeing this new or modified file
                self._pending_files[cand] = (current_size, now)
            else:
                last_size, first_seen = self._pending_files[cand]
                # Check if file size has stabilized for at least 0.8s
                if current_size > 0 and current_size == last_size and (now - first_seen >= 0.8):
                    # File write is complete! Test if we can open it for reading
                    try:
                        with open(cand, "rb") as test_f:
                            test_f.read(4096)
                        # Fully written and unlocked by editor/browser!
                        self._file_snapshots[cand] = (current_size, current_mtime)
                        del self._pending_files[cand]
                        self.video_detected.emit(str(cand), current_size)
                    except (OSError, PermissionError):
                        # Still locked by writer/antivirus, retry next tick without resetting first_seen
                        pass
                else:
                    # File is still growing or recently modified
                    self._pending_files[cand] = (
                        current_size,
                        now if current_size != last_size else first_seen,
                    )
