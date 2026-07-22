"""
workspace_pool.py

The unified, single-source-of-truth pool of ``Model`` and ``Workflow`` objects.

Every discovery — a local filesystem scan, a remote S3 listing, or a workflow
parse — funnels through the ``_upsert_*`` methods. If a key already exists the
existing object is *enriched* in place (its location flags / paths / sizes are
updated); otherwise a new object is created. This is what guarantees "one
logical entity → one object": a model discovered locally and then again
remotely ends up as a single object with ``Location.BOTH``.

The pool is a ``QObject`` purely so it can emit change notifications (mirroring
the ``ConnectionController.stateChanged`` pattern). All mutation is expected to
happen on a single worker thread (see the threading model in the design), so
the pool itself performs no locking.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .entities import Location, Model, Workflow, normalise_key
from .workflow_scanner import ModelRef


class WorkspacePool(QObject):
    # -- signals (consumed by SyncController -> view) ------------------------
    poolRefreshed = pyqtSignal()  # bulk change complete
    workflowUpdated = pyqtSignal(str)  # workflow key
    modelUpdated = pyqtSignal(str)  # model key
    scanProgress = pyqtSignal(str, int, int)  # phase, done, total

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.models: dict[str, Model] = {}
        self.workflows: dict[str, Workflow] = {}

    # ------------------------------------------------------------------ upsert
    def upsert_model(
        self,
        *,
        subfolder: str,
        filename: str,
        category: str = "unknown",
        location: Optional[Location] = None,
        local_path: Optional[str] = None,
        remote_key: Optional[str] = None,
        size_bytes: Optional[int] = None,
        last_seen: Optional[datetime] = None,
        extra: Optional[dict] = None,
    ) -> Model:
        """Create or enrich a Model. Returns the (single) pool object.

        ``location`` says which side this fact came from. When ``LOCAL`` the
        ``local_path`` / local size / local timestamp are set; when ``REMOTE``
        the remote equivalents are set. Passing no location merely ensures the
        object exists (used for workflow-referenced models not yet on disk).
        """
        key = normalise_key(subfolder, filename)
        model = self.models.get(key)
        if model is None:
            model = Model(key=key, filename=filename, subfolder=subfolder, category=category)
            self.models[key] = model
        else:
            # Enrich category if we now have a better (non-unknown) guess.
            if category and category != "unknown" and model.category == "unknown":
                model.category = category

        if location and Location.LOCAL in location:
            model.exists_on_local = True
            if local_path is not None:
                model.local_path = local_path
            if size_bytes is not None:
                model.size_bytes_local = size_bytes
            model.last_seen_local = last_seen or datetime.now()
        if location and Location.REMOTE in location:
            model.exists_on_remote = True
            if remote_key is not None:
                model.remote_key = remote_key
            if size_bytes is not None:
                model.size_bytes_remote = size_bytes
            model.last_seen_remote = last_seen or datetime.now()

        if extra:
            model.extra.update(extra)

        self.modelUpdated.emit(key)
        return model

    def upsert_workflow(
        self,
        *,
        filename: str,
        location: Optional[Location] = None,
        local_path: Optional[str] = None,
        remote_key: Optional[str] = None,
    ) -> Workflow:
        """Create or enrich a Workflow. Returns the (single) pool object."""
        key = normalise_key(filename)
        wf = self.workflows.get(key)
        if wf is None:
            name = filename.rsplit(".", 1)[0] if "." in filename else filename
            wf = Workflow(key=key, name=name, filename=filename)
            self.workflows[key] = wf

        if location and Location.LOCAL in location:
            wf.exists_on_local = True
            if local_path is not None:
                wf.local_path = local_path
        if location and Location.REMOTE in location:
            wf.exists_on_remote = True
            if remote_key is not None:
                wf.remote_key = remote_key

        self.workflowUpdated.emit(key)
        return wf

    def apply_parse_result(
        self,
        workflow_key: str,
        refs: list[ModelRef],
        *,
        signature: Optional[str] = None,
    ) -> None:
        """Record the parse result for a workflow: link referenced models,
        creating placeholder models (existing nowhere) for unresolved refs."""
        wf = self.workflows.get(workflow_key)
        if wf is None:
            return

        # Clear previous links (re-parse fully replaces the reference set).
        self.unlink_workflow(workflow_key)

        for ref in refs:
            model = self.upsert_model(
                subfolder=ref.subfolder,
                filename=ref.filename,
                category=ref.category,
                extra={ref.node_type: ref.extra} if ref.extra else None,
            )
            self.link(workflow_key, model.key)

        wf.parsed = True
        wf.parse_error = None
        wf.parsed_signature = signature
        wf.unresolved_refs = [
            m.filename
            for m in self.models_for(workflow_key)
            if not m.exists_on_local and not m.exists_on_remote
        ]
        self.workflowUpdated.emit(workflow_key)

    def record_parse_error(self, workflow_key: str, error: str) -> None:
        wf = self.workflows.get(workflow_key)
        if wf is None:
            return
        wf.parsed = False
        wf.parse_error = error
        self.workflowUpdated.emit(workflow_key)

    # -------------------------------------------------------------- navigation
    def models_for(self, workflow_key: str) -> list[Model]:
        wf = self.workflows.get(workflow_key)
        if wf is None:
            return []
        return [self.models[k] for k in wf.model_keys if k in self.models]

    def workflows_for(self, model_key: str) -> list[Workflow]:
        model = self.models.get(model_key)
        if model is None:
            return []
        return [self.workflows[k] for k in model.workflow_keys if k in self.workflows]

    def link(self, workflow_key: str, model_key: str) -> None:
        """Create a bidirectional workflow<->model link."""
        wf = self.workflows.get(workflow_key)
        model = self.models.get(model_key)
        if wf is None or model is None:
            return
        wf.model_keys.add(model_key)
        model.workflow_keys.add(workflow_key)

    def unlink_workflow(self, workflow_key: str) -> None:
        """Clear all links from a workflow and prune orphaned placeholders."""
        wf = self.workflows.get(workflow_key)
        if wf is None:
            return
        for model_key in list(wf.model_keys):
            model = self.models.get(model_key)
            if model is not None:
                model.workflow_keys.discard(workflow_key)
                self._maybe_prune_model(model_key)
        wf.model_keys.clear()

    # ------------------------------------------------------------- reconcile
    def reconcile_location(
        self,
        location: Location,
        kind: str,
        present_keys: set[str],
    ) -> None:
        """Clear stale location flags after a fresh enumeration.

        ``kind`` is "models" or "workflows". Any object previously flagged for
        ``location`` but absent from ``present_keys`` has that flag cleared
        (the file was deleted out-of-band). Objects that become ``NOWHERE`` and
        have no links are dropped; linked objects are retained as *missing*.
        """
        if kind == "models":
            for key in list(self.models.keys()):
                model = self.models[key]
                if location is Location.LOCAL and model.exists_on_local and key not in present_keys:
                    model.exists_on_local = False
                    model.local_path = None
                    model.size_bytes_local = None
                    self.modelUpdated.emit(key)
                elif location is Location.REMOTE and model.exists_on_remote and key not in present_keys:
                    model.exists_on_remote = False
                    model.remote_key = None
                    model.size_bytes_remote = None
                    self.modelUpdated.emit(key)
                self._maybe_prune_model(key)
        elif kind == "workflows":
            for key in list(self.workflows.keys()):
                wf = self.workflows[key]
                if location is Location.LOCAL and wf.exists_on_local and key not in present_keys:
                    wf.exists_on_local = False
                    wf.local_path = None
                    self.workflowUpdated.emit(key)
                elif location is Location.REMOTE and wf.exists_on_remote and key not in present_keys:
                    wf.exists_on_remote = False
                    wf.remote_key = None
                    self.workflowUpdated.emit(key)
                self._maybe_prune_workflow(key)

    def _maybe_prune_model(self, model_key: str) -> None:
        model = self.models.get(model_key)
        if model is None:
            return
        if model.location is Location.NOWHERE and not model.workflow_keys:
            del self.models[model_key]

    def _maybe_prune_workflow(self, workflow_key: str) -> None:
        wf = self.workflows.get(workflow_key)
        if wf is None:
            return
        if wf.location is Location.NOWHERE:
            self.unlink_workflow(workflow_key)
            del self.workflows[workflow_key]

    # ---------------------------------------------------------------- queries
    def workflow_ready(self, workflow_key: str, location: Location) -> bool:
        """True iff the workflow file AND every linked model exist at `location`."""
        wf = self.workflows.get(workflow_key)
        if wf is None:
            return False
        file_ok = (
            wf.exists_on_local if location is Location.LOCAL else wf.exists_on_remote
        )
        if not file_ok:
            return False
        models = self.models_for(workflow_key)
        if not models and wf.parse_error:
            return False
        for m in models:
            present = m.exists_on_local if location is Location.LOCAL else m.exists_on_remote
            if not present:
                return False
        return True

    def missing_models(self, workflow_key: str, location: Location) -> list[Model]:
        result = []
        for m in self.models_for(workflow_key):
            present = m.exists_on_local if location is Location.LOCAL else m.exists_on_remote
            if not present:
                result.append(m)
        return result

    def shared_models(self, workflow_key: str) -> list[Model]:
        """Models referenced by at least one OTHER remote-existing workflow."""
        result = []
        for m in self.models_for(workflow_key):
            for other_key in m.workflow_keys:
                if other_key == workflow_key:
                    continue
                other = self.workflows.get(other_key)
                if other is not None and other.exists_on_remote:
                    result.append(m)
                    break
        return result

    def projected_local_usage_delta(self, workflow_key: str) -> int:
        """Bytes that would be added locally to make this workflow ready
        locally (i.e. sum of sizes of models missing on local)."""
        total = 0
        for m in self.missing_models(workflow_key, Location.LOCAL):
            total += m.size_bytes or 0
        return total

    def projected_remote_upload_bytes(self, workflow_key: str) -> int:
        """Sum of sizes of models missing on remote (the actual transfer cost)."""
        total = 0
        for m in self.missing_models(workflow_key, Location.REMOTE):
            total += m.size_bytes or 0
        return total

    def clear(self) -> None:
        self.models.clear()
        self.workflows.clear()
        self.poolRefreshed.emit()
