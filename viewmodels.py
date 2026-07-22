"""
viewmodels.py

Immutable data objects that form the contract between the ``SyncController`` and
the view. The view renders these and never touches the domain pool directly;
the controller builds them and never touches Qt widgets. This keeps the MVC
boundary sharp and both sides independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ConnectionStatusVM:
    connection_established: bool = False
    drive_mounted: bool = False
    drive_letter: Optional[str] = None
    drive_name: str = ""
    total_space_bytes: Optional[int] = None
    used_bytes: Optional[int] = None


@dataclass(frozen=True)
class ComboItemVM:
    key: str
    label: str


@dataclass(frozen=True)
class TreeRowVM:
    key: str  # model key
    name: str  # filename
    size_bytes: Optional[int]
    used_by_count: int
    on_other_side: bool  # local tree -> on remote; remote tree -> on local
    required_by_active: bool
    missing: bool  # required by active workflow but absent on this side (ghost)


@dataclass(frozen=True)
class TreeGroupVM:
    subfolder: str
    rows: tuple[TreeRowVM, ...]


@dataclass(frozen=True)
class WorkflowStatusVM:
    has_active: bool = False
    name: str = ""
    num_models: int = 0
    models_on_local: int = 0
    models_on_remote: int = 0
    exists_on_local: bool = False
    exists_on_remote: bool = False
    ready_local: bool = False
    ready_remote: bool = False
    projected_label: str = ""
    projected_percent: int = 0
    projected_warn_level: str = "ok"  # "ok" | "amber" | "red"
    unresolved_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelInfoRowVM:
    name: str
    category: str
    subfolder: str
    size_bytes: Optional[int]
    on_local: bool
    on_remote: bool
    shared_with: tuple[str, ...]
    extra: dict = field(default_factory=dict)
    unresolved: bool = False


@dataclass(frozen=True)
class TransferItemVM:
    name: str
    size_bytes: int


@dataclass(frozen=True)
class SyncPlanVM:
    workflow_key: str
    workflow_name: str
    uploads: tuple[TransferItemVM, ...]
    skip_count: int
    total_bytes: int
    unresolved_refs: tuple[str, ...]
    projected_label: str
    over_capacity: bool


@dataclass(frozen=True)
class RetainedSharedVM:
    name: str
    shared_with: tuple[str, ...]


@dataclass(frozen=True)
class RemovePlanVM:
    workflow_key: str
    workflow_name: str
    deletes: tuple[TransferItemVM, ...]
    retained_shared: tuple[RetainedSharedVM, ...]
    reclaimed_bytes: int


@dataclass(frozen=True)
class JobReportVM:
    kind: str
    succeeded: int
    failures: tuple[tuple[str, str], ...]  # (item name, error)
    message: str


@dataclass(frozen=True)
class EnablementVM:
    connected: bool
    job_running: bool
    has_active_workflow: bool
    active_exists_local: bool
    active_exists_remote: bool
    remote_paths_set: bool


def human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
