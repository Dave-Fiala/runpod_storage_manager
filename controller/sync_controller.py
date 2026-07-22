"""
sync_controller.py

Translates view intents into model operations, marshals long-running work onto
the single worker thread, and shapes domain state into immutable view-models for
the view. Holds no domain state of its own beyond references and transient job
handles.

Path-change events are debounced and funnelled through the ``JobRunner`` FIFO so
the pool has a single writer. The view never receives raw entities — only the
view-models defined in ``viewmodels.py``.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from model.app_config import AppConfig
from model.entities import Location
from model.log_model import LogModel
from model.workflow_scanner import WorkflowParseError, WorkflowScanner
from model.workspace_pool import WorkspacePool
from services.local_storage import LocalStorageError, LocalStorageService
from viewmodels import (
    ComboItemVM,
    ConnectionStatusVM,
    EnablementVM,
    JobReportVM,
    ModelInfoRowVM,
    RemovePlanVM,
    RetainedSharedVM,
    SyncPlanVM,
    TransferItemVM,
    TreeGroupVM,
    TreeRowVM,
    WorkflowStatusVM,
    human_bytes,
)
from workers.job_runner import Job, JobRunner

logger = logging.getLogger(__name__)


def _norm_prefix(prefix: str) -> str:
    """Normalise an S3 prefix: forward slashes, no leading slash, single trailing slash."""
    p = (prefix or "").replace("\\", "/").strip().lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


def _split_relpath(relpath: str) -> tuple[str, str]:
    rel = relpath.replace("\\", "/").strip("/")
    subfolder, _, filename = rel.rpartition("/")
    return subfolder, filename


class SyncController(QObject):
    # -- outbound -> view ----------------------------------------------------
    connectionStatusChanged = pyqtSignal(object)  # ConnectionStatusVM
    localTreeChanged = pyqtSignal(list)  # list[TreeGroupVM]
    remoteTreeChanged = pyqtSignal(list)
    localWorkflowsChanged = pyqtSignal(list, str)  # items, active_key
    remoteWorkflowsChanged = pyqtSignal(list, str)
    workflowStatusChanged = pyqtSignal(object)  # WorkflowStatusVM
    enablementChanged = pyqtSignal(object)  # EnablementVM
    jobProgress = pyqtSignal(int, int, str)
    currentOperation = pyqtSignal(str)
    jobStarted = pyqtSignal(str, str)
    jobFinished = pyqtSignal(object)  # JobReportVM
    logLine = pyqtSignal(object)  # LogRecord
    syncPlanReady = pyqtSignal(object)  # SyncPlanVM
    removePlanReady = pyqtSignal(object)  # RemovePlanVM
    modelInfoReady = pyqtSignal(str, list)  # title, list[ModelInfoRowVM]
    activeWorkflowSelected = pyqtSignal(str)

    def __init__(
        self,
        pool: WorkspacePool,
        local_storage: LocalStorageService,
        app_config: AppConfig,
        job_runner: JobRunner,
        log_model: LogModel,
        connection_controller: Optional[QObject] = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pool = pool
        self._local = local_storage
        self._config = app_config
        self._jobs = job_runner
        self._log = log_model
        self._conn = connection_controller
        self._scanner = WorkflowScanner()

        self._active_key: str = ""
        self._remote = None  # RemoteStorageService, set on connect
        self._connected = False
        self._drive_letter: Optional[str] = None
        self._drive_name: str = ""
        self._remote_total_bytes: Optional[int] = None
        self._remote_used_bytes: Optional[int] = None

        # Debounce timers per path-kind.
        self._debouncers: dict[str, QTimer] = {}
        self._pending_paths: dict[str, str] = {}

        # Periodic remote-usage refresh while connected (60s).
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(60_000)
        self._usage_timer.timeout.connect(self._refresh_remote_usage)

        # Wire job runner signals through to the view.
        self._jobs.jobProgress.connect(self.jobProgress)
        self._jobs.jobStarted.connect(self.jobStarted)
        self._jobs.jobStatus.connect(self.currentOperation)
        self._jobs.jobLog.connect(lambda msg: self._log.add_line(msg, source="sync"))
        self._jobs.jobFinished.connect(self._on_job_finished)
        self._jobs.jobFailed.connect(self._on_job_failed)
        self._jobs.queueIdle.connect(self._on_queue_idle)

        self._log.recordAdded.connect(self.logLine)

    # ================================================================ startup
    def start(self) -> None:
        self._jobs.start()
        data = self._config.data
        if data.local_model_dir:
            self.on_local_model_path_changed(data.local_model_dir)
        if data.local_workflow_dir:
            self.on_local_workflow_path_changed(data.local_workflow_dir)
        self._active_key = data.last_active_workflow or ""
        self._emit_all()

    # ============================================================ path events
    def on_local_model_path_changed(self, path: str) -> None:
        self._debounce("local_model", path)

    def on_local_workflow_path_changed(self, path: str) -> None:
        self._debounce("local_workflow", path)

    def on_remote_model_path_changed(self, prefix: str) -> None:
        self._debounce("remote_model", prefix)

    def on_remote_workflow_path_changed(self, prefix: str) -> None:
        self._debounce("remote_workflow", prefix)

    def _debounce(self, kind: str, value: str) -> None:
        self._pending_paths[kind] = value
        timer = self._debouncers.get(kind)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(250)
            timer.timeout.connect(lambda k=kind: self._fire_path_event(k))
            self._debouncers[kind] = timer
        timer.start()

    def _fire_path_event(self, kind: str) -> None:
        value = self._pending_paths.get(kind, "")
        if kind == "local_model":
            self._start_local_models_refresh(value)
        elif kind == "local_workflow":
            self._start_local_workflows_refresh(value)
        elif kind == "remote_model":
            self._start_remote_models_refresh(value)
        elif kind == "remote_workflow":
            self._start_remote_workflows_refresh(value)

    # -------------------------------------------------------- local refreshes
    def _start_local_models_refresh(self, root: str) -> None:
        if not root:
            return
        if not os.path.isdir(root):
            self._log.add("WARNING", "sync", f"Local models directory not found: {root}")
            return
        self._config.update(local_model_dir=root)

        def job(ctx):
            present: set[str] = set()
            files = list(self._local.walk_models(root))
            total = len(files)
            ctx.status(f"Scanning local models ({total} files)")
            for i, fs in enumerate(files):
                subfolder, filename = _split_relpath(fs.relpath)
                model = self._pool.upsert_model(
                    subfolder=subfolder, filename=filename,
                    location=Location.LOCAL, local_path=fs.abspath,
                    size_bytes=fs.size_bytes,
                )
                present.add(model.key)
                if i % 100 == 0:
                    ctx.progress(i, total, f"Scanning local models {i}/{total}")
            self._pool.reconcile_location(Location.LOCAL, "models", present)
            return None

        self._jobs.submit(Job(kind="refresh", fn=job, description="Local models scan"))

    def _start_local_workflows_refresh(self, root: str) -> None:
        if not root:
            return
        if not os.path.isdir(root):
            self._log.add("WARNING", "sync", f"Local workflows directory not found: {root}")
            return
        self._config.update(local_workflow_dir=root)

        def job(ctx):
            present: set[str] = set()
            files = list(self._local.walk_workflows(root))
            total = len(files)
            ctx.status(f"Scanning local workflows ({total} files)")
            for i, fs in enumerate(files):
                wf = self._pool.upsert_workflow(
                    filename=fs.relpath, location=Location.LOCAL, local_path=fs.abspath
                )
                present.add(wf.key)
                signature = f"local:{fs.mtime}"
                if wf.parsed and wf.parsed_signature == signature:
                    continue
                try:
                    data = self._local.read_workflow(fs.abspath)
                    refs = self._scanner.extract(data)
                    self._pool.apply_parse_result(wf.key, refs, signature=signature)
                except (WorkflowParseError, LocalStorageError) as exc:
                    self._pool.record_parse_error(wf.key, str(exc))
                ctx.progress(i + 1, total, f"Parsing workflows {i + 1}/{total}")
            self._pool.reconcile_location(Location.LOCAL, "workflows", present)
            return None

        self._jobs.submit(Job(kind="refresh", fn=job, description="Local workflows scan"))

    # ------------------------------------------------------- remote refreshes
    def _start_remote_models_refresh(self, prefix: str) -> None:
        prefix = _norm_prefix(prefix)
        if self._remote is None:
            self._log.add("WARNING", "sync", "Not connected — cannot scan remote models.")
            return
        self._config.update(remote_model_prefix=prefix)

        def job(ctx):
            present: set[str] = set()
            ctx.status("Listing remote models")
            objects = list(self._remote.list_objects(prefix))
            total = len(objects)
            for i, obj in enumerate(objects):
                subfolder, filename = _split_relpath(obj.relpath)
                if not filename:
                    continue
                model = self._pool.upsert_model(
                    subfolder=subfolder, filename=filename,
                    location=Location.REMOTE, remote_key=obj.key,
                    size_bytes=obj.size_bytes,
                )
                present.add(model.key)
                if i % 100 == 0:
                    ctx.progress(i, total, f"Listing remote models {i}/{total}")
            self._pool.reconcile_location(Location.REMOTE, "models", present)
            return None

        self._jobs.submit(Job(kind="refresh", fn=job, description="Remote models scan"))

    def _start_remote_workflows_refresh(self, prefix: str) -> None:
        prefix = _norm_prefix(prefix)
        if self._remote is None:
            self._log.add("WARNING", "sync", "Not connected — cannot scan remote workflows.")
            return
        self._config.update(remote_workflow_prefix=prefix)

        def job(ctx):
            present: set[str] = set()
            ctx.status("Listing remote workflows")
            objects = [o for o in self._remote.list_objects(prefix)
                       if o.relpath.lower().endswith(".json")]
            total = len(objects)
            for i, obj in enumerate(objects):
                _sub, filename = _split_relpath(obj.relpath)
                if not filename:
                    continue
                wf = self._pool.upsert_workflow(
                    filename=filename, location=Location.REMOTE, remote_key=obj.key
                )
                present.add(wf.key)
                signature = f"remote:{obj.mtime}"
                if wf.parsed and wf.parsed_signature == signature:
                    continue
                try:
                    data = self._remote.download_json(obj.key)
                    refs = self._scanner.extract(data)
                    self._pool.apply_parse_result(wf.key, refs, signature=signature)
                except WorkflowParseError as exc:
                    self._pool.record_parse_error(wf.key, str(exc))
                except Exception as exc:  # noqa: BLE001
                    self._pool.record_parse_error(wf.key, str(exc))
                ctx.progress(i + 1, total, f"Parsing remote workflows {i + 1}/{total}")
            self._pool.reconcile_location(Location.REMOTE, "workflows", present)
            return None

        self._jobs.submit(Job(kind="refresh", fn=job, description="Remote workflows scan"))

    # =============================================================== selection
    def on_active_workflow_changed(self, key: str) -> None:
        self._active_key = key or ""
        self._config.update(last_active_workflow=self._active_key)
        self.activeWorkflowSelected.emit(self._active_key)
        self._emit_workflows()
        self._emit_trees()
        self._emit_status()
        self._emit_enablement()

    # ================================================================ connect
    def on_connection_state_changed(self, state, detail: str = "") -> None:
        from connection_manager.model.mount_state import MountState

        if state == MountState.MOUNTED:
            self._drive_name = self._active_profile_name()
            self._drive_letter = self._active_drive_letter()
            self._build_remote_service()
        elif state in (MountState.DISCONNECTED, MountState.ERROR):
            self._teardown_remote_service()
            self._drive_letter = None
        self._emit_connection_status(drive_mounted=(state == MountState.MOUNTED))
        self._emit_enablement()

    def _build_remote_service(self) -> None:
        profile = self._active_profile()
        if profile is None or self._conn is None:
            return
        secret = self._conn.load_secret(profile.name) or ""
        if not secret:
            self._log.add("ERROR", "sync", "No stored secret for the active profile.")
            return
        try:
            from services.remote_storage import RemoteStorageService

            self._remote = RemoteStorageService(profile, secret)
        except Exception as exc:  # noqa: BLE001
            self._log.add("ERROR", "sync", f"Could not build S3 client: {exc}")
            self._remote = None
            return

        # Probe S3 independently to light the "connection established" lamp,
        # then refresh remote usage and (if persisted) remote prefixes.
        def job(ctx):
            ctx.status("Verifying S3 connection")
            self._remote.probe()
            used, _count = self._remote.bucket_usage("")
            total = self._remote.capacity_bytes()
            return {"used": used, "total": total}

        self._jobs.submit(Job(kind="usage", fn=job, description="S3 probe", silent=True))
        self._usage_timer.start()

    def _teardown_remote_service(self) -> None:
        self._usage_timer.stop()
        self._remote = None
        self._connected = False
        self._remote_used_bytes = None
        self._remote_total_bytes = None

    def _active_profile(self):
        if self._conn is None:
            return None
        return getattr(self._conn, "_active_profile", None)

    def _active_profile_name(self) -> str:
        profile = self._active_profile()
        return profile.volume_id if profile else ""

    def _active_drive_letter(self) -> Optional[str]:
        profile = self._active_profile()
        return profile.drive_letter if profile else None

    # ============================================================ sync/remove
    def on_sync_requested(self, key: str) -> None:
        if not key or self._remote is None:
            return
        from services.sync_engine import SyncEngine

        engine = SyncEngine(self._pool)
        plan = engine.plan_sync(
            key,
            remote_model_prefix=_norm_prefix(self._config.data.remote_model_prefix),
            remote_workflow_prefix=_norm_prefix(self._config.data.remote_workflow_prefix),
        )
        wf = self._pool.workflows.get(key)
        projected_label, _pct, _lvl, over = self._projection_for_upload(plan.total_bytes)
        vm = SyncPlanVM(
            workflow_key=key,
            workflow_name=wf.name if wf else key,
            uploads=tuple(TransferItemVM(i.display_name, i.size_bytes) for i in plan.uploads),
            skip_count=len(plan.skips),
            total_bytes=plan.total_bytes,
            unresolved_refs=tuple(wf.unresolved_refs) if wf else (),
            projected_label=projected_label,
            over_capacity=over,
        )
        self.syncPlanReady.emit(vm)

    def on_sync_confirmed(self, key: str) -> None:
        if not key or self._remote is None:
            return
        from services.sync_engine import SyncEngine

        engine = SyncEngine(self._pool)
        plan = engine.plan_sync(
            key,
            remote_model_prefix=_norm_prefix(self._config.data.remote_model_prefix),
            remote_workflow_prefix=_norm_prefix(self._config.data.remote_workflow_prefix),
        )

        def job(ctx):
            return engine.execute_sync(plan, self._remote, ctx)

        self._jobs.submit(Job(kind="sync", fn=job, description=f"Sync {key}"))

    def on_remove_requested(self, key: str) -> None:
        if not key or self._remote is None:
            return
        from services.sync_engine import SyncEngine

        engine = SyncEngine(self._pool)
        plan = engine.plan_remove(key)
        wf = self._pool.workflows.get(key)
        vm = RemovePlanVM(
            workflow_key=key,
            workflow_name=wf.name if wf else key,
            deletes=tuple(TransferItemVM(i.display_name, i.size_bytes) for i in plan.delete),
            retained_shared=tuple(
                RetainedSharedVM(i.display_name, tuple(i.shared_with)) for i in plan.retained_shared
            ),
            reclaimed_bytes=plan.reclaimed_bytes,
        )
        self.removePlanReady.emit(vm)

    def on_remove_confirmed(self, key: str) -> None:
        if not key or self._remote is None:
            return
        from services.sync_engine import SyncEngine

        engine = SyncEngine(self._pool)
        plan = engine.plan_remove(key)

        def job(ctx):
            return engine.execute_remove(plan, self._remote, ctx)

        self._jobs.submit(Job(kind="remove", fn=job, description=f"Remove {key}"))

    def on_cancel_requested(self) -> None:
        self._jobs.cancel_current()

    # ============================================================ model info
    def on_model_info_requested(self, key: str) -> None:
        wf = self._pool.workflows.get(key)
        if wf is None:
            return
        rows = []
        for m in sorted(self._pool.models_for(key), key=lambda x: x.filename.lower()):
            shared = [
                other.name for other in self._pool.workflows_for(m.key)
                if other.key != key
            ]
            rows.append(ModelInfoRowVM(
                name=m.filename, category=m.category, subfolder=m.subfolder,
                size_bytes=m.size_bytes, on_local=m.exists_on_local,
                on_remote=m.exists_on_remote, shared_with=tuple(sorted(shared)),
                extra=dict(m.extra),
                unresolved=(not m.exists_on_local and not m.exists_on_remote),
            ))
        self.modelInfoReady.emit(wf.name, rows)

    # ============================================================ reveal (mount)
    def on_reveal_local(self, model_key: str) -> None:
        model = self._pool.models.get(model_key)
        if model and model.local_path and os.path.exists(model.local_path):
            self._reveal(model.local_path)

    def on_reveal_remote(self, model_key: str) -> None:
        if not self._drive_letter:
            return
        model = self._pool.models.get(model_key)
        if model is None:
            return
        prefix = _norm_prefix(self._config.data.remote_model_prefix)
        rel = f"{model.subfolder}/{model.filename}".replace("/", "\\")
        path = f"{self._drive_letter}:\\{prefix.replace('/', chr(92))}{rel}"
        self._reveal(path)

    @staticmethod
    def _reveal(path: str) -> None:
        try:
            if os.path.isdir(path):
                os.startfile(path)  # noqa: S606 (Windows explorer)
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:  # noqa: BLE001
            logger.exception("Reveal failed for %s", path)

    # ================================================================ job hooks
    def _on_job_finished(self, kind: str, result) -> None:
        if kind == "usage" and isinstance(result, dict):
            self._connected = True
            self._remote_used_bytes = result.get("used")
            self._remote_total_bytes = result.get("total")
            self._emit_connection_status(drive_mounted=True)
            # Auto-refresh remote prefixes persisted from a previous session.
            data = self._config.data
            if data.remote_model_prefix:
                self._start_remote_models_refresh(data.remote_model_prefix)
            if data.remote_workflow_prefix:
                self._start_remote_workflows_refresh(data.remote_workflow_prefix)
        elif kind in ("sync", "remove"):
            report = result if isinstance(result, JobReportVM) else JobReportVM(kind, 0, (), "")
            self.jobFinished.emit(report)
            self._refresh_remote_usage()
        elif kind == "refresh":
            self.jobFinished.emit(JobReportVM(kind, 0, (), ""))
        self._emit_all()

    def _on_job_failed(self, kind: str, error: str) -> None:
        self._log.add("ERROR", "sync", f"{kind} failed: {error}")
        self.jobFinished.emit(JobReportVM(kind, 0, ((kind, error),), error))
        self._emit_enablement()

    def _on_queue_idle(self) -> None:
        self._emit_enablement()

    def _refresh_remote_usage(self) -> None:
        if self._remote is None:
            return

        def job(ctx):
            used, _count = self._remote.bucket_usage("")
            total = self._remote.capacity_bytes()
            return {"used": used, "total": total}

        self._jobs.submit(Job(kind="usage", fn=job, description="Remote usage", silent=True))

    # ================================================================ VM build
    def _emit_all(self) -> None:
        self._emit_connection_status(drive_mounted=self._drive_letter is not None)
        self._emit_workflows()
        self._emit_trees()
        self._emit_status()
        self._emit_enablement()

    def _emit_connection_status(self, drive_mounted: bool) -> None:
        vm = ConnectionStatusVM(
            connection_established=self._connected,
            drive_mounted=drive_mounted,
            drive_letter=self._drive_letter,
            drive_name=self._drive_name,
            total_space_bytes=self._remote_total_bytes,
            used_bytes=self._remote_used_bytes,
        )
        self.connectionStatusChanged.emit(vm)

    def _emit_workflows(self) -> None:
        local_items = [
            ComboItemVM(wf.key, self._wf_label(wf))
            for wf in sorted(self._pool.workflows.values(), key=lambda w: w.name.lower())
            if wf.exists_on_local
        ]
        remote_items = [
            ComboItemVM(wf.key, self._wf_label(wf))
            for wf in sorted(self._pool.workflows.values(), key=lambda w: w.name.lower())
            if wf.exists_on_remote
        ] if self._connected else []
        self.localWorkflowsChanged.emit(local_items, self._active_key)
        self.remoteWorkflowsChanged.emit(remote_items, self._active_key)

    @staticmethod
    def _wf_label(wf) -> str:
        return f"{wf.name}  ⚠" if wf.parse_error else wf.name

    def _emit_trees(self) -> None:
        self.localTreeChanged.emit(self._build_tree(Location.LOCAL))
        remote_groups = self._build_tree(Location.REMOTE) if self._connected else []
        self.remoteTreeChanged.emit(remote_groups)

    def _build_tree(self, location: Location) -> list[TreeGroupVM]:
        active_models = set()
        if self._active_key:
            active_models = {m.key for m in self._pool.models_for(self._active_key)}

        groups: dict[str, list[TreeRowVM]] = {}
        for model in self._pool.models.values():
            present = (model.exists_on_local if location is Location.LOCAL
                       else model.exists_on_remote)
            required = model.key in active_models
            if not present and not required:
                continue
            other = (model.exists_on_remote if location is Location.LOCAL
                     else model.exists_on_local)
            row = TreeRowVM(
                key=model.key, name=model.filename, size_bytes=model.size_bytes,
                used_by_count=len(model.workflow_keys), on_other_side=other,
                required_by_active=required, missing=(required and not present),
            )
            groups.setdefault(model.subfolder or "(root)", []).append(row)

        result = []
        for subfolder in sorted(groups):
            rows = tuple(sorted(groups[subfolder], key=lambda r: r.name.lower()))
            result.append(TreeGroupVM(subfolder=subfolder, rows=rows))
        return result

    def _emit_status(self) -> None:
        wf = self._pool.workflows.get(self._active_key) if self._active_key else None
        if wf is None:
            self.workflowStatusChanged.emit(WorkflowStatusVM(has_active=False))
            return
        models = self._pool.models_for(self._active_key)
        on_local = sum(1 for m in models if m.exists_on_local)
        on_remote = sum(1 for m in models if m.exists_on_remote)
        upload_bytes = self._pool.projected_remote_upload_bytes(self._active_key)
        projected_label, pct, level, _over = self._projection_for_upload(upload_bytes)
        vm = WorkflowStatusVM(
            has_active=True,
            name=wf.name,
            num_models=len(models),
            models_on_local=on_local,
            models_on_remote=on_remote,
            exists_on_local=wf.exists_on_local,
            exists_on_remote=wf.exists_on_remote,
            ready_local=self._pool.workflow_ready(self._active_key, Location.LOCAL),
            ready_remote=self._pool.workflow_ready(self._active_key, Location.REMOTE),
            projected_label=projected_label,
            projected_percent=pct,
            projected_warn_level=level,
            unresolved_refs=tuple(wf.unresolved_refs),
        )
        self.workflowStatusChanged.emit(vm)

    def _projection_for_upload(self, upload_bytes: int) -> tuple[str, int, str, bool]:
        """Projected remote usage after syncing: label, percent, warn level, over-capacity."""
        if self._remote_used_bytes is None:
            return ("Projected Remote Use: (connect to calculate)", 0, "ok", False)
        projected = self._remote_used_bytes + upload_bytes
        if not self._remote_total_bytes:
            # RunPod does not expose provisioned capacity over the S3 API.
            label = (
                f"Projected Remote Use: {human_bytes(projected)} "
                f"(+{human_bytes(upload_bytes)}, capacity unknown)"
            )
            return (label, 0, "ok", False)
        total = self._remote_total_bytes
        pct = int(projected * 100 / total)
        over = projected > total
        level = "red" if pct > 95 else "amber" if pct > 85 else "ok"
        label = (
            f"Projected Remote Use: {human_bytes(projected)} / {human_bytes(total)} "
            f"(+{human_bytes(upload_bytes)})"
        )
        return (label, pct, level, over)

    def _emit_enablement(self) -> None:
        wf = self._pool.workflows.get(self._active_key) if self._active_key else None
        data = self._config.data
        vm = EnablementVM(
            connected=self._connected,
            job_running=self._jobs.busy,
            has_active_workflow=wf is not None,
            active_exists_local=bool(wf and wf.exists_on_local),
            active_exists_remote=bool(wf and wf.exists_on_remote),
            remote_paths_set=bool(data.remote_model_prefix and data.remote_workflow_prefix),
        )
        self.enablementChanged.emit(vm)
