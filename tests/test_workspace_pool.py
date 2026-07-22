"""Unit tests for WorkspacePool invariants.

These require a QApplication because WorkspacePool is a QObject that emits
signals; a session-scoped app fixture is provided in conftest.py.
"""
from __future__ import annotations

from model.entities import Location
from model.workflow_scanner import ModelRef
from model.workspace_pool import WorkspacePool


def test_upsert_merges_locations_into_single_object(qapp):
    pool = WorkspacePool()
    pool.upsert_model(subfolder="loras", filename="a.safetensors",
                      location=Location.LOCAL, local_path="D:/m/loras/a.safetensors",
                      size_bytes=100)
    pool.upsert_model(subfolder="loras", filename="a.safetensors",
                      location=Location.REMOTE, remote_key="models/loras/a.safetensors",
                      size_bytes=100)
    assert len(pool.models) == 1
    model = next(iter(pool.models.values()))
    assert model.location is Location.BOTH
    assert model.exists_on_local and model.exists_on_remote
    assert model.local_path == "D:/m/loras/a.safetensors"
    assert model.remote_key == "models/loras/a.safetensors"


def test_key_normalisation_case_and_slashes(qapp):
    pool = WorkspacePool()
    m1 = pool.upsert_model(subfolder="LoRAs", filename="Foo.safetensors", location=Location.LOCAL)
    m2 = pool.upsert_model(subfolder="loras", filename="foo.safetensors", location=Location.REMOTE)
    assert m1.key == m2.key
    assert len(pool.models) == 1


def test_stale_flag_cleared_on_rescan(qapp):
    pool = WorkspacePool()
    pool.upsert_model(subfolder="loras", filename="a.safetensors", location=Location.LOCAL)
    key = next(iter(pool.models))
    # Re-scan of local finds nothing → local flag cleared, object pruned (no links).
    pool.reconcile_location(Location.LOCAL, "models", present_keys=set())
    assert key not in pool.models


def test_linked_orphan_retained_as_missing(qapp):
    pool = WorkspacePool()
    pool.upsert_workflow(filename="wf.json", location=Location.LOCAL)
    wf_key = next(iter(pool.workflows))
    pool.apply_parse_result(
        wf_key,
        [ModelRef(node_id=1, node_type="CheckpointLoaderSimple", node_title=None,
                  category="checkpoint", filename="a.safetensors", subfolder="checkpoints")],
    )
    model_key = next(iter(pool.models))
    # The model exists nowhere but is linked → retained after reconcile.
    pool.reconcile_location(Location.LOCAL, "models", present_keys=set())
    assert model_key in pool.models
    assert pool.models[model_key].location is Location.NOWHERE


def test_workflow_ready_requires_file_and_all_models(qapp):
    pool = WorkspacePool()
    pool.upsert_workflow(filename="wf.json", location=Location.LOCAL)
    wf_key = next(iter(pool.workflows))
    pool.apply_parse_result(
        wf_key,
        [ModelRef(node_id=1, node_type="CheckpointLoaderSimple", node_title=None,
                  category="checkpoint", filename="a.safetensors", subfolder="checkpoints")],
    )
    assert not pool.workflow_ready(wf_key, Location.LOCAL)  # model missing
    # Model appears locally → ready.
    pool.upsert_model(subfolder="checkpoints", filename="a.safetensors", location=Location.LOCAL)
    assert pool.workflow_ready(wf_key, Location.LOCAL)


def test_shared_models_reference_counting(qapp):
    pool = WorkspacePool()
    pool.upsert_workflow(filename="wf1.json", location=Location.REMOTE)
    pool.upsert_workflow(filename="wf2.json", location=Location.REMOTE)
    wf1 = next(k for k in pool.workflows if k.startswith("wf1"))
    wf2 = next(k for k in pool.workflows if k.startswith("wf2"))
    shared_ref = ModelRef(node_id=1, node_type="CheckpointLoaderSimple", node_title=None,
                          category="checkpoint", filename="shared.safetensors", subfolder="checkpoints")
    lone_ref = ModelRef(node_id=2, node_type="VAELoader", node_title=None,
                        category="vae", filename="lone.safetensors", subfolder="vae")
    pool.apply_parse_result(wf1, [shared_ref, lone_ref])
    pool.apply_parse_result(wf2, [dict_to_ref(shared_ref)])

    shared = pool.shared_models(wf1)
    names = {m.filename for m in shared}
    assert names == {"shared.safetensors"}


def dict_to_ref(ref: ModelRef) -> ModelRef:
    return ModelRef(node_id=ref.node_id, node_type=ref.node_type, node_title=ref.node_title,
                    category=ref.category, filename=ref.filename, subfolder=ref.subfolder,
                    subgraph_path=ref.subgraph_path, extra=dict(ref.extra))


def test_projected_remote_upload_bytes(qapp):
    pool = WorkspacePool()
    pool.upsert_workflow(filename="wf.json", location=Location.LOCAL)
    wf_key = next(iter(pool.workflows))
    pool.apply_parse_result(
        wf_key,
        [ModelRef(node_id=1, node_type="CheckpointLoaderSimple", node_title=None,
                  category="checkpoint", filename="a.safetensors", subfolder="checkpoints")],
    )
    pool.upsert_model(subfolder="checkpoints", filename="a.safetensors",
                      location=Location.LOCAL, size_bytes=500)
    # Missing on remote → counts toward upload.
    assert pool.projected_remote_upload_bytes(wf_key) == 500
