from __future__ import annotations

from abc import ABC, abstractmethod

from ..model.connection_profile import ConnectionProfile


class MountError(Exception):
    """Raised when a mount or unmount operation fails."""


class MountService(ABC):
    @abstractmethod
    def mount(
        self,
        profile: ConnectionProfile,
        geesefs_path: str,
        secret: str,
        log_callback: object | None = None,
    ) -> None:
        """Mount the volume. Raises MountError on failure."""

    @abstractmethod
    def unmount(self) -> None:
        """Unmount the currently mounted volume."""

    @abstractmethod
    def is_mounted(self) -> bool:
        """Return True if a volume is currently mounted."""

    @abstractmethod
    def poll(self) -> bool:
        """Check if the backing process is alive. Returns False if it died unexpectedly."""
