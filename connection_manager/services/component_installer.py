from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .component_checker import ComponentChecker, ComponentStatus

logger = logging.getLogger(__name__)

_GEESEFS_VERSION = "0.42.1"
_GEESEFS_URL = (
    f"https://github.com/yandex-cloud/geesefs/releases/download/v{_GEESEFS_VERSION}"
    f"/geesefs-win-x64.exe"
)
_GEESEFS_DIR = os.path.join(
    os.getenv("LOCALAPPDATA", ""), "RunpodStorageTool", "bin"
)
_WINFSP_MSI_URL = "https://github.com/winfsp/winfsp/releases/download/v2.0/winfsp-2.0.23075.msi"


@dataclass
class InstallProgress:
    component: str
    percent: int  # 0..100, -1 for indeterminate
    message: str


ProgressCallback = Callable[[InstallProgress], None]


def _noop_progress(_: InstallProgress) -> None:
    pass


class ComponentInstaller:
    def __init__(self, checker: ComponentChecker) -> None:
        self._checker = checker

    def ensure_winfsp(self, progress_cb: ProgressCallback = _noop_progress) -> ComponentStatus:
        status = self._checker.check_winfsp()
        if status.installed:
            progress_cb(InstallProgress("WinFsp", 100, "Already installed"))
            return status

        progress_cb(InstallProgress("WinFsp", 0, "Downloading WinFsp installer..."))

        msi_dir = Path(tempfile.mkdtemp(prefix="runpod_winfsp_"))
        msi_path = msi_dir / "winfsp.msi"
        try:
            self._download(str(_WINFSP_MSI_URL), str(msi_path), "WinFsp", progress_cb)
        except Exception as exc:
            progress_cb(InstallProgress("WinFsp", -1, f"Download failed: {exc}"))
            raise

        progress_cb(InstallProgress("WinFsp", 70, "Installing WinFsp (requesting elevation)..."))

        try:
            ret = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[union-attr]
                None,
                "runas",
                "msiexec",
                f'/i "{msi_path}" /qn /norestart',
                None,
                0,  # SW_HIDE
            )
            if ret <= 32:
                msg = "User declined UAC prompt" if ret == 5 else f"ShellExecute failed ({ret})"
                progress_cb(InstallProgress("WinFsp", -1, msg))
                return ComponentStatus(
                    name="WinFsp", installed=False, version=None, path=None, detail=msg,
                )

            progress_cb(InstallProgress("WinFsp", 90, "Waiting for installer to finish..."))
            # ShellExecuteW with runas doesn't give us a process handle to wait on
            # directly; poll the checker until installed or timeout
            import time
            deadline = time.time() + 120
            while time.time() < deadline:
                time.sleep(3)
                status = self._checker.check_winfsp()
                if status.installed:
                    progress_cb(InstallProgress("WinFsp", 100, "WinFsp installed successfully"))
                    return status

            progress_cb(InstallProgress(
                "WinFsp", -1,
                "Installation may require a reboot. Restart your computer and try again.",
            ))
            return self._checker.check_winfsp()

        except AttributeError:
            msg = "Elevated install not available on this platform"
            progress_cb(InstallProgress("WinFsp", -1, msg))
            return ComponentStatus(
                name="WinFsp", installed=False, version=None, path=None, detail=msg,
            )

    def ensure_geesefs(self, progress_cb: ProgressCallback = _noop_progress) -> ComponentStatus:
        status = self._checker.check_geesefs()
        if status.installed:
            progress_cb(InstallProgress("geesefs", 100, "Already installed"))
            return status

        progress_cb(InstallProgress("geesefs", 0, "Downloading geesefs..."))

        dest_dir = Path(_GEESEFS_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "geesefs.exe"

        try:
            self._download(_GEESEFS_URL, str(dest), "geesefs", progress_cb)
        except Exception as exc:
            progress_cb(InstallProgress("geesefs", -1, f"Download failed: {exc}"))
            raise

        self._clear_zone_identifier(dest)

        progress_cb(InstallProgress("geesefs", 90, "Verifying geesefs..."))

        try:
            result = subprocess.run(
                [str(dest), "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            version = (result.stdout or result.stderr or "").strip()
        except Exception as exc:
            progress_cb(InstallProgress("geesefs", -1, f"Verification failed: {exc}"))
            return ComponentStatus(
                name="geesefs", installed=False, version=None,
                path=str(dest), detail=f"Binary downloaded but won't execute: {exc}",
            )

        progress_cb(InstallProgress("geesefs", 100, f"geesefs {version} ready"))
        return ComponentStatus(
            name="geesefs", installed=True, version=version, path=str(dest),
        )

    # -- internal helpers --

    @staticmethod
    def _download(url: str, dest: str, label: str, progress_cb: ProgressCallback) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "RunpodStorageTool/0.1"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = min(int(downloaded / total * 60) + 10, 70)
                        progress_cb(InstallProgress(
                            label, pct,
                            f"Downloading... {downloaded // (1024 * 1024)}MB"
                            f" / {total // (1024 * 1024)}MB",
                        ))

    @staticmethod
    def _clear_zone_identifier(path: Path) -> None:
        """Remove the Zone.Identifier ADS that triggers SmartScreen warnings."""
        ads = Path(f"{path}:Zone.Identifier")
        try:
            ads.unlink(missing_ok=True)
        except OSError:
            pass
