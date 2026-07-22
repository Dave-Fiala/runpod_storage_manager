"""
local_storage.py

Thin, testable wrapper over the local filesystem. Enumerates the local ComfyUI
``models/`` tree and workflows directory, yielding ``FileStat`` records. Raises
typed exceptions rather than returning partial silent results — the pool never
sees a half-scanned directory.

Qt-free and safe to call from a worker thread.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterator

from model.workflow_scanner import MODEL_FILE_EXTENSIONS


class LocalStorageError(Exception):
    """Base for local storage failures."""


class DirectoryNotFound(LocalStorageError):
    pass


class PermissionDenied(LocalStorageError):
    pass


@dataclass
class FileStat:
    relpath: str  # path relative to the scanned root, e.g. "loras/foo.safetensors"
    abspath: str
    size_bytes: int
    mtime: float


class LocalStorageService:
    def walk_models(self, root: str) -> Iterator[FileStat]:
        """Recursively yield model files (filtered by extension) under ``root``.

        ``relpath`` uses forward slashes and is relative to ``root`` so it maps
        directly to a ``subfolder/filename`` pool identity.
        """
        self._require_dir(root)
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if not name.lower().endswith(MODEL_FILE_EXTENSIONS):
                        continue
                    abspath = os.path.join(dirpath, name)
                    try:
                        st = os.stat(abspath)
                    except OSError:
                        continue
                    rel = os.path.relpath(abspath, root).replace("\\", "/")
                    yield FileStat(relpath=rel, abspath=abspath,
                                   size_bytes=st.st_size, mtime=st.st_mtime)
        except PermissionError as exc:
            raise PermissionDenied(str(exc)) from exc

    def walk_workflows(self, root: str) -> Iterator[FileStat]:
        """Yield ``*.json`` workflow files directly under ``root`` (flat).

        ComfyUI workflow exports are flat, so this is non-recursive.
        """
        self._require_dir(root)
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    if not entry.name.lower().endswith(".json"):
                        continue
                    st = entry.stat()
                    yield FileStat(relpath=entry.name, abspath=entry.path,
                                   size_bytes=st.st_size, mtime=st.st_mtime)
        except PermissionError as exc:
            raise PermissionDenied(str(exc)) from exc

    def read_workflow(self, path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError as exc:
            raise DirectoryNotFound(str(exc)) from exc
        except PermissionError as exc:
            raise PermissionDenied(str(exc)) from exc

    @staticmethod
    def _require_dir(root: str) -> None:
        if not root:
            raise DirectoryNotFound("No directory specified.")
        if not os.path.isdir(root):
            raise DirectoryNotFound(f"Directory not found: {root}")
