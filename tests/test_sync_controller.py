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
