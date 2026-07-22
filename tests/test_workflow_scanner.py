"""Unit tests for WorkflowScanner (no Qt, no filesystem required)."""
from __future__ import annotations

import pytest

from model.workflow_scanner import WorkflowScanner, WorkflowParseError


@pytest.fixture
def scanner() -> WorkflowScanner:
    return WorkflowScanner()


def _wf(nodes, definitions=None):
    doc = {"nodes": nodes}
    if definitions is not None:
        doc["definitions"] = definitions
    return doc


def test_registry_checkpoint_loader(scanner):
    refs = scanner.extract(
        _wf([{"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["sd_xl_base.safetensors"]}])
    )
    assert len(refs) == 1
    assert refs[0].category == "checkpoint"
    assert refs[0].subfolder == "checkpoints"
    assert refs[0].filename == "sd_xl_base.safetensors"


def test_gguf_unet_loader(scanner):
    refs = scanner.extract(
        _wf([{"id": 2, "type": "UnetLoaderGGUF", "widgets_values": ["flux1-dev-Q4.gguf"]}])
    )
    assert refs[0].category == "diffusion_model"
    assert refs[0].subfolder == "diffusion_models"


def test_dual_clip_loader_multi_widget(scanner):
    refs = scanner.extract(
        _wf([{"id": 3, "type": "DualCLIPLoader",
              "widgets_values": ["clip_l.safetensors", "t5xxl.safetensors", "flux"]}])
    )
    assert {r.filename for r in refs} == {"clip_l.safetensors", "t5xxl.safetensors"}
    assert all(r.subfolder == "text_encoders" for r in refs)


def test_lora_strength_captured_in_extra(scanner):
    refs = scanner.extract(
        _wf([{"id": 4, "type": "LoraLoader",
              "widgets_values": ["style.safetensors", 0.7, 0.9]}])
    )
    assert refs[0].extra["strength_model"] == 0.7
    assert refs[0].extra["strength_clip"] == 0.9


def test_subgraph_recursion(scanner):
    sub_id = "uuid-sampler"
    doc = _wf(
        nodes=[{"id": 10, "type": sub_id, "widgets_values": []}],
        definitions={
            "subgraphs": [
                {
                    "id": sub_id,
                    "name": "SAMPLER",
                    "nodes": [
                        {"id": 11, "type": "VAELoader", "widgets_values": ["ae.safetensors"]}
                    ],
                }
            ]
        },
    )
    refs = scanner.extract(doc)
    assert len(refs) == 1
    assert refs[0].filename == "ae.safetensors"
    assert refs[0].subgraph_path == "SAMPLER"


def test_fallback_heuristic_custom_loader(scanner):
    refs = scanner.extract(
        _wf([{"id": 5, "type": "MyCustomControlNetLoader",
              "widgets_values": ["cn_depth.safetensors"]}])
    )
    assert len(refs) == 1
    assert refs[0].category == "controlnet"
    assert refs[0].extra.get("detected_via") == "fallback_extension_scan"


def test_non_model_loader_excluded(scanner):
    refs = scanner.extract(
        _wf([{"id": 6, "type": "LoadImage", "widgets_values": ["photo.png"]}])
    )
    assert refs == []


def test_dedupe_identical_refs(scanner):
    node = {"id": 7, "type": "CheckpointLoaderSimple", "widgets_values": ["a.safetensors"]}
    refs = scanner.extract(_wf([node, dict(node)]))
    # Same node id + filename → de-duped to one.
    assert len(refs) == 1


def test_malformed_json_missing_nodes(scanner):
    with pytest.raises(WorkflowParseError):
        scanner.extract({"not_nodes": []})


def test_non_dict_root(scanner):
    with pytest.raises(WorkflowParseError):
        scanner.extract([])  # type: ignore[arg-type]
