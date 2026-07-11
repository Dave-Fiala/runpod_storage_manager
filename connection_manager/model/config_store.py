from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .connection_profile import ConnectionProfile

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(os.getenv("APPDATA", ""), "RunpodStorageTool")
_CONFIG_FILE = "config.json"
_CURRENT_VERSION = 1


@dataclass
class ConfigData:
    version: int = _CURRENT_VERSION
    geesefs_path: str = ""
    default_profile: str = ""
    profiles: list[ConnectionProfile] = field(default_factory=list)


class ConfigStore:
    def __init__(self, config_dir: str | None = None) -> None:
        self._dir = Path(config_dir or _CONFIG_DIR)
        self._path = self._dir / _CONFIG_FILE
        self._data: ConfigData | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ConfigData:
        if not self._path.exists():
            self._data = ConfigData()
            return self._data
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = self._parse(raw)
        except Exception:
            logger.exception("Corrupt config at %s — backing up and starting fresh", self._path)
            backup = self._path.with_suffix(".json.bak")
            try:
                shutil.copy2(self._path, backup)
            except OSError:
                pass
            self._data = ConfigData()
        return self._data

    def save(self, data: ConfigData | None = None) -> None:
        if data is not None:
            self._data = data
        if self._data is None:
            return

        self._dir.mkdir(parents=True, exist_ok=True)

        blob = {
            "version": self._data.version,
            "geesefs_path": self._data.geesefs_path,
            "default_profile": self._data.default_profile,
            "profiles": [p.to_dict() for p in self._data.profiles],
        }

        fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(blob, f, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- profile helpers --

    def _ensure_loaded(self) -> ConfigData:
        if self._data is None:
            self.load()
        assert self._data is not None
        return self._data

    def list_profiles(self) -> list[ConnectionProfile]:
        return list(self._ensure_loaded().profiles)

    def get_profile(self, name: str) -> ConnectionProfile | None:
        for p in self._ensure_loaded().profiles:
            if p.name == name:
                return p
        return None

    def add_profile(self, profile: ConnectionProfile) -> None:
        data = self._ensure_loaded()
        if any(p.name == profile.name for p in data.profiles):
            raise ValueError(f"Profile '{profile.name}' already exists")
        data.profiles.append(profile)
        self.save()

    def update_profile(self, profile: ConnectionProfile) -> None:
        data = self._ensure_loaded()
        for i, p in enumerate(data.profiles):
            if p.name == profile.name:
                data.profiles[i] = profile
                self.save()
                return
        raise KeyError(f"Profile '{profile.name}' not found")

    def remove_profile(self, name: str) -> None:
        data = self._ensure_loaded()
        data.profiles = [p for p in data.profiles if p.name != name]
        if data.default_profile == name:
            data.default_profile = ""
        self.save()

    def get_geesefs_path(self) -> str:
        return self._ensure_loaded().geesefs_path

    def set_geesefs_path(self, path: str) -> None:
        self._ensure_loaded().geesefs_path = path
        self.save()

    # -- internal --

    @staticmethod
    def _parse(raw: dict) -> ConfigData:
        profiles: list[ConnectionProfile] = []
        for p in raw.get("profiles", []):
            if isinstance(p, dict):
                p.pop("secret_access_key", None)
                try:
                    profiles.append(ConnectionProfile.from_dict(p))
                except Exception:
                    logger.warning("Skipping malformed profile entry: %s", p)
        return ConfigData(
            version=raw.get("version", _CURRENT_VERSION),
            geesefs_path=raw.get("geesefs_path", ""),
            default_profile=raw.get("default_profile", ""),
            profiles=profiles,
        )
