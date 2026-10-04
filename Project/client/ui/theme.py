"""Dark theme: qdarktheme base plus the few rules this app needs on top."""

from __future__ import annotations

APP_STYLE = """
QWidget#Root { background: #0f1117; }

QLabel#Title      { color: #f8fafc; font-size: 18pt; font-weight: 700; font-family: "Segoe UI", sans-serif; }
QLabel#Subtitle   { color: #94a3b8; font-size: 9.5pt; font-weight: 400; }
QLabel#CardTitle  { color: #e2e8f0; font-size: 10pt; font-weight: 700; letter-spacing: 0.8px; }
QLabel#Hint       { color: #78859e; font-size: 8.5pt; }
QLabel#Metric     { color: #38bdf8; font-size: 13pt; font-weight: 700; font-family: "Segoe UI", sans-serif; }
QLabel#MetricName { color: #94a3b8; font-size: 8pt; font-weight: 600; letter-spacing: 0.5px; }

QFrame#Card {
    background: #161922;
    border: 1px solid #262d3d;
    border-radius: 9px;
}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #0f121a;
    border: 1px solid #2b3345;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f1f5f9;
    font-size: 9pt;
    selection-background-color: #2563eb;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1.5px solid #3b82f6; background: #131722; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: #525e75; background: #0c0e14; border-color: #1e2430; }

QPushButton {
    background: #222736; border: 1px solid #323b4e; border-radius: 6px;
    padding: 6px 14px; color: #e2e8f0; font-size: 9pt; font-weight: 500;
}
QPushButton:hover  { background: #2b3245; border-color: #414d66; }
QPushButton:pressed { background: #1a1e2b; }
QPushButton:disabled { color: #4b556b; background: #141720; border-color: #1e2432; }

QPushButton#Primary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2563eb, stop:1 #1d4ed8);
    border: 1px solid #3b82f6; color: #ffffff; font-weight: 600;
}
QPushButton#Primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3b82f6, stop:1 #2563eb);
    border-color: #60a5fa;
}
QPushButton#Primary:disabled { background: #1a2338; border-color: #1e2942; color: #475569; }

QPushButton#Danger {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #dc2626, stop:1 #b91c1c);
    border: 1px solid #ef4444; color: #ffffff; font-weight: 600;
}
QPushButton#Danger:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ef4444, stop:1 #dc2626);
}

QProgressBar {
    background: #0d1017; border: 1px solid #232a3a; border-radius: 7px;
    height: 20px; text-align: center; color: #ffffff; font-size: 9pt; font-weight: 600;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #10b981);
    border-radius: 6px;
}

QPlainTextEdit#Log {
    background: #090b0f; border: 1px solid #202633; border-radius: 7px;
    font-family: "Cascadia Code", "Cascadia Mono", "Consolas", monospace;
    font-size: 9pt; color: #e2e8f0;
}
QPlainTextEdit#Caps {
    background: #0f121a; border: 1px solid #252c3c; border-radius: 6px;
    font-family: "Cascadia Code", "Cascadia Mono", "Consolas", monospace;
    font-size: 8.5pt; color: #cbd5e1;
}

QTabWidget::pane { border: 1px solid #262d3d; border-radius: 7px; top: -1px; background: #161922; }
QTabBar::tab {
    background: #11141c; color: #94a3b8; padding: 7px 16px;
    border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 3px; font-weight: 500;
}
QTabBar::tab:selected { background: #1a1f2b; color: #38bdf8; border-bottom: 2px solid #38bdf8; font-weight: 600; }

QCheckBox { color: #cbd5e1; font-size: 9pt; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #333a49; background: #0f121a; }
QCheckBox::indicator:checked { background: #2563eb; border-color: #60a5fa; }

QScrollBar:vertical   { background: #0f1117; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #2d3547; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #3b465e; }
QScrollBar:horizontal { background: #0f1117; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #2d3547; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QGroupBox { border: 1px solid #262d3d; border-radius: 6px; margin-top: 10px; }
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #94a3b8;
}
QToolTip {
    background: #1e2433; color: #f8fafc; border: 1px solid #3b82f6;
    padding: 6px 10px; border-radius: 6px; font-size: 9pt;
}
"""

LEVEL_COLOURS = {
    "trace": "#6f7a90",
    "info": "#a8b3c7",
    "ok": "#37b3a4",
    "warn": "#e0b155",
    "error": "#e0657a",
    "command": "#7f9fd6",
}


def build_stylesheet() -> str:
    """qdarktheme when it is installed, otherwise the built-in sheet alone."""
    try:
        import qdarktheme

        return qdarktheme.load_stylesheet("dark") + APP_STYLE
    except Exception:  # noqa: BLE001 - the app must run without the extra package
        return APP_STYLE


def stylesheet_available() -> bool:
    try:
        import qdarktheme  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = ["build_stylesheet", "stylesheet_available", "LEVEL_COLOURS", "APP_STYLE"]