from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hl_advanced_orders.audit import AuditEvent, JsonlAuditLog


class AuditLogTest(unittest.TestCase):
    def test_events_are_jsonl_with_utc_timestamp_and_sorted_payload_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = JsonlAuditLog(path)

            log.append(AuditEvent.create("dry_run_triggered", "triggered", payload={"z": 1, "a": 2}))

            raw = path.read_text(encoding="utf-8").strip()
            parsed = json.loads(raw)
            self.assertEqual(parsed["event_type"], "dry_run_triggered")
            self.assertIn("+00:00", parsed["created_at"])
            self.assertLess(raw.index('"a"'), raw.index('"z"'))

    def test_audit_payload_does_not_need_private_key_material(self) -> None:
        event = AuditEvent.create(
            "live_submission_blocked",
            "blocked",
            payload={"rule_id": "rule_1", "coin": "ETH", "outcome": "blocked"},
        )

        self.assertNotIn("private_key", event.payload)


if __name__ == "__main__":
    unittest.main()
