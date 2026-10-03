"""Dark theme: qdarktheme base plus the few rules this app needs on top."""

from __future__ import annotations

APP_STYLE = """
QWidget#Root { background: #14161c; }

QLabel#Title      { color: #e8ecf4; font-size: 17pt; font-weight: 600; }
QLabel#Subtitle   { color: #8a93a6; font-size: 9pt; }
QLabel#CardTitle  { color: #c7d0e0; font-size: 10pt; font-weight: 600;
                    letter-spacing: 0.6px; }
QLabel#Hint       { color: #6f7a90; font-size: 8pt; }
QLabel#Metric     { color: #e8ecf4; font-size: 12pt; font-weight: 600; }
QLabel#MetricName { color: #8a93a6; font-size: 8pt; }

QFrame#Card {
    background: #1b1e26;
    border: 1px solid #262b36;
    border-radius: 8px;
}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #12141a;
    border: 1px solid #2c3240;
    border-radius: 5px;
    padding: 5px 8px;
    selection-background-color: #3d6fd4;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #3d6fd4; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: #5b6478; }

QPushButton {
    background: #262b36; border: 1px solid #333a49; border-radius: 5px;
    padding: 6px 14px; color: #dde3ef;
}
QPushButton:hover  { background: #2f3542; }
QPushButton:pressed { background: #1f232c; }
QPushButton:disabled { color: #5b6478; background: #1b1e26; border-color: #262b36; }
QPushButton#Primary {
    background: #3d6fd4; border: 1px solid #4d7de0; color: #ffffff; font-weight: 600;
}
QPushButton#Primary:hover { background: #4a7ce0; }
QPushButton#Primary:disabled { background: #26314a; border-color: #26314a; color: #6c7891; }
QPushButton#Danger { background: #8f3540; border-color: #a84550; color: #ffffff; }
QPushButton#Danger:hover { background: #a04049; }

QProgressBar {
    background: #12141a; border: 1px solid #2c3240; border-radius: 6px;
    height: 18px; text-align: center; color: #dde3ef; font-size: 9pt;
}
QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #3d6fd4, stop:1 #37b3a4); border-radius: 5px; }

QPlainTextEdit#Log {
    background: #0d0f14; border: 1px solid #262b36; border-radius: 6px;
    font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 9pt;
}
QTabWidget::pane { border: 1px solid #262b36; border-radius: 6px; top: -1px; }
QTabBar::tab {
    background: #1b1e26; color: #8a93a6; padding: 6px 14px;
    border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px;
}
QTabBar::tab:selected { background: #262b36; color: #e8ecf4; }

QScrollBar:vertical   { background: #14161c; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #333a49; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #414a5e; }
QScrollBar:horizontal { background: #14161c; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #333a49; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QGroupBox { border: 1px solid #262b36; border-radius: 6px; margin-top: 10px; }
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #8a93a6;
}
QToolTip {
    background: #262b36; color: #e8ecf4; border: 1px solid #3d6fd4;
    padding: 4px; border-radius: 4px;
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