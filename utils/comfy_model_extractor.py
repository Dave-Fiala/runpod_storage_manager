"""
comfy_model_extractor.py

Scans a ComfyUI workflow (.json) export and extracts every reference to a
file that lives under ComfyUI's `models/` directory tree (checkpoints,
diffusion_models/unet, text_encoders/clip, vae, loras, controlnet,
upscale_models, clip_vision, style_models, gligen, photomaker, ipadapter,
etc.), returning a list of `ModelRef` dataclass instances.

Handles:
- Top-level `nodes`
- ComfyUI "subgraph" nodes (a node whose `type` is a UUID pointing into
  `definitions.subgraphs`) by recursing into the subgraph's own `nodes`.
- Known loader node types via a registry (so we know which widget index(es)
  hold the filename).
- Unknown/custom loader nodes via a fallback heuristic that scans all
  widget values for strings ending in a known model file extension.
- Resolving on-disk path + file size, if a models directory is supplied.
"""

from __future__ import annotations

import json
import os
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
# files (images, videos, etc). Used to keep the fallback heuristic from
# misfiring on things like LoadImage.
NON_MODEL_LOADER_TYPES = {
    "LoadImage", "LoadImageMask", "LoadImageOutput", "VHS_LoadVideo",
    "VHS_LoadVideoPath", "LoadAudio",
}


@dataclass
class ModelRef:
    node_id: Any
    node_type: str
    node_title: Optional[str]
    category: str                 # checkpoint / diffusion_model / text_encoder / vae / lora / controlnet / ...
    filename: str                 # e.g. "flux-2-klein-9b-fp8.safetensors"
    subfolder: str                # guessed ComfyUI models subfolder, e.g. "diffusion_models"
    subgraph_path: str            # "" if in the main graph, else e.g. "SAMPLER"
    resolved_path: Optional[str]  # absolute path if a models_dir was supplied
    exists_on_disk: bool
    size_bytes: Optional[int]
    extra: dict = field(default_factory=dict)  # e.g. {"strength_model": 0.7}


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
                extra = {}
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
                        resolved_path=None,
                        exists_on_disk=False,
                        size_bytes=None,
                        extra=extra,
                    )
                )
        return results

    # Fallback: unknown node type. Only consider it if it smells like a
    # loader and isn't explicitly excluded, then scan widgets for filenames.
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
                        resolved_path=None,
                        exists_on_disk=False,
                        size_bytes=None,
                        extra={"detected_via": "fallback_extension_scan"},
                    )
                )
    return results


def extract_models_from_workflow(
    workflow_json_path: str,
    models_dir: Optional[str] = None,
) -> list[ModelRef]:
    """
    Parse a ComfyUI workflow JSON file and return a list of ModelRef objects
    describing every checkpoint / diffusion model / clip / vae / lora /
    controlnet / etc. referenced anywhere in the graph (including inside
    subgraphs).

    Args:
        workflow_json_path: path to the exported ComfyUI workflow .json file.
        models_dir: optional path to a local ComfyUI `models/` directory.
            If given, each ModelRef's `resolved_path`, `exists_on_disk`, and
            `size_bytes` will be filled in by looking for
            `<models_dir>/<subfolder>/<filename>`.

    Returns:
        List[ModelRef]
    """
    with open(workflow_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = data.get("nodes", [])
    subgraph_defs = {sg["id"]: sg for sg in data.get("definitions", {}).get("subgraphs", [])}

    models: list[ModelRef] = []
    seen = set()  # de-dupe identical (node_id, filename) pairs across traversal quirks

    for node, subgraph_path in _iter_all_nodes(nodes, subgraph_defs):
        for ref in _extract_from_node(node, subgraph_path):
            key = (ref.node_id, ref.subgraph_path, ref.filename)
            if key in seen:
                continue
            seen.add(key)
            models.append(ref)

    if models_dir:
        for ref in models:
            candidate = os.path.join(models_dir, ref.subfolder, ref.filename)
            ref.resolved_path = candidate
            if os.path.isfile(candidate):
                ref.exists_on_disk = True
                ref.size_bytes = os.path.getsize(candidate)
            else:
                ref.exists_on_disk = False
                ref.size_bytes = None

    return models


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "260615_MICKMUMPITZ_FLUX_KLEIN_9B_V01.json"
    models_dir_arg = sys.argv[2] if len(sys.argv) > 2 else None

    results = extract_models_from_workflow(path, models_dir=models_dir_arg)

    print(f"Found {len(results)} model reference(s):\n")
    for m in results:
        loc = f" (subgraph: {m.subgraph_path})" if m.subgraph_path else ""
        size = f"{m.size_bytes:,} bytes" if m.size_bytes is not None else "size unknown"
        print(f"- [{m.category}] {m.filename}{loc}")
        print(f"    node: {m.node_type} (id={m.node_id}, title={m.node_title!r})")
        print(f"    models/{m.subfolder}/{m.filename}")
        if m.resolved_path:
            print(f"    resolved: {m.resolved_path} | exists={m.exists_on_disk} | {size}")
        if m.extra:
            print(f"    extra: {m.extra}")
        print()
