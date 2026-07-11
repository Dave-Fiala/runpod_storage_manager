from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable

from ..model.connection_profile import ConnectionProfile
from .mount_service import MountError, MountService

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
_MOUNT_TIMEOUT = 30  # seconds


class WindowsMountService(MountService):
    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._log_callback: Callable[[str], None] | None = None
        self._lock = threading.Lock()

    def mount(
        self,
        profile: ConnectionProfile,
        geesefs_path: str,
        secret: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise MountError("A mount is already active")

        self._log_callback = log_callback
        cmd = self.build_command(profile, geesefs_path)

        env = os.environ.copy()
        env["AWS_ACCESS_KEY_ID"] = profile.access_key_id
        env["AWS_SECRET_ACCESS_KEY"] = secret

        logger.info(
            "Mounting %s to %s: via %s",
            profile.volume_id, f"{profile.drive_letter}:", geesefs_path,
        )

        try:
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                text=True,
            )
        except FileNotFoundError:
            raise MountError(f"geesefs not found at {geesefs_path}")
        except OSError as exc:
            raise MountError(f"Failed to launch geesefs: {exc}")

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="geesefs-stderr",
        )
        self._stderr_thread.start()

        drive = f"{profile.drive_letter}:\\"
        deadline = time.time() + _MOUNT_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                remaining_err = ""
                if self._proc.stderr:
                    try:
                        remaining_err = self._proc.stderr.read()
                    except Exception:
                        pass
                raise MountError(
                    f"geesefs exited during mount (code {self._proc.returncode}): {remaining_err}"
                )
            if os.path.exists(drive):
                logger.info("Drive %s is live", drive)
                return
            time.sleep(0.5)

        self.unmount()
        raise MountError(f"Drive {drive} not ready after {_MOUNT_TIMEOUT}s")

    def unmount(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                return

        logger.info("Unmounting (PID %s)...", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Graceful shutdown timed out, killing PID %s", proc.pid)
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error("Failed to kill PID %s", proc.pid)

        with self._lock:
            self._proc = None

        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2)
        self._stderr_thread = None

    def is_mounted(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def poll(self) -> bool:
        with self._lock:
            if self._proc is None:
                return False
            if self._proc.poll() is not None:
                logger.warning("geesefs process died (code %s)", self._proc.returncode)
                self._proc = None
                return False
            return True

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._proc.pid if self._proc else None

    @staticmethod
    def build_command(profile: ConnectionProfile, geesefs_path: str) -> list[str]:
        return [
            geesefs_path,
            "--endpoint", profile.endpoint,
            "--region", profile.region,
            f"--file-mode={profile.file_mode}",
            f"--dir-mode={profile.dir_mode}",
            profile.volume_id,
            f"{profile.drive_letter}:",
        ]

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                line = line.rstrip("\n\r")
                if line:
                    logger.debug("[geesefs] %s", line)
                    if self._log_callback:
                        try:
                            self._log_callback(line)
                        except Exception:
                            pass
        except (ValueError, OSError):
            pass
