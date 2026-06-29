from __future__ import annotations

import unittest
from decimal import Decimal

from hl_advanced_orders.models import PositionSide, PriceTick, TrailMode, TrailingStopRule
from hl_advanced_orders.trailing import TrailingStopEngine, TrailingStopState


class TrailingStopEngineTest(unittest.TestCase):
    def test_long_percent_trailing_stop_sells_when_price_falls_through_stop(self) -> None:
        rule = TrailingStopRule(
            coin="ETH",
            side=PositionSide.LONG,
            size=Decimal("4"),
            trail_mode=TrailMode.PERCENT,
            trail_value=Decimal("10"),
        )
        state = TrailingStopState(rule=rule)
        state.increase_protected_size(Decimal("4"))
        engine = TrailingStopEngine()

        self.assertIsNone(engine.observe(state, PriceTick.now("ETH", Decimal("100"))))
        self.assertEqual(state.stop_price, Decimal("90.0"))
        self.assertIsNone(engine.observe(state, PriceTick.now("ETH", Decimal("120"))))
        self.assertEqual(state.stop_price, Decimal("108.0"))

        exit_order = engine.observe(state, PriceTick.now("ETH", Decimal("107")))

        self.assertIsNotNone(exit_order)
        assert exit_order is not None
        self.assertEqual(exit_order.side, "sell")
        self.assertEqual(exit_order.size, Decimal("4"))

    def test_short_absolute_trailing_stop_buys_when_price_rises_through_stop(self) -> None:
        rule = TrailingStopRule(
            coin="BTC",
            side=PositionSide.SHORT,
            size=Decimal("0.5"),
            trail_mode=TrailMode.ABSOLUTE,
            trail_value=Decimal("1000"),
        )
        state = TrailingStopState(rule=rule)
        state.increase_protected_size(Decimal("0.5"))
        engine = TrailingStopEngine()

        self.assertIsNone(engine.observe(state, PriceTick.now("BTC", Decimal("65000"))))
        self.assertEqual(state.stop_price, Decimal("66000"))
        self.assertIsNone(engine.observe(state, PriceTick.now("BTC", Decimal("62000"))))
        self.assertEqual(state.stop_price, Decimal("63000"))

        exit_order = engine.observe(state, PriceTick.now("BTC", Decimal("63100")))

        self.assertIsNotNone(exit_order)
        assert exit_order is not None
        self.assertEqual(exit_order.side, "buy")
        self.assertEqual(exit_order.size, Decimal("0.5"))

    def test_partial_fills_increase_protected_size_up_to_rule_size(self) -> None:
        rule = TrailingStopRule(
            coin="ETH",
            side=PositionSide.LONG,
            size=Decimal("10"),
            trail_mode=TrailMode.ABSOLUTE,
            trail_value=Decimal("50"),
        )
        state = TrailingStopState(rule=rule)

        state.increase_protected_size(Decimal("4"))
        state.increase_protected_size(Decimal("7"))

        self.assertEqual(state.protected_size, Decimal("10"))


if __name__ == "__main__":
    unittest.main()
