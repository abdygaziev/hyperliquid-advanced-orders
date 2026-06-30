from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE_PAYLOAD_KEY_PARTS = ("private_key", "secret", "seed", "mnemonic")


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    rule_id: str | None
    message: str
    payload: dict[str, Any]
    created_at: str

    @classmethod
    def create(
        cls,
        event_type: str,
        message: str,
        *,
        rule_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> "AuditEvent":
        return cls(
            event_type=event_type,
            rule_id=rule_id,
            message=message,
            payload=redact_payload(payload or {}),
            created_at=datetime.now(timezone.utc).isoformat(),
        )


class JsonlAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: AuditEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), default=str, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in SENSITIVE_PAYLOAD_KEY_PARTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value
