"""Server GUI window for managing the distributed task offloading node."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from common.discovery import DiscoveryService, get_local_ip_addresses, get_primary_lan_ip
from common.messages import GPU_ENCODERS
from common.paths import CREATE_NO_WINDOW, DEFAULT_PORT, PROTOCOL_VERSION
from client.ui.log_terminal import LogTerminal
from server import storage
from server.capabilities import build_capabilities
from server.job_queue import JobPool, JobQueue
from server.listener import Listener

LOG = logging.getLogger("rdo.server_gui")

STATE_COLOURS = {
    "stopped": "#ef4444",
    "starting": "#f59e0b",
    "listening": "#10b981",
    "busy": "#a855f7",
    "transferring": "#38bdf8",
}


def card(title: str, icon: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    heading = f"{icon}  {title.upper()}" if icon else title.upper()
    label = QLabel(heading)
    label.setObjectName("CardTitle")
    layout.addWidget(label)
    return frame, layout


def metric_tile(title: str, default_val: str = "--") -> tuple[QFrame, QLabel]:
    """A styled top metrics tile for real-time counters."""
    frame = QFrame()
    frame.setObjectName("MetricTile")
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    box = QVBoxLayout(frame)
    box.setContentsMargins(12, 8, 12, 8)
    box.setSpacing(2)

    name = QLabel(title.upper())
    name.setObjectName("MetricName")
    name.setAlignment(Qt.AlignmentFlag.AlignCenter)

    val = QLabel(default_val)
    val.setObjectName("Metric")
    val.setAlignment(Qt.AlignmentFlag.AlignCenter)

    box.addWidget(name)
    box.addWidget(val)
    return frame, val


def spec_box(title: str, default_val: str = "--", badge_color: str = "") -> tuple[QFrame, QLabel]:
    """A hardware specification tile with clean wrapping and no truncation."""
    frame = QFrame()
    frame.setObjectName("MetricTile")
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    frame.setMinimumHeight(64)
    box = QVBoxLayout(frame)
    box.setContentsMargins(10, 8, 10, 8)
    box.setSpacing(4)

    name = QLabel(title.upper())
    name.setObjectName("MetricName")
    name.setAlignment(Qt.AlignmentFlag.AlignLeft)

    val = QLabel(default_val)
    val.setWordWrap(True)
    val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    if badge_color:
        val.setStyleSheet(f"color:{badge_color};font-size:9.5pt;font-weight:700;")
    else:
        val.setStyleSheet("color:#f8fafc;font-size:9.5pt;font-weight:600;")

    box.addWidget(name)
    box.addWidget(val)
    return frame, val


class QtLogBridge(logging.Handler):
    """Bridges standard Python logging records to a Qt signal for the GUI terminal."""

    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            lvl = record.levelname.lower()
            tag = "info"
            if lvl in ("warning", "warn"):
                tag = "warn"
            elif lvl in ("error", "critical"):
                tag = "error"
            elif "ok" in lvl or "success" in lvl:
                tag = "ok"
            elif lvl == "debug":
                tag = "trace"
            self.callback(tag, msg)
        except Exception:
            pass


class ServerWorkerThread(QThread):
    """Manages the background server accept loop and UDP discovery service."""

    status_changed = pyqtSignal(str, str)
    client_connected_signal = pyqtSignal(dict)
    client_disconnected_signal = pyqtSignal(str)
    stats_updated = pyqtSignal(dict)

    def __init__(
        self,
        host: str,
        port: int,
        workers: int,
        allow_all_lan: bool,
        enable_discovery: bool,
        prune_on_start: bool,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.workers = workers
        self.allow_all_lan = allow_all_lan
        self.enable_discovery = enable_discovery
        self.prune_on_start = prune_on_start

        self.listener: Listener | None = None
        self.pool: JobPool | None = None
        self.queue: JobQueue | None = None
        self.discovery: DiscoveryService | None = None
        self.caps = None
        self._stopping = threading.Event()
        self.start_time = 0.0

    def run(self) -> None:
        self.status_changed.emit("starting", "Probing hardware capabilities...")
        try:
            self.caps = build_capabilities(max_concurrent_jobs=self.workers)
            storage.ensure_dirs()
            if self.prune_on_start:
                storage.prune(keep_jobs=0)

            allowed = ["0.0.0.0/0", "::/0"] if self.allow_all_lan else [
                "127.0.0.0/8", "::1/128", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "169.254.0.0/16"
            ]

            self.queue = JobQueue()
            self.pool = JobPool(self.queue, self.workers)

            def on_start(session):
                info = {
                    "tag": getattr(session, "tag", f"{session.peer[0]}:{session.peer[1]}"),
                    "ip": session.peer[0],
                    "port": session.peer[1],
                    "name": getattr(session, "client_name", "connecting"),
                    "connected_at": time.time(),
                }
                self.client_connected_signal.emit(info)

            def on_end(session):
                tag = getattr(session, "tag", f"{session.peer[0]}:{session.peer[1]}")
                self.client_disconnected_signal.emit(tag)

            self.listener = Listener(
                self.host,
                self.port,
                self.caps,
                self.pool,
                allowed,
                on_session_start=on_start,
                on_session_end=on_end,
            )
            self.listener.bind()
            self.pool.start()
            self.start_time = time.time()

            if self.enable_discovery:
                def get_info():
                    return {
                        "gpu": self.caps.gpu_name or "CPU Only",
                        "engines": list(self.caps.engines),
                        "nvenc": self.caps.nvenc_available,
                        "workers": self.workers,
                    }

                self.discovery = DiscoveryService(self.listener.port, get_info)
                self.discovery.start()

            bound_port = self.listener.port
            self.status_changed.emit("listening", f"Listening on port {bound_port}")

            # Serve forever until stopped
            self.listener.serve_forever()
        except Exception as exc:
            LOG.exception("Server encountered error: %s", exc)
            self.status_changed.emit("stopped", f"Error: {exc}")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        if self.discovery:
            try:
                self.discovery.stop()
            except Exception:
                pass
            self.discovery = None
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
        if self.pool:
            try:
                self.pool.shutdown()
            except Exception:
                pass
        self.status_changed.emit("stopped", "Server stopped cleanly")

    def stop_server(self) -> None:
        self._stopping.set()
        if self.listener:
            self.listener.stop()
        if self.discovery:
            self.discovery.stop()


class ServerWindow(QMainWindow):
    """Comprehensive GUI Control Application for the Remote GPU/Compute Server."""

    log_signal = pyqtSignal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote Compute & Render Server — Worker Node")
        self.setMinimumSize(840, 500)
        self.resize(1200, 780)

        self.server_thread: ServerWorkerThread | None = None
        self.sessions_data: dict[str, dict] = {}
        self.job_history: list[dict] = []
        self.uptime_seconds = 0
        self.start_timestamp = 0.0

        # Hardware capabilities
        self.caps = build_capabilities(max_concurrent_jobs=2)

        self._build_ui()
        self._setup_logging()

        # Polling timer for live stats & session duration updates
        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(1000)
        self.stats_timer.timeout.connect(self._on_stats_tick)
        self.stats_timer.start()

        self._log("info", f"Server node ready on {socket.gethostname()} ({self.caps.os})")
        self._log("info", f"Detected GPU: {self.caps.gpu_name or 'None (CPU Mode)'}")
        if self.caps.hardware_encoders:
            self._log("ok", f"Hardware encoders: {', '.join(self.caps.hardware_encoders)}")
        else:
            self._log("warn", "No GPU hardware encoder detected - CPU encoding (libx264) will be used")

    # ------------------------------------------------------------------ UI Layout
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)

        # 1. Header with title and live state badge
        outer.addLayout(self._build_header())

        # 2. Live Top Metrics Ribbon (Clean, prominent real-time counters)
        outer.addWidget(self._build_stats_ribbon())

        # 3. Main Body Splitter: Scrollable Dashboard Cards on top, Terminal on bottom
        splitter = QSplitter(Qt.Orientation.Vertical)

        top_scroll = QScrollArea()
        top_scroll.setObjectName("DashboardScroll")
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        top_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        top_widget = QWidget()
        top_widget.setObjectName("ServerDashboard")
        top_layout = QHBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(10)

        top_layout.addWidget(self._build_left_panel(), 0)
        top_layout.addWidget(self._build_right_panel(), 1)

        top_scroll.setWidget(top_widget)

        splitter.addWidget(top_scroll)
        splitter.addWidget(self._build_log_panel())
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([480, 200])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        outer.addWidget(splitter, 1)
        outer.addLayout(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        title = QLabel("Remote Compute & Render Node")
        title.setObjectName("Title")
        subtitle = QLabel("Distributed Task Offloading & GPU Hardware Acceleration Server")
        subtitle.setObjectName("Subtitle")
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(title)
        col.addWidget(subtitle)
        row.addLayout(col)

        row.addStretch(1)

        self.state_pill = QLabel("🔴 SERVER STOPPED")
        self.state_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_pill.setMinimumWidth(180)
        self.state_pill.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.state_pill.setStyleSheet(
            "background:#0f172a;border:1.5px solid #ef4444;border-radius:12px;"
            f"color:{STATE_COLOURS['stopped']};padding:6px 16px;"
        )
        row.addWidget(self.state_pill)
        return row

    def _build_stats_ribbon(self) -> QWidget:
        """Horizontal ribbon displaying real-time server counters."""
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        self.client_metric, self.client_metric_val = metric_tile("ACTIVE CLIENTS", "0 Connected")
        self.jobs_metric, self.jobs_metric_val = metric_tile("TASKS COMPLETED", "0 Finished")
        self.peak_metric, self.peak_metric_val = metric_tile("PEAK SESSIONS", "0 Total")
        self.uptime_metric, self.uptime_metric_val = metric_tile("SERVER UPTIME", "--:--:--")

        layout.addWidget(self.client_metric)
        layout.addWidget(self.jobs_metric)
        layout.addWidget(self.peak_metric)
        layout.addWidget(self.uptime_metric)
        return frame

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(320)
        panel.setMaximumWidth(380)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 1. Server Configuration & Start/Stop Card
        ctrl_frame, ctrl_box = card("server controls", "⚡")

        self.toggle_btn = QPushButton("▶ Start Worker Server")
        self.toggle_btn.setObjectName("Primary")
        self.toggle_btn.setMinimumHeight(40)
        self.toggle_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.toggle_btn.setToolTip("Start listening for client connections across the LAN.")
        self.toggle_btn.clicked.connect(self._on_toggle_server)
        ctrl_box.addWidget(self.toggle_btn)

        cfg_form = QFormLayout()
        cfg_form.setSpacing(7)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(DEFAULT_PORT)
        self.port_spin.setToolTip("TCP port for offloading connections (default: 7575).")
        cfg_form.addRow("TCP Port", self.port_spin)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(2)
        self.workers_spin.setToolTip("Maximum concurrent rendering / compute jobs.")
        cfg_form.addRow("Worker Threads", self.workers_spin)
        ctrl_box.addLayout(cfg_form)

        options_box = QVBoxLayout()
        options_box.setSpacing(5)

        self.allow_all_lan_check = QCheckBox("Allow all LAN connections")
        self.allow_all_lan_check.setChecked(True)
        self.allow_all_lan_check.setToolTip("Permits clients from any local Wi-Fi, Ethernet, or direct cable IP.")
        options_box.addWidget(self.allow_all_lan_check)

        self.discovery_check = QCheckBox("Enable LAN Auto-Discovery (UDP)")
        self.discovery_check.setChecked(True)
        self.discovery_check.setToolTip("Allows client laptops to find this server automatically without typing an IP.")
        options_box.addWidget(self.discovery_check)

        self.prune_check = QCheckBox("Prune old job files on start")
        self.prune_check.setChecked(True)
        options_box.addWidget(self.prune_check)
        ctrl_box.addLayout(options_box)

        layout.addWidget(ctrl_frame)

        # 2. LAN Connection Info Card
        net_frame, net_box = card("lan connection info", "🌐")

        ip_box = QFrame()
        ip_box.setObjectName("FileBadge")
        ip_layout = QVBoxLayout(ip_box)
        ip_layout.setContentsMargins(10, 7, 10, 7)
        ip_layout.setSpacing(2)

        ip_title = QLabel("PRIMARY LAN IP FOR CLIENTS:")
        ip_title.setObjectName("MetricName")
        self.lan_ip_label = QLabel(get_primary_lan_ip())
        self.lan_ip_label.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        self.lan_ip_label.setStyleSheet("color:#38bdf8;")
        ip_layout.addWidget(ip_title)
        ip_layout.addWidget(self.lan_ip_label)
        net_box.addWidget(ip_box)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.copy_ip_btn = QPushButton("📋 Copy IP")
        self.copy_ip_btn.setToolTip("Copy this server's LAN IP address to share with the client.")
        self.copy_ip_btn.clicked.connect(self._on_copy_ip)
        btn_row.addWidget(self.copy_ip_btn)

        self.firewall_btn = QPushButton("🛡️ Firewall Rule")
        self.firewall_btn.setToolTip("Add Windows Firewall rule to permit TCP 7575 & UDP 7576.")
        self.firewall_btn.clicked.connect(self._on_add_firewall_rule)
        btn_row.addWidget(self.firewall_btn)
        net_box.addLayout(btn_row)

        adapter_label = QLabel("Active Network Adapters:")
        adapter_label.setObjectName("MetricName")
        net_box.addWidget(adapter_label)

        self.adapter_combo = QComboBox()
        self._populate_adapters()
        self.adapter_combo.currentIndexChanged.connect(self._on_adapter_changed)
        net_box.addWidget(self.adapter_combo)

        layout.addWidget(net_frame)
        layout.addStretch(1)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 1. Hardware Specs Card (2x3 grid, clean full text, no string slicing!)
        hw_frame, hw_box = card("hardware & acceleration engine", "💻")

        hw_grid = QGridLayout()
        hw_grid.setSpacing(8)

        # GPU info
        gpu_name = self.caps.gpu_name if self.caps.gpu_name else "Standard CPU (No GPU)"
        gpu_tile, self.gpu_val = spec_box("GRAPHICS HARDWARE", gpu_name)
        self.gpu_val.setToolTip(self.caps.gpu_name or "CPU Mode")
        hw_grid.addWidget(gpu_tile, 0, 0)

        # NVENC status
        if self.caps.nvenc_available:
            nvenc_txt = "✅ Hardware NVENC Active"
            nvenc_col = "#10b981"
        else:
            nvenc_txt = "⚠️ CPU Software (libx264)"
            nvenc_col = "#f59e0b"
        nvenc_tile, self.nvenc_val = spec_box("NVENC ACCELERATION", nvenc_txt, nvenc_col)
        hw_grid.addWidget(nvenc_tile, 0, 1)

        # Compute engine
        if self.caps.cuda_available:
            comp_txt = "⚡ PyTorch (CUDA Active)"
            comp_col = "#38bdf8"
        elif self.caps.torch_available:
            comp_txt = "⚡ PyTorch (CPU Mode)"
            comp_col = "#38bdf8"
        else:
            comp_txt = "NumPy Tensor Engine"
            comp_col = "#94a3b8"
        comp_tile, self.comp_val = spec_box("COMPUTE ENGINE", comp_txt, comp_col)
        hw_grid.addWidget(comp_tile, 0, 2)

        # CPU Cores & OS
        cpu_txt = f"{self.caps.cpu_count} Cores ({self.caps.os})"
        cpu_tile, self.cpu_val = spec_box("CPU / SYSTEM", cpu_txt)
        hw_grid.addWidget(cpu_tile, 1, 0)

        # FFmpeg Build (Clean formatting without ' | f' bug)
        if self.caps.ffmpeg_version:
            raw_ver = self.caps.ffmpeg_version.split(" | ")[0]
            ff_txt = f"FFmpeg {raw_ver}"
        else:
            ff_txt = "FFmpeg Detected" if self.caps.ffmpeg_path else "Not Installed"
        ffmpeg_tile, self.ffmpeg_val = spec_box("FFMPEG BUILD", ff_txt)
        self.ffmpeg_val.setToolTip(self.caps.ffmpeg_path or "FFmpeg")
        hw_grid.addWidget(ffmpeg_tile, 1, 1)

        # Supported Encoders (Clean list with word wrap)
        encs_list = ", ".join(self.caps.hardware_encoders) if self.caps.hardware_encoders else "libx264 (CPU)"
        encs_tile, self.encs_val = spec_box("SUPPORTED ENCODERS", encs_list)
        self.encs_val.setToolTip(encs_list)
        hw_grid.addWidget(encs_tile, 1, 2)

        hw_box.addLayout(hw_grid)
        layout.addWidget(hw_frame)

        # 2. Activity Tabs (Connected Clients & Job Queue)
        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(180)

        # Clients Table Tab
        self.clients_table = QTableWidget(0, 5)
        self.clients_table.setHorizontalHeaderLabels(["Client IP", "Port", "Client Name", "Connected Time", "Active Jobs"])
        self.clients_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.clients_table.verticalHeader().setVisible(False)
        self.clients_table.setAlternatingRowColors(True)
        self.clients_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabs.addTab(self.clients_table, "🌐 Connected Clients (0)")

        # Jobs Table Tab (Note: escaped && so '&' is displayed, not eaten as shortcut!)
        self.jobs_table = QTableWidget(0, 5)
        self.jobs_table.setHorizontalHeaderLabels(["Job ID", "Client", "Task Type", "Status", "Elapsed Time"])
        self.jobs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.jobs_table.verticalHeader().setVisible(False)
        self.jobs_table.setAlternatingRowColors(True)
        self.tabs.addTab(self.jobs_table, "⚙️ Task Activity && Queue (0)")

        layout.addWidget(self.tabs, 1)
        return panel

    def _build_log_panel(self) -> QWidget:
        frame, box = card("server diagnostic terminal", "📋")
        box.setContentsMargins(10, 8, 10, 8)
        self.terminal = LogTerminal()
        box.addWidget(self.terminal, 1)
        return frame

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.terminal.clear)
        row.addWidget(clear_btn)

        save_btn = QPushButton("Save Log...")
        save_btn.clicked.connect(self._on_save_log)
        row.addWidget(save_btn)

        open_data_btn = QPushButton("Open Storage Folder")
        open_data_btn.clicked.connect(self._on_open_storage)
        row.addWidget(open_data_btn)

        row.addStretch(1)

        self.footer_status = QLabel("Ready to start worker node.")
        self.footer_status.setObjectName("Hint")
        row.addWidget(self.footer_status)
        return row

    # ------------------------------------------------------------------ Server Control
    def _populate_adapters(self) -> None:
        self.adapter_combo.clear()
        addrs = get_local_ip_addresses()
        primary = get_primary_lan_ip()
        selected_idx = 0
        for idx, a in enumerate(addrs):
            ip = a["ip"]
            desc = a["description"]
            text = f"{ip}  ({desc})"
            self.adapter_combo.addItem(text, ip)
            if ip == primary:
                selected_idx = idx
        self.adapter_combo.setCurrentIndex(selected_idx)

    def _on_adapter_changed(self) -> None:
        ip = self.adapter_combo.currentData()
        if ip:
            self.lan_ip_label.setText(str(ip))

    def _on_copy_ip(self) -> None:
        clipboard = QApplication.clipboard()
        ip = self.lan_ip_label.text()
        clipboard.setText(ip)
        self.copy_ip_btn.setText("✅ Copied!")
        QTimer.singleShot(1800, lambda: self.copy_ip_btn.setText("📋 Copy IP"))

    def _on_add_firewall_rule(self) -> None:
        """Add Windows Firewall rules to permit TCP 7575 and UDP 7576."""
        port = self.port_spin.value()
        cmd_tcp = f'netsh advfirewall firewall add rule name="RDO Server TCP {port}" dir=in action=allow protocol=TCP localport={port}'
        cmd_udp = 'netsh advfirewall firewall add rule name="RDO Discovery UDP 7576" dir=in action=allow protocol=UDP localport=7576'
        try:
            r1 = subprocess.run(cmd_tcp, shell=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            r2 = subprocess.run(cmd_udp, shell=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if r1.returncode == 0:
                QMessageBox.information(
                    self,
                    "Firewall Configured",
                    f"Windows Firewall rules added successfully!\nIncoming TCP port {port} and UDP port 7576 are now permitted on LAN.",
                )
                self._log("ok", f"Windows Firewall rules created for port {port} and UDP 7576")
            else:
                QMessageBox.warning(
                    self,
                    "Administrator Privileges Needed",
                    "To add firewall rules automatically, please right-click the application and select 'Run as Administrator'.\n\nCommand to run:\n" + cmd_tcp,
                )
        except Exception as exc:
            QMessageBox.warning(self, "Firewall Configuration", f"Could not execute netsh: {exc}")

    def _on_toggle_server(self) -> None:
        if self.server_thread and self.server_thread.isRunning():
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self) -> None:
        port = self.port_spin.value()
        workers = self.workers_spin.value()
        allow_all = self.allow_all_lan_check.isChecked()
        discovery = self.discovery_check.isChecked()
        prune = self.prune_check.isChecked()

        self.toggle_btn.setEnabled(False)
        self.toggle_btn.setText("Starting...")
        self.port_spin.setEnabled(False)
        self.workers_spin.setEnabled(False)
        self.allow_all_lan_check.setEnabled(False)

        self.server_thread = ServerWorkerThread(
            host="0.0.0.0",
            port=port,
            workers=workers,
            allow_all_lan=allow_all,
            enable_discovery=discovery,
            prune_on_start=prune,
        )
        self.server_thread.status_changed.connect(self._on_status_changed)
        self.server_thread.client_connected_signal.connect(self._on_client_connected)
        self.server_thread.client_disconnected_signal.connect(self._on_client_disconnected)
        self.server_thread.start()

    def _stop_server(self) -> None:
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.setText("Stopping...")
        if self.server_thread:
            self.server_thread.stop_server()

    @pyqtSlot(str, str)
    def _on_status_changed(self, state: str, detail: str) -> None:
        self.toggle_btn.setEnabled(True)
        if state == "listening":
            self.toggle_btn.setText("⏹ Stop Worker Server")
            self.toggle_btn.setStyleSheet("background-color:#7f1d1d;color:#fca5a5;border:1.5px solid #ef4444;font-weight:700;")
            port = self.server_thread.listener.port if self.server_thread and self.server_thread.listener else self.port_spin.value()
            self.state_pill.setText(f"🟢 LISTENING :{port}")
            self.state_pill.setStyleSheet(
                "background:#0f172a;border:1.5px solid #10b981;border-radius:12px;"
                f"color:{STATE_COLOURS['listening']};padding:6px 16px;"
            )
            self.footer_status.setText(f"Server is ONLINE on 0.0.0.0:{port} ({detail})")
            self._log("ok", f"Server listening on all network interfaces (port {port})")
        elif state == "stopped":
            self.toggle_btn.setText("▶ Start Worker Server")
            self.toggle_btn.setStyleSheet("")
            self.state_pill.setText("🔴 SERVER STOPPED")
            self.state_pill.setStyleSheet(
                "background:#0f172a;border:1.5px solid #ef4444;border-radius:12px;"
                f"color:{STATE_COLOURS['stopped']};padding:6px 16px;"
            )
            self.port_spin.setEnabled(True)
            self.workers_spin.setEnabled(True)
            self.allow_all_lan_check.setEnabled(True)
            self.footer_status.setText(f"Server stopped. {detail}")
            self.clients_table.setRowCount(0)
            self.sessions_data.clear()
            self.tabs.setTabText(0, "🌐 Connected Clients (0)")
            self.client_metric_val.setText("0 Connected")
            self.uptime_metric_val.setText("--:--:--")
            self._log("info", f"Server stopped ({detail})")

    @pyqtSlot(dict)
    def _on_client_connected(self, info: dict) -> None:
        tag = info["tag"]
        self.sessions_data[tag] = info
        self._refresh_clients_table()
        self._log("ok", f"Client connected: {info['ip']}:{info['port']} (Session active)")

    @pyqtSlot(str)
    def _on_client_disconnected(self, tag: str) -> None:
        if tag in self.sessions_data:
            info = self.sessions_data.pop(tag)
            self._log("info", f"Client disconnected: {info.get('ip', tag)}")
        self._refresh_clients_table()

    def _refresh_clients_table(self) -> None:
        count = len(self.sessions_data)
        self.tabs.setTabText(0, f"🌐 Connected Clients ({count})")
        self.clients_table.setRowCount(count)
        row = 0
        now = time.time()
        for tag, info in self.sessions_data.items():
            duration = int(now - info.get("connected_at", now))
            m, s = divmod(duration, 60)
            dur_str = f"{m:02d}:{s:02d}"

            self.clients_table.setItem(row, 0, QTableWidgetItem(str(info.get("ip", "--"))))
            self.clients_table.setItem(row, 1, QTableWidgetItem(str(info.get("port", "--"))))
            self.clients_table.setItem(row, 2, QTableWidgetItem(str(info.get("name", "rdo-client"))))
            self.clients_table.setItem(row, 3, QTableWidgetItem(dur_str))
            self.clients_table.setItem(row, 4, QTableWidgetItem(str(info.get("active_jobs", 0))))
            row += 1

    def _on_stats_tick(self) -> None:
        if self.server_thread and self.server_thread.isRunning():
            listener = self.server_thread.listener
            pool = self.server_thread.pool
            queue = self.server_thread.queue

            if listener:
                active_conn = listener.active_connections
                total_conn = listener.total_connections
                self.client_metric_val.setText(f"{active_conn} Connected" if active_conn != 1 else "1 Connected")
                self.peak_metric_val.setText(f"{total_conn} Total")

                # Update live sessions table
                sessions = listener.get_active_sessions_info()
                if len(sessions) != len(self.sessions_data):
                    for s in sessions:
                        self.sessions_data[s["tag"]] = s
                    self._refresh_clients_table()

            if queue:
                snap = queue.get_snapshot()
                completed = snap["completed_count"]
                active_jobs = snap["active_count"]
                self.jobs_metric_val.setText(f"{completed} Finished")
                self.tabs.setTabText(1, f"⚙️ Task Activity && Queue ({active_jobs})")

                # Update state pill if jobs are running
                if active_jobs > 0:
                    self.state_pill.setText(f"⚡ PROCESSING ({active_jobs} ACTIVE)")
                    self.state_pill.setStyleSheet(
                        "background:#0f172a;border:1.5px solid #a855f7;border-radius:12px;"
                        f"color:{STATE_COLOURS['busy']};padding:6px 16px;"
                    )

            if self.server_thread.start_time > 0:
                elapsed = int(time.time() - self.server_thread.start_time)
                h, rem = divmod(elapsed, 3600)
                m, s = divmod(rem, 60)
                self.uptime_metric_val.setText(f"{h:02d}:{m:02d}:{s:02d}")
        else:
            self.client_metric_val.setText("0 Connected")
            self.uptime_metric_val.setText("--:--:--")

    # ------------------------------------------------------------------ Logging & Storage
    def _setup_logging(self) -> None:
        self.log_signal.connect(self._log)
        handler = QtLogBridge(self.log_signal.emit)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root_rdo = logging.getLogger("rdo")
        root_rdo.setLevel(logging.INFO)
        root_rdo.addHandler(handler)

    def _log(self, level: str, message: str) -> None:
        self.terminal.append_log(level, message)

    def _on_save_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Log", "server_log.txt", "Text Files (*.txt)")
        if path:
            try:
                Path(path).write_text(self.terminal.toPlainText(), encoding="utf-8")
                self._log("ok", f"Log saved to {path}")
            except Exception as exc:
                QMessageBox.warning(self, "Save Error", f"Failed to save log: {exc}")

    def _on_open_storage(self) -> None:
        storage_path = str(storage.DATA_ROOT)
        if sys.platform == "win32":
            os.startfile(storage_path)
        else:
            subprocess.run(["xdg-open", storage_path], check=False)

    def closeEvent(self, event) -> None:
        if self.server_thread and self.server_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "Confirm Exit",
                "The worker server is currently running. Stopping it will disconnect active clients.\n\nAre you sure you want to exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._stop_server()
            self.server_thread.wait(2000)
        event.accept()
