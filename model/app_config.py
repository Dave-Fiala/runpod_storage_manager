"""
app_config.py

Persisted paths and UI preferences for the storage manager, following the
Connection Manager's ``ConfigStore`` pattern (atomic JSON write under the user
config dir). Credentials are NEVER stored here — they remain the Connection
Manager's ``CredentialsStore`` responsibility.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(os.getenv("APPDATA", ""), "comfyui-runpod-storage-manager")
_CONFIG_FILE = "config.json"


@dataclass
class AppConfigData:
    local_model_dir: str = ""
    local_workflow_dir: str = ""
    remote_model_prefix: str = ""
    remote_workflow_prefix: str = ""
    last_active_workflow: str = ""
    window_geometry: str = ""  # base64-encoded QByteArray


class AppConfig:
    def __init__(self, config_dir: str | None = None) -> None:
        self._dir = Path(config_dir or _CONFIG_DIR)
        self._path = self._dir / _CONFIG_FILE
        self._data: AppConfigData | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data(self) -> AppConfigData:
        if self._data is None:
            self.load()
        assert self._data is not None
        return self._data

    def load(self) -> AppConfigData:
        if not self._path.exists():
            self._data = AppConfigData()
            return self._data
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            known = {f for f in AppConfigData.__dataclass_fields__}
            self._data = AppConfigData(**{k: v for k, v in raw.items() if k in known})
        except Exception:
            logger.exception("Corrupt app config at %s — starting fresh", self._path)
            self._data = AppConfigData()
        return self._data

    def save(self) -> None:
        if self._data is None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(self._data), f, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, **fields) -> None:
        data = self.data
        for k, v in fields.items():
            if hasattr(data, k):
                setattr(data, k, v)
        self.save()
