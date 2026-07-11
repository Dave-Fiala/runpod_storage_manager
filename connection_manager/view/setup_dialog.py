from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..services.component_checker import ComponentStatus
from ..services.component_installer import InstallProgress


class _ComponentRow(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        self._icon = QLabel()
        self._icon.setFixedWidth(24)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon)

        self._name = QLabel()
        self._name.setMinimumWidth(80)
        layout.addWidget(self._name)

        self._detail = QLabel()
        self._detail.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._detail)

        self._action_btn = QPushButton("Install")
        self._action_btn.setFixedWidth(80)
        self._action_btn.setVisible(False)
        layout.addWidget(self._action_btn)

    @property
    def action_button(self) -> QPushButton:
        return self._action_btn

    def update_status(self, status: ComponentStatus) -> None:
        self._name.setText(status.name)
        if status.installed:
            self._icon.setText("✓")
            self._icon.setStyleSheet("color: #00CC00; font-size: 16px; font-weight: bold;")
            ver = f"v{status.version}" if status.version else "installed"
            self._detail.setText(ver)
            self._action_btn.setVisible(False)
        else:
            self._icon.setText("✗")
            self._icon.setStyleSheet("color: #CC0000; font-size: 16px; font-weight: bold;")
            self._detail.setText(status.detail or "Not installed")
            self._action_btn.setVisible(True)


class SetupDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System Components")
        self.setMinimumWidth(450)
        self.setModal(True)

        layout = QVBoxLayout(self)

        header = QLabel(
            "The following components are required to mount RunPod volumes.\n"
            "Install any that are missing, then close this dialog."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self._winfsp_row = _ComponentRow()
        layout.addWidget(self._winfsp_row)

        self._geesefs_row = _ComponentRow()
        layout.addWidget(self._geesefs_row)

        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        self._progress_label = QLabel()
        self._progress_label.setVisible(False)
        self._progress_label.setWordWrap(True)
        layout.addWidget(self._progress_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._install_all_btn = QPushButton("Install All Missing")
        btn_layout.addWidget(self._install_all_btn)

        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self._close_btn)

        layout.addLayout(btn_layout)

    @property
    def winfsp_install_button(self) -> QPushButton:
        return self._winfsp_row.action_button

    @property
    def geesefs_install_button(self) -> QPushButton:
        return self._geesefs_row.action_button

    @property
    def install_all_button(self) -> QPushButton:
        return self._install_all_btn

    def on_components_checked(self, statuses: list[ComponentStatus]) -> None:
        for s in statuses:
            if s.name == "WinFsp":
                self._winfsp_row.update_status(s)
            elif s.name == "geesefs":
                self._geesefs_row.update_status(s)

        all_installed = all(s.installed for s in statuses)
        self._install_all_btn.setEnabled(not all_installed)
        if all_installed:
            self._install_all_btn.setText("All Installed")

    def on_install_progress(self, progress: InstallProgress) -> None:
        self._progress_bar.setVisible(True)
        self._progress_label.setVisible(True)
        self._progress_label.setText(f"[{progress.component}] {progress.message}")
        if progress.percent < 0:
            self._progress_bar.setRange(0, 0)
        else:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(progress.percent)

        if progress.percent == 100:
            self._progress_bar.setVisible(False)
