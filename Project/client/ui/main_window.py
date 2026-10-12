"""Main application window.

Layout::

    +------------------------------------------------------------------+
    |  title bar: "Remote GPU Rendering"  + worker state pill            |
    +----------------+-------------------------------------------------+
    |  CONNECTION    |   JOB CONFIGURATION                             |
    |  host / port   |   input file (+ drag & drop)                    |
    |  connect/disc  |   job type, encoder, resolution, preset, bitrate |
    |  RTT metrics   |   output name                                   |
    |  capabilities  +-------------------------------------------------+
    |                |   TRANSFER / EXECUTION progress bars            |
    +----------------+-------------------------------------------------+
    |  live log terminal (colour coded, timestamps)                     |
    +------------------------------------------------------------------+
    |  submit | cancel | open output folder | clear log   status text|
    +------------------------------------------------------------------+
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from common.discovery import discover_servers, get_primary_lan_ip, test_tcp_connection
from common.messages import (
    AUDIO_CODECS,
    COMPUTE_OPS,
    GPU_ENCODERS,
    NVENC_PRESETS,
    RESOLUTIONS,
    X264_PRESETS,
    JobSpec,
)
from common.paths import CLIENT_DATA, CREATE_NO_WINDOW, DEFAULT_PORT
from client.net.session_worker import SessionWorker
from client.ui.log_terminal import LogTerminal
from client.ui.theme import build_stylesheet
from client.watcher import FolderWatcher

VIDEO_FILTER = "Video files (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.mpg *.mpeg *.ts);;All files (*)"

STATE_COLOURS = {
    "offline": "#94a3b8",
    "connecting": "#f59e0b",
    "online": "#10b981",
    "uploading": "#38bdf8",
    "submitting": "#38bdf8",
    "running": "#6366f1",
    "downloading": "#38bdf8",
    "done": "#10b981",
    "error": "#ef4444",
}


def card(title: str, icon: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(15, 13, 15, 15)
    layout.setSpacing(10)
    heading = f"{icon}  {title.upper()}" if icon else title.upper()
    label = QLabel(heading)
    label.setObjectName("CardTitle")
    layout.addWidget(label)
    return frame, layout


def metric(label: str) -> tuple[QFrame, QLabel]:
    """A titled metric tile. The caller must keep the frame alive."""
    frame = QFrame()
    frame.setObjectName("MetricTile")
    frame.setMinimumWidth(100)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    box = QVBoxLayout(frame)
    box.setContentsMargins(10, 8, 10, 8)
    box.setSpacing(2)
    name = QLabel(label)
    name.setObjectName("MetricName")
    name.setAlignment(Qt.AlignmentFlag.AlignCenter)
    value = QLabel("--")
    value.setObjectName("Metric")
    value.setAlignment(Qt.AlignmentFlag.AlignCenter)
    box.addWidget(name)
    box.addWidget(value)
    return frame, value


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote GPU Rendering - Distributed Task Offloading")
        self.setMinimumSize(840, 500)
        self.resize(1200, 780)
        self.setAcceptDrops(True)

        self.thread: QThread | None = None
        self.worker: SessionWorker | None = None
        self.job_started_at = 0.0
        self.job_busy = False
        self.remote_speedup: float | None = None

        self.watcher = FolderWatcher(Path.home() / "Downloads")
        self.watcher.video_detected.connect(self._on_video_detected)
        self.watcher.watching_state.connect(self._on_watcher_state)

        self._build()
        self._start_worker()

        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(12000)
        self.heartbeat_timer.timeout.connect(self._on_heartbeat)

        self.watcher.start()

        self._log("info", "client ready - configure the worker address and press Connect")

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)

        outer.addLayout(self._header())

        # Vertical splitter: Scrollable Dashboard Cards on top, Live Terminal on bottom
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Scrollable container for the cards to prevent overflow on any screen size/scaling
        top_scroll = QScrollArea()
        top_scroll.setObjectName("DashboardScroll")
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        top_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        top_container = QWidget()
        top_container.setObjectName("TopDashboard")
        body = QHBoxLayout(top_container)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(11)

        body.addWidget(self._connection_card(), 0)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._config_card(), 1)
        right.addWidget(self._progress_card(), 0)
        body.addLayout(right, 1)

        top_scroll.setWidget(top_container)

        splitter.addWidget(top_scroll)
        splitter.addWidget(self._log_panel())
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([480, 180])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        outer.addWidget(splitter, 1)
        outer.addLayout(self._footer())

    def _header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        title = QLabel("Remote GPU Accelerator")
        title.setObjectName("Title")
        subtitle = QLabel("Distributed Task Offloading & GPU Hardware Acceleration Client")
        subtitle.setObjectName("Subtitle")
        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(title)
        column.addWidget(subtitle)
        row.addLayout(column)
        row.addStretch(1)

        self.state_pill = QLabel("⚪ OFFLINE")
        self.state_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_pill.setMinimumWidth(130)
        self.state_pill.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.state_pill.setStyleSheet(
            "background:#0f172a;border:1.5px solid #475569;border-radius:12px;"
            f"color:{STATE_COLOURS['offline']};padding:5px 14px;"
        )
        row.addWidget(self.state_pill)
        return row

    def _connection_card(self) -> QWidget:
        frame, layout = card("worker connection", "🌐")
        frame.setMinimumWidth(300)
        frame.setMaximumWidth(350)

        # Zero-Config LAN Discovery row
        scan_row = QHBoxLayout()
        scan_row.setSpacing(6)
        self.servers_combo = QComboBox()
        self.servers_combo.addItem("Select or scan server...")
        self.servers_combo.currentIndexChanged.connect(self._on_server_selected)
        self.scan_btn = QPushButton("🔍 Scan")
        self.scan_btn.setToolTip("Auto-discover GPU worker servers running on the local network (LAN).")
        self.scan_btn.clicked.connect(self._on_scan_lan)
        scan_row.addWidget(self.servers_combo, 1)
        scan_row.addWidget(self.scan_btn, 0)
        layout.addLayout(scan_row)

        self.host_edit = QLineEdit(self._default_host())
        self.host_edit.setPlaceholderText("192.168.1.1 or 127.0.0.1")
        self.host_edit.setToolTip("IP address of the remote GPU worker daemon (e.g. 192.168.1.1).")
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(DEFAULT_PORT)
        self.port_edit.setToolTip("TCP port the worker listens on (default: 7575).")

        form = QFormLayout()
        form.setSpacing(7)
        form.addRow("Worker IP", self.host_edit)
        form.addRow("Port", self.port_edit)
        layout.addLayout(form)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("Primary")
        self.connect_btn.setToolTip("Connect to the worker and negotiate hardware capabilities.")
        self.connect_btn.clicked.connect(self._on_connect)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        self.disconnect_btn.setEnabled(False)
        self.test_conn_btn = QPushButton("⚡ Test")
        self.test_conn_btn.setToolTip("Test TCP connectivity and latency before connecting.")
        self.test_conn_btn.clicked.connect(self._on_test_connection)

        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(6)
        btn_row1.addWidget(self.connect_btn, 1)
        btn_row1.addWidget(self.disconnect_btn, 1)
        layout.addLayout(btn_row1)

        self.ping_btn = QPushButton("Ping RTT")
        self.ping_btn.setToolTip("Send a batch of pings to measure round-trip time (RTT) and jitter.")
        self.ping_btn.clicked.connect(lambda: self._call("do_ping", 20))
        self.ping_btn.setEnabled(False)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(6)
        btn_row2.addWidget(self.test_conn_btn, 1)
        btn_row2.addWidget(self.ping_btn, 1)
        layout.addLayout(btn_row2)

        my_lan = get_primary_lan_ip()
        self.lan_hint = QLabel(f"Client LAN IP: {my_lan}")
        self.lan_hint.setObjectName("Hint")
        self.lan_hint.setWordWrap(True)
        layout.addWidget(self.lan_hint)

        layout.addSpacing(6)
        grid = QGridLayout()
        grid.setSpacing(7)
        self.rtt_metric, self.rtt_value = metric("PING (RTT)")
        self.jit_metric, self.jit_value = metric("JITTER")
        self.queued_metric, self.queued_value = metric("QUEUE SLOTS")
        grid.addWidget(self.rtt_metric, 0, 0)
        grid.addWidget(self.jit_metric, 0, 1)
        grid.addWidget(self.queued_metric, 1, 0, 1, 2)
        layout.addLayout(grid)

        layout.addSpacing(6)
        self.cap_list = QPlainTextEdit()
        self.cap_list.setObjectName("Caps")
        self.cap_list.setReadOnly(True)
        self.cap_list.setMinimumHeight(95)
        self.cap_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.cap_list.setFont(QFont("Cascadia Mono", 8))
        self.cap_list.setPlaceholderText("Worker capabilities appear after a handshake...")
        layout.addWidget(self.cap_list, 1)
        return frame

    def _config_card(self) -> QWidget:
        frame, layout = card("job configuration", "⚙️")
        frame.setMinimumHeight(280)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText("Drag and drop a video file here, or click Browse...")
        browse = QPushButton("📂 Browse...")
        browse.setMinimumHeight(32)
        browse.setMinimumWidth(110)
        browse.clicked.connect(self._on_browse)
        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(self.file_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        self.file_info = QLabel("No video selected. Choose an input video above.")
        self.file_info.setObjectName("FileBadge")
        layout.addWidget(self.file_info)

        # Widgets built before tabs so _transcode_tab can reference them
        self.crf_check = QCheckBox("Use target bitrate (disables CRF)")
        self.out_edit = QLineEdit("output.mp4")
        self.out_edit.setToolTip("Destination filename for the rendered output.")

        tabs = QTabWidget()
        self.tabs = tabs
        self.tabs.setMinimumHeight(190)
        tabs.addTab(self._transcode_tab(), "Video Transcode (NVENC / CPU)")
        tabs.addTab(self._compute_tab(), "Tensor Compute (CUDA / Torch)")
        tabs.addTab(self._watcher_tab(), "🎬 Clipchamp Watch Folder")
        layout.addWidget(tabs, 1)
        return frame

    def _progress_card(self) -> QWidget:
        frame, layout = card("live progress & metrics", "▶")
        frame.setMinimumHeight(135)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(7)

        self.transfer_bar = QProgressBar()
        self.transfer_bar.setRange(0, 100)
        self.transfer_bar.setValue(0)
        self.transfer_bar.setFormat("network transfer:  %p%")
        layout.addWidget(self.transfer_bar)

        self.job_bar = QProgressBar()
        self.job_bar.setRange(0, 100)
        self.job_bar.setValue(0)
        self.job_bar.setFormat("remote worker execution:  %p%")
        layout.addWidget(self.job_bar)

        self.detail_label = QLabel("idle")
        self.detail_label.setObjectName("Hint")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        stats = QHBoxLayout()
        stats.setSpacing(9)
        elapsed_tile, self.elapsed_value = metric("ELAPSED")
        rate_tile, self.speed_value = metric("TRANSFER RATE")
        speed_tile, self.encode_value = metric("ENCODE SPEED")
        self._tiles = (elapsed_tile, rate_tile, speed_tile)
        for widget in self._tiles:
            stats.addWidget(widget)
        layout.addLayout(stats)
        return frame

    def _transcode_tab(self) -> QWidget:
        page = QWidget()
        page.setMinimumHeight(190)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(10, 6, 10, 6)
        page_layout.setSpacing(6)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)

        self.encoder_box = QComboBox()
        self.encoder_box.setToolTip("Video encoder engine (NVENC hardware acceleration or CPU software fallback).")
        for name in GPU_ENCODERS:
            self.encoder_box.addItem(f"{name}  -  {GPU_ENCODERS[name]}", name)
        self.encoder_box.setCurrentIndex(0)
        self.encoder_box.currentIndexChanged.connect(self._sync_presets)

        self.preset_box = QComboBox()
        self.preset_box.setToolTip("Speed vs quality preset.")
        self.preset_caption = QLabel("Encoding preset")
        self.preset_caption.setObjectName("MetricName")
        self._sync_presets()

        self.res_box = QComboBox()
        self.res_box.setToolTip("Target output resolution (preserves aspect ratio, never upscales).")
        for key in RESOLUTIONS:
            label = "keep source" if key == "source" else f"{key} ({RESOLUTIONS[key][1]}p)"
            self.res_box.addItem(label, key)

        self.bitrate_box = QSpinBox()
        self.bitrate_box.setRange(0, 200_000)
        self.bitrate_box.setValue(8000)
        self.bitrate_box.setSuffix(" kbit/s")
        self.bitrate_box.setEnabled(False)
        self.bitrate_box.setToolTip("Target average video bitrate in kbit/s (used when CRF is unchecked).")
        self.crf_check.toggled.connect(
            lambda on: (self.bitrate_box.setEnabled(on), self.crf_box.setEnabled(not on))
        )

        self.crf_box = QSpinBox()
        self.crf_box.setRange(0, 51)
        self.crf_box.setValue(23)
        self.crf_box.setToolTip("Constant Rate Factor (0-51). Lower = higher visual quality. 23 is recommended.")

        self.audio_box = QComboBox()
        self.audio_box.setToolTip("Audio codec for output streams.")
        for codec in AUDIO_CODECS:
            self.audio_box.addItem(codec, codec)

        self.audio_kbps = QSpinBox()
        self.audio_kbps.setRange(32, 512)
        self.audio_kbps.setValue(128)
        self.audio_kbps.setSuffix(" kbit/s")
        self.audio_kbps.setToolTip("Audio stream bitrate in kbit/s.")

        # 2 columns x 4 rows grid - spacious, responsive, never overflows
        pairs = [
            # Row 0: Video Engine & Preset
            ("Video encoder", self.encoder_box, 0, 0),
            (self.preset_caption, self.preset_box, 0, 1),
            # Row 1: Resolution & Filename
            ("Output resolution", self.res_box, 1, 0),
            ("Destination filename", self.out_edit, 1, 1),
            # Row 2: Quality & Rate Control
            ("CRF quality (lower = better)", self.crf_box, 2, 0),
            (self.crf_check, self.bitrate_box, 2, 1),
            # Row 3: Audio Settings
            ("Audio codec", self.audio_box, 3, 0),
            ("Audio bitrate", self.audio_kbps, 3, 1),
        ]

        for caption_item, widget, row, col in pairs:
            if isinstance(caption_item, (str, QLabel)):
                if isinstance(caption_item, str):
                    caption = QLabel(caption_item)
                    caption.setObjectName("MetricName")
                else:
                    caption = caption_item
            else:
                caption = caption_item
            box = QVBoxLayout()
            box.setSpacing(3)
            box.addWidget(caption)
            box.addWidget(widget)
            grid.addLayout(box, row, col)
            grid.setRowMinimumHeight(row, 44)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        page_layout.addLayout(grid)
        page_layout.addStretch(1)
        return page

    def _compute_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(6, 10, 6, 6)
        form.setSpacing(9)

        self.op_box = QComboBox()
        self.op_box.setToolTip("Mathematical workload to offload to the worker.")
        for op in COMPUTE_OPS:
            self.op_box.addItem(op, op)
        self.size_box = QSpinBox()
        self.size_box.setRange(64, 8192)
        self.size_box.setValue(2048)
        self.size_box.setSingleStep(256)
        self.size_box.setToolTip("Matrix or tensor dimension N for N×N workloads.")
        self.iters_box = QSpinBox()
        self.iters_box.setRange(1, 500)
        self.iters_box.setValue(20)
        self.iters_box.setToolTip("Number of compute iterations for benchmarking throughput.")

        note = QLabel(
            "Runs a hardware-accelerated CUDA kernel on the remote worker node. Requires PyTorch "
            "with CUDA on the worker; automatically falls back to NumPy/CPU when absent."
        )
        note.setObjectName("Hint")
        note.setWordWrap(True)
        form.addRow("Operation", self.op_box)
        form.addRow("Matrix size", self.size_box)
        form.addRow("Iterations", self.iters_box)
        form.addRow("", note)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        return page

    def _watcher_tab(self) -> QWidget:
        page = QWidget()
        page.setMinimumHeight(160)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(7)

        # Row 1: Folder Selection
        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)

        folder_lbl = QLabel("Watch Folder:")
        folder_lbl.setObjectName("MetricName")
        self.watch_folder_edit = QLineEdit(str(self.watcher.watch_dir))
        self.watch_folder_edit.setReadOnly(True)
        self.watch_folder_edit.setToolTip("Directory actively monitored for new video exports from Clipchamp or other editors.")

        browse_folder_btn = QPushButton("📁 Change...")
        browse_folder_btn.setToolTip("Select a different folder to monitor.")
        browse_folder_btn.clicked.connect(self._on_change_watch_folder)

        open_folder_btn = QPushButton("📂 Open Folder")
        open_folder_btn.setToolTip("Open the monitored folder in Windows Explorer.")
        open_folder_btn.clicked.connect(self._on_open_watch_folder)

        folder_row.addWidget(folder_lbl)
        folder_row.addWidget(self.watch_folder_edit, 1)
        folder_row.addWidget(browse_folder_btn)
        folder_row.addWidget(open_folder_btn)
        layout.addLayout(folder_row)

        # Row 2: Status & Quick Launch Actions
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)

        self.watcher_status_lbl = QLabel(f"🟢 Active - Watching: {self.watcher.watch_dir.name}")
        self.watcher_status_lbl.setStyleSheet("color: #10b981; font-weight: bold;")
        self.watcher_status_lbl.setToolTip("Current watcher monitoring status.")

        self.watcher_toggle_btn = QPushButton("⏸ Pause Watcher")
        self.watcher_toggle_btn.clicked.connect(self._on_toggle_watcher)

        self.launch_clipchamp_btn = QPushButton("🎬 Launch Clipchamp")
        self.launch_clipchamp_btn.setToolTip("Launch Microsoft Clipchamp video editor on this computer.")
        self.launch_clipchamp_btn.clicked.connect(self._on_launch_clipchamp)

        ctrl_row.addWidget(self.watcher_status_lbl)
        ctrl_row.addWidget(self.watcher_toggle_btn)
        ctrl_row.addWidget(self.launch_clipchamp_btn)
        ctrl_row.addStretch(1)
        layout.addLayout(ctrl_row)

        # Row 3: Auto-offload checkbox
        self.auto_submit_check = QCheckBox("⚡ Auto-offload to GPU immediately upon export")
        self.auto_submit_check.setChecked(True)
        self.auto_submit_check.setToolTip(
            "When checked, newly exported videos are automatically submitted to the connected GPU server without requiring you to click 'Offload'."
        )
        layout.addWidget(self.auto_submit_check)

        # Row 4: Detection Feed / History
        self.detected_history = QPlainTextEdit()
        self.detected_history.setReadOnly(True)
        self.detected_history.setMaximumHeight(65)
        self.detected_history.setPlaceholderText("No exports detected yet. Export a video from Clipchamp to see it appear here automatically.")
        layout.addWidget(self.detected_history)

        return page

    def _log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)

        header = QHBoxLayout()
        title = QLabel("LIVE LOG")
        title.setObjectName("CardTitle")
        self.log_meta = QLabel("")
        self.log_meta.setObjectName("Hint")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.log_meta)
        layout.addLayout(header)

        self.terminal = LogTerminal()
        self.terminal.customContextMenuRequested.connect(self._log_menu)
        layout.addWidget(self.terminal, 1)
        return panel

    def _footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)

        self.submit_btn = QPushButton("🚀  Offload to GPU")
        self.submit_btn.setObjectName("Primary")
        self.submit_btn.setMinimumHeight(40)
        self.submit_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.submit_btn.clicked.connect(self._on_submit)
        self.cancel_btn = QPushButton("⏹  Cancel")
        self.cancel_btn.setObjectName("Danger")
        self.cancel_btn.setMinimumHeight(40)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.open_btn = QPushButton("📂  Open Output Folder")
        self.open_btn.setMinimumHeight(40)
        self.open_btn.clicked.connect(self._on_open_outputs)
        self.clear_btn = QPushButton("🧹  Clear Log")
        self.clear_btn.setMinimumHeight(40)
        self.clear_btn.clicked.connect(self.terminal.clear_log)

        row.addWidget(self.submit_btn)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.open_btn)
        row.addWidget(self.clear_btn)
        row.addStretch(1)

        self.status_label = QLabel("idle")
        self.status_label.setObjectName("Hint")
        row.addWidget(self.status_label)
        return row

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _default_host() -> str:
        state = Path(__file__).resolve().parents[2] / "server" / "data" / "daemon.json"
        if state.exists():
            import json

            try:
                return json.loads(state.read_text(encoding="utf-8")).get("host", "")
            except (ValueError, OSError):
                return ""
        return os.environ.get("RDO_WORKER", "")

    def _sync_presets(self) -> None:
        encoder = self.encoder_box.currentData() or "h264_nvenc"
        self.preset_box.clear()
        options = NVENC_PRESETS if encoder.endswith("_nvenc") else X264_PRESETS
        for option in options:
            self.preset_box.addItem(option, option)
        preferred = "p4" if encoder.endswith("_nvenc") else "medium"
        if preferred in options:
            self.preset_box.setCurrentIndex(list(options).index(preferred))
        if hasattr(self, "preset_caption"):
            if encoder.endswith("_nvenc"):
                self.preset_caption.setText("NVENC Preset (P1-P7)")
            else:
                self.preset_caption.setText("CPU Preset (Speed vs Quality)")

    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = SessionWorker(self.host_edit.text().strip() or "127.0.0.1",
                                    self.port_edit.value())
        self.worker.moveToThread(self.thread)
        self.worker.logged.connect(self.terminal.append_log)
        self.worker.logged.connect(lambda level, text: self._mirror(level, text))
        self.worker.state_changed.connect(self._on_state)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.rtt_measured.connect(self._on_rtt)
        self.worker.job_progress.connect(self._on_job_progress)
        self.worker.transfer_progress.connect(self._on_transfer)
        self.worker.job_finished.connect(self._on_job_finished)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker.capabilities_changed.connect(self._on_capabilities)
        self.thread.start()
        # Nothing can be submitted until a handshake has told us what this
        # worker supports.
        self._sync_buttons(online=False, busy=False)

    def _call(self, slot: str, *args) -> None:
        """Run a worker slot **on the worker thread**.

        Calling ``worker.do_submit(...)`` directly would execute it in the GUI
        thread, because a plain Python call ignores Qt affinity - ``moveToThread``
        only affects queued signal/slot invocations. That is exactly the bug this
        helper exists to prevent: a 400 MB upload would freeze the window.

        Emitting ``SessionWorker.invoke`` posts the call to the worker's event
        loop instead, so the GUI returns immediately and the blocking work happens
        where it belongs.
        """
        if self.worker is None:
            return
        if not hasattr(self.worker, slot):
            self.terminal.append_log("error", f"unknown worker slot: {slot}")
            return
        self.worker.invoke.emit(slot, tuple(args))

    def _log(self, level: str, message: str) -> None:
        self.terminal.append_log(level, message)

    def _mirror(self, level: str, text: str) -> None:
        if level in ("error", "warn"):
            self.status_label.setText(text[:96])
            self.status_label.setStyleSheet(f"color:{STATE_COLOURS.get(level, '#8a93a6')}")

    def _on_connect(self) -> None:
        host = self.host_edit.text().strip()
        if not host:
            self._log("error", "enter the worker IP address first")
            self.host_edit.setFocus()
            return
        self.worker.host = host
        self.worker.port = self.port_edit.value()
        self._call("do_connect")

    def _on_disconnect(self) -> None:
        self._call("do_disconnect")

    def _on_scan_lan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning...")
        self._log("info", "scanning local network (LAN) for GPU worker servers...")

        def run_scan():
            servers = discover_servers(timeout=1.0)
            QTimer.singleShot(0, lambda: self._on_scan_completed(servers))

        threading.Thread(target=run_scan, daemon=True).start()

    def _on_scan_completed(self, servers: list[dict]) -> None:
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("🔍 Scan")
        self.servers_combo.blockSignals(True)
        self.servers_combo.clear()
        if not servers:
            self.servers_combo.addItem("No servers found (click Scan)")
            self.servers_combo.blockSignals(False)
            self._log("warn", "no GPU worker servers detected on LAN - ensure Server application is started")
            return

        self.servers_combo.addItem(f"Found {len(servers)} server(s)...")
        for s in servers:
            ip = s.get("ip", "")
            port = s.get("port", 7575)
            host = s.get("hostname", ip)
            gpu = s.get("gpu", "")
            label = f"🖥️ {host} ({ip}:{port})" + (f" - {gpu}" if gpu and gpu != "Unknown" else "")
            self.servers_combo.addItem(label, (ip, port))
        self.servers_combo.blockSignals(False)
        self._log("ok", f"discovered {len(servers)} GPU worker server(s) on LAN")
        if len(servers) >= 1:
            self.servers_combo.setCurrentIndex(1)

    def _on_server_selected(self, index: int) -> None:
        data = self.servers_combo.currentData()
        if data and isinstance(data, (tuple, list)):
            ip, port = data
            self.host_edit.setText(str(ip))
            self.port_edit.setValue(int(port))
            self._log("info", f"selected worker server: {ip}:{port}")

    def _on_test_connection(self) -> None:
        host = self.host_edit.text().strip()
        port = self.port_edit.value()
        if not host:
            self._log("error", "enter an IP address or select a server to test")
            return
        self.test_conn_btn.setEnabled(False)
        self.test_conn_btn.setText("Testing...")

        def run_test():
            ok, lat, msg = test_tcp_connection(host, port, timeout=2.0)
            QTimer.singleShot(0, lambda: self._on_test_result(ok, lat, msg))

        threading.Thread(target=run_test, daemon=True).start()

    def _on_test_result(self, ok: bool, latency: float, msg: str) -> None:
        self.test_conn_btn.setEnabled(True)
        self.test_conn_btn.setText("⚡ Test")
        if ok:
            self._log("ok", f"Connection test passed: {msg}")
            QMessageBox.information(
                self,
                "Connection Test Passed",
                f"✅ Successfully connected to server!\n\nHost: {self.host_edit.text()}:{self.port_edit.value()}\nLatency: {latency:.1f} ms\n\nClick 'Connect' to initialize session.",
            )
        else:
            self._log("error", f"Connection test failed: {msg}")
            QMessageBox.warning(
                self,
                "Connection Test Failed",
                f"❌ Could not reach server:\n\n{msg}\n\nTroubleshooting suggestions:\n1. Make sure the Server application is running on that machine.\n2. Ensure port 7575 is allowed in Windows Firewall on the server.\n3. Verify both machines are on the same Wi-Fi or Ethernet network.",
            )

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose an input video", str(Path.home()),
                                              VIDEO_FILTER)
        if path:
            self._set_file(Path(path))

    def _set_file(self, path: Path) -> None:
        self.file_edit.setText(str(path))
        size = path.stat().st_size
        stem = safe_stem(path)
        self.out_edit.setText(f"{stem}-remote.mp4")
        self.file_info.setText(
            f"📹  {path.name}  •  {size / (1024 * 1024):.2f} MiB  •  {path.suffix.upper() or 'UNKNOWN'}"
        )
        self._log("info", f"input selected: {path.name} ({size / (1024 * 1024):.2f} MiB)")

    # -- watcher slots -----------------------------------------------------
    def _on_change_watch_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder to Watch for Video Exports", str(self.watcher.watch_dir)
        )
        if folder:
            p = Path(folder)
            self.watcher.set_watch_dir(p)
            self.watch_folder_edit.setText(str(p))
            self._log("info", f"watch folder changed to: {p}")

    def _on_open_watch_folder(self) -> None:
        p = self.watcher.watch_dir
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as exc:
            self._log("warn", f"could not open watch folder: {exc}")

    def _on_launch_clipchamp(self) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile("ms-clipchamp:")
                self._log("ok", "launched Microsoft Clipchamp")
            else:
                self._log("info", "Clipchamp is a Windows app")
        except Exception:
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "ms-clipchamp:"],
                    creationflags=CREATE_NO_WINDOW,
                )
                self._log("ok", "launched Microsoft Clipchamp via shell protocol")
            except Exception as exc:
                self._log("warn", f"could not launch Clipchamp automatically: {exc}")
                QMessageBox.information(
                    self,
                    "Launch Clipchamp",
                    "Could not open Clipchamp automatically via protocol link.\n\n"
                    "Please launch Clipchamp from your Windows Start Menu.",
                )

    def _on_toggle_watcher(self) -> None:
        if self.watcher.is_active():
            self.watcher.stop()
        else:
            self.watcher.start()

    @pyqtSlot(bool, str)
    def _on_watcher_state(self, active: bool, folder: str) -> None:
        folder_name = Path(folder).name or folder
        if hasattr(self, "watcher_status_lbl"):
            if active:
                self.watcher_toggle_btn.setText("⏸ Pause Watcher")
                self.watcher_status_lbl.setText(f"🟢 Active - Watching: {folder_name}")
                self.watcher_status_lbl.setStyleSheet("color: #10b981; font-weight: bold;")
            else:
                self.watcher_toggle_btn.setText("▶ Resume Watcher")
                self.watcher_status_lbl.setText("⏸ Watcher Paused")
                self.watcher_status_lbl.setStyleSheet("color: #f59e0b; font-weight: bold;")

    @pyqtSlot(str, int)
    def _on_video_detected(self, file_path_str: str, file_size: int) -> None:
        path = Path(file_path_str)
        size_mb = file_size / (1024 * 1024)
        timestamp = time.strftime("%H:%M:%S")
        self._log("ok", f"🎬 New video detected from editor: {path.name} ({size_mb:.2f} MiB)")
        if hasattr(self, "detected_history"):
            self.detected_history.appendPlainText(f"[{timestamp}] {path.name} ({size_mb:.2f} MiB)")
        self._set_file(path)

        if hasattr(self, "auto_submit_check") and self.auto_submit_check.isChecked():
            if self.worker and self.worker.transport.is_open and not self.job_busy:
                self._log("ok", f"⚡ Auto-offload triggered: sending {path.name} to GPU server...")
                self._on_submit()
            elif not (self.worker and self.worker.transport.is_open):
                self._log("warn", f"video {path.name} staged for offloading. Connect to worker server to render.")
            elif self.job_busy:
                self._log("warn", f"worker is currently rendering another job. {path.name} staged in input field.")

    # -- drag and drop -----------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        for url in event.mimeData().urls():
            local = Path(url.toLocalFile())
            if local.is_file():
                self._set_file(local)
                break
        event.acceptProposedAction()

    # -- submit ------------------------------------------------------------
    def _current_spec(self) -> JobSpec:
        job_type = "compute" if self.tabs.currentIndex() == 1 else "transcode"
        spec = JobSpec(
            job_type=job_type,
            output_name=self.out_edit.text().strip() or "output.mp4",
            audio_codec=self.audio_box.currentData(),
            audio_bitrate_kbps=self.audio_kbps.value(),
            compute_op=self.op_box.currentData(),
            compute_size=self.size_box.value(),
            compute_iters=self.iters_box.value(),
        )
        if job_type == "transcode":
            spec.video_codec = self.encoder_box.currentData()
            spec.preset = self.preset_box.currentData()
            spec.resolution = self.res_box.currentData()
            spec.bitrate_kbps = self.bitrate_box.value() if self.crf_check.isChecked() else 0
            spec.crf = 0 if self.crf_check.isChecked() else self.crf_box.value()
        return spec

    def _on_submit(self) -> None:
        spec = self._current_spec()
        errors = spec.validate()
        if errors:
            for error in errors:
                self._log("error", error)
            return
        if spec.job_type == "transcode":
            path_text = self.file_edit.text().strip()
            if not path_text:
                self._log("error", "choose an input file first")
                return
            if not Path(path_text).is_file():
                self._log("error", f"{path_text} does not exist")
                return

        self.job_started_at = time.perf_counter()
        self.job_busy = True
        self._sync_buttons(online=self.worker.transport.is_open, busy=True)
        self.terminal.rule(f"{spec.job_type} job")
        self._set_progress(0, "starting")
        if spec.job_type == "transcode":
            self._call("do_submit", self.file_edit.text().strip(), spec.to_dict())
        else:
            self._call("do_submit", "", spec.to_dict())

    def _on_cancel(self) -> None:
        self._call("do_cancel")

    def _on_open_outputs(self) -> None:
        outputs = CLIENT_DATA / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(outputs)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(outputs)])
            else:
                subprocess.Popen(["xdg-open", str(outputs)])
        except OSError as exc:
            self._log("warn", f"could not open the output folder: {exc}")

    def _log_menu(self, position) -> None:
        menu = QMenu(self)
        menu.addAction("Copy all", self.terminal.selectAll)
        menu.addSeparator()
        menu.addAction("Clear", self.terminal.clear_log)
        menu.exec(self.terminal.mapToGlobal(position))

    # ----------------------------------------------------------- callbacks
    def _set_progress(self, pct: int, detail: str) -> None:
        """Update the job bar and the side metrics. Called from several slots."""
        self.job_bar.setValue(max(0, min(100, int(pct))))
        self.detail_label.setText(detail[:150])
        if self.job_started_at:
            self.elapsed_value.setText(f"{time.perf_counter() - self.job_started_at:.1f} s")

    @pyqtSlot(str, str)
    def _on_state(self, state: str, detail: str) -> None:
        colour = STATE_COLOURS.get(state, "#94a3b8")
        pill_bg = {
            "online": "#064e3b",
            "done": "#064e3b",
            "running": "#1e3a8a",
            "uploading": "#451a03",
            "submitting": "#451a03",
            "downloading": "#451a03",
            "connecting": "#451a03",
            "error": "#450a0a",
            "offline": "#1e293b",
        }.get(state, "#1e293b")
        icon = {
            "online": "🟢",
            "done": "✅",
            "running": "⚡",
            "uploading": "📤",
            "submitting": "⏳",
            "downloading": "📥",
            "connecting": "🔄",
            "error": "❌",
            "offline": "⚪",
        }.get(state, "●")
        self.state_pill.setText(f"{icon} {state.upper()}")
        self.state_pill.setStyleSheet(
            f"background:{pill_bg};border:1.5px solid {colour};border-radius:12px;"
            f"color:{colour};padding:5px 14px;font-weight:700;font-size:9pt;"
        )
        self.status_label.setStyleSheet(f"color:{colour}")
        self.status_label.setText(f"{state}: {detail}" if detail else state)
        online = state not in ("offline", "error", "connecting")
        self._sync_buttons(online=online, busy=self.job_busy)

    def _sync_buttons(self, online: bool, busy: bool) -> None:
        """One place that decides which buttons make sense right now.

        Submitting needs a completed handshake, because the job spec has to match
        what the worker can actually do - an encoder chosen before the
        capabilities arrive is a request the worker will reject.
        """
        self.disconnect_btn.setEnabled(online)
        self.ping_btn.setEnabled(online and not busy)
        self.submit_btn.setEnabled(online and not busy)
        self.cancel_btn.setEnabled(online and busy)

    @pyqtSlot(dict)
    def _on_connected(self, info: dict) -> None:
        self.connect_btn.setEnabled(False)
        self.cap_list.setPlainText(self._format_caps(info.get("capabilities", {})))
        self.heartbeat_timer.start()

    @pyqtSlot(str)
    def _on_disconnected(self, reason: str) -> None:
        self.heartbeat_timer.stop()
        self.connect_btn.setEnabled(True)
        self.job_busy = False
        self.cap_list.setPlainText("")
        self._sync_buttons(online=False, busy=False)

    def _on_heartbeat(self) -> None:
        if not self.job_busy and self.disconnect_btn.isEnabled():
            self._call("do_ping", 1)

    @pyqtSlot(dict)
    def _on_capabilities(self, caps: dict) -> None:
        self.cap_list.setPlainText(self._format_caps(caps))
        available = set(caps.get("hardware_encoders", []))
        usable = available | {"libx264"}
        for index in range(self.encoder_box.count()):
            name = self.encoder_box.itemData(index)
            self.encoder_box.model().item(index).setEnabled(name in usable)

        # Greying an item out is not enough: a disabled item can still be the
        # current one, and the spec would then ask the worker for an encoder it
        # does not have. Move the selection to something that actually works.
        chosen = None
        for name in (self.encoder_box.currentData(), "h264_nvenc", "libx264"):
            if name in usable:
                chosen = name
                break
        if chosen is None:
            chosen = self.encoder_box.itemData(0)
        index = self.encoder_box.findData(chosen)
        if index >= 0:
            self.encoder_box.setCurrentIndex(index)
        if available:
            self._log("info", f"encoders offered by this worker: "
                              f"{', '.join(sorted(available))}")

    @staticmethod
    def _format_caps(caps: dict) -> str:
        if not caps:
            return ""
        gpu = caps.get("gpu_name") or "None detected"
        nvenc = "Active (Hardware Accelerated)" if caps.get("nvenc_available") else "Unavailable (CPU Fallback)"
        cuda = caps.get("cuda_device") or "Unavailable"
        torch = "Available" if caps.get("torch_available") else "NumPy fallback"
        ffmpeg_ver = (caps.get("ffmpeg_version") or "n/a").split(" | ")[0]
        encoders = ", ".join(caps.get("hardware_encoders", [])) or "libx264, mpeg4 (CPU)"
        cpus = str(caps.get("cpu_count", "?"))

        return (
            f"Host System  : {caps.get('hostname', '?')} ({caps.get('os', '?')})\n"
            f"GPU Device   : {gpu}\n"
            f"NVENC Engine : {nvenc}\n"
            f"CUDA / Torch : {cuda} (PyTorch: {torch})\n"
            f"Encoders     : {encoders}\n"
            f"FFmpeg Build : {ffmpeg_ver} | CPUs: {cpus}"
        )

    @pyqtSlot(dict)
    def _on_rtt(self, stats: dict) -> None:
        self.rtt_value.setText(f"{stats['avg_ms']:.2f} ms")
        self.jit_value.setText(f"{stats['jitter_ms']:.2f} ms")
        self.rtt_value.setToolTip(
            f"min {stats['min_ms']} / avg {stats['avg_ms']} / max {stats['max_ms']} ms "
            f"over {stats['count']} samples"
        )

    @pyqtSlot(str, int, int)
    def _on_transfer(self, phase: str, done: int, total: int) -> None:
        pct = int(done / max(total, 1) * 100)
        self.transfer_bar.setValue(pct)
        self.transfer_bar.setFormat(f"{phase}  %p%")
        megabits = done * 8 / max(time.perf_counter() - self.job_started_at, 1e-6) / 1e6
        self.speed_value.setText(f"{megabits:.1f} Mb/s")
        self.terminal.banner(
            f"{phase:<10} {pct:>3}%  {done / (1024 * 1024):>8.2f} / "
            f"{total / (1024 * 1024):.2f} MiB"
        )

    @pyqtSlot(dict)
    def _on_job_progress(self, body: dict) -> None:
        detail = " | ".join(
            part for part in (body.get("stage"), body.get("detail")) if part
        )
        if body.get("eta_s"):
            detail += f"  |  eta {body['eta_s']:.0f}s"
        self._set_progress(int(body.get("pct", 0)), detail)
        if body.get("speed"):
            self.encode_value.setText(str(body["speed"]))

    @pyqtSlot(dict)
    def _on_job_finished(self, stats: dict) -> None:
        self.job_busy = False
        self._sync_buttons(online=True, busy=False)
        self.transfer_bar.setValue(100 if "output_path" in stats else 0)
        if "output_path" in stats:
            self._set_progress(100, "complete")
            self.queued_value.setText("-")
            self.status_label.setText(f"saved {Path(stats['output_path']).name}")
        else:
            self._set_progress(0, stats.get("error", "failed"))

    @pyqtSlot(str, str)
    def _on_job_failed(self, code: str, message: str) -> None:
        self.job_busy = False
        self._sync_buttons(online=True, busy=False)
        self._log("error", f"job failed [{code}]: {message}")
        self._set_progress(0, "failed")
        self.status_label.setText("failed")

    # -------------------------------------------------------------- closing
    def shutdown_worker(self) -> None:
        """Close the transport and stop the worker thread. Safe to call twice."""
        if hasattr(self, "watcher") and self.watcher is not None:
            self.watcher.stop()
        if self.worker is not None:
            self.worker.shutdown()
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(2500)
            self.thread = None
            self.worker = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.shutdown_worker()
        self.log_meta.setText("")
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.log_meta.setText(self.terminal.summary())
        super().resizeEvent(event)


def safe_stem(path: Path) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in path.stem)
    return cleaned.strip("-") or "clip"


__all__ = ["MainWindow"]