from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_WINFSP_DLL_PATH = r"C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll"
_GEESEFS_APP_DIR = os.path.join(
    os.getenv("LOCALAPPDATA", ""), "RunpodStorageTool", "bin"
)


@dataclass
class ComponentStatus:
    name: str
    installed: bool
    version: str | None
    path: str | None
    detail: str = ""


class ComponentChecker:
    def __init__(self, config_geesefs_path: str = "") -> None:
        self._config_geesefs_path = config_geesefs_path

    def check_winfsp(self) -> ComponentStatus:
        version, display_name = self._check_winfsp_registry()
        if version:
            dll = _WINFSP_DLL_PATH if os.path.isfile(_WINFSP_DLL_PATH) else None
            return ComponentStatus(
                name="WinFsp",
                installed=True,
                version=version,
                path=dll,
                detail=display_name or "",
            )

        if os.path.isfile(_WINFSP_DLL_PATH):
            return ComponentStatus(
                name="WinFsp",
                installed=True,
                version=None,
                path=_WINFSP_DLL_PATH,
                detail="Detected via filesystem (registry entry not found)",
            )

        return ComponentStatus(
            name="WinFsp",
            installed=False,
            version=None,
            path=None,
            detail="WinFsp is not installed",
        )

    def check_geesefs(self) -> ComponentStatus:
        candidates = [
            self._config_geesefs_path,
            os.path.join(_GEESEFS_APP_DIR, "geesefs.exe"),
        ]
        which_result = shutil.which("geesefs")
        if which_result:
            candidates.append(which_result)

        for path in candidates:
            if not path or not os.path.isfile(path):
                continue
            version = self._run_geesefs_version(path)
            if version is not None:
                return ComponentStatus(
                    name="geesefs",
                    installed=True,
                    version=version,
                    path=path,
                )

        return ComponentStatus(
            name="geesefs",
            installed=False,
            version=None,
            path=None,
            detail="geesefs.exe not found or not executable",
        )

    def check_all(self) -> list[ComponentStatus]:
        return [self.check_winfsp(), self.check_geesefs()]

    # -- internal helpers --

    @staticmethod
    def _check_winfsp_registry() -> tuple[str | None, str | None]:
        try:
            import winreg
        except ImportError:
            return None, None

        uninstall_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ]
        for reg_path in uninstall_paths:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        subkey = winreg.OpenKey(key, subkey_name)
                        display, _ = winreg.QueryValueEx(subkey, "DisplayName")
                        if isinstance(display, str) and display.startswith("WinFsp"):
                            try:
                                ver, _ = winreg.QueryValueEx(subkey, "DisplayVersion")
                            except OSError:
                                ver = None
                            winreg.CloseKey(subkey)
                            return ver, display
                        winreg.CloseKey(subkey)
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)
        return None, None

    @staticmethod
    def _run_geesefs_version(path: str) -> str | None:
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = (result.stdout or result.stderr or "").strip()
            if output:
                return output
        except Exception:
            logger.debug("Failed to run %s --version", path, exc_info=True)
        return None
