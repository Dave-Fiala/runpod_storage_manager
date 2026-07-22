"""Shared pytest fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the whole test session (needed by QObjects)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
