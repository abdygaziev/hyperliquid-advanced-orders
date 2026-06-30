from __future__ import annotations

import unittest
from decimal import Decimal

from hl_advanced_orders.hyperliquid_client import HyperliquidInfoGateway
from hl_advanced_orders.models import ObservationSource, PositionSide


class FakeInfo:
    def meta_and_asset_ctxs(self):
        return [{"universe": [{"name": "ETH"}]}, [{"markPx": "3100.25"}]]

    def all_mids(self):
        return {"BTC": "65000"}

    def meta(self):
        return {"universe": [{"name": "ETH"}]}

    def user_state(self, account):
        return {
            "assetPositions": [
                {"position": {"coin": "ETH", "szi": "1.5"}},
                {"position": {"coin": "BTC", "szi": "-0.2"}},
                {"position": {"coin": "SOL", "szi": "0"}},
            ]
        }

    def user_fills(self, account):
        return [{"coin": "ETH", "side": "B", "sz": "0.4", "oid": 123}]


class HyperliquidInfoGatewayTest(unittest.TestCase):
    def test_mark_price_payload_becomes_decimal_price_tick(self) -> None:
        tick = HyperliquidInfoGateway(FakeInfo()).latest_price("eth")

        self.assertIsNotNone(tick)
        assert tick is not None
        self.assertEqual(tick.coin, "ETH")
        self.assertEqual(tick.mark_price, Decimal("3100.25"))
        self.assertEqual(tick.source, ObservationSource.LIVE_MARK)

    def test_all_mids_fallback_is_labeled_non_mark(self) -> None:
        tick = HyperliquidInfoGateway(FakeInfo()).latest_price("BTC")

        self.assertIsNotNone(tick)
        assert tick is not None
        self.assertEqual(tick.source, ObservationSource.MID_PRICE_FALLBACK)

    def test_positions_and_fills_are_normalized(self) -> None:
        gateway = HyperliquidInfoGateway(FakeInfo())

        positions = gateway.positions("0xabc")
        fills = gateway.fills("0xabc")

        self.assertEqual(positions[0].side, PositionSide.LONG)
        self.assertEqual(positions[1].side, PositionSide.SHORT)
        self.assertEqual(fills[0].coin, "ETH")
        self.assertEqual(fills[0].side, PositionSide.LONG)
        self.assertEqual(fills[0].order_id, "123")
        self.assertEqual(fills[0].fill_id, "oid:123")


if __name__ == "__main__":
    unittest.main()
