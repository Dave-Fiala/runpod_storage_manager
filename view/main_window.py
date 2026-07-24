"""
main_window.py

QMainWindow that hosts the generated ``Ui_MainWindow`` layout and adds behaviour
only. It contains zero business logic: it emits intent signals when the user
acts, and renders view-models passed to its ``on_*`` slots. It never touches the
pool or services.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from viewmodels import (
    ComboItemVM,
    ConnectionStatusVM,
    EnablementVM,
    JobReportVM,
    RemovePlanVM,
    SyncPlanVM,
    TreeGroupVM,
    WorkflowStatusVM,
    human_bytes,
)
from view.model_sync_tool import Ui_MainWindow

_MISSING_COLOR = QColor("#c0392b")
_AMBER = "#e67e22"
_RED = "#c0392b"


class MainWindow(QMainWindow):
    # -- intent signals (view -> controller) --------------------------------
    launchConnectionManagerRequested = pyqtSignal()
    openLogViewerRequested = pyqtSignal()
    localModelPathChanged = pyqtSignal(str)
    localWorkflowPathChanged = pyqtSignal(str)
    remoteModelPathChanged = pyqtSignal(str)
    remoteWorkflowPathChanged = pyqtSignal(str)
    activeWorkflowChanged = pyqtSignal(str)
    syncWorkflowRequested = pyqtSignal(str)
    syncConfirmed = pyqtSignal(str)
    removeWorkflowRequested = pyqtSignal(str)
    removeConfirmed = pyqtSignal(str)
    cancelRequested = pyqtSignal()
    launchModelInfoRequested = pyqtSignal(str)
    revealLocalRequested = pyqtSignal(str)
    revealRemoteRequested = pyqtSignal(str)
    window_closing = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self._active_key: str = ""
        self._suppress_combo = False
        self._connection_widget: Optional[QWidget] = None
        self._connection_window: Optional[QWidget] = None
        self._log_viewer_widget: Optional[QWidget] = None
        self._log_viewer_window: Optional[QWidget] = None
        self._log_model = None

        self.ui.comboBox_LocalSelectWorkflow.setPlaceholderText("No local workflows")
        self.ui.comboBox_RemoteSelectWorkflow.setPlaceholderText("Not connected")
        self._setup_trees()
        self._inject_cancel_button()
        self._wire_view()
        self.on_enablement_changed(EnablementVM(False, False, False, False, False, False))

    # ------------------------------------------------------------------ setup
    def _setup_trees(self) -> None:
        self.ui.treeWidget_LocalModelDirectory.setColumnCount(4)
        self.ui.treeWidget_LocalModelDirectory.setHeaderLabels(
            ["Name", "Size", "Used by", "On Remote"]
        )
        self.ui.treeWidget_LocalModelDirectory.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.ui.treeWidget_RemoteModelDirectory.setColumnCount(4)
        self.ui.treeWidget_RemoteModelDirectory.setHeaderLabels(
            ["Name", "Size", "Used by", "On Local"]
        )
        self.ui.treeWidget_RemoteModelDirectory.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )

    def _inject_cancel_button(self) -> None:
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setMaximumWidth(90)
        self._cancel_button.setVisible(False)
        self._cancel_button.clicked.connect(self.cancelRequested)
        # Injected into the spacer position of the progress row.
        self.ui.horizontalLayout_11.addWidget(self._cancel_button)

    def _wire_view(self) -> None:
        u = self.ui
        u.pushButton_LaunchConnectionManager.clicked.connect(self.launchConnectionManagerRequested)
        u.pushButton_OpenLogViewer.clicked.connect(self.openLogViewerRequested)

        u.pushButton_LocalModelPath.clicked.connect(self._browse_local_model)
        u.pushButton_LocalWorkflowPath.clicked.connect(self._browse_local_workflow)
        u.lineEdit_LocalModelPath.editingFinished.connect(
            lambda: self.localModelPathChanged.emit(u.lineEdit_LocalModelPath.text().strip())
        )
        u.lineEdit_LocalWorkflowPath.editingFinished.connect(
            lambda: self.localWorkflowPathChanged.emit(u.lineEdit_LocalWorkflowPath.text().strip())
        )

        u.pushButton_RemoteModelPath.clicked.connect(
            lambda: self.remoteModelPathChanged.emit(u.lineEdit_RemoteModelPath.text().strip())
        )
        u.pushButton_RemoteWorkflowPath.clicked.connect(
            lambda: self.remoteWorkflowPathChanged.emit(u.lineEdit_RemoteWorkflowPath.text().strip())
        )
        u.lineEdit_RemoteModelPath.editingFinished.connect(
            lambda: self.remoteModelPathChanged.emit(u.lineEdit_RemoteModelPath.text().strip())
        )
        u.lineEdit_RemoteWorkflowPath.editingFinished.connect(
            lambda: self.remoteWorkflowPathChanged.emit(u.lineEdit_RemoteWorkflowPath.text().strip())
        )

        u.comboBox_LocalSelectWorkflow.currentIndexChanged.connect(self._on_local_combo)
        u.comboBox_RemoteSelectWorkflow.currentIndexChanged.connect(self._on_remote_combo)

        u.pushButton_SyncWorkflow.clicked.connect(
            lambda: self.syncWorkflowRequested.emit(self._active_key)
        )
        u.pushButton_RemoveWorkflow.clicked.connect(
            lambda: self.removeWorkflowRequested.emit(self._active_key)
        )
        u.pushButton_LaunchModelInfoViewer.clicked.connect(
            lambda: self.launchModelInfoRequested.emit(self._active_key)
        )

        u.treeWidget_LocalModelDirectory.itemDoubleClicked.connect(
            lambda item, _col: self._tree_double_click(item)
        )
        u.treeWidget_RemoteModelDirectory.itemDoubleClicked.connect(
            lambda item, _col: self._tree_double_click(item)
        )
        u.treeWidget_LocalModelDirectory.customContextMenuRequested.connect(
            lambda pos: self._tree_menu(u.treeWidget_LocalModelDirectory, pos, local=True)
        )
        u.treeWidget_RemoteModelDirectory.customContextMenuRequested.connect(
            lambda pos: self._tree_menu(u.treeWidget_RemoteModelDirectory, pos, local=False)
        )

    # ------------------------------------------------- collaborators (from main)
    def set_connection_widget(self, widget: QWidget) -> None:
        self._connection_widget = widget

    def set_log_model(self, model) -> None:
        self._log_model = model

    def open_connection_manager(self) -> None:
        if self._connection_widget is None:
            QMessageBox.information(self, "Connection Manager", "Connection Manager unavailable.")
            return
        if self._connection_window is None:
            self._connection_window = QWidget()
            self._connection_window.setWindowTitle("Connection Manager")
            self._connection_window.setMinimumSize(500, 500)
            layout = QVBoxLayout(self._connection_window)
            layout.addWidget(self._connection_widget)
        self._connection_window.show()
        self._connection_window.raise_()
        self._connection_window.activateWindow()

    def open_log_viewer(self) -> None:
        from view.log_viewer import LogViewerWidget

        if self._log_model is None:
            QMessageBox.information(self, "Log Viewer", "Log model unavailable.")
            return
        if self._log_viewer_window is None:
            self._log_viewer_window = QWidget(None, Qt.WindowType.Window)
            self._log_viewer_window.setWindowTitle("Log Viewer")
            self._log_viewer_window.setMinimumSize(720, 480)
            layout = QVBoxLayout(self._log_viewer_window)
            layout.setContentsMargins(0, 0, 0, 0)
            self._log_viewer_widget = LogViewerWidget(self._log_model)
            layout.addWidget(self._log_viewer_widget)
        self._log_viewer_window.show()
        self._log_viewer_window.raise_()
        self._log_viewer_window.activateWindow()

    # ------------------------------------------------------------- path browse
    def _browse_local_model(self) -> None:
        start = self.ui.lineEdit_LocalModelPath.text().strip()
        path = QFileDialog.getExistingDirectory(self, "Select local models directory", start)
        if path:
            self.ui.lineEdit_LocalModelPath.setText(path)
            self.localModelPathChanged.emit(path)

    def _browse_local_workflow(self) -> None:
        start = self.ui.lineEdit_LocalWorkflowPath.text().strip()
        path = QFileDialog.getExistingDirectory(self, "Select local workflows directory", start)
        if path:
            self.ui.lineEdit_LocalWorkflowPath.setText(path)
            self.localWorkflowPathChanged.emit(path)

    def set_paths(self, local_model: str, local_workflow: str,
                  remote_model: str, remote_workflow: str) -> None:
        self.ui.lineEdit_LocalModelPath.setText(local_model)
        self.ui.lineEdit_LocalWorkflowPath.setText(local_workflow)
        self.ui.lineEdit_RemoteModelPath.setText(remote_model)
        self.ui.lineEdit_RemoteWorkflowPath.setText(remote_workflow)

    def mark_path_invalid(self, which: str, invalid: bool, message: str = "") -> None:
        edit = {
            "local_model": self.ui.lineEdit_LocalModelPath,
            "local_workflow": self.ui.lineEdit_LocalWorkflowPath,
            "remote_model": self.ui.lineEdit_RemoteModelPath,
            "remote_workflow": self.ui.lineEdit_RemoteWorkflowPath,
        }.get(which)
        if edit is None:
            return
        edit.setStyleSheet("border: 1px solid #c0392b;" if invalid else "")
        edit.setToolTip(message if invalid else "")

    # ---------------------------------------------------------------- combos
    def _on_local_combo(self, _idx: int) -> None:
        if self._suppress_combo:
            return
        key = self.ui.comboBox_LocalSelectWorkflow.currentData()
        if key is not None:
            self._active_key = key
            self.activeWorkflowChanged.emit(key)

    def _on_remote_combo(self, _idx: int) -> None:
        if self._suppress_combo:
            return
        key = self.ui.comboBox_RemoteSelectWorkflow.currentData()
        if key is not None:
            self._active_key = key
            self.activeWorkflowChanged.emit(key)

    # ----------------------------------------------------------------- slots
    def on_connection_status_changed(self, vm: ConnectionStatusVM) -> None:
        u = self.ui
        u.checkBox_ConnectionEstablished.setChecked(vm.connection_established)
        u.checkBox_DriveMounted.setChecked(vm.drive_mounted)
        u.label_DriveLetter.setText(f"Drive Letter: [ {vm.drive_letter or 'None'} ]")
        u.label_RunpodDriveName.setText(f"Runpod Drive Name: [{vm.drive_name}]")
        u.label_TotalSpace.setText(f"Total Space: [{human_bytes(vm.total_space_bytes)}]")
        if vm.used_bytes is not None and vm.total_space_bytes is not None:
            avail = max(0, vm.total_space_bytes - vm.used_bytes)
            u.label_SpaceUsage.setText(
                f"Used (models): [{human_bytes(vm.used_bytes)}] "
                f"Available: [{human_bytes(avail)}]"
            )
        elif vm.connection_established and vm.used_bytes is None:
            u.label_SpaceUsage.setText(
                "Used (models): [(set remote model path)] Available: []"
            )
        else:
            u.label_SpaceUsage.setText("Used (models): [] Available: []")

    def on_local_workflows_changed(self, items: list[ComboItemVM], active_key: str) -> None:
        self._populate_combo(self.ui.comboBox_LocalSelectWorkflow, items, active_key)

    def on_remote_workflows_changed(self, items: list[ComboItemVM], active_key: str) -> None:
        self._populate_combo(self.ui.comboBox_RemoteSelectWorkflow, items, active_key)

    def _populate_combo(self, combo, items: list[ComboItemVM], active_key: str) -> None:
        self._suppress_combo = True
        combo.clear()
        combo.setEnabled(bool(items))
        for it in items:
            combo.addItem(it.label, it.key)
        if active_key:
            idx = combo.findData(active_key)
            combo.setCurrentIndex(idx)  # -1 clears selection if not present
        self._suppress_combo = False

    def set_active_key(self, key: str) -> None:
        self._active_key = key

    def on_local_tree_changed(self, groups: list[TreeGroupVM]) -> None:
        self._populate_tree(self.ui.treeWidget_LocalModelDirectory, groups)

    def on_remote_tree_changed(self, groups: list[TreeGroupVM]) -> None:
        self._populate_tree(self.ui.treeWidget_RemoteModelDirectory, groups)

    def _populate_tree(self, tree, groups: list[TreeGroupVM]) -> None:
        tree.clear()
        bold = QFont()
        bold.setBold(True)
        for group in groups:
            parent = QTreeWidgetItem([group.subfolder, "", "", ""])
            for row in group.rows:
                child = QTreeWidgetItem([
                    row.name,
                    human_bytes(row.size_bytes),
                    str(row.used_by_count),
                    "yes" if row.on_other_side else "",
                ])
                child.setData(0, Qt.ItemDataRole.UserRole, row.key)
                if row.required_by_active:
                    child.setFont(0, bold)
                if row.missing:
                    for col in range(4):
                        child.setForeground(col, QBrush(_MISSING_COLOR))
                parent.addChild(child)
            tree.addTopLevelItem(parent)
            parent.setExpanded(True)

    def on_workflow_status_changed(self, vm: WorkflowStatusVM) -> None:
        u = self.ui
        u.groupBox_WorkflowStatus.setEnabled(vm.has_active)
        u.label_WorkspaceName.setText(f"Workspace Name: [{vm.name}]")
        u.label_NumberOfModels.setText(f"Models: [{vm.num_models}]")
        u.label_ModelsOnLocal.setText(f"Models on Local: [{vm.models_on_local}/{vm.num_models}]")
        u.label_ModelsOnRemote.setText(f"Models on Remote: [{vm.models_on_remote}/{vm.num_models}]")
        u.checkBox_ExistsOnLocal.setChecked(vm.exists_on_local)
        u.checkBox_ExistsOnRemote.setChecked(vm.exists_on_remote)
        u.checkBox_ReadyToUseLocal.setChecked(vm.ready_local)
        u.checkBox_ReadyToUseRemote.setChecked(vm.ready_remote)
        u.label_ProjectedDiskUsage.setText(vm.projected_label)
        u.progressBar_ProjectedDiskUsage.setValue(max(0, min(100, vm.projected_percent)))
        colour = {"amber": _AMBER, "red": _RED}.get(vm.projected_warn_level, "")
        u.progressBar_ProjectedDiskUsage.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {colour}; }}" if colour else ""
        )

    def on_job_progress(self, done: int, total: int, message: str) -> None:
        bar = self.ui.progressBar_MainProgress
        if total <= 0:
            bar.setRange(0, 0)  # indeterminate
        else:
            bar.setRange(0, 100)
            bar.setValue(int(done * 100 / total))
        if message:
            self.ui.label_CurrentOperation.setText(f"Current Operation: {message}")

    def on_current_operation(self, message: str) -> None:
        self.ui.label_CurrentOperation.setText(f"Current Operation: [{message}]")

    def on_job_started(self, kind: str, description: str) -> None:
        if kind in ("sync", "remove"):
            self._cancel_button.setVisible(True)

    def on_job_finished(self, report: JobReportVM) -> None:
        self._cancel_button.setVisible(False)
        bar = self.ui.progressBar_MainProgress
        bar.setRange(0, 100)
        bar.setValue(0)
        self.ui.label_CurrentOperation.setText("Current Operation: [Idle]")
        if report.failures:
            self._show_failure_report(report)

    def on_log_line(self, record) -> None:
        text = record.message if hasattr(record, "message") else str(record)
        self.ui.label_LogUpdate.setText(text[:160])

    def on_enablement_changed(self, vm: EnablementVM) -> None:
        u = self.ui
        remote_ok = vm.connected and not vm.job_running
        u.lineEdit_RemoteModelPath.setEnabled(remote_ok)
        u.lineEdit_RemoteWorkflowPath.setEnabled(remote_ok)
        u.pushButton_RemoteModelPath.setEnabled(remote_ok)
        u.pushButton_RemoteWorkflowPath.setEnabled(remote_ok)

        local_ok = not vm.job_running
        u.lineEdit_LocalModelPath.setEnabled(local_ok)
        u.lineEdit_LocalWorkflowPath.setEnabled(local_ok)
        u.pushButton_LocalModelPath.setEnabled(local_ok)
        u.pushButton_LocalWorkflowPath.setEnabled(local_ok)

        can_sync = (
            vm.has_active_workflow and vm.active_exists_local and vm.connected
            and vm.remote_paths_set and not vm.job_running
        )
        u.pushButton_SyncWorkflow.setEnabled(can_sync)

        can_remove = (
            vm.has_active_workflow and vm.active_exists_remote and vm.connected
            and not vm.job_running
        )
        u.pushButton_RemoveWorkflow.setEnabled(can_remove)
        u.pushButton_LaunchModelInfoViewer.setEnabled(vm.has_active_workflow)

    # ------------------------------------------------------- confirm dialogs
    def show_sync_plan(self, vm: SyncPlanVM) -> None:
        lines = [f"Workflow: {vm.workflow_name}", ""]
        lines.append(f"Upload {len(vm.uploads)} file(s), {human_bytes(vm.total_bytes)} total.")
        lines.append(f"{vm.skip_count} already present on remote (skipped).")
        if vm.projected_label:
            lines.append("")
            lines.append(vm.projected_label)
        for it in vm.uploads[:40]:
            lines.append(f"  + {it.name} ({human_bytes(it.size_bytes)})")
        if len(vm.uploads) > 40:
            lines.append(f"  ... and {len(vm.uploads) - 40} more")

        box = QMessageBox(self)
        box.setWindowTitle("Confirm Sync to Remote")
        box.setText("\n".join(lines))
        if vm.unresolved_refs:
            box.setInformativeText(
                "WARNING — these referenced models exist nowhere and cannot be uploaded:\n"
                + "\n".join(f"  - {r}" for r in vm.unresolved_refs)
            )
            box.setIcon(QMessageBox.Icon.Warning)
        elif vm.over_capacity:
            box.setInformativeText("WARNING — projected usage exceeds remote capacity.")
            box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Ok:
            self.syncConfirmed.emit(vm.workflow_key)

    def show_remove_plan(self, vm: RemovePlanVM) -> None:
        lines = [f"Workflow: {vm.workflow_name}", ""]
        lines.append(f"Delete {len(vm.deletes)} file(s) from remote.")
        lines.append(f"Reclaim {human_bytes(vm.reclaimed_bytes)}.")
        for it in vm.deletes[:40]:
            lines.append(f"  - {it.name} ({human_bytes(it.size_bytes)})")
        if vm.retained_shared:
            lines.append("")
            lines.append("Kept because shared with other remote workflows:")
            for rs in vm.retained_shared:
                lines.append(f"  = {rs.name}  (used by: {', '.join(rs.shared_with)})")

        box = QMessageBox(self)
        box.setWindowTitle("Confirm Remove from Remote")
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Ok:
            self.removeConfirmed.emit(vm.workflow_key)

    def show_model_info(self, title: str, rows) -> None:
        from view.model_info_dialog import ModelInfoDialog

        dlg = ModelInfoDialog(title, rows, self)
        dlg.show()

    def _show_failure_report(self, report: JobReportVM) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Operation completed with failures")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            f"{report.succeeded} succeeded, {len(report.failures)} failed:"
        ))
        area = QScrollArea()
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        for name, err in report.failures:
            inner_layout.addWidget(QLabel(f"- {name}: {err}"))
        area.setWidget(inner)
        area.setWidgetResizable(True)
        layout.addWidget(area)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        # Re-running the operation for the active workflow naturally retries only
        # the items that did not already succeed (the pool now reflects those).
        if report.kind in ("sync", "remove") and self._active_key:
            retry = buttons.addButton("Retry Failed", QDialogButtonBox.ButtonRole.ActionRole)

            def _do_retry() -> None:
                dlg.accept()
                if report.kind == "sync":
                    self.syncWorkflowRequested.emit(self._active_key)
                else:
                    self.removeWorkflowRequested.emit(self._active_key)

            retry.clicked.connect(_do_retry)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.resize(480, 320)
        dlg.exec()

    # ------------------------------------------------------------------ trees
    def _tree_double_click(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key:
            self.launchModelInfoRequested.emit(self._active_key)

    def _tree_menu(self, tree, pos, local: bool) -> None:
        item = tree.itemAt(pos)
        if item is None:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key:
            return
        menu = QMenu(tree)
        reveal = menu.addAction("Reveal in Explorer")
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))
        if chosen == reveal:
            if local:
                self.revealLocalRequested.emit(key)
            else:
                self.revealRemoteRequested.emit(key)

    # ------------------------------------------------------------------ close
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.window_closing.emit()
        super().closeEvent(event)
