"""Client GUI entry point.

    python client/app.py

Loads the dark theme, opens the main window and starts the network worker thread.
Command-line flags let the same binary be launched for a quick health check
without anyone clicking a button, which is handy when the two machines are on
different sides of a switch.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from client.ui.main_window import MainWindow  # noqa: E402
from client.ui.theme import build_stylesheet, stylesheet_available  # noqa: E402

APP_NAME = "Remote GPU Rendering"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.py", description=f"{APP_NAME} client")
    parser.add_argument("--host", default=os.environ.get("RDO_WORKER", ""),
                        help="pre-fill the worker IP address")
    parser.add_argument("--port", type=int, default=7575, help="pre-fill the worker port")
    parser.add_argument("--no-dark", action="store_true",
                        help="skip the qdarktheme base stylesheet")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("RDO")

    if args.no_dark:
        from client.ui.theme import APP_STYLE

        app.setStyleSheet(APP_STYLE)
    else:
        app.setStyleSheet(build_stylesheet())

    window = MainWindow()
    if args.host:
        window.host_edit.setText(args.host)
    if args.port:
        window.port_edit.setValue(args.port)
    if not stylesheet_available():
        window.terminal.append_log(
            "warn", "pyqtdarktheme is not installed - using the built-in stylesheet only")
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())