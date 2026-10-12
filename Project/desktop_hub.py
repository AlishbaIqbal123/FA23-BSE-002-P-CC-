"""Unified Desktop Control Hub for the Distributed Computing System.

Provides an intuitive graphical launcher on both desktops, eliminating
the need for Shell or CMD commands. Users can start either the Server Node,
the Client Controller, test network connectivity, or install desktop shortcuts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from common.discovery import get_local_ip_addresses, get_primary_lan_ip, test_tcp_connection
from common.paths import CREATE_NO_WINDOW, DEFAULT_PORT
from client.ui.theme import build_stylesheet, stylesheet_available

APP_NAME = "Distributed Computing Hub"


def card_frame(title: str, icon: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
    heading = f"{icon}  {title.upper()}" if icon else title.upper()
    label = QLabel(heading)
    label.setObjectName("CardTitle")
    layout.addWidget(label)
    return frame, layout


class DiagnosticsDialog(QDialog):
    """Network diagnostic and IP inspector dialog."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("LAN Diagnostics & IP Inspector")
        self.resize(550, 420)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Local Network Adapters & Addresses")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        addrs = get_local_ip_addresses()
        primary = get_primary_lan_ip()

        addrs_scroll = QScrollArea()
        addrs_scroll.setWidgetResizable(True)
        addrs_scroll.setFrameShape(QFrame.Shape.NoFrame)
        addrs_scroll.setMaximumHeight(150)
        addrs_widget = QWidget()
        addrs_layout = QVBoxLayout(addrs_widget)
        addrs_layout.setContentsMargins(0, 0, 0, 0)
        addrs_layout.setSpacing(6)

        for a in addrs:
            box = QFrame()
            box.setObjectName("MetricTile")
            b_layout = QHBoxLayout(box)
            b_layout.setContentsMargins(10, 8, 10, 8)
            ip_lbl = QLabel(a["ip"])
            ip_lbl.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
            if a["ip"] == primary:
                ip_lbl.setStyleSheet("color:#38bdf8;")
                tag = QLabel("★ PRIMARY LAN")
                tag.setStyleSheet("color:#10b981;font-weight:700;")
            else:
                tag = QLabel(a["description"])
                tag.setStyleSheet("color:#94a3b8;")
            b_layout.addWidget(ip_lbl)
            b_layout.addWidget(tag)
            b_layout.addStretch(1)
            addrs_layout.addWidget(box)

        addrs_scroll.setWidget(addrs_widget)
        layout.addWidget(addrs_scroll)

        # Test reachability form
        test_title = QLabel("Test Reachability to Another Computer")
        test_title.setObjectName("CardTitle")
        layout.addWidget(test_title)

        form = QFormLayout()
        self.target_ip = QLineEdit()
        self.target_ip.setPlaceholderText("e.g. 192.168.100.3 or 192.168.1.1")
        self.target_port = QSpinBox()
        self.target_port.setRange(1, 65535)
        self.target_port.setValue(DEFAULT_PORT)
        form.addRow("Target IP:", self.target_ip)
        form.addRow("Target Port:", self.target_port)
        layout.addLayout(form)

        self.res_lbl = QLabel("")
        self.res_lbl.setWordWrap(True)
        layout.addWidget(self.res_lbl)

        btn_row = QHBoxLayout()
        test_btn = QPushButton("⚡ Ping & Test TCP Port")
        test_btn.setObjectName("Primary")
        test_btn.clicked.connect(self._run_test)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(test_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _run_test(self) -> None:
        ip = self.target_ip.text().strip()
        port = self.target_port.value()
        if not ip:
            self.res_lbl.setText("❌ Please enter an IP address.")
            return
        self.res_lbl.setText("Testing connection...")
        QApplication.processEvents()
        ok, lat, msg = test_tcp_connection(ip, port, timeout=2.0)
        if ok:
            self.res_lbl.setText(f"✅ Reachable! Latency: {lat:.1f} ms. Server is active on port {port}.")
            self.res_lbl.setStyleSheet("color:#10b981;font-weight:bold;")
        else:
            self.res_lbl.setText(f"❌ Failed: {msg}")
            self.res_lbl.setStyleSheet("color:#ef4444;")


class DesktopHub(QMainWindow):
    """Main launcher hub for the distributed computing system."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(700, 440)
        self.resize(920, 580)
        self._build_ui()

    def _build_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setObjectName("DashboardScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        root = QWidget()
        root.setObjectName("Root")
        scroll.setWidget(root)
        self.setCentralWidget(scroll)

        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(18, 14, 18, 14)
        main_layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        subtitle = QLabel("High-Performance Parallel Task Offloading & GPU Acceleration System")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        lan_ip = get_primary_lan_ip()
        ip_badge = QLabel(f"🌐 This Machine LAN IP: {lan_ip}")
        ip_badge.setObjectName("FileBadge")
        header.addWidget(ip_badge)
        main_layout.addLayout(header)

        # Welcome / Explanation
        intro = QLabel(
            "Welcome! This system connects two computers over your local network (LAN) "
            "to offload heavy rendering and compute tasks. Choose how to run this computer below:"
        )
        intro.setObjectName("Hint")
        intro.setStyleSheet("font-size:9.5pt;")
        main_layout.addWidget(intro)

        # 2 Main Mode Cards Side by Side
        cards_row = QHBoxLayout()
        cards_row.setSpacing(14)

        # Server Mode Card
        srv_frame, srv_box = card_frame("server node (compute / gpu worker)", "🖥️")
        srv_desc = QLabel(
            "Run this computer as the <b>Compute & Render Server</b>.<br><br>"
            "• Listens for offloading requests from client machines.<br>"
            "• Utilizes hardware GPU (NVENC, CUDA) or multi-core CPU.<br>"
            "• Features live client tracking, job queue monitor, and auto-discovery.<br><br>"
            "<i>(Start this first on the computer doing the heavy work)</i>"
        )
        srv_desc.setStyleSheet("color:#cbd5e1;line-height:1.4;")
        srv_desc.setWordWrap(True)
        srv_box.addWidget(srv_desc)
        srv_box.addStretch(1)

        self.btn_launch_server = QPushButton("▶ Launch Server Node")
        self.btn_launch_server.setObjectName("Primary")
        self.btn_launch_server.setMinimumHeight(44)
        self.btn_launch_server.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_launch_server.clicked.connect(self._launch_server)
        srv_box.addWidget(self.btn_launch_server)
        cards_row.addWidget(srv_frame, 1)

        # Client Mode Card
        cli_frame, cli_box = card_frame("client controller (task offloader)", "💻")
        cli_desc = QLabel(
            "Run this computer as the <b>Client Controller</b>.<br><br>"
            "• Offload video transcoding (MP4/MKV) and tensor operations.<br>"
            "• Zero-Config LAN scanner automatically discovers servers.<br>"
            "• Live dual progress bars, transfer speeds, and speedup metrics.<br><br>"
            "<i>(Run this on the laptop or desktop submitting the tasks)</i>"
        )
        cli_desc.setStyleSheet("color:#cbd5e1;line-height:1.4;")
        cli_desc.setWordWrap(True)
        cli_box.addWidget(cli_desc)
        cli_box.addStretch(1)

        self.btn_launch_client = QPushButton("🚀 Launch Client Controller")
        self.btn_launch_client.setObjectName("Primary")
        self.btn_launch_client.setStyleSheet("background-color:#1e3a8a;border:1.5px solid #38bdf8;")
        self.btn_launch_client.setMinimumHeight(44)
        self.btn_launch_client.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.btn_launch_client.clicked.connect(self._launch_client)
        cli_box.addWidget(self.btn_launch_client)
        cards_row.addWidget(cli_frame, 1)

        main_layout.addLayout(cards_row, 1)

        # Bottom Utilities Bar
        util_frame = QFrame()
        util_frame.setObjectName("Card")
        util_box = QHBoxLayout(util_frame)
        util_box.setContentsMargins(12, 10, 12, 10)
        util_box.setSpacing(10)

        util_title = QLabel("Desktop Utilities:")
        util_title.setObjectName("CardTitle")
        util_box.addWidget(util_title)

        diag_btn = QPushButton("🔍 LAN Diagnostics")
        diag_btn.setToolTip("Check network adapters and test connection to remote machine.")
        diag_btn.clicked.connect(self._open_diagnostics)
        util_box.addWidget(diag_btn)

        fw_btn = QPushButton("🛡️ Windows Firewall Helper")
        fw_btn.setToolTip("Add firewall rules so the two computers can communicate over LAN.")
        fw_btn.clicked.connect(self._open_firewall_helper)
        util_box.addWidget(fw_btn)

        sc_btn = QPushButton("📌 Create Desktop Shortcuts")
        sc_btn.setToolTip("Create convenient 1-click shortcuts directly on your Windows Desktop.")
        sc_btn.clicked.connect(self._install_shortcuts)
        util_box.addWidget(sc_btn)

        util_box.addStretch(1)

        exit_btn = QPushButton("Exit")
        exit_btn.clicked.connect(self.close)
        util_box.addWidget(exit_btn)

        main_layout.addWidget(util_frame)

    def _launch_server(self) -> None:
        try:
            from server.app import ServerWindow
            self.server_win = ServerWindow()
            self.server_win.show()
        except Exception as exc:
            QMessageBox.critical(self, "Error Launching Server", f"Failed to start server window: {exc}")

    def _launch_client(self) -> None:
        try:
            from client.ui.main_window import MainWindow
            self.client_win = MainWindow()
            self.client_win.show()
        except Exception as exc:
            QMessageBox.critical(self, "Error Launching Client", f"Failed to start client window: {exc}")

    def _open_diagnostics(self) -> None:
        dlg = DiagnosticsDialog(self)
        dlg.exec()

    def _open_firewall_helper(self) -> None:
        cmd_tcp = f'netsh advfirewall firewall add rule name="RDO Server TCP {DEFAULT_PORT}" dir=in action=allow protocol=TCP localport={DEFAULT_PORT}'
        cmd_udp = 'netsh advfirewall firewall add rule name="RDO Discovery UDP 7576" dir=in action=allow protocol=UDP localport=7576'
        try:
            r1 = subprocess.run(cmd_tcp, shell=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            r2 = subprocess.run(cmd_udp, shell=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if r1.returncode == 0:
                QMessageBox.information(
                    self,
                    "Firewall Configured",
                    "✅ Windows Firewall has been configured!\n\n"
                    f"Incoming TCP port {DEFAULT_PORT} and UDP discovery port 7576 are now allowed for LAN connections.",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Administrator Privileges Needed",
                    "To configure firewall rules automatically, please right-click this application and select 'Run as Administrator'.\n\n"
                    "Command:\n" + cmd_tcp,
                )
        except Exception as exc:
            QMessageBox.warning(self, "Firewall Configuration", f"Could not run netsh: {exc}")

    def _install_shortcuts(self) -> None:
        try:
            from tools.install_shortcuts import create_shortcuts
            paths = create_shortcuts()
            if paths:
                msg = "✅ Shortcuts successfully created on your Desktop:\n\n" + "\n".join(f"• {Path(p).name}" for p in paths)
                QMessageBox.information(self, "Shortcuts Created", msg)
            else:
                QMessageBox.warning(self, "Shortcut Creation", "Could not locate Desktop directory.")
        except Exception as exc:
            QMessageBox.warning(self, "Shortcut Creation", f"Error creating shortcuts: {exc}")


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("RDO")
    app.setStyleSheet(build_stylesheet())

    hub = DesktopHub()
    hub.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
