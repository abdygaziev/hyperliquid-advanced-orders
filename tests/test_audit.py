from __future__ import annotations

import json
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from hl_advanced_orders.audit import AuditEvent, JsonlAuditLog


class AuditLogTest(unittest.TestCase):
    def test_events_are_jsonl_with_utc_timestamps_and_sorted_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "audit.jsonl"
            event = AuditEvent.create(
                "rule_created",
                "Created rule.",
                rule_id="rule_123",
                payload={"z": "last", "a": "first"},
            )

            JsonlAuditLog(path).append(event)

            line = path.read_text(encoding="utf-8").splitlines()[0]
            decoded = json.loads(line)
            self.assertLess(line.index('"created_at"'), line.index('"event_type"'))
            created_at = datetime.fromisoformat(decoded["created_at"])
            self.assertIsNotNone(created_at.tzinfo)
            self.assertIn("+00:00", decoded["created_at"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_audit_payload_recursively_redacts_private_key_material(self) -> None:
        event = AuditEvent.create(
            "live_submission_failed",
            "Failed.",
            payload={
                "private_key": "super-secret",
                "nested": {"api_secret": "also-secret"},
                "safe": "visible",
            },
        )

        self.assertEqual(event.payload["private_key"], "[REDACTED]")
        self.assertEqual(event.payload["nested"]["api_secret"], "[REDACTED]")
        self.assertEqual(event.payload["safe"], "visible")


if __name__ == "__main__":
    unittest.main()
