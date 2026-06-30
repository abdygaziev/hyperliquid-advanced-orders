from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from hl_advanced_orders.audit import JsonlAuditLog
from hl_advanced_orders.daemon import DaemonService
from hl_advanced_orders.models import (
    ExistingPosition,
    FillEvent,
    PositionSide,
    PriceTick,
    TrailMode,
    TrailingStopRule,
)
from hl_advanced_orders.storage import JsonRuleStore


class FakeMarket:
    def __init__(self, ticks: list[PriceTick]) -> None:
        self.ticks = ticks

    def latest_price(self, coin: str):
        if not self.ticks:
            return None
        return self.ticks.pop(0)

    def market_exists(self, coin: str) -> bool:
        return True


class FakeAccount:
    def __init__(
        self,
        positions: list[ExistingPosition] | None = None,
        fills: list[FillEvent] | None = None,
    ) -> None:
        self._positions = positions or []
        self._fills = fills or []

    def positions(self, account: str):
        return self._positions

    def fills(self, account: str):
        return self._fills


class FakePolicy:
    def __init__(self) -> None:
        self.exits = []

    def handle(self, exit_order, snapshot):
        self.exits.append(exit_order)


class DaemonServiceTest(unittest.TestCase):
    def test_existing_long_position_dry_run_exit_is_persisted_and_audited_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonRuleStore(Path(tmp) / "state.json")
            audit = JsonlAuditLog(Path(tmp) / "audit.jsonl")
            rule = TrailingStopRule(
                id="rule_1",
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.PERCENT,
                trail_value=Decimal("10"),
            )
            store.add_rule(rule)
            policy = FakePolicy()
            service = DaemonService(
                store=store,
                audit=audit,
                market_data=FakeMarket(
                    [
                        PriceTick.now("ETH", Decimal("100")),
                        PriceTick.now("ETH", Decimal("120")),
                        PriceTick.now("ETH", Decimal("107")),
                    ]
                ),
                account_gateway=FakeAccount([ExistingPosition("ETH", PositionSide.LONG, Decimal("1"))]),
                submission_policy=policy,
                account="0xabc",
            )

            service.tick()
            service.tick()
            service.tick()
            service.tick()

            snapshot = store.load()
            self.assertTrue(snapshot.states["rule_1"].triggered)
            self.assertEqual(snapshot.states["rule_1"].protected_size, Decimal("1"))
            self.assertEqual(len(policy.exits), 1)

    def test_opening_order_fill_protects_only_matching_order_and_caps_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonRuleStore(Path(tmp) / "state.json")
            audit = JsonlAuditLog(Path(tmp) / "audit.jsonl")
            rule = TrailingStopRule(
                id="rule_1",
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("5"),
                protect_existing=False,
                opening_order_id="abc",
            )
            store.add_rule(rule)
            service = DaemonService(
                store=store,
                audit=audit,
                market_data=FakeMarket([PriceTick.now("ETH", Decimal("100"))]),
                account_gateway=FakeAccount(
                    fills=[
                        FillEvent("ETH", PositionSide.LONG, Decimal("0.4"), "wrong", "fill_wrong"),
                        FillEvent("ETH", PositionSide.LONG, Decimal("1.2"), "abc", "fill_1"),
                    ]
                ),
                submission_policy=FakePolicy(),
                account="0xabc",
            )

            service.tick()

            self.assertEqual(store.load().states["rule_1"].protected_size, Decimal("1"))

    def test_repeated_historical_fill_is_not_applied_on_every_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonRuleStore(Path(tmp) / "state.json")
            rule = TrailingStopRule(
                id="rule_1",
                coin="ETH",
                side=PositionSide.LONG,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("5"),
                protect_existing=False,
                opening_order_id="abc",
            )
            store.add_rule(rule)
            service = DaemonService(
                store=store,
                audit=JsonlAuditLog(Path(tmp) / "audit.jsonl"),
                market_data=FakeMarket(
                    [PriceTick.now("ETH", Decimal("100")), PriceTick.now("ETH", Decimal("101"))]
                ),
                account_gateway=FakeAccount(
                    fills=[FillEvent("ETH", PositionSide.LONG, Decimal("0.4"), "abc", "fill_1")]
                ),
                submission_policy=FakePolicy(),
                account="0xabc",
            )

            service.tick()
            service.tick()

            state = store.load().states["rule_1"]
            self.assertEqual(state.protected_size, Decimal("0.4"))
            self.assertEqual(state.processed_fill_ids, {"fill_1"})

    def test_mark_price_for_other_coin_leaves_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonRuleStore(Path(tmp) / "state.json")
            rule = TrailingStopRule(
                id="rule_1",
                coin="ETH",
                side=PositionSide.SHORT,
                size=Decimal("1"),
                trail_mode=TrailMode.ABSOLUTE,
                trail_value=Decimal("5"),
            )
            store.add_rule(rule)
            service = DaemonService(
                store=store,
                audit=JsonlAuditLog(Path(tmp) / "audit.jsonl"),
                market_data=FakeMarket([PriceTick.now("BTC", Decimal("100"))]),
                account_gateway=FakeAccount([ExistingPosition("ETH", PositionSide.SHORT, Decimal("1"))]),
                submission_policy=FakePolicy(),
                account="0xabc",
            )

            service.tick()

            state = store.load().states["rule_1"]
            self.assertIsNone(state.stop_price)


if __name__ == "__main__":
    unittest.main()
