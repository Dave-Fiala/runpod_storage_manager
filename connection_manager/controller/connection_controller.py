from __future__ import annotations

import logging
import os
from typing import Callable

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ..model.config_store import ConfigStore
from ..model.connection_profile import ConnectionProfile
from ..model.credentials_store import CredentialsStore
from ..model.mount_state import MountState
from ..services.component_checker import ComponentChecker, ComponentStatus
from ..services.component_installer import ComponentInstaller, InstallProgress
from ..services.mount_service import MountError, MountService

logger = logging.getLogger(__name__)


class _Worker(QThread):
    """Generic worker that runs a callable off the main thread."""

    finished_result = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, fn: Callable, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
            self.finished_result.emit(result)
        except Exception as exc:
            logger.exception("Worker failed")
            self.error.emit(str(exc))


class ConnectionController(QObject):
    stateChanged = pyqtSignal(object, str)  # (MountState, detail)
    componentsChecked = pyqtSignal(list)  # list[ComponentStatus]
    installProgress = pyqtSignal(object)  # InstallProgress
    logLine = pyqtSignal(str)

    def __init__(
        self,
        config_store: ConfigStore,
        credentials_store: CredentialsStore,
        checker: ComponentChecker,
        installer: ComponentInstaller,
        mount_service: MountService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config_store
        self._credentials = credentials_store
        self._checker = checker
        self._installer = installer
        self._mount_service = mount_service
        self._state = MountState.DISCONNECTED
        self._active_profile: ConnectionProfile | None = None
        self._worker: _Worker | None = None

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(3000)
        self._health_timer.timeout.connect(self._check_process_health)

    @property
    def state(self) -> MountState:
        return self._state

    def _set_state(self, state: MountState, detail: str = "") -> None:
        self._state = state
        self.stateChanged.emit(state, detail)

    # -- public slots --

    def connect_profile(self, profile: ConnectionProfile, secret: str) -> None:
        # NOTE: Drive mounting (geesefs/WinFsp) is intentionally disabled. The
        # application talks to the volume exclusively over the S3 API, so we no
        # longer mount a local drive letter (which slowed the system down). We
        # still validate the profile/secret and report the connection as
        # "established" so downstream API services can initialise.
        if self._state in (MountState.MOUNTING, MountState.UNMOUNTING):
            return

        self.logLine.emit(
            f"Connect requested for profile '{profile.name}' "
            f"(volume {profile.volume_id}, {profile.region})"
        )

        errors = profile.validate()
        if errors:
            detail = "; ".join(errors)
            self.logLine.emit(f"Profile validation failed: {detail}")
            self._set_state(MountState.ERROR, detail)
            return

        if not secret:
            self.logLine.emit("Connect failed: Secret Access Key is required")
            self._set_state(MountState.ERROR, "Secret Access Key is required")
            return

        self._active_profile = profile
        self.logLine.emit(f"Profile '{profile.name}' validated — opening S3 API session")
        self._set_state(MountState.MOUNTED, f"Connected to {profile.volume_id} (S3 API)")

    def disconnect(self) -> None:
        # Mounting is disabled, so there is no drive/process to tear down; just
        # reset the connection state.
        if self._state in (MountState.MOUNTING, MountState.UNMOUNTING):
            return
        was_connected = self._state == MountState.MOUNTED
        if was_connected:
            self.logLine.emit("Disconnect requested")
        self._health_timer.stop()
        self._active_profile = None
        self._set_state(MountState.DISCONNECTED, "Disconnected")
        if was_connected:
            self.logLine.emit("Disconnected")

    def run_component_check(self) -> None:
        def do_check() -> list[ComponentStatus]:
            geesefs_path = self._config.get_geesefs_path()
            self._checker._config_geesefs_path = geesefs_path
            return self._checker.check_all()

        worker = _Worker(do_check, self)
        worker.finished_result.connect(lambda result: self.componentsChecked.emit(result))
        worker.error.connect(lambda msg: self.componentsChecked.emit([]))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def provision_missing(self) -> None:
        def do_provision() -> list[ComponentStatus]:
            def emit_progress(p: InstallProgress) -> None:
                self.installProgress.emit(p)

            self._installer.ensure_winfsp(emit_progress)
            status = self._installer.ensure_geesefs(emit_progress)
            if status.installed and status.path:
                self._config.set_geesefs_path(status.path)
            return self._checker.check_all()

        worker = _Worker(do_provision, self)
        worker.finished_result.connect(lambda result: self.componentsChecked.emit(result))
        worker.error.connect(
            lambda msg: self.installProgress.emit(
                InstallProgress("", -1, f"Provisioning failed: {msg}")
            )
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def save_profile(self, profile: ConnectionProfile, secret: str) -> None:
        existing = self._config.get_profile(profile.name)
        if existing:
            self._config.update_profile(profile)
        else:
            self._config.add_profile(profile)
        if secret:
            self._credentials.save_secret(profile.name, secret)

    def delete_profile(self, name: str) -> None:
        self._config.remove_profile(name)
        self._credentials.delete_secret(name)

    def load_profiles(self) -> list[ConnectionProfile]:
        return self._config.list_profiles()

    def load_secret(self, profile_name: str) -> str | None:
        return self._credentials.load_secret(profile_name)

    def cleanup(self) -> None:
        """Call on app exit to ensure clean unmount."""
        self._health_timer.stop()
        if self._mount_service.is_mounted():
            logger.info("App exit: unmounting active connection")
            try:
                self._mount_service.unmount()
            except Exception:
                logger.exception("Failed to unmount on exit")

    # -- internal --

    def _resolve_geesefs_path(self) -> str | None:
        path = self._config.get_geesefs_path()
        if path and os.path.isfile(path):
            return path
        status = self._checker.check_geesefs()
        if status.installed and status.path:
            self._config.set_geesefs_path(status.path)
            return status.path
        return None

    def _on_log_line(self, line: str) -> None:
        self.logLine.emit(line)

    def _on_mount_success(self, result: object) -> None:
        self._set_state(MountState.MOUNTED, str(result))
        self._health_timer.start()

    def _on_mount_error(self, msg: str) -> None:
        friendly = self._map_error(msg)
        self._set_state(MountState.ERROR, friendly)

    def _on_unmount_success(self, result: object) -> None:
        self._active_profile = None
        self._set_state(MountState.DISCONNECTED, str(result))

    def _on_unmount_error(self, msg: str) -> None:
        self._set_state(MountState.ERROR, f"Unmount failed: {msg}")

    def _check_process_health(self) -> None:
        if self._state != MountState.MOUNTED:
            return
        if not self._mount_service.poll():
            self._health_timer.stop()
            self._set_state(
                MountState.ERROR,
                "geesefs process died unexpectedly. Check logs for details.",
            )

    def _run_worker(
        self,
        fn: Callable,
        on_success: Callable,
        on_error: Callable,
    ) -> None:
        worker = _Worker(fn, self)
        worker.finished_result.connect(on_success)
        worker.error.connect(on_error)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    @staticmethod
    def _map_error(msg: str) -> str:
        lower = msg.lower()
        if "signaturedoesnotmatch" in lower:
            return (
                "Signature mismatch — the access key or secret is wrong, "
                "or the system clock is skewed."
            )
        if "access is denied" in lower or "access denied" in lower:
            return (
                "Access denied after mount — ensure --file-mode and --dir-mode flags "
                "are set (0666/0777)."
            )
        if "not ready" in lower and "drive" in lower:
            return (
                "Drive did not become ready. WinFsp may be missing or "
                "a reboot may be required after installation."
            )
        return msg
