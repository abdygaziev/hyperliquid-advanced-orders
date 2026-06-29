from __future__ import annotations

import keyring


SERVICE_NAME = "hyperliquid-advanced-orders"


class KeychainSecrets:
    def set_private_key(self, account: str, private_key: str) -> None:
        keyring.set_password(SERVICE_NAME, account, private_key)

    def get_private_key(self, account: str) -> str | None:
        return keyring.get_password(SERVICE_NAME, account)

    def has_private_key(self, account: str) -> bool:
        return self.get_private_key(account) is not None
