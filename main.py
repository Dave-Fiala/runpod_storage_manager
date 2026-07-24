"""
main.py

Composition root for the ComfyUI RunPod Storage Manager. Mirrors the Connection
Manager's ``app.py`` style: build stores -> services -> controllers -> view,
wire every signal pair explicitly, then run ``app.exec()``.
"""
from __future__ import annotations

import logging
import sys

from PyQt6.QtCore import QByteArray
from PyQt6.QtWidgets import QApplication

from connection_manager.app import create_connection_widget, show_setup_dialog
from controller.sync_controller import SyncController
from model.app_config import AppConfig
from model.log_model import LogModel, install_log_handler
from model.workspace_pool import WorkspacePool
from services.local_storage import LocalStorageService
from view.main_window import MainWindow
from workers.job_runner import JobRunner

logger = logging.getLogger(__name__)


def _wire_view_to_controller(window: MainWindow, controller: SyncController) -> None:
    window.launchConnectionManagerRequested.connect(window.open_connection_manager)
    window.openLogViewerRequested.connect(window.open_log_viewer)
    window.localModelPathChanged.connect(controller.on_local_model_path_changed)
    window.localWorkflowPathChanged.connect(controller.on_local_workflow_path_changed)
    window.remoteModelPathChanged.connect(controller.on_remote_model_path_changed)
    window.remoteWorkflowPathChanged.connect(controller.on_remote_workflow_path_changed)
    window.activeWorkflowChanged.connect(controller.on_active_workflow_changed)
    window.syncWorkflowRequested.connect(controller.on_sync_requested)
    window.syncConfirmed.connect(controller.on_sync_confirmed)
    window.removeWorkflowRequested.connect(controller.on_remove_requested)
    window.removeConfirmed.connect(controller.on_remove_confirmed)
    window.cancelRequested.connect(controller.on_cancel_requested)
    window.launchModelInfoRequested.connect(controller.on_model_info_requested)
    window.revealLocalRequested.connect(controller.on_reveal_local)
    window.revealRemoteRequested.connect(controller.on_reveal_remote)


def _wire_controller_to_view(controller: SyncController, window: MainWindow) -> None:
    controller.connectionStatusChanged.connect(window.on_connection_status_changed)
    controller.localTreeChanged.connect(window.on_local_tree_changed)
    controller.remoteTreeChanged.connect(window.on_remote_tree_changed)
    controller.localWorkflowsChanged.connect(window.on_local_workflows_changed)
    controller.remoteWorkflowsChanged.connect(window.on_remote_workflows_changed)
    controller.workflowStatusChanged.connect(window.on_workflow_status_changed)
    controller.enablementChanged.connect(window.on_enablement_changed)
    controller.jobProgress.connect(window.on_job_progress)
    controller.currentOperation.connect(window.on_current_operation)
    controller.jobStarted.connect(window.on_job_started)
    controller.jobFinished.connect(window.on_job_finished)
    controller.logLine.connect(window.on_log_line)
    controller.syncPlanReady.connect(window.show_sync_plan)
    controller.removePlanReady.connect(window.show_remove_plan)
    controller.modelInfoReady.connect(window.show_model_info)
    controller.activeWorkflowSelected.connect(window.set_active_key)


def _wire_connection_usage(conn_widget, controller: SyncController) -> None:
    conn_widget.refreshUsageRequested.connect(controller.on_refresh_usage_requested)
    controller.usageScanStarted.connect(conn_widget.on_usage_scan_started)
    controller.usageScanProgress.connect(conn_widget.on_usage_scan_progress)
    controller.usageScanFinished.connect(conn_widget.on_usage_scan_finished)
    controller.usageScanDeferred.connect(conn_widget.on_usage_deferred)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("ComfyUI RunPod Storage Manager")

    # -- shared stores / services -------------------------------------------
    app_config = AppConfig()
    app_config.load()
    log_model = LogModel()
    install_log_handler(log_model)

    pool = WorkspacePool()
    local_storage = LocalStorageService()
    job_runner = JobRunner()

    # -- Connection Manager (existing package, consumed as-is) --------------
    conn_controller, conn_widget = create_connection_widget()
    conn_controller.logLine.connect(lambda line: log_model.add_line(line, source="connection"))

    # -- controller ---------------------------------------------------------
    controller = SyncController(
        pool=pool,
        local_storage=local_storage,
        app_config=app_config,
        job_runner=job_runner,
        log_model=log_model,
        connection_controller=conn_controller,
    )

    # -- view ---------------------------------------------------------------
    window = MainWindow()
    window.set_connection_widget(conn_widget)
    window.set_log_model(log_model)

    _wire_view_to_controller(window, controller)
    _wire_controller_to_view(controller, window)
    _wire_connection_usage(conn_widget, controller)
    conn_controller.stateChanged.connect(controller.on_connection_state_changed)

    # -- restore persisted UI state -----------------------------------------
    data = app_config.data
    window.set_paths(
        data.local_model_dir, data.local_workflow_dir,
        data.remote_model_prefix, data.remote_workflow_prefix,
    )
    if data.window_geometry:
        try:
            window.restoreGeometry(QByteArray.fromBase64(data.window_geometry.encode()))
        except Exception:  # noqa: BLE001
            pass

    # -- lifecycle ----------------------------------------------------------
    def _on_quit() -> None:
        try:
            geo = bytes(window.saveGeometry().toBase64()).decode()
            app_config.update(window_geometry=geo)
        except Exception:  # noqa: BLE001
            pass
        job_runner.shutdown()
        conn_controller.cleanup()

    app.aboutToQuit.connect(_on_quit)

    window.show()

    # First-run provisioning: show setup dialog if components are missing.
    def _on_initial_check(statuses: list) -> None:
        if any(not s.installed for s in statuses):
            show_setup_dialog(conn_controller, window)
        for profile in conn_controller.load_profiles():
            if profile.auto_mount:
                secret = conn_controller.load_secret(profile.name)
                if secret:
                    conn_controller.connect_profile(profile, secret)
                break

    conn_controller.componentsChecked.connect(_on_initial_check)
    conn_controller.run_component_check()

    controller.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
