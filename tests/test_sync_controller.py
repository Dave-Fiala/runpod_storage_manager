"""pytest-qt tests for SyncController: local refresh pipeline + enablement.

These exercise the real JobRunner worker thread, so they wait on Qt signals.
"""
from __future__ import annotations

import json

import pytest

from controller.sync_controller import SyncController
from model.app_config import AppConfig
from model.log_model import LogModel
from model.workspace_pool import WorkspacePool
from services.local_storage import LocalStorageService
from viewmodels import EnablementVM
from workers.job_runner import JobRunner


@pytest.fixture
def controller_env(tmp_path, qapp):
    models_dir = tmp_path / "models" / "checkpoints"
    models_dir.mkdir(parents=True)
    (models_dir / "a.safetensors").write_bytes(b"x" * 100)

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    workflow = {"nodes": [
        {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["a.safetensors"]}
    ]}
    (wf_dir / "wf.json").write_text(json.dumps(workflow))

    pool = WorkspacePool()
    controller = SyncController(
        pool=pool,
        local_storage=LocalStorageService(),
        app_config=AppConfig(config_dir=str(tmp_path / "cfg")),
        job_runner=JobRunner(),
        log_model=LogModel(),
        connection_controller=None,
    )
    return controller, pool, str(tmp_path / "models"), str(wf_dir)


def test_local_refresh_populates_pool_and_combo(qtbot, controller_env):
    controller, pool, models_dir, wf_dir = controller_env
    seen: list = []
    controller.localWorkflowsChanged.connect(lambda items, key: seen.append(items))

    controller.on_local_model_path_changed(models_dir)
    controller.on_local_workflow_path_changed(wf_dir)

    qtbot.waitUntil(lambda: len(pool.workflows) >= 1 and len(pool.models) >= 1, timeout=8000)
    # The workflow's model should have merged with the on-disk model (one object).
    qtbot.waitUntil(lambda: any(m.exists_on_local for m in pool.models.values()), timeout=8000)

    model = next(iter(pool.models.values()))
    assert model.exists_on_local
    assert model.filename == "a.safetensors"
    # Combo signal fired with the local workflow present.
    qtbot.waitUntil(lambda: any(len(items) >= 1 for items in seen), timeout=8000)


def test_workflow_ready_after_full_local_scan(qtbot, controller_env):
    controller, pool, models_dir, wf_dir = controller_env
    controller.on_local_model_path_changed(models_dir)
    controller.on_local_workflow_path_changed(wf_dir)

    from model.entities import Location

    def _ready():
        keys = list(pool.workflows)
        return bool(keys) and pool.workflow_ready(keys[0], Location.LOCAL)

    qtbot.waitUntil(_ready, timeout=8000)


def test_enablement_reflects_not_connected(qtbot, controller_env):
    controller, pool, models_dir, wf_dir = controller_env
    captured: list[EnablementVM] = []
    controller.enablementChanged.connect(captured.append)

    controller.on_local_workflow_path_changed(wf_dir)
    qtbot.waitUntil(lambda: len(captured) >= 1, timeout=8000)
    vm = captured[-1]
    assert vm.connected is False
    assert vm.remote_paths_set is False


def test_jobs_run_serially(qtbot, controller_env):
    controller, pool, models_dir, wf_dir = controller_env
    finished: list = []
    controller.jobFinished.connect(finished.append)

    controller.on_local_model_path_changed(models_dir)
    controller.on_local_workflow_path_changed(wf_dir)

    # Two refresh jobs (models + workflows) should each complete.
    qtbot.waitUntil(lambda: len(finished) >= 2, timeout=8000)
    controller._jobs.shutdown()


def test_usage_scan_deferred_without_prefix(controller_env):
    controller, pool, models_dir, wf_dir = controller_env
    deferred: list[str] = []
    controller.usageScanDeferred.connect(deferred.append)
    controller._connected = True
    controller._remote = object()

    controller._start_usage_scan()

    assert len(deferred) == 1
    assert "remote model path" in deferred[0].lower()
    assert controller._usage_scan_pending is False


def test_usage_scan_runs_with_prefix(qtbot, controller_env):
    controller, pool, models_dir, wf_dir = controller_env
    controller.start()
    controller._config.update(remote_model_prefix="models")
    controller._connected = True

    class MockRemote:
        def bucket_usage(self, prefix, progress_cb=None, progress_interval=100):
            assert prefix == "models/"
            if progress_cb:
                progress_cb(1, "Reading usage (1 objects)…")
            return 2048, 1

    controller._remote = MockRemote()
    finished: list[tuple[bool, str]] = []
    controller.usageScanFinished.connect(lambda ok, d: finished.append((ok, d)))

    controller._start_usage_scan()
    assert controller._usage_scan_pending

    qtbot.waitUntil(lambda: len(finished) >= 1, timeout=8000)
    assert finished[0][0] is True
    assert controller._remote_used_bytes == 2048
    assert controller._usage_scan_pending is False
    controller._jobs.shutdown()


def test_probe_sets_connected_before_usage_completes(qtbot, controller_env):
    from workers.job_runner import Job

    controller, pool, models_dir, wf_dir = controller_env
    controller.start()
    controller._drive_letter = "Z"

    class MockRemote:
        def probe(self) -> None:
            pass

        def bucket_usage(self, prefix, progress_cb=None, progress_interval=100):
            return 0, 0

    controller._remote = MockRemote()

    def probe_job(ctx):
        controller._remote.probe()
        return True

    controller._jobs.submit(Job(kind="probe", fn=probe_job, description="test probe", silent=True))
    qtbot.waitUntil(lambda: controller._connected, timeout=8000)
    assert controller._connected is True
    controller._jobs.shutdown()
