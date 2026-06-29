from __future__ import annotations

from typing import Protocol


SERVICE_NAME = "hyperliquid-advanced-orders"


class SecretStore(Protocol):
    def set_private_key(self, account: str, private_key: str) -> None:
        pass

    def get_private_key(self, account: str) -> str | None:
        pass

    def has_private_key(self, account: str) -> bool:
        pass


class KeychainSecrets:
    def set_private_key(self, account: str, private_key: str) -> None:
        import keyring

        keyring.set_password(SERVICE_NAME, account, private_key)

    def get_private_key(self, account: str) -> str | None:
        import keyring

        return keyring.get_password(SERVICE_NAME, account)

    def has_private_key(self, account: str) -> bool:
        return self.get_private_key(account) is not None


class InMemorySecrets:
    def __init__(self) -> None:
        self._private_keys: dict[str, str] = {}

    def set_private_key(self, account: str, private_key: str) -> None:
        self._private_keys[account] = private_key

    def get_private_key(self, account: str) -> str | None:
        return self._private_keys.get(account)

    def has_private_key(self, account: str) -> bool:
        return account in self._private_keys
