from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from hl_advanced_orders.models import PositionSide, TrailMode, TrailingStopRule
from hl_advanced_orders.storage import JsonRuleStore, StorageError, StoredRuleState


class JsonRuleStoreTest(unittest.TestCase):
    def test_rule_and_state_round_trip_preserves_decimals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonRuleStore(Path(tmp) / "state.json")
            rule = TrailingStopRule(
                coin="eth",
                side=PositionSide.LONG,
                size=Decimal("1.0"),
                trail_mode=TrailMode.PERCENT,
                trail_value=Decimal("5"),
            )
            store.add_rule(rule)
            snapshot = store.load()
            snapshot.states[rule.id] = StoredRuleState(
                protected_size=Decimal("0.4"),
                processed_fill_ids={"fill_1"},
            )
            store.save(snapshot)

            loaded = store.load()

            self.assertEqual(loaded.rules[rule.id].coin, "ETH")
            self.assertEqual(loaded.rules[rule.id].execution_mode.value, "dry_run")
            self.assertEqual(loaded.states[rule.id].protected_size, Decimal("0.4"))
            self.assertEqual(loaded.states[rule.id].processed_fill_ids, {"fill_1"})

    def test_missing_store_returns_empty_snapshot_with_inactive_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = JsonRuleStore(Path(tmp) / "missing.json").load()

            self.assertEqual(snapshot.rules, {})
            self.assertFalse(snapshot.kill_switch_active)

    def test_malformed_json_raises_storage_error_without_deleting_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(StorageError):
                JsonRuleStore(path).load()

            self.assertTrue(path.exists())

    def test_kill_switch_persists_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            JsonRuleStore(path).set_kill_switch(True)

            self.assertTrue(JsonRuleStore(path).load().kill_switch_active)


if __name__ == "__main__":
    unittest.main()
