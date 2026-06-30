from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from hl_advanced_orders.audit import JsonlAuditLog
from hl_advanced_orders.daemon import DaemonService
from hl_advanced_orders.hyperliquid_client import FillEvent, PositionSnapshot
from hl_advanced_orders.models import ExecutionMode, PositionSide, PriceTick, TrailMode, TrailingStopRule
from hl_advanced_orders.storage import LocalStateStore


class FakeMarketData:
    def __init__(self, prices: dict[str, list[Decimal]]) -> None:
        self.prices = {coin: list(values) for coin, values in prices.items()}

    def get_mark_price(self, coin: str):
        return PriceTick.now(coin.upper(), self.prices[coin.upper()].pop(0))


class WrongCoinMarketData:
    def get_mark_price(self, coin: str):
        return PriceTick.now("BTC", Decimal("2000"))


class FakeAccount:
    def __init__(
        self,
        positions: list[PositionSnapshot] | None = None,
        fills: list[FillEvent] | None = None,
    ) -> None:
        self.positions = positions or []
        self.fills = fills or []

    def get_positions(self) -> list[PositionSnapshot]:
        return self.positions

    def get_fills(self) -> list[FillEvent]:
        return self.fills


class DaemonServiceTest(unittest.TestCase):
    def test_existing_long_position_dry_run_audits_trigger_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.PERCENT,
                trail_value=Decimal("10"),
            )
            state.ensure_rule_state(rule)
            store.save(state)
            audit_path = Path(temp_dir) / "audit.jsonl"
            daemon = DaemonService(
                store=store,
                audit=JsonlAuditLog(audit_path),
                market_data=FakeMarketData({"ETH": [Decimal("100"), Decimal("120"), Decimal("107")]}),
                account=FakeAccount(
                    positions=[PositionSnapshot("ETH", PositionSide.LONG, Decimal("1"))]
                ),
            )

            daemon.run_once()
            daemon.run_once()
            daemon.run_once()
            loaded = store.load()
            events = read_events(audit_path)

            self.assertTrue(loaded.rule_states[rule.id].triggered)
            self.assertEqual(loaded.rule_states[rule.id].protected_size, Decimal("1"))
            self.assertEqual(events[-1]["event_type"], "dry_run_exit")
            self.assertEqual(events[-1]["payload"]["side"], "sell")
            self.assertEqual(events[-1]["payload"]["mark_price"], "107")

    def test_attached_opening_order_partial_fills_protect_up_to_close_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("50"),
                attached_order_id="123",
            )
            state.ensure_rule_state(rule)
            store.save(state)
            daemon = DaemonService(
                store=store,
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                market_data=FakeMarketData({"ETH": [Decimal("2000")]}),
                account=FakeAccount(
                    fills=[
                        FillEvent("ETH", PositionSide.LONG, "123", Decimal("0.4"), fill_id="a"),
                        FillEvent("ETH", PositionSide.LONG, "123", Decimal("0.8"), fill_id="b"),
                    ]
                ),
            )

            daemon.run_once()
            loaded = store.load()

            self.assertEqual(loaded.rule_states[rule.id].protected_size, Decimal("1"))

    def test_same_size_fills_without_fill_ids_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("50"),
                attached_order_id="123",
            )
            state.ensure_rule_state(rule)
            store.save(state)
            daemon = DaemonService(
                store=store,
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                market_data=FakeMarketData({"ETH": [Decimal("2000")]}),
                account=FakeAccount(
                    fills=[
                        FillEvent("ETH", PositionSide.LONG, "123", Decimal("0.4")),
                        FillEvent("ETH", PositionSide.LONG, "123", Decimal("0.4")),
                    ]
                ),
            )

            daemon.run_once()
            loaded = store.load()

            self.assertEqual(loaded.rule_states[rule.id].protected_size, Decimal("0.8"))

    def test_other_coin_tick_leaves_rule_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("50"),
            )
            runtime = state.ensure_rule_state(rule)
            runtime.protected_size = Decimal("1")
            store.save(state)
            daemon = DaemonService(
                store=store,
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                market_data=WrongCoinMarketData(),
                account=FakeAccount(),
            )

            daemon.run_once()

            loaded_runtime = store.load().rule_states[rule.id]
            self.assertIsNone(loaded_runtime.stop_price)
            self.assertFalse(loaded_runtime.triggered)

    def test_missing_existing_position_clears_protected_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("50"),
            )
            runtime = state.ensure_rule_state(rule)
            runtime.protected_size = Decimal("1")
            store.save(state)
            daemon = DaemonService(
                store=store,
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                market_data=FakeMarketData({"ETH": [Decimal("100")]}),
                account=FakeAccount(positions=[]),
            )

            daemon.run_once()
            loaded_runtime = store.load().rule_states[rule.id]

            self.assertEqual(loaded_runtime.protected_size, Decimal("0"))

    def test_triggered_rule_is_not_audited_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("5"),
            )
            runtime = state.ensure_rule_state(rule)
            runtime.protected_size = Decimal("1")
            runtime.favorable_price = Decimal("100")
            runtime.stop_price = Decimal("95")
            store.save(state)
            audit_path = Path(temp_dir) / "audit.jsonl"
            daemon = DaemonService(
                store=store,
                audit=JsonlAuditLog(audit_path),
                market_data=FakeMarketData({"ETH": [Decimal("94"), Decimal("93")]}),
                account=FakeAccount(positions=[PositionSnapshot("ETH", PositionSide.LONG, Decimal("1"))]),
            )

            daemon.run_once()
            daemon.run_once()

            self.assertEqual(len(read_events(audit_path)), 1)

    def test_kill_switch_blocks_live_submission_as_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            state = store.load()
            state.kill_switch_active = True
            rule = TrailingStopRule(
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("5"),
                execution_mode=ExecutionMode.AUTO_SUBMIT,
            )
            runtime = state.ensure_rule_state(rule)
            runtime.protected_size = Decimal("1")
            runtime.favorable_price = Decimal("100")
            runtime.stop_price = Decimal("95")
            store.save(state)
            audit_path = Path(temp_dir) / "audit.jsonl"
            daemon = DaemonService(
                store=store,
                audit=JsonlAuditLog(audit_path),
                market_data=FakeMarketData({"ETH": [Decimal("94")]}),
                account=FakeAccount(positions=[PositionSnapshot("ETH", PositionSide.LONG, Decimal("1"))]),
            )

            daemon.run_once()

            events = read_events(audit_path)
            self.assertEqual(events[-1]["event_type"], "live_submission_blocked")
            self.assertIn("kill switch is active", events[-1]["payload"]["reasons"])
            self.assertFalse(store.load().rule_states[rule.id].triggered)


def read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
