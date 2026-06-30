from __future__ import annotations

import keyring
from typing import Protocol


SERVICE_NAME = "hyperliquid-advanced-orders"


class SecretStore(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None:
        pass

    def get_password(self, service_name: str, username: str) -> str | None:
        pass


class KeychainSecrets:
    def __init__(self, backend: SecretStore = keyring) -> None:
        self.backend = backend

    def set_private_key(self, account: str, private_key: str) -> None:
        self.backend.set_password(SERVICE_NAME, account, private_key)

    def get_private_key(self, account: str) -> str | None:
        return self.backend.get_password(SERVICE_NAME, account)

    def has_private_key(self, account: str) -> bool:
        return self.get_private_key(account) is not None
