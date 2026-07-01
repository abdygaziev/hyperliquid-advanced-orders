from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from hl_advanced_orders.audit import AuditEvent, JsonlAuditLog
from hl_advanced_orders.hyperliquid_client import PositionSnapshot
from hl_advanced_orders.models import (
    ExecutionMode,
    LiveEnablementStatus,
    PositionSide,
    TrailMode,
    TrailingStopRule,
)
from hl_advanced_orders.recovery import (
    diagnostics_payload,
    manual_review_rule_ids,
    reset_triggered_rule,
    validate_state,
)
from hl_advanced_orders.storage import LocalStateStore


class FakeAccount:
    def __init__(self, positions: list[PositionSnapshot]) -> None:
        self.positions = positions

    def get_positions(self) -> list[PositionSnapshot]:
        return self.positions


class RecoveryTest(unittest.TestCase):
    def test_validate_state_reports_malformed_file_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text("{not-json", encoding="utf-8")

            valid, message = validate_state(LocalStateStore(path))

            self.assertFalse(valid)
            self.assertIn("failed to load local state", message)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")

    def test_manual_review_rule_ids_returns_active_manual_review_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = auto_rule(live_status=LiveEnablementStatus.MANUAL_REVIEW)
            state.ensure_rule_state(rule)
            store.save(state)

            self.assertEqual(manual_review_rule_ids(store), [rule.id])

    def test_reset_triggered_rule_reconciles_position_and_audits_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            audit_path = Path(temp_dir) / "audit.jsonl"
            state = store.load()
            rule = auto_rule(live_status=LiveEnablementStatus.MANUAL_REVIEW)
            runtime = state.ensure_rule_state(rule)
            runtime.triggered = True
            runtime.protected_size = Decimal("1")
            store.save(state)

            reset_triggered_rule(
                store=store,
                audit=JsonlAuditLog(audit_path),
                account=FakeAccount([PositionSnapshot("ETH", PositionSide.LONG, Decimal("0.4"))]),
                rule_id=rule.id,
                reason="operator reviewed exchange fill",
            )

            loaded = store.load()
            events = read_events(audit_path)
            self.assertFalse(loaded.rule_states[rule.id].triggered)
            self.assertEqual(loaded.rule_states[rule.id].protected_size, Decimal("0.4"))
            self.assertEqual(loaded.rules[rule.id].live_status, LiveEnablementStatus.CANARY_PENDING)
            self.assertEqual(events[-1]["event_type"], "rule_trigger_reset")
            self.assertEqual(events[-1]["payload"]["reason"], "operator reviewed exchange fill")

    def test_reset_without_matching_position_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = auto_rule(live_status=LiveEnablementStatus.MANUAL_REVIEW)
            state.ensure_rule_state(rule).triggered = True
            store.save(state)

            with self.assertRaisesRegex(ValueError, "matching current account position"):
                reset_triggered_rule(
                    store=store,
                    audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                    account=FakeAccount([]),
                    rule_id=rule.id,
                    reason="operator reviewed exchange fill",
                )

    def test_diagnostics_payload_redacts_sensitive_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = auto_rule(live_status=LiveEnablementStatus.MANUAL_REVIEW)
            state.ensure_rule_state(rule).triggered = True
            state.health.mode = "running"
            store.save(state)
            audit_path = Path(temp_dir) / "audit.jsonl"
            JsonlAuditLog(audit_path).append(
                AuditEvent.create(
                    "debug",
                    "Debug event.",
                    payload={"nested": {"private_key": "super-secret"}},
                )
            )

            payload = diagnostics_payload(store, audit_path)

            self.assertEqual(payload["health"]["mode"], "running")
            self.assertEqual(payload["rules"][0]["live_status"], "manual_review")
            self.assertEqual(
                payload["recent_events"][0]["payload"]["nested"]["private_key"],
                "[REDACTED]",
            )


def auto_rule(live_status: LiveEnablementStatus) -> TrailingStopRule:
    return TrailingStopRule(
        id="rule_123",
        coin="ETH",
        side=PositionSide.LONG,
        size=Decimal("1"),
        trail_mode=TrailMode.ABSOLUTE,
        trail_value=Decimal("50"),
        execution_mode=ExecutionMode.AUTO_SUBMIT,
        live_status=live_status,
    )


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
