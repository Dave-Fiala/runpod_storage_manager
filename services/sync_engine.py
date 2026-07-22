"""
sync_engine.py

Two-phase plan -> execute engine so the GUI can show the user exactly what will
happen before any bytes move.

- ``plan_sync`` / ``plan_remove`` are pure pool queries (instant, no IO).
- ``execute_sync`` / ``execute_remove`` run on the worker thread, updating the
  pool per completed item (so the trees tick over *during* the operation) and
  isolating per-item failures (a 40 GB batch must not die on one flaky file).

The remove plan is the reference-counted safety guarantee: a model is deletable
iff no *other* remote-existing workflow links it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from model.entities import Location
from model.workspace_pool import WorkspacePool
from viewmodels import JobReportVM

logger = logging.getLogger(__name__)


class Direction(Enum):
    PUSH = auto()  # v1
    PULL = auto()  # future


def _join_key(prefix: str, subfolder: str, filename: str) -> str:
    rel = f"{subfolder}/{filename}".replace("\\", "/").strip("/")
    return f"{prefix}{rel}"


@dataclass
class TransferItem:
    display_name: str
    remote_key: str
    size_bytes: int
    model_key: Optional[str] = None  # None for the workflow JSON itself
    local_path: Optional[str] = None
    is_workflow: bool = False
    shared_with: list[str] = field(default_factory=list)


@dataclass
class SyncPlan:
    workflow_key: str
    direction: Direction
    uploads: list[TransferItem]
    skips: list[TransferItem]
    total_bytes: int


@dataclass
class RemovePlan:
    workflow_key: str
    delete: list[TransferItem]
    retained_shared: list[TransferItem]
    reclaimed_bytes: int


class SyncEngine:
    def __init__(self, pool: WorkspacePool) -> None:
        self._pool = pool

    # ------------------------------------------------------------- plan: sync
    def plan_sync(
        self,
        workflow_key: str,
        remote_model_prefix: str,
        remote_workflow_prefix: str,
    ) -> SyncPlan:
        wf = self._pool.workflows.get(workflow_key)
        uploads: list[TransferItem] = []
        skips: list[TransferItem] = []
        if wf is None:
            return SyncPlan(workflow_key, Direction.PUSH, uploads, skips, 0)

        for model in self._pool.models_for(workflow_key):
            if not model.exists_on_local or not model.local_path:
                continue  # can't push what we don't have locally (unresolved)
            remote_key = model.remote_key or _join_key(
                remote_model_prefix, model.subfolder, model.filename
            )
            size = model.size_bytes_local or model.size_bytes or 0
            item = TransferItem(
                display_name=model.filename, remote_key=remote_key, size_bytes=size,
                model_key=model.key, local_path=model.local_path,
            )
            if self._already_present(model):
                skips.append(item)
            else:
                uploads.append(item)

        # The workflow JSON itself.
        if wf.exists_on_local and wf.local_path:
            wf_remote_key = wf.remote_key or f"{remote_workflow_prefix}{wf.filename}"
            wf_item = TransferItem(
                display_name=wf.filename, remote_key=wf_remote_key, size_bytes=0,
                local_path=wf.local_path, is_workflow=True,
            )
            if wf.exists_on_remote:
                skips.append(wf_item)
            else:
                uploads.append(wf_item)

        total = sum(i.size_bytes for i in uploads)
        return SyncPlan(workflow_key, Direction.PUSH, uploads, skips, total)

    @staticmethod
    def _already_present(model) -> bool:
        if not model.exists_on_remote:
            return False
        if model.size_bytes_remote is None or model.size_bytes_local is None:
            return True  # present; size unknown -> assume equal (v1: existence + size)
        return model.size_bytes_remote == model.size_bytes_local

    # ----------------------------------------------------------- plan: remove
    def plan_remove(self, workflow_key: str) -> RemovePlan:
        wf = self._pool.workflows.get(workflow_key)
        delete: list[TransferItem] = []
        retained: list[TransferItem] = []
        reclaimed = 0
        if wf is None:
            return RemovePlan(workflow_key, delete, retained, 0)

        shared_keys = {m.key for m in self._pool.shared_models(workflow_key)}
        for model in self._pool.models_for(workflow_key):
            if not model.exists_on_remote:
                continue
            size = model.size_bytes_remote or model.size_bytes or 0
            remote_key = model.remote_key or ""
            if model.key in shared_keys:
                others = [
                    other.name for other in self._pool.workflows_for(model.key)
                    if other.key != workflow_key and other.exists_on_remote
                ]
                retained.append(TransferItem(
                    display_name=model.filename, remote_key=remote_key, size_bytes=size,
                    model_key=model.key, shared_with=sorted(others),
                ))
            else:
                delete.append(TransferItem(
                    display_name=model.filename, remote_key=remote_key, size_bytes=size,
                    model_key=model.key,
                ))
                reclaimed += size

        if wf.exists_on_remote and wf.remote_key:
            delete.append(TransferItem(
                display_name=wf.filename, remote_key=wf.remote_key, size_bytes=0,
                is_workflow=True,
            ))

        return RemovePlan(workflow_key, delete, retained, reclaimed)

    # -------------------------------------------------------------- execute
    def execute_sync(self, plan: SyncPlan, remote, ctx) -> JobReportVM:
        total = plan.total_bytes or 1
        done = 0
        succeeded = 0
        failures: list[tuple[str, str]] = []
        n = len(plan.uploads)

        for i, item in enumerate(plan.uploads, start=1):
            if ctx.cancelled:
                failures.append((item.display_name, "cancelled"))
                continue
            ctx.status(f"Uploading {i}/{n}: {item.display_name}")
            base = done

            def cb(inc: int, _base=base, _item=item, _i=i) -> None:
                nonlocal done
                done += inc
                ctx.progress(done, total,
                             f"Uploading {_i}/{n}: {_item.display_name}")

            try:
                remote.upload_file(item.local_path, item.remote_key, cb, ctx.cancel_token)
                self._mark_uploaded(item)
                succeeded += 1
                done = base + item.size_bytes
                ctx.progress(done, total, f"Uploaded {i}/{n}: {item.display_name}")
            except Exception as exc:  # noqa: BLE001
                if ctx.cancelled:
                    failures.append((item.display_name, "cancelled"))
                    break
                logger.exception("Upload failed for %s", item.remote_key)
                failures.append((item.display_name, str(exc)))
                done = base + item.size_bytes  # advance bar past the failed item

        msg = f"Uploaded {succeeded} item(s)."
        if failures:
            msg += f" {len(failures)} failed."
        return JobReportVM("sync", succeeded, tuple(failures), msg)

    def execute_remove(self, plan: RemovePlan, remote, ctx) -> JobReportVM:
        succeeded = 0
        failures: list[tuple[str, str]] = []
        n = len(plan.delete)

        for i, item in enumerate(plan.delete, start=1):
            if ctx.cancelled:
                failures.append((item.display_name, "cancelled"))
                break
            ctx.status(f"Deleting {i}/{n}: {item.display_name}")
            ctx.progress(i - 1, n, f"Deleting {i}/{n}: {item.display_name}")
            try:
                remote.delete_object(item.remote_key)  # one key at a time (no bulk delete)
                self._mark_deleted(item)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Delete failed for %s", item.remote_key)
                failures.append((item.display_name, str(exc)))

        ctx.progress(n, n, "Delete complete")
        msg = f"Deleted {succeeded} item(s), reclaimed space."
        if failures:
            msg += f" {len(failures)} failed."
        return JobReportVM("remove", succeeded, tuple(failures), msg)

    # -------------------------------------------------------- pool mutations
    def _mark_uploaded(self, item: TransferItem) -> None:
        if item.is_workflow:
            wf = next(
                (w for w in self._pool.workflows.values() if w.filename == item.display_name),
                None,
            )
            if wf is not None:
                wf.exists_on_remote = True
                wf.remote_key = item.remote_key
                self._pool.workflowUpdated.emit(wf.key)
            return
        if item.model_key and item.model_key in self._pool.models:
            model = self._pool.models[item.model_key]
            model.exists_on_remote = True
            model.remote_key = item.remote_key
            model.size_bytes_remote = item.size_bytes
            self._pool.modelUpdated.emit(model.key)

    def _mark_deleted(self, item: TransferItem) -> None:
        if item.is_workflow:
            wf = next(
                (w for w in self._pool.workflows.values() if w.remote_key == item.remote_key),
                None,
            )
            if wf is not None:
                wf.exists_on_remote = False
                wf.remote_key = None
                self._pool.workflowUpdated.emit(wf.key)
            return
        if item.model_key and item.model_key in self._pool.models:
            model = self._pool.models[item.model_key]
            model.exists_on_remote = False
            model.remote_key = None
            model.size_bytes_remote = None
            self._pool.modelUpdated.emit(model.key)
