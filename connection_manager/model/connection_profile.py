from __future__ import annotations

from dataclasses import dataclass, asdict, field
from urllib.parse import urlparse


DATACENTER_ENDPOINTS: dict[str, str] = {
    "EU-CZ-1": "https://s3api-eu-cz-1.runpod.io/",
    "EU-RO-1": "https://s3api-eu-ro-1.runpod.io/",
    "EUR-IS-1": "https://s3api-eur-is-1.runpod.io/",
    "EUR-NO-1": "https://s3api-eur-no-1.runpod.io/",
    "US-CA-2": "https://s3api-us-ca-2.runpod.io/",
    "US-GA-2": "https://s3api-us-ga-2.runpod.io/",
    "US-IL-1": "https://s3api-us-il-1.runpod.io/",
    "US-KS-2": "https://s3api-us-ks-2.runpod.io/",
    "US-MD-1": "https://s3api-us-md-1.runpod.io/",
    "US-MO-1": "https://s3api-us-mo-1.runpod.io/",
    "US-MO-2": "https://s3api-us-mo-2.runpod.io/",
    "US-NC-1": "https://s3api-us-nc-1.runpod.io/",
    "US-NC-2": "https://s3api-us-nc-2.runpod.io/",
    "US-NE-1": "https://s3api-us-ne-1.runpod.io/",
    "US-WA-1": "https://s3api-us-wa-1.runpod.io/",
}


def endpoint_for(datacenter: str) -> str:
    return f"https://s3api-{datacenter.lower()}.runpod.io/"


def datacenter_for(endpoint: str) -> str:
    host = endpoint.split("//", 1)[-1].split("/", 1)[0]
    return host.removeprefix("s3api-").removesuffix(".runpod.io").upper()


@dataclass
class ConnectionProfile:
    name: str
    endpoint: str
    region: str
    volume_id: str
    access_key_id: str
    drive_letter: str
    remote_volume_size: int = 0  # provisioned volume capacity, in gigabytes
    file_mode: str = "0666"
    dir_mode: str = "0777"
    auto_mount: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ConnectionProfile:
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in known_fields}
        if "remote_volume_size" in kwargs:
            try:
                kwargs["remote_volume_size"] = int(kwargs["remote_volume_size"])
            except (TypeError, ValueError):
                kwargs["remote_volume_size"] = 0
        return cls(**kwargs)

    def validate(self) -> list[str]:
        """Return a list of validation error strings (empty means valid)."""
        errors: list[str] = []
        if not self.name.strip():
            errors.append("Profile name is required.")
        if not self.volume_id.strip():
            errors.append("Network Volume ID is required.")
        if not self.access_key_id.strip():
            errors.append("Access Key ID is required.")
        elif not self.access_key_id.startswith("user_"):
            errors.append(
                "Access Key ID should start with 'user_'. "
                "You may have pasted a regular RunPod API key by mistake."
            )
        parsed = urlparse(self.endpoint)
        if not parsed.scheme or not parsed.netloc:
            errors.append("Endpoint must be a valid URL (e.g. https://s3api-eu-ro-1.runpod.io/).")
        if not self.drive_letter or len(self.drive_letter) != 1 or not self.drive_letter.isalpha():
            errors.append("Drive letter must be a single letter (A-Z).")
        if not isinstance(self.remote_volume_size, int) or self.remote_volume_size <= 0:
            errors.append("Remote Volume Size (GB) must be a positive whole number.")
        return errors
