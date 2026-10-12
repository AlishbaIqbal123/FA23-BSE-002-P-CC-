"""Server GUI application entry point.

    python server/app.py

Launches the desktop GUI for the remote GPU worker server with live network monitoring,
hardware capability inspection, active connection tracking, and one-click start/stop.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from client.ui.theme import build_stylesheet, stylesheet_available  # noqa: E402
from server.ui.server_window import ServerWindow  # noqa: E402

APP_NAME = "Remote GPU Server Node"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server/app.py", description=f"{APP_NAME}")
    parser.add_argument("--port", type=int, default=7575, help="default port to listen on")
    parser.add_argument("--auto-start", action="store_true", help="start listening immediately on launch")
    parser.add_argument("--no-dark", action="store_true", help="skip the qdarktheme base stylesheet")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("RDO-Server")
    app.setOrganizationName("RDO")

    if args.no_dark:
        from client.ui.theme import APP_STYLE

        app.setStyleSheet(APP_STYLE)
    else:
        app.setStyleSheet(build_stylesheet())

    window = ServerWindow()
    if args.port:
        window.port_spin.setValue(args.port)
    if args.auto_start:
        window._start_server()

    if not stylesheet_available():
        window._log("warn", "pyqtdarktheme is not installed - using the built-in dark stylesheet")

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
