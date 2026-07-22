"""
workflow_scanner.py

Refactor of the ``comfy_model_extractor.py`` prototype into a pure-parse
service. ``WorkflowScanner`` takes already-parsed workflow JSON (a ``dict``) and
returns ``ModelRef`` parse results. It performs **no filesystem access** — all
location/size resolution is the responsibility of ``WorkspacePool`` (see
``model/workspace_pool.py``). Decoupling from IO lets the same code parse both
local files and remote-only workflows fetched via the S3 API.

The node registry, fallback heuristic, subgraph recursion, and de-dupe logic
are carried over verbatim from the prototype.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


MODEL_FILE_EXTENSIONS = (
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin",
    ".gguf", ".onnx", ".sft", ".pb",
)

# node_type -> (category, default_subfolder, [indices into widgets_values that are filenames])
# subfolder is relative to the ComfyUI `models/` directory.
MODEL_NODE_REGISTRY: dict[str, dict[str, Any]] = {
    "CheckpointLoaderSimple":   {"category": "checkpoint",    "subfolder": "checkpoints",     "filename_widgets": [0]},
    "CheckpointLoader":         {"category": "checkpoint",    "subfolder": "checkpoints",     "filename_widgets": [0]},
    "unCLIPCheckpointLoader":   {"category": "checkpoint",    "subfolder": "checkpoints",     "filename_widgets": [0]},
    "ImageOnlyCheckpointLoader":{"category": "checkpoint",    "subfolder": "checkpoints",     "filename_widgets": [0]},
    "DiffusersLoader":          {"category": "diffusion_model","subfolder": "diffusers",       "filename_widgets": [0]},
    "UNETLoader":               {"category": "diffusion_model","subfolder": "diffusion_models","filename_widgets": [0]},
    "UnetLoaderGGUF":           {"category": "diffusion_model","subfolder": "diffusion_models","filename_widgets": [0]},
    "CLIPLoader":               {"category": "text_encoder",  "subfolder": "text_encoders",    "filename_widgets": [0]},
    "CLIPLoaderGGUF":           {"category": "text_encoder",  "subfolder": "text_encoders",    "filename_widgets": [0]},
    "DualCLIPLoader":           {"category": "text_encoder",  "subfolder": "text_encoders",    "filename_widgets": [0, 1]},
    "TripleCLIPLoader":         {"category": "text_encoder",  "subfolder": "text_encoders",    "filename_widgets": [0, 1, 2]},
    "QuadrupleCLIPLoader":      {"category": "text_encoder",  "subfolder": "text_encoders",    "filename_widgets": [0, 1, 2, 3]},
    "VAELoader":                {"category": "vae",            "subfolder": "vae",              "filename_widgets": [0]},
    "LoraLoader":               {"category": "lora",           "subfolder": "loras",            "filename_widgets": [0]},
    "LoraLoaderModelOnly":      {"category": "lora",           "subfolder": "loras",            "filename_widgets": [0]},
    "LoraLoaderGGUF":           {"category": "lora",           "subfolder": "loras",            "filename_widgets": [0]},
    "ControlNetLoader":         {"category": "controlnet",     "subfolder": "controlnet",       "filename_widgets": [0]},
    "DiffControlNetLoader":     {"category": "controlnet",     "subfolder": "controlnet",       "filename_widgets": [0]},
    "UpscaleModelLoader":       {"category": "upscale_model",  "subfolder": "upscale_models",   "filename_widgets": [0]},
    "CLIPVisionLoader":         {"category": "clip_vision",    "subfolder": "clip_vision",      "filename_widgets": [0]},
    "StyleModelLoader":         {"category": "style_model",    "subfolder": "style_models",     "filename_widgets": [0]},
    "GLIGENLoader":             {"category": "gligen",         "subfolder": "gligen",           "filename_widgets": [0]},
    "PhotoMakerLoader":         {"category": "photomaker",     "subfolder": "photomaker",       "filename_widgets": [0]},
    "IPAdapterModelLoader":     {"category": "ipadapter",      "subfolder": "ipadapter",        "filename_widgets": [0]},
}

# Node types that legitimately load "*Loader"-style files but are NOT model
# files (images, videos, etc). Keeps the fallback heuristic from misfiring.
NON_MODEL_LOADER_TYPES = {
    "LoadImage", "LoadImageMask", "LoadImageOutput", "VHS_LoadVideo",
    "VHS_LoadVideoPath", "LoadAudio",
}


class WorkflowParseError(Exception):
    """Raised when a workflow JSON is malformed or missing required structure."""


@dataclass
class ModelRef:
    """A pure parse result: no location/size fields — the pool resolves those."""

    node_id: Any
    node_type: str
    node_title: Optional[str]
    category: str
    filename: str
    subfolder: str
    subgraph_path: str = ""
    extra: dict = field(default_factory=dict)


def _iter_all_nodes(nodes: list[dict], subgraph_defs: dict[str, dict], path: str = ""):
    """Yield (node, subgraph_path) for every node in the main graph and,
    recursively, every subgraph referenced by a node whose `type` is a
    subgraph UUID."""
    for node in nodes:
        yield node, path
        ntype = node.get("type")
        sg = subgraph_defs.get(ntype)
        if sg:
            new_path = f"{path}/{sg.get('name', ntype)}" if path else sg.get("name", ntype)
            yield from _iter_all_nodes(sg.get("nodes", []), subgraph_defs, new_path)


def _looks_like_model_file(value: Any) -> bool:
    return isinstance(value, str) and value.lower().endswith(MODEL_FILE_EXTENSIONS)


def _guess_category_and_subfolder(node_type: str, title: Optional[str]) -> tuple[str, str]:
    """Best-effort guess for node types not in the registry, based on
    keywords in the node type / title."""
    haystack = f"{node_type} {title or ''}".lower()
    if "checkpoint" in haystack:
        return "checkpoint", "checkpoints"
    if "controlnet" in haystack:
        return "controlnet", "controlnet"
    if "vae" in haystack:
        return "vae", "vae"
    if "lora" in haystack:
        return "lora", "loras"
    if "clipvision" in haystack or "clip_vision" in haystack:
        return "clip_vision", "clip_vision"
    if "clip" in haystack:
        return "text_encoder", "text_encoders"
    if "unet" in haystack or "diffusion" in haystack:
        return "diffusion_model", "diffusion_models"
    if "upscale" in haystack:
        return "upscale_model", "upscale_models"
    if "ipadapter" in haystack or "ip_adapter" in haystack:
        return "ipadapter", "ipadapter"
    if "styles" in haystack or "style_model" in haystack or "stylemodel" in haystack:
        return "style_model", "style_models"
    if "gligen" in haystack:
        return "gligen", "gligen"
    return "unknown", "unknown"


def _extract_from_node(node: dict, subgraph_path: str) -> list[ModelRef]:
    node_type = node.get("type", "")
    node_id = node.get("id")
    title = node.get("title")
    widgets = node.get("widgets_values") or []

    results: list[ModelRef] = []

    registry_entry = MODEL_NODE_REGISTRY.get(node_type)
    if registry_entry:
        category = registry_entry["category"]
        subfolder = registry_entry["subfolder"]
        for idx in registry_entry["filename_widgets"]:
            if idx < len(widgets) and isinstance(widgets[idx], str) and widgets[idx]:
                extra: dict = {}
                # Grab common trailing numeric widgets (e.g. LoRA strength) as metadata.
                trailing = widgets[len(registry_entry["filename_widgets"]):]
                if node_type in ("LoraLoader", "LoraLoaderModelOnly", "LoraLoaderGGUF") and trailing:
                    extra["strength_model"] = trailing[0] if len(trailing) > 0 else None
                    if node_type == "LoraLoader" and len(trailing) > 1:
                        extra["strength_clip"] = trailing[1]
                results.append(
                    ModelRef(
                        node_id=node_id,
                        node_type=node_type,
                        node_title=title,
                        category=category,
                        filename=widgets[idx],
                        subfolder=subfolder,
                        subgraph_path=subgraph_path,
                        extra=extra,
                    )
                )
        return results

    # Fallback: unknown node type. Only consider it if it smells like a loader
    # and isn't explicitly excluded, then scan widgets for filenames.
    if node_type in NON_MODEL_LOADER_TYPES:
        return results

    if "loader" in node_type.lower() or "load" in node_type.lower():
        category, subfolder = _guess_category_and_subfolder(node_type, title)
        for val in widgets:
            if _looks_like_model_file(val):
                results.append(
                    ModelRef(
                        node_id=node_id,
                        node_type=node_type,
                        node_title=title,
                        category=category,
                        filename=val,
                        subfolder=subfolder,
                        subgraph_path=subgraph_path,
                        extra={"detected_via": "fallback_extension_scan"},
                    )
                )
    return results


class WorkflowScanner:
    """Parses ComfyUI workflow JSON into a list of ``ModelRef`` parse results.

    Stateless and filesystem-free: pass parsed JSON to :meth:`extract`, or use
    the :meth:`extract_from_file` convenience for local files.
    """

    def extract(self, workflow_json: dict) -> list[ModelRef]:
        """Return every model reference found in the graph (incl. subgraphs).

        Raises:
            WorkflowParseError: if the JSON lacks the expected ``nodes`` array.
        """
        if not isinstance(workflow_json, dict):
            raise WorkflowParseError("Workflow JSON root is not an object.")
        if "nodes" not in workflow_json:
            raise WorkflowParseError("Workflow JSON has no 'nodes' array.")

        nodes = workflow_json.get("nodes") or []
        subgraph_defs = {
            sg["id"]: sg
            for sg in workflow_json.get("definitions", {}).get("subgraphs", [])
            if isinstance(sg, dict) and "id" in sg
        }

        models: list[ModelRef] = []
        seen: set[tuple] = set()  # de-dupe (node_id, subgraph_path, filename)

        for node, subgraph_path in _iter_all_nodes(nodes, subgraph_defs):
            for ref in _extract_from_node(node, subgraph_path):
                dedupe_key = (ref.node_id, ref.subgraph_path, ref.filename)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                models.append(ref)

        return models

    def extract_from_file(self, workflow_json_path: str) -> list[ModelRef]:
        """Read a local workflow JSON file and extract its model references."""
        try:
            with open(workflow_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise WorkflowParseError(f"Invalid JSON: {exc}") from exc
        except OSError as exc:
            raise WorkflowParseError(f"Could not read workflow file: {exc}") from exc
        return self.extract(data)


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "260615_MICKMUMPITZ_FLUX_KLEIN_9B_V01.json"
    scanner = WorkflowScanner()
    refs = scanner.extract_from_file(path)

    print(f"Found {len(refs)} model reference(s):\n")
    for m in refs:
        loc = f" (subgraph: {m.subgraph_path})" if m.subgraph_path else ""
        print(f"- [{m.category}] {m.filename}{loc}")
        print(f"    node: {m.node_type} (id={m.node_id}, title={m.node_title!r})")
        print(f"    models/{m.subfolder}/{m.filename}")
        if m.extra:
            print(f"    extra: {m.extra}")
        print()
