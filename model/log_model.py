"""
log_model.py

A shared, in-memory log store feeding the Log Viewer window and the rolling
single-line echo in the Progress group. Both the Connection Manager's log lines
and the sync tool's own ``logging`` records are funnelled here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal


@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    level: str  # "INFO" | "WARNING" | "ERROR" | ...
    source: str
    message: str

    def formatted(self) -> str:
        return f"{self.timestamp:%H:%M:%S} [{self.level}] {self.source}: {self.message}"


class LogModel(QObject):
    recordAdded = pyqtSignal(object)  # LogRecord

    def __init__(self, parent: QObject | None = None, max_records: int = 5000) -> None:
        super().__init__(parent)
        self._records: list[LogRecord] = []
        self._max = max_records

    @property
    def records(self) -> list[LogRecord]:
        return list(self._records)

    def add(self, level: str, source: str, message: str) -> None:
        record = LogRecord(datetime.now(), level.upper(), source, message)
        self._records.append(record)
        if len(self._records) > self._max:
            del self._records[: len(self._records) - self._max]
        self.recordAdded.emit(record)

    def add_line(self, message: str, source: str = "connection", level: str = "INFO") -> None:
        """Convenience for the Connection Manager's plain string log lines."""
        self.add(level, source, message)


class _QtLogHandler(logging.Handler):
    """Bridges stdlib logging into the LogModel (installed by main.py)."""

    def __init__(self, model: LogModel) -> None:
        super().__init__()
        self._model = model

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._model.add(record.levelname, record.name, record.getMessage())
        except Exception:  # never let logging crash the app
            pass


def install_log_handler(model: LogModel, level: int = logging.INFO) -> _QtLogHandler:
    handler = _QtLogHandler(model)
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)
    return handler
