"""
log_viewer.py

Read-only log viewer widget fed by the shared ``LogModel`` (connection logs +
sync logs, timestamped and level-coloured). Follows the tail unless the user has
scrolled up. Includes a level filter and copy-all.

The widget is intended to be hosted inside a separate top-level window (see
``MainWindow.open_log_viewer``), mirroring how ``ConnectionWidget`` is shown.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_LEVELS = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"]
_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LEVEL_COLOURS = {
    "DEBUG": "#7f8c8d",
    "INFO": "#2c3e50",
    "WARNING": "#e67e22",
    "ERROR": "#c0392b",
    "CRITICAL": "#c0392b",
}


class LogViewerWidget(QWidget):
    """Self-contained log viewer content; host in a top-level window."""

    def __init__(self, log_model, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = log_model
        self._min_level = _LEVEL_ORDER["INFO"]
        self._setup_ui()

        if self._model is not None:
            for record in self._model.records:
                self._append(record)
            self._model.recordAdded.connect(self._append)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Minimum level:"))
        self._filter = QComboBox()
        self._filter.addItems(_LEVELS)
        self._filter.setCurrentText("INFO")
        self._filter.currentTextChanged.connect(self._on_filter_changed)
        top.addWidget(self._filter)
        top.addStretch()

        clear_btn = QPushButton("Clear View")
        clear_btn.clicked.connect(self._clear_view)
        top.addWidget(clear_btn)

        copy_btn = QPushButton("Copy All")
        copy_btn.clicked.connect(self._copy_all)
        top.addWidget(copy_btn)

        layout.addLayout(top)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(20000)
        self._text.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._text.setPlaceholderText("Log output will appear here…")
        layout.addWidget(self._text)

    def _on_filter_changed(self, text: str) -> None:
        self._min_level = _LEVEL_ORDER.get(text, 0) if text != "ALL" else 0
        self._text.clear()
        if self._model is not None:
            for record in self._model.records:
                self._append(record)

    def _append(self, record) -> None:
        if _LEVEL_ORDER.get(record.level, 20) < self._min_level:
            return
        at_bottom = self._is_at_bottom()
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_LEVEL_COLOURS.get(record.level, "#2c3e50")))
        cursor.insertText(record.formatted() + "\n", fmt)
        if at_bottom:
            self._text.verticalScrollBar().setValue(self._text.verticalScrollBar().maximum())

    def _is_at_bottom(self) -> bool:
        bar = self._text.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _clear_view(self) -> None:
        """Clear the on-screen buffer without deleting records from the model."""
        self._text.clear()

    def _copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self._text.toPlainText())


# Backward-compatible alias (older code referenced LogViewerWindow directly).
LogViewerWindow = LogViewerWidget
