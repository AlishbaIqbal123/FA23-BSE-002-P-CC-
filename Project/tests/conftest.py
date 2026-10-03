"""Shared pytest configuration.

``QT_QPA_PLATFORM=offscreen`` is set before PyQt is imported anywhere, so the GUI
tests can run on a headless machine (CI, or an SSH session into the worker box)
without a desktop session.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session - Qt allows only one."""
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()