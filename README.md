# ComfyUI RunPod Storage Manager

A visual tool to manage RunPod storage volumes for ComfyUI. It performs
**workflow-driven, on-demand synchronisation** of the `models/` directories of a
local ComfyUI install and a remote ComfyUI install backed by a RunPod network
volume (S3-compatible).

Pick a workflow `.json`, and the tool works out exactly which model files it
needs and copies **only those** (plus the workflow itself) to the remote volume
via the S3 API. Removing a workflow reclaims its remote storage, while protecting
models still referenced by other remote workflows.

## Architecture

The app follows the same MVC discipline as the bundled Connection Manager:

- `model/` — domain layer: `entities.py`, `workspace_pool.py` (the unified,
  single-source-of-truth pool), `workflow_scanner.py` (pure workflow parser),
  `app_config.py`, `log_model.py`.
- `services/` — `local_storage.py`, `remote_storage.py` (boto3 / RunPod S3),
  `sync_engine.py` (plan + execute), `usage_calculator.py`.
- `controller/sync_controller.py` — turns view intents into pool operations,
  marshals work onto the worker thread, and shapes immutable view-models.
- `workers/job_runner.py` — single background worker thread (FIFO), so the pool
  has one writer and no locking is needed.
- `view/` — `main_window.py` (wraps the generated `model_sync_tool.py`),
  `log_viewer.py`, `model_info_dialog.py`.
- `connection_manager/` — existing package, consumed as-is for mounting and S3
  credentials.
- `viewmodels.py` — the immutable contract between controller and view.

All remote reads/writes go through the S3 API (boto3), never the mounted drive
(the mount is a read-only convenience for "reveal in Explorer").

## Running from source

```powershell
python -m venv venv
venv\Scripts\pip install -e ".[dev]"
venv\Scripts\python main.py
```

## Tests

```powershell
venv\Scripts\python -m pytest
```

Unit tests cover the workflow scanner and pool invariants (no Qt or network);
`moto` mocks S3 for the remote-storage integration tests; `pytest-qt` covers the
controller wiring.

## Packaging

```powershell
venv\Scripts\pip install pyinstaller
venv\Scripts\pyinstaller comfyui_runpod_storage_manager.spec
```

The windowed executable is written to `dist/`.
