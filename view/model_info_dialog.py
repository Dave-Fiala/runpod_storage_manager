"""
model_info_dialog.py

Shows every model linked to the active workflow: Name, Category, Subfolder,
Size, On Local, On Remote, Shared-with, plus ``extra`` metadata (e.g. LoRA
strengths). Unresolved references are flagged prominently. The table is
sortable.
"""
from __future__ import annotations

from typing import Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from viewmodels import ModelInfoRowVM, human_bytes

_COLUMNS = ["Name", "Category", "Subfolder", "Size", "On Local", "On Remote", "Shared with", "Extra"]
_MISSING = QColor("#c0392b")


class ModelInfoDialog(QDialog):
    def __init__(self, title: str, rows: Sequence[ModelInfoRowVM],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Model Info — {title}")
        self.setMinimumSize(820, 460)

        layout = QVBoxLayout(self)

        unresolved = [r for r in rows if r.unresolved]
        if unresolved:
            warn = QLabel(
                "Unresolved references (exist nowhere): "
                + ", ".join(r.name for r in unresolved)
            )
            warn.setStyleSheet("color: #c0392b; font-weight: bold;")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        table = QTableWidget(len(rows), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSortingEnabled(False)

        for r, row in enumerate(rows):
            values = [
                row.name,
                row.category,
                row.subfolder,
                human_bytes(row.size_bytes),
                "yes" if row.on_local else "no",
                "yes" if row.on_remote else "no",
                ", ".join(row.shared_with),
                _format_extra(row.extra),
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                if row.unresolved:
                    item.setForeground(QBrush(_MISSING))
                table.setItem(r, c, item)

        table.setSortingEnabled(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)


def _format_extra(extra: dict) -> str:
    if not extra:
        return ""
    parts = []
    for node_type, meta in extra.items():
        if isinstance(meta, dict):
            inner = ", ".join(f"{k}={v}" for k, v in meta.items() if v is not None)
            if inner:
                parts.append(f"{node_type}: {inner}")
        elif meta not in (None, ""):
            parts.append(f"{node_type}={meta}")
    return "; ".join(parts)
