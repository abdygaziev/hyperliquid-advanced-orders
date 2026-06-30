from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
            payload=payload or {},
            created_at=datetime.now(timezone.utc).isoformat(),
        )


class JsonlAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: AuditEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), default=str, sort_keys=True))
            handle.write("\n")

    def events(self) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        events: list[AuditEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                events.append(
                    AuditEvent(
                        event_type=raw["event_type"],
                        rule_id=raw.get("rule_id"),
                        message=raw["message"],
                        payload=raw.get("payload", {}),
                        created_at=raw["created_at"],
                    )
                )
        return events

    def count_rule_events(self, rule_id: str, event_type: str) -> int:
        return sum(
            1
            for event in self.events()
            if event.rule_id == rule_id and event.event_type == event_type
        )
