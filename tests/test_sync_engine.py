"""Table-driven tests for SyncEngine plan logic (skip + shared-model protection)."""
from __future__ import annotations

from model.entities import Location
from model.workflow_scanner import ModelRef
from model.workspace_pool import WorkspacePool
from services.sync_engine import SyncEngine


def _ref(filename, subfolder="checkpoints", category="checkpoint", node_id=1):
    return ModelRef(node_id=node_id, node_type="CheckpointLoaderSimple", node_title=None,
                    category=category, filename=filename, subfolder=subfolder)


def _make_pool_with_workflow(qapp):
    pool = WorkspacePool()
    pool.upsert_workflow(filename="wf.json", location=Location.LOCAL,
                         local_path="D:/wf/wf.json")
    wf_key = next(iter(pool.workflows))
    return pool, wf_key


def test_plan_sync_uploads_missing_and_skips_present(qapp):
    pool, wf_key = _make_pool_with_workflow(qapp)
    pool.apply_parse_result(wf_key, [_ref("a.safetensors"), _ref("b.safetensors", node_id=2)])
    # a is local-only (needs upload); b is on both with matching size (skip).
    pool.upsert_model(subfolder="checkpoints", filename="a.safetensors",
                      location=Location.LOCAL, local_path="D:/m/checkpoints/a.safetensors",
                      size_bytes=1000)
    pool.upsert_model(subfolder="checkpoints", filename="b.safetensors",
                      location=Location.LOCAL, local_path="D:/m/checkpoints/b.safetensors",
                      size_bytes=2000)
    pool.upsert_model(subfolder="checkpoints", filename="b.safetensors",
                      location=Location.REMOTE, remote_key="models/checkpoints/b.safetensors",
                      size_bytes=2000)

    engine = SyncEngine(pool)
    plan = engine.plan_sync(wf_key, "models/", "workflows/")

    upload_names = {i.display_name for i in plan.uploads}
    skip_names = {i.display_name for i in plan.skips}
    assert "a.safetensors" in upload_names
    assert "wf.json" in upload_names  # workflow json not on remote yet
    assert "b.safetensors" in skip_names
    assert plan.total_bytes == 1000  # only 'a' contributes (wf json size 0)


def test_plan_sync_skips_when_remote_size_matches(qapp):
    pool, wf_key = _make_pool_with_workflow(qapp)
    pool.apply_parse_result(wf_key, [_ref("c.safetensors")])
    pool.upsert_model(subfolder="checkpoints", filename="c.safetensors",
                      location=Location.LOCAL, local_path="D:/m/checkpoints/c.safetensors",
                      size_bytes=500)
    pool.upsert_model(subfolder="checkpoints", filename="c.safetensors",
                      location=Location.REMOTE, remote_key="models/checkpoints/c.safetensors",
                      size_bytes=999)  # size mismatch -> re-upload
    engine = SyncEngine(pool)
    plan = engine.plan_sync(wf_key, "models/", "workflows/")
    assert any(i.display_name == "c.safetensors" for i in plan.uploads)


def test_plan_remove_protects_shared_models(qapp):
    pool = WorkspacePool()
    pool.upsert_workflow(filename="wf1.json", location=Location.REMOTE, remote_key="workflows/wf1.json")
    pool.upsert_workflow(filename="wf2.json", location=Location.REMOTE, remote_key="workflows/wf2.json")
    wf1 = next(k for k in pool.workflows if k.startswith("wf1"))
    wf2 = next(k for k in pool.workflows if k.startswith("wf2"))
    pool.apply_parse_result(wf1, [_ref("shared.safetensors", node_id=1),
                                  _ref("lone.safetensors", subfolder="vae", category="vae", node_id=2)])
    pool.apply_parse_result(wf2, [_ref("shared.safetensors", node_id=1)])
    # Everything exists remotely.
    for fn, sub in [("shared.safetensors", "checkpoints"), ("lone.safetensors", "vae")]:
        pool.upsert_model(subfolder=sub, filename=fn, location=Location.REMOTE,
                          remote_key=f"models/{sub}/{fn}", size_bytes=100)

    engine = SyncEngine(pool)
    plan = engine.plan_remove(wf1)

    delete_names = {i.display_name for i in plan.delete}
    retained_names = {i.display_name for i in plan.retained_shared}
    assert "lone.safetensors" in delete_names
    assert "wf1.json" in delete_names  # the workflow json itself
    assert "shared.safetensors" in retained_names  # protected — wf2 still needs it
    assert plan.reclaimed_bytes == 100  # only 'lone'


def test_plan_remove_deletes_all_when_not_shared(qapp):
    pool = WorkspacePool()
    pool.upsert_workflow(filename="solo.json", location=Location.REMOTE, remote_key="workflows/solo.json")
    key = next(iter(pool.workflows))
    pool.apply_parse_result(key, [_ref("x.safetensors")])
    pool.upsert_model(subfolder="checkpoints", filename="x.safetensors",
                      location=Location.REMOTE, remote_key="models/checkpoints/x.safetensors",
                      size_bytes=42)
    engine = SyncEngine(pool)
    plan = engine.plan_remove(key)
    assert {i.display_name for i in plan.delete} == {"x.safetensors", "solo.json"}
    assert not plan.retained_shared
    assert plan.reclaimed_bytes == 42
