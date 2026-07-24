"""
Domain entities for the ComfyUI RunPod Storage Manager.

A `Model` or `Workflow` is a single logical object regardless of whether the
underlying file currently lives locally, remotely, or in both places. Location
is an *attribute* of the object (see `Location`), never a reason to duplicate it.

These dataclasses are deliberately Qt-free and hold relationships as *sets of
keys* (not object references) so they remain trivially serialisable and free of
reference cycles. Object-level navigation is provided by `WorkspacePool`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Flag, auto
from typing import Optional


class Location(Flag):
    """Where a logical entity currently exists."""

    NOWHERE = 0
    LOCAL = auto()
    REMOTE = auto()
    BOTH = LOCAL | REMOTE


def normalise_key(*parts: str) -> str:
    """Build a pool key from path components.

    Identity is content-addressed by relative path, case-insensitive (Windows
    filesystems are case-insensitive). Backslashes are normalised to forward
    slashes so a locally-scanned ``loras\\foo.safetensors`` and a remote S3 key
    ``loras/foo.safetensors`` collapse to the same object.
    """
    joined = "/".join(p for p in parts if p)
    return joined.replace("\\", "/").strip("/").lower()


@dataclass
class Model:
    # -- identity ------------------------------------------------------------
    key: str  # normalised "subfolder/filename", lowercase — pool dict key
    filename: str  # "flux-2-klein-9b-fp8.safetensors"
    subfolder: str  # "diffusion_models"
    category: str  # "diffusion_model" | "checkpoint" | "lora" | ...

    # -- location state ------------------------------------------------------
    exists_on_local: bool = False
    exists_on_remote: bool = False
    local_path: Optional[str] = None  # resolved absolute local path
    remote_key: Optional[str] = None  # resolved S3 object key

    # -- metadata ------------------------------------------------------------
    size_bytes_local: Optional[int] = None
    size_bytes_remote: Optional[int] = None
    last_seen_local: Optional[datetime] = None
    last_seen_remote: Optional[datetime] = None
    extra: dict = field(default_factory=dict)  # e.g. lora strengths per node

    # -- relationships -------------------------------------------------------
    workflow_keys: set[str] = field(default_factory=set)

    @property
    def location(self) -> Location:
        loc = Location.NOWHERE
        if self.exists_on_local:
            loc |= Location.LOCAL
        if self.exists_on_remote:
            loc |= Location.REMOTE
        return loc

    @property
    def size_bytes(self) -> Optional[int]:
        """Best-known size (local preferred, else remote)."""
        if self.size_bytes_local is not None:
            return self.size_bytes_local
        return self.size_bytes_remote


@dataclass
class Workflow:
    # -- identity ------------------------------------------------------------
    key: str  # normalised workflow filename, lowercase
    name: str  # display name (filename without extension)
    filename: str  # "260615_MICKMUMPITZ_FLUX_KLEIN_9B_V01.json"

    # -- location state ------------------------------------------------------
    exists_on_local: bool = False
    exists_on_remote: bool = False
    local_path: Optional[str] = None
    remote_key: Optional[str] = None

    # -- parse state ---------------------------------------------------------
    parsed: bool = False
    parse_error: Optional[str] = None
    # source signature (mtime/etag) of the copy last parsed, so re-scans can
    # skip re-parsing unchanged files.
    parsed_signature: Optional[str] = None

    # -- relationships -------------------------------------------------------
    model_keys: set[str] = field(default_factory=set)

    @property
    def location(self) -> Location:
        loc = Location.NOWHERE
        if self.exists_on_local:
            loc |= Location.LOCAL
        if self.exists_on_remote:
            loc |= Location.REMOTE
        return loc
