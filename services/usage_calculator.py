"""
usage_calculator.py

Disk-usage and projection helpers. Local usage is read via ``shutil.disk_usage``
on the models drive; remote usage comes from the ``RemoteStorageService``
listing. Kept Qt-free and side-effect-light so it is trivially unit-testable.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    used_bytes: int
    free_bytes: int


class UsageCalculator:
    @staticmethod
    def local_disk_usage(path: str) -> Optional[DiskUsage]:
        try:
            total, used, free = shutil.disk_usage(path)
            return DiskUsage(total_bytes=total, used_bytes=used, free_bytes=free)
        except OSError:
            return None

    @staticmethod
    def remote_usage(remote, prefix: str = "") -> tuple[int, int]:
        """Return (used_bytes, object_count) for the connected volume/prefix."""
        return remote.bucket_usage(prefix)

    @staticmethod
    def projection_percent(used_bytes: int, added_bytes: int,
                           capacity_bytes: Optional[int]) -> Optional[int]:
        if not capacity_bytes:
            return None
        return int((used_bytes + added_bytes) * 100 / capacity_bytes)
