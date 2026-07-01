from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from hl_advanced_orders.models import (
    ExecutionMode,
    LiveEnablementStatus,
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
            state.live_mark_observed_at_by_rule[rule.id] = "2026-06-30T10:00:00+00:00"

            store.save(state)
            loaded = store.load()

            loaded_rule = loaded.rules[rule.id]
            loaded_runtime = loaded.rule_states[rule.id]
            self.assertEqual(loaded_rule.execution_mode, ExecutionMode.DRY_RUN)
            self.assertEqual(loaded_rule.live_status, LiveEnablementStatus.DRY_RUN)
            self.assertEqual(loaded_rule.coin, "ETH")
            self.assertEqual(loaded_rule.side, PositionSide.LONG)
            self.assertEqual(loaded_rule.size, Decimal("1.0"))
            self.assertEqual(loaded_rule.trail_mode, TrailMode.PERCENT)
            self.assertEqual(loaded_rule.trail_value, Decimal("7.5"))
            self.assertEqual(loaded_runtime.protected_size, Decimal("0.4"))
            self.assertEqual(
                loaded.live_mark_observed_at_by_rule[rule.id],
                "2026-06-30T10:00:00+00:00",
            )

    def test_auto_submit_rule_defaults_to_canary_pending_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("50"),
                execution_mode=ExecutionMode.AUTO_SUBMIT,
            )
            state.ensure_rule_state(rule)

            store.save(state)
            loaded = store.load()

            self.assertEqual(rule.live_status, LiveEnablementStatus.CANARY_PENDING)
            self.assertEqual(
                loaded.rules[rule.id].live_status,
                LiveEnablementStatus.CANARY_PENDING,
            )

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

    def test_health_state_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            state = LocalStateStore(path).load()
            state.health.mode = "running"
            state.health.last_tick_started_at = "2026-06-30T10:00:00+00:00"
            state.health.last_tick_completed_at = "2026-06-30T10:00:01+00:00"
            state.health.last_successful_account_snapshot_at = "2026-06-30T10:00:01+00:00"
            state.health.last_successful_market_snapshot_at = "2026-06-30T10:00:01+00:00"
            state.health.consecutive_failures = 2
            state.health.active_error = "market unavailable"
            state.health.active_rules_count = 3
            state.health.last_blocked_reasons = ["kill switch is active"]

            LocalStateStore(path).save(state)
            loaded = LocalStateStore(path).load()

            self.assertEqual(loaded.health.mode, "running")
            self.assertEqual(loaded.health.last_tick_started_at, "2026-06-30T10:00:00+00:00")
            self.assertEqual(loaded.health.last_tick_completed_at, "2026-06-30T10:00:01+00:00")
            self.assertEqual(
                loaded.health.last_successful_account_snapshot_at,
                "2026-06-30T10:00:01+00:00",
            )
            self.assertEqual(
                loaded.health.last_successful_market_snapshot_at,
                "2026-06-30T10:00:01+00:00",
            )
            self.assertEqual(loaded.health.consecutive_failures, 2)
            self.assertEqual(loaded.health.active_error, "market unavailable")
            self.assertEqual(loaded.health.active_rules_count, 3)
            self.assertEqual(loaded.health.last_blocked_reasons, ["kill switch is active"])

    def test_failed_atomic_replace_leaves_previous_state_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = LocalStateStore(path)
            state = store.load()
            state.kill_switch_active = True
            store.save(state)

            next_state = store.load()
            next_state.kill_switch_active = False
            with patch.object(store, "_replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.save(next_state)

            loaded = LocalStateStore(path).load()
            self.assertTrue(loaded.kill_switch_active)

    def test_save_fsyncs_parent_directory_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")

            with patch.object(store, "_fsync_parent_dir") as fsync_parent:
                store.save(store.load())

            fsync_parent.assert_called_once_with()

    def test_daemon_save_preserves_externally_activated_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = LocalStateStore(path)
            stale_state = store.load()
            store.save(stale_state)

            latest_state = store.load()
            latest_state.kill_switch_active = True
            store.save(latest_state)

            stale_state.kill_switch_active = False
            store.save_preserving_active_kill_switch(stale_state)

            self.assertTrue(store.load().kill_switch_active)

    def test_preserve_kill_switch_does_not_overwrite_malformed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = LocalStateStore(path)
            stale_state = store.load()
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(StorageError):
                store.save_preserving_active_kill_switch(stale_state)

            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")


if __name__ == "__main__":
    unittest.main()
