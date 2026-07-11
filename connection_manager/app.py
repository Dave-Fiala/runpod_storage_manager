"""
Wires the Connection Manager MVC layers together.

Provides:
- create_connection_widget(): factory that returns a fully-wired (controller, widget) pair
  for embedding in the larger app.
- main(): standalone entry point for running the connection manager as its own window.
"""
from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from .controller.connection_controller import ConnectionController
from .model.config_store import ConfigStore
from .model.connection_profile import ConnectionProfile
from .model.credentials_store import CredentialsStore
from .model.mount_state import MountState
from .services.component_checker import ComponentChecker
from .services.component_installer import ComponentInstaller
from .services.windows_mount_service import WindowsMountService
from .view.connection_widget import ConnectionWidget
from .view.setup_dialog import SetupDialog

logger = logging.getLogger(__name__)


def create_connection_widget(
    parent: QWidget | None = None,
) -> tuple[ConnectionController, ConnectionWidget]:
    """Build and wire all layers, returning (controller, widget).

    The caller is responsible for calling controller.cleanup() on app exit.
    """
    config_store = ConfigStore()
    config_store.load()

    credentials_store = CredentialsStore()

    checker = ComponentChecker(config_geesefs_path=config_store.get_geesefs_path())
    installer = ComponentInstaller(checker)
    mount_service = WindowsMountService()

    controller = ConnectionController(
        config_store=config_store,
        credentials_store=credentials_store,
        checker=checker,
        installer=installer,
        mount_service=mount_service,
    )

    widget = ConnectionWidget(parent)

    # Wire controller -> view
    controller.stateChanged.connect(widget.on_state_changed)
    controller.logLine.connect(widget.on_log_line)

    # Wire view -> controller
    widget.connectRequested.connect(controller.connect_profile)
    widget.disconnectRequested.connect(controller.disconnect)
    widget.saveRequested.connect(
        lambda profile, secret: _on_save(controller, widget, config_store, profile, secret)
    )
    widget.profileSelected.connect(
        lambda name: _on_profile_selected(controller, widget, name)
    )

    # Load saved profiles into the widget
    profiles = controller.load_profiles()
    default = config_store._ensure_loaded().default_profile
    widget.set_profiles(profiles, default)
    if profiles:
        target = default or profiles[0].name
        _on_profile_selected(controller, widget, target)

    return controller, widget


def _on_save(
    controller: ConnectionController,
    widget: ConnectionWidget,
    config_store: ConfigStore,
    profile: ConnectionProfile,
    secret: str,
) -> None:
    controller.save_profile(profile, secret)
    profiles = controller.load_profiles()
    config_store._ensure_loaded().default_profile = profile.name
    config_store.save()
    widget.set_profiles(profiles, profile.name)


def _on_profile_selected(
    controller: ConnectionController,
    widget: ConnectionWidget,
    name: str,
) -> None:
    profiles = controller.load_profiles()
    for p in profiles:
        if p.name == name:
            secret = controller.load_secret(name) or ""
            widget.populate_from_profile(p, secret)
            return


def show_setup_dialog(
    controller: ConnectionController,
    parent: QWidget | None = None,
) -> None:
    dialog = SetupDialog(parent)

    controller.componentsChecked.connect(dialog.on_components_checked)
    controller.installProgress.connect(dialog.on_install_progress)

    dialog.install_all_button.clicked.connect(controller.provision_missing)
    dialog.winfsp_install_button.clicked.connect(controller.provision_missing)
    dialog.geesefs_install_button.clicked.connect(controller.provision_missing)

    controller.run_component_check()
    dialog.exec()


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("RunPod Storage Tool — Connection Manager")

    controller, widget = create_connection_widget()
    app.aboutToQuit.connect(controller.cleanup)

    window = QWidget()
    window.setWindowTitle("Connection Manager")
    window.setMinimumSize(480, 400)
    layout = QVBoxLayout(window)
    layout.addWidget(widget)
    window.show()

    # Auto-run component check; show setup dialog if anything is missing
    def _on_initial_check(statuses: list) -> None:
        if any(not s.installed for s in statuses):
            show_setup_dialog(controller, window)
        # Auto-mount profiles that have auto_mount enabled
        for profile in controller.load_profiles():
            if profile.auto_mount:
                secret = controller.load_secret(profile.name)
                if secret:
                    controller.connect_profile(profile, secret)
                break

    controller.componentsChecked.connect(_on_initial_check)
    controller.run_component_check()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
