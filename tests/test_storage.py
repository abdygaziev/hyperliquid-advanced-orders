from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from hl_advanced_orders.models import (
    ExecutionMode,
    PositionSide,
    TrailMode,
    TrailingStopRule,
)
from hl_advanced_orders.storage import LocalStateStore, StorageError


class LocalStateStoreTest(unittest.TestCase):
    def test_missing_store_loads_empty_state_with_inactive_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")

            state = store.load()

            self.assertEqual(state.rules, {})
            self.assertEqual(state.rule_states, {})
            self.assertFalse(state.kill_switch_active)

    def test_rule_and_runtime_state_round_trip_without_decimal_precision_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1.0"),
                trail_mode=TrailMode.PERCENT,
                trail_value=Decimal("7.5"),
            )
            runtime = state.ensure_rule_state(rule)
            runtime.protected_size = Decimal("0.4")

            store.save(state)
            loaded = store.load()

            loaded_rule = loaded.rules[rule.id]
            loaded_runtime = loaded.rule_states[rule.id]
            self.assertEqual(loaded_rule.execution_mode, ExecutionMode.DRY_RUN)
            self.assertEqual(loaded_rule.coin, "ETH")
            self.assertEqual(loaded_rule.side, PositionSide.LONG)
            self.assertEqual(loaded_rule.size, Decimal("1.0"))
            self.assertEqual(loaded_rule.trail_mode, TrailMode.PERCENT)
            self.assertEqual(loaded_rule.trail_value, Decimal("7.5"))
            self.assertEqual(loaded_runtime.protected_size, Decimal("0.4"))

    def test_malformed_json_raises_storage_error_without_deleting_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text("{not-json", encoding="utf-8")
            store = LocalStateStore(path)

            with self.assertRaisesRegex(StorageError, "failed to load local state"):
                store.load()

            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")

    def test_kill_switch_persists_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            state = LocalStateStore(path).load()
            state.kill_switch_active = True

            LocalStateStore(path).save(state)
            loaded = LocalStateStore(path).load()

            self.assertTrue(loaded.kill_switch_active)


if __name__ == "__main__":
    unittest.main()
