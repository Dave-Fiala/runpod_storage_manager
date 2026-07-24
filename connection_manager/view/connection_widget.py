from __future__ import annotations

import os
import string

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..model.connection_profile import (
    DATACENTER_ENDPOINTS,
    ConnectionProfile,
    endpoint_for,
)
from ..model.mount_state import MountState


class ConnectionWidget(QWidget):
    connectRequested = pyqtSignal(object, str)  # (ConnectionProfile, secret)
    disconnectRequested = pyqtSignal()
    saveRequested = pyqtSignal(object, str)  # (ConnectionProfile, secret)
    profileSelected = pyqtSignal(str)  # profile name
    refreshUsageRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._building = True
        self._setup_ui()
        self._building = False

    # -- UI construction --

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Profile section
        profile_group = QGroupBox("Profile")
        profile_layout = QFormLayout(profile_group)

        self._profile_combo = QComboBox()
        self._profile_combo.setEditable(True)
        self._profile_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._profile_combo.currentTextChanged.connect(self._on_profile_changed)
        profile_layout.addRow("Profile:", self._profile_combo)

        layout.addWidget(profile_group)

        # Connection details
        details_group = QGroupBox("Connection Details")
        details_layout = QFormLayout(details_group)

        self._region_combo = QComboBox()
        self._region_combo.setEditable(False)
        for dc in DATACENTER_ENDPOINTS:
            self._region_combo.addItem(dc)
        self._region_combo.currentTextChanged.connect(self._on_region_changed)
        details_layout.addRow("Datacenter / Region:", self._region_combo)

        self._endpoint_edit = QLineEdit()
        self._endpoint_edit.setPlaceholderText("https://s3api-eu-ro-1.runpod.io/")
        details_layout.addRow("Endpoint URL:", self._endpoint_edit)

        self._volume_edit = QLineEdit()
        self._volume_edit.setPlaceholderText("Network volume ID (= bucket name)")
        details_layout.addRow("Volume ID:", self._volume_edit)

        self._volume_size_edit = QLineEdit()
        self._volume_size_edit.setValidator(QIntValidator(1, 1_000_000, self))
        self._volume_size_edit.setPlaceholderText("Provisioned size in GB (e.g. 50)")
        details_layout.addRow("Volume Size (GB):", self._volume_size_edit)

        self._access_key_edit = QLineEdit()
        self._access_key_edit.setPlaceholderText("user_...")
        details_layout.addRow("Access Key ID:", self._access_key_edit)

        self._secret_edit = QLineEdit()
        self._secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._secret_edit.setPlaceholderText("rps_... (stored in Windows Credential Manager)")
        details_layout.addRow("Secret Access Key:", self._secret_edit)

        self._drive_combo = QComboBox()
        self._refresh_drive_letters()
        details_layout.addRow("Drive Letter:", self._drive_combo)

        layout.addWidget(details_group)

        # Advanced section (collapsible)
        self._advanced_group = QGroupBox("Advanced Options")
        self._advanced_group.setCheckable(True)
        self._advanced_group.setChecked(False)
        adv_layout = QFormLayout(self._advanced_group)

        self._file_mode_edit = QLineEdit("0666")
        adv_layout.addRow("File Mode:", self._file_mode_edit)

        self._dir_mode_edit = QLineEdit("0777")
        adv_layout.addRow("Dir Mode:", self._dir_mode_edit)

        self._auto_mount_check = QCheckBox("Mount automatically on app start")
        adv_layout.addRow(self._auto_mount_check)

        layout.addWidget(self._advanced_group)

        # Buttons
        btn_layout = QHBoxLayout()

        self._save_btn = QPushButton("Save Profile")
        self._save_btn.clicked.connect(self._on_save_clicked)
        btn_layout.addWidget(self._save_btn)

        btn_layout.addStretch()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setMinimumWidth(100)
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        btn_layout.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setMinimumWidth(100)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        btn_layout.addWidget(self._disconnect_btn)

        layout.addLayout(btn_layout)

        # Status strip
        status_layout = QHBoxLayout()

        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(20)
        status_layout.addWidget(self._status_dot)

        self._status_label = QLabel("Disconnected")
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        status_layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setMaximumWidth(150)
        self._progress_bar.setVisible(False)
        status_layout.addWidget(self._progress_bar)

        layout.addLayout(status_layout)

        # Volume usage scan (model-prefix scoped; progress shown here, not main window)
        usage_layout = QHBoxLayout()
        self._usage_label = QLabel("Volume usage: idle")
        self._usage_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        usage_layout.addWidget(self._usage_label)

        self._usage_progress_bar = QProgressBar()
        self._usage_progress_bar.setMaximumWidth(150)
        self._usage_progress_bar.setVisible(False)
        usage_layout.addWidget(self._usage_progress_bar)

        self._refresh_usage_btn = QPushButton("Refresh Usage")
        self._refresh_usage_btn.setEnabled(False)
        self._refresh_usage_btn.clicked.connect(self.refreshUsageRequested.emit)
        usage_layout.addWidget(self._refresh_usage_btn)

        layout.addLayout(usage_layout)

        # Log pane
        self._log_pane = QPlainTextEdit()
        self._log_pane.setReadOnly(True)
        self._log_pane.setMaximumHeight(120)
        self._log_pane.setPlaceholderText("geesefs log output...")
        self._log_pane.setVisible(False)
        layout.addWidget(self._log_pane)

        self._log_toggle = QPushButton("Show Log")
        self._log_toggle.setFlat(True)
        self._log_toggle.setMaximumWidth(80)
        self._log_toggle.clicked.connect(self._toggle_log)
        layout.addWidget(self._log_toggle, alignment=Qt.AlignmentFlag.AlignRight)

        # Init endpoint from default region
        self._on_region_changed(self._region_combo.currentText())
        self.on_state_changed(MountState.DISCONNECTED, "")

    # -- public interface --

    def on_state_changed(self, state: MountState, detail: str) -> None:
        """Single slot that drives all widget enablement from state."""
        inputs_enabled = state in (MountState.DISCONNECTED, MountState.ERROR)
        self._profile_combo.setEnabled(inputs_enabled)
        self._region_combo.setEnabled(inputs_enabled)
        self._endpoint_edit.setEnabled(inputs_enabled)
        self._volume_edit.setEnabled(inputs_enabled)
        self._volume_size_edit.setEnabled(inputs_enabled)
        self._access_key_edit.setEnabled(inputs_enabled)
        self._secret_edit.setEnabled(inputs_enabled)
        self._drive_combo.setEnabled(inputs_enabled)
        self._advanced_group.setEnabled(inputs_enabled)
        self._save_btn.setEnabled(inputs_enabled)

        self._connect_btn.setEnabled(state in (MountState.DISCONNECTED, MountState.ERROR))
        self._disconnect_btn.setEnabled(state == MountState.MOUNTED)
        self._refresh_usage_btn.setEnabled(state == MountState.MOUNTED)

        in_progress = state in (MountState.MOUNTING, MountState.UNMOUNTING)
        self._progress_bar.setVisible(in_progress)
        if in_progress:
            self._progress_bar.setRange(0, 0)  # indeterminate

        dot_colors = {
            MountState.DISCONNECTED: "#888888",
            MountState.MOUNTING: "#FFA500",
            MountState.MOUNTED: "#00CC00",
            MountState.UNMOUNTING: "#FFA500",
            MountState.ERROR: "#CC0000",
        }
        color = dot_colors.get(state, "#888888")
        self._status_dot.setStyleSheet(f"color: {color}; font-size: 16px;")

        status_text = {
            MountState.DISCONNECTED: "Disconnected",
            MountState.MOUNTING: "Mounting...",
            MountState.MOUNTED: "Connected",
            MountState.UNMOUNTING: "Unmounting...",
            MountState.ERROR: "Error",
        }
        label = status_text.get(state, "")
        if detail:
            label = f"{label} — {detail}"
        self._status_label.setText(label)

        if state in (MountState.DISCONNECTED, MountState.ERROR):
            self._refresh_drive_letters()
            self.on_usage_scan_finished(False, "Disconnected")

    def on_usage_scan_started(self, prefix: str) -> None:
        self._usage_progress_bar.setVisible(True)
        self._usage_progress_bar.setRange(0, 0)
        self._usage_label.setText(f"Scanning models prefix: {prefix}")

    def on_usage_scan_progress(self, objects_seen: int, message: str) -> None:
        self._usage_label.setText(message)

    def on_usage_scan_finished(self, success: bool, detail: str = "") -> None:
        self._usage_progress_bar.setVisible(False)
        if success and detail:
            self._usage_label.setText(f"Volume usage: {detail}")
        elif detail:
            self._usage_label.setText(f"Volume usage: {detail}")
        else:
            self._usage_label.setText("Volume usage: idle")

    def on_usage_deferred(self, reason: str) -> None:
        self._usage_progress_bar.setVisible(False)
        self._usage_label.setText(f"Volume usage: {reason}")

    def on_log_line(self, line: str) -> None:
        self._log_pane.appendPlainText(line)

    def set_profiles(self, profiles: list[ConnectionProfile], default: str = "") -> None:
        self._building = True
        self._profile_combo.clear()
        for p in profiles:
            self._profile_combo.addItem(p.name)
        if default:
            idx = self._profile_combo.findText(default)
            if idx >= 0:
                self._profile_combo.setCurrentIndex(idx)
        self._building = False

    def populate_from_profile(self, profile: ConnectionProfile, secret: str = "") -> None:
        self._building = True
        idx = self._region_combo.findText(profile.region)
        if idx >= 0:
            self._region_combo.setCurrentIndex(idx)
        self._endpoint_edit.setText(profile.endpoint)
        self._volume_edit.setText(profile.volume_id)
        self._volume_size_edit.setText(
            str(profile.remote_volume_size) if profile.remote_volume_size > 0 else ""
        )
        self._access_key_edit.setText(profile.access_key_id)
        if secret:
            self._secret_edit.setText(secret)
        drive_idx = self._drive_combo.findText(profile.drive_letter.upper())
        if drive_idx >= 0:
            self._drive_combo.setCurrentIndex(drive_idx)
        elif self._drive_combo.count() > 0:
            self._drive_combo.addItem(profile.drive_letter.upper())
            self._drive_combo.setCurrentIndex(self._drive_combo.count() - 1)
        self._file_mode_edit.setText(profile.file_mode)
        self._dir_mode_edit.setText(profile.dir_mode)
        self._auto_mount_check.setChecked(profile.auto_mount)
        self._building = False

    def build_profile(self) -> ConnectionProfile:
        return ConnectionProfile(
            name=self._profile_combo.currentText().strip(),
            endpoint=self._endpoint_edit.text().strip(),
            region=self._region_combo.currentText(),
            volume_id=self._volume_edit.text().strip(),
            access_key_id=self._access_key_edit.text().strip(),
            drive_letter=self._drive_combo.currentText().strip().upper(),
            remote_volume_size=self._parse_volume_size(),
            file_mode=self._file_mode_edit.text().strip() or "0666",
            dir_mode=self._dir_mode_edit.text().strip() or "0777",
            auto_mount=self._auto_mount_check.isChecked(),
        )

    def get_secret(self) -> str:
        return self._secret_edit.text()

    def _parse_volume_size(self) -> int:
        try:
            return int(self._volume_size_edit.text().strip())
        except (TypeError, ValueError):
            return 0

    # -- internal slots --

    def _on_region_changed(self, region: str) -> None:
        if not self._building and region:
            self._endpoint_edit.setText(endpoint_for(region))

    def _on_profile_changed(self, name: str) -> None:
        if not self._building and name:
            self.profileSelected.emit(name)

    def _on_connect_clicked(self) -> None:
        profile = self.build_profile()
        self.connectRequested.emit(profile, self.get_secret())

    def _on_disconnect_clicked(self) -> None:
        self.disconnectRequested.emit()

    def _on_save_clicked(self) -> None:
        profile = self.build_profile()
        self.saveRequested.emit(profile, self.get_secret())

    def _toggle_log(self) -> None:
        visible = not self._log_pane.isVisible()
        self._log_pane.setVisible(visible)
        self._log_toggle.setText("Hide Log" if visible else "Show Log")

    def _refresh_drive_letters(self) -> None:
        current = self._drive_combo.currentText()
        self._drive_combo.clear()
        free = self._get_free_drive_letters()
        self._drive_combo.addItems(free)
        if current and current in free:
            self._drive_combo.setCurrentText(current)

    @staticmethod
    def _get_free_drive_letters() -> list[str]:
        used = set()
        for letter in string.ascii_uppercase:
            if os.path.exists(f"{letter}:\\"):
                used.add(letter)
        free = [ch for ch in string.ascii_uppercase if ch not in used]
        return free
