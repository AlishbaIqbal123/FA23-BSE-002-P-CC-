"""Colour-coded, auto-scrolling live log terminal.

Deliberately a ``QPlainTextEdit`` with the rich-text API disabled: HTML is what
makes a log pane flicker on every append, and with ~20 progress frames per second
that flicker is the difference between a usable and an unusable UI. Colour is
applied per-line with ``appendHtml`` only when a level is present, and the
document is capped so a long session cannot grow without bound.
"""

from __future__ import annotations

import html
import time

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QPlainTextEdit

from .theme import LEVEL_COLOURS

MAX_BLOCKS = 4000


class LogTerminal(QPlainTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Log")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(MAX_BLOCKS)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFont("Cascadia Mono", 9))
        self._formats = {
            level: self._format(colour) for level, colour in LEVEL_COLOURS.items()
        }
        self._counts: dict[str, int] = {}

    @staticmethod
    def _format(colour: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(Qt.GlobalColor.white)
        from PyQt6.QtGui import QColor

        fmt.setForeground(QColor(colour))
        return fmt

    @pyqtSlot(str, str)
    def append_log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        safe = html.escape(message).replace("\n", "<br>")
        colour = LEVEL_COLOURS.get(level, LEVEL_COLOURS["info"])
        tag = level.upper().ljust(5)
        self.appendHtml(
            f'<span style="color:#4a5163">{stamp}</span> '
            f'<span style="color:{colour}">{tag}</span> '
            f'<span style="color:#c7d0e0">{safe}</span>'
        )
        self._counts[level] = self._counts.get(level, 0) + 1
        self._scroll_to_end()

    def banner(self, text: str) -> None:
        safe = html.escape(text)
        self.appendHtml(
            f'<div style="color:#37b3a4;font-weight:600;padding:2px 0">{safe}</div>'
        )
        self._scroll_to_end()

    def rule(self, text: str = "") -> None:
        left = "-" * max(0, 46 - len(text) // 2)
        self.appendHtml(
            f'<div style="color:#333a49">{left} '
            f'<span style="color:#4a5163">{html.escape(text)}</span> '
            f'{"-" * 46}</div>'
        )
        self._scroll_to_end()

    def clear_log(self) -> None:
        self.clear()
        self._counts.clear()

    def summary(self) -> str:
        return "  ".join(f"{k}:{v}" for k, v in sorted(self._counts.items())) or "empty"

    def _scroll_to_end(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)


__all__ = ["LogTerminal"]