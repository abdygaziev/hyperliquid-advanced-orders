from __future__ import annotations

import unittest
from decimal import Decimal

from hl_advanced_orders.hyperliquid_client import (
    FillEvent,
    HyperliquidAccountGateway,
    HyperliquidExchangeGateway,
    HyperliquidMarketDataGateway,
    MissingPrivateKeyError,
)
from hl_advanced_orders.models import PositionSide, PriceSource
from hl_advanced_orders.secrets import InMemorySecrets


class FakeInfo:
    def __init__(self) -> None:
        self.mids = {"ETH": "2450.125"}
        self.user_state_payload = {
            "assetPositions": [
                {"position": {"coin": "ETH", "szi": "1.5"}},
                {"position": {"coin": "BTC", "szi": "-0.25"}},
                {"position": {"coin": "SOL", "szi": "0"}},
            ]
        }
        self.fills = [
            {"coin": "ETH", "oid": 42, "side": "B", "sz": "0.4"},
            {"coin": "BTC", "oid": 42, "side": "A", "sz": "0.1"},
        ]

    def all_mids(self) -> dict[str, str]:
        return self.mids

    def user_state(self, address: str) -> dict[str, object]:
        self.address = address
        return self.user_state_payload

    def user_fills(self, address: str) -> list[dict[str, object]]:
        self.address = address
        return self.fills


class FakeExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Decimal]] = []

    def market_close(self, coin: str, sz: Decimal) -> dict[str, object]:
        self.calls.append((coin, sz))
        return {"status": "ok", "coin": coin, "sz": str(sz)}


class HyperliquidGatewayTest(unittest.TestCase):
    def test_mark_price_context_payload_becomes_decimal_price_tick(self) -> None:
        class MarkInfo(FakeInfo):
            def meta_and_asset_ctxs(self):
                return (
                    {"universe": [{"name": "ETH"}]},
                    [{"markPx": "2451.25"}],
                )

        gateway = HyperliquidMarketDataGateway(info=MarkInfo())

        tick = gateway.get_mark_price("eth")

        self.assertEqual(tick.coin, "ETH")
        self.assertEqual(tick.mark_price, Decimal("2451.25"))
        self.assertEqual(tick.source, PriceSource.MARK)

    def test_all_mids_payload_is_labeled_mid_price_fallback(self) -> None:
        gateway = HyperliquidMarketDataGateway(info=FakeInfo())

        tick = gateway.get_mark_price("eth")

        self.assertEqual(tick.coin, "ETH")
        self.assertEqual(tick.mark_price, Decimal("2450.125"))
        self.assertEqual(tick.source, PriceSource.MID)

    def test_user_positions_map_positive_long_negative_short_and_skip_zero(self) -> None:
        gateway = HyperliquidAccountGateway(info=FakeInfo(), address="0xabc")

        positions = gateway.get_positions()

        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0].coin, "ETH")
        self.assertEqual(positions[0].side, PositionSide.LONG)
        self.assertEqual(positions[0].size, Decimal("1.5"))
        self.assertEqual(positions[1].coin, "BTC")
        self.assertEqual(positions[1].side, PositionSide.SHORT)
        self.assertEqual(positions[1].size, Decimal("0.25"))

    def test_user_fills_parse_order_identity_coin_side_and_size(self) -> None:
        gateway = HyperliquidAccountGateway(info=FakeInfo(), address="0xabc")

        fills = gateway.get_fills()

        self.assertEqual(
            fills[0],
            FillEvent(coin="ETH", side=PositionSide.LONG, order_id="42", size=Decimal("0.4")),
        )
        self.assertEqual(fills[1].side, PositionSide.SHORT)

    def test_missing_private_key_prevents_exchange_gateway_construction(self) -> None:
        secrets = InMemorySecrets()

        with self.assertRaises(MissingPrivateKeyError):
            HyperliquidExchangeGateway.from_keychain(
                account="trader",
                wallet_address="0xabc",
                secrets=secrets,
            )

    def test_live_exit_uses_reduce_only_market_close_with_protected_size(self) -> None:
        exchange = FakeExchange()
        gateway = HyperliquidExchangeGateway(exchange=exchange)

        response = gateway.submit_market_close("ETH", Decimal("0.4"))

        self.assertEqual(exchange.calls, [("ETH", Decimal("0.4"))])
        self.assertEqual(response["status"], "ok")


if __name__ == "__main__":
    unittest.main()
