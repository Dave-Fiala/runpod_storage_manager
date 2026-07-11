from __future__ import annotations

import keyring

_SERVICE = "RunpodStorageTool"


class CredentialsStore:
    def __init__(self, service: str = _SERVICE) -> None:
        self._service = service

    def save_secret(self, profile_name: str, secret: str) -> None:
        keyring.set_password(self._service, profile_name, secret)

    def load_secret(self, profile_name: str) -> str | None:
        return keyring.get_password(self._service, profile_name)

    def delete_secret(self, profile_name: str) -> None:
        try:
            keyring.delete_password(self._service, profile_name)
        except keyring.errors.PasswordDeleteError:
            pass
