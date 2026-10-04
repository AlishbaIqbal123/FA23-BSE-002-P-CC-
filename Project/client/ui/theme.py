"""Dark tech theme: modern Dark Slate, Electric Cyan, and Royal Indigo design system."""

from __future__ import annotations

APP_STYLE = """
/* Base Window & Container Styling */
QMainWindow, QWidget#Root {
    background-color: #0b0f19;
    color: #f8fafc;
}

/* Typography Hierarchy */
QLabel#Title {
    color: #f8fafc;
    font-size: 17pt;
    font-weight: 700;
    font-family: "Segoe UI", -apple-system, sans-serif;
    letter-spacing: -0.3px;
}

QLabel#Subtitle {
    color: #38bdf8;
    font-size: 9pt;
    font-weight: 500;
    font-family: "Segoe UI", sans-serif;
}

QLabel#CardTitle {
    color: #38bdf8;
    font-size: 8.5pt;
    font-weight: 700;
    font-family: "Segoe UI", sans-serif;
    letter-spacing: 0.8px;
}

QLabel#Hint {
    color: #94a3b8;
    font-size: 8.5pt;
}

QLabel#Metric {
    color: #38bdf8;
    font-size: 13pt;
    font-weight: 700;
    font-family: "Segoe UI", sans-serif;
}

QLabel#MetricName {
    color: #94a3b8;
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 0.5px;
}

QLabel#FileBadge {
    background-color: #0b1220;
    border: 1.5px solid #2563eb;
    border-radius: 6px;
    padding: 6px 14px;
    color: #38bdf8;
    font-size: 9pt;
    font-weight: 600;
}

/* Cards & Surface Containers */
QFrame#Card {
    background-color: #131b2e;
    border: 1.5px solid #23324d;
    border-radius: 10px;
}

QFrame#MetricTile {
    background-color: #17223b;
    border: 1.5px solid #283953;
    border-radius: 8px;
}
QFrame#MetricTile:hover {
    border-color: #38bdf8;
    background-color: #1c2b4a;
}

/* Form Inputs, SpinBoxes & Dropdowns */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #0b1220;
    border: 1.5px solid #283953;
    border-radius: 6px;
    padding: 5px 10px;
    color: #f8fafc;
    font-size: 9pt;
    selection-background-color: #2563eb;
    min-height: 24px;
}

QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1.5px solid #38bdf8;
    background-color: #101c33;
}

QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
    color: #64748b;
    background-color: #080c14;
    border-color: #1a2436;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border-left: 1px solid #283953;
}

QComboBox QAbstractItemView {
    background-color: #0f172a;
    border: 1px solid #38bdf8;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
    color: #f8fafc;
    padding: 4px;
}

/* Push Buttons */
QPushButton {
    background-color: #1e293b;
    border: 1.5px solid #3b4d66;
    border-radius: 6px;
    padding: 6px 16px;
    color: #f1f5f9;
    font-size: 9.5pt;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #2a3a52;
    border-color: #60a5fa;
    color: #ffffff;
}

QPushButton:pressed {
    background-color: #131b2e;
}

QPushButton:disabled {
    color: #64748b;
    background-color: #0e1524;
    border-color: #1c283d;
}

QPushButton#Primary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2563eb, stop:1 #1d4ed8);
    border: 1.5px solid #3b82f6;
    color: #ffffff;
    font-weight: 700;
}

QPushButton#Primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3b82f6, stop:1 #2563eb);
    border-color: #60a5fa;
}

QPushButton#Primary:disabled {
    background-color: #0e1524;
    border-color: #1c283d;
    color: #64748b;
}

QPushButton#Danger {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #dc2626, stop:1 #991b1c);
    border: 1.5px solid #ef4444;
    color: #ffffff;
    font-weight: 700;
}

QPushButton#Danger:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ef4444, stop:1 #dc2626);
    border-color: #f87171;
}

QPushButton#Danger:disabled {
    background-color: #0d1320;
    border-color: #1a2436;
    color: #475569;
}

/* Progress Bars */
QProgressBar {
    background-color: #090e1a;
    border: 1.5px solid #23324d;
    border-radius: 7px;
    height: 22px;
    text-align: center;
    color: #ffffff;
    font-size: 9pt;
    font-weight: 700;
}

QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:0.5 #06b6d4, stop:1 #10b981);
    border-radius: 6px;
}

/* Terminal & Logs */
QPlainTextEdit#Log {
    background-color: #070b14;
    border: 1.5px solid #23324d;
    border-radius: 8px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 9pt;
    color: #e2e8f0;
}

QPlainTextEdit#Caps {
    background-color: #0b1220;
    border: 1.5px solid #23324d;
    border-radius: 8px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 8.5pt;
    color: #cbd5e1;
    padding: 8px;
}

/* Tab Widgets & Tab Bar */
QTabWidget::pane {
    border: 1.5px solid #23324d;
    border-radius: 8px;
    top: -1px;
    background-color: #131b2e;
}

QTabBar::tab {
    background-color: #0d1424;
    color: #94a3b8;
    padding: 8px 22px;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    margin-right: 4px;
    font-weight: 600;
    font-size: 9pt;
    border: 1.5px solid #23324d;
    border-bottom: none;
}

QTabBar::tab:selected {
    background-color: #131b2e;
    color: #38bdf8;
    border-top: 2.5px solid #38bdf8;
    border-bottom: 1.5px solid #131b2e;
    font-weight: 700;
}

QTabBar::tab:hover:!selected {
    background-color: #17223b;
    color: #cbd5e1;
}

/* Checkboxes */
QCheckBox {
    color: #cbd5e1;
    font-size: 9pt;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 17px;
    height: 17px;
    border-radius: 4px;
    border: 1.5px solid #3b4d66;
    background-color: #0b1220;
}

QCheckBox::indicator:hover {
    border-color: #38bdf8;
}

QCheckBox::indicator:checked {
    background-color: #2563eb;
    border-color: #38bdf8;
}

/* Scrollbars */
QScrollBar:vertical {
    background-color: #090e1a;
    width: 10px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background-color: #23324d;
    border-radius: 5px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background-color: #38bdf8;
}

QScrollBar:horizontal {
    background-color: #090e1a;
    height: 10px;
    margin: 0;
}

QScrollBar::handle:horizontal {
    background-color: #23324d;
    border-radius: 5px;
    min-width: 24px;
}

QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
    width: 0;
}

QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}

/* Splitter */
QSplitter::handle {
    background-color: #23324d;
    height: 4px;
}

QSplitter::handle:hover {
    background-color: #38bdf8;
}

/* Context Menus */
QMenu {
    background-color: #131b2e;
    border: 1.5px solid #23324d;
    border-radius: 6px;
    padding: 4px;
}

QMenu::item {
    color: #f1f5f9;
    padding: 6px 20px;
    border-radius: 4px;
    font-size: 9pt;
}

QMenu::item:selected {
    background-color: #2563eb;
    color: #ffffff;
}

/* Tooltips */
QToolTip {
    background-color: #131b2e;
    color: #f8fafc;
    border: 1px solid #38bdf8;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 9pt;
}
"""

LEVEL_COLOURS = {
    "trace": "#6f7a90",
    "info": "#38bdf8",
    "ok": "#10b981",
    "warn": "#f59e0b",
    "error": "#ef4444",
    "command": "#818cf8",
}


def build_stylesheet() -> str:
    """Return the application stylesheet."""
    return APP_STYLE


def stylesheet_available() -> bool:
    return True


__all__ = ["build_stylesheet", "stylesheet_available", "LEVEL_COLOURS", "APP_STYLE"]