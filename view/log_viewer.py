"""
log_viewer.py

Read-only Log Viewer window fed by the shared ``LogModel`` (connection logs +
sync logs, timestamped and level-coloured). Follows the tail unless the user has
scrolled up. Includes a level filter and copy-all.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtGui import QColor, QGuiApplication, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
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


class LogViewerWindow(QWidget):
    def __init__(self, log_model, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Log Viewer")
        self.setMinimumSize(700, 400)
        self._model = log_model
        self._min_level = 0

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Level:"))
        self._filter = QComboBox()
        self._filter.addItems(_LEVELS)
        self._filter.setCurrentText("INFO")
        self._min_level = _LEVEL_ORDER["INFO"]
        self._filter.currentTextChanged.connect(self._on_filter_changed)
        top.addWidget(self._filter)
        top.addStretch()
        copy_btn = QPushButton("Copy All")
        copy_btn.clicked.connect(self._copy_all)
        top.addWidget(copy_btn)
        layout.addLayout(top)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(20000)
        layout.addWidget(self._text)

        if self._model is not None:
            for record in self._model.records:
                self._append(record)
            self._model.recordAdded.connect(self._append)

    def _on_filter_changed(self, text: str) -> None:
        self._min_level = _LEVEL_ORDER.get(text, 0)
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

    def _copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self._text.toPlainText())
