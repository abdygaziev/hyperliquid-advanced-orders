from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hl_advanced_orders.hyperliquid_client import MarketMetadata, PositionSnapshot
from hl_advanced_orders.models import PositionSide, TrailMode, TrailingStopRule
from hl_advanced_orders.preflight import PreflightService
from hl_advanced_orders.readiness import MAINNET_CONFIRMATION_PHRASE
from hl_advanced_orders.secrets import InMemorySecrets
from hl_advanced_orders.storage import LocalDaemonState


@dataclass
class FakeMarketData:
    exists: bool = True

    def get_market_metadata(self, coin: str) -> MarketMetadata:
        return MarketMetadata(coin=coin.upper(), exists=self.exists, source="fake_metadata")


class FakeAccount:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def get_positions(self) -> list[PositionSnapshot]:
        if self.fail:
            raise RuntimeError("account unavailable")
        return [PositionSnapshot("ETH", PositionSide.LONG, Decimal("1"))]


class PreflightServiceTest(unittest.TestCase):
    def test_metadata_containing_rule_coin_marks_market_valid(self) -> None:
        secrets = InMemorySecrets()
        secrets.set_private_key("trader", "private-key")
        rule = trailing_rule()
        state = LocalDaemonState()
        state.ensure_rule_state(rule)
        now = datetime(2026, 6, 30, 10, tzinfo=timezone.utc)
        state.live_mark_observed_at_by_rule[rule.id] = now.isoformat()

        result = PreflightService(
            secrets=secrets,
            market_data=FakeMarketData(exists=True),
            account=FakeAccount(),
        ).check_rule(
            state=state,
            rule=rule,
            account_name="trader",
            dry_run_events_count=1,
            confirmation_phrase=MAINNET_CONFIRMATION_PHRASE,
            now=now,
        )

        self.assertTrue(result.passed)
        self.assertTrue(result.context.market_exists)
        self.assertEqual(result.market_metadata_source, "fake_metadata")

    def test_missing_metadata_blocks_with_market_reason(self) -> None:
        secrets = InMemorySecrets()
        secrets.set_private_key("trader", "private-key")
        rule = trailing_rule()
        state = LocalDaemonState()
        state.ensure_rule_state(rule)
        now = datetime(2026, 6, 30, 10, tzinfo=timezone.utc)
        state.live_mark_observed_at_by_rule[rule.id] = now.isoformat()

        result = PreflightService(
            secrets=secrets,
            market_data=FakeMarketData(exists=False),
            account=FakeAccount(),
        ).check_rule(
            state=state,
            rule=rule,
            account_name="trader",
            dry_run_events_count=1,
            confirmation_phrase=MAINNET_CONFIRMATION_PHRASE,
            now=now,
        )

        self.assertFalse(result.passed)
        self.assertIn("market does not exist: ETH", result.reasons)

    def test_stale_mark_observation_blocks_live_readiness(self) -> None:
        secrets = InMemorySecrets()
        secrets.set_private_key("trader", "private-key")
        rule = trailing_rule()
        state = LocalDaemonState()
        state.ensure_rule_state(rule)
        now = datetime(2026, 6, 30, 10, tzinfo=timezone.utc)
        state.live_mark_observed_at_by_rule[rule.id] = (now - timedelta(minutes=10)).isoformat()

        result = PreflightService(
            secrets=secrets,
            market_data=FakeMarketData(exists=True),
            account=FakeAccount(),
            freshness_window=timedelta(minutes=5),
        ).check_rule(
            state=state,
            rule=rule,
            account_name="trader",
            dry_run_events_count=1,
            confirmation_phrase=MAINNET_CONFIRMATION_PHRASE,
            now=now,
        )

        self.assertFalse(result.passed)
        self.assertFalse(result.context.observed_live_mark_price)
        self.assertIn("rule has not observed live mark prices", result.reasons)

    def test_account_snapshot_failure_blocks_preflight(self) -> None:
        secrets = InMemorySecrets()
        secrets.set_private_key("trader", "private-key")
        rule = trailing_rule()
        state = LocalDaemonState()
        state.ensure_rule_state(rule)
        now = datetime(2026, 6, 30, 10, tzinfo=timezone.utc)
        state.live_mark_observed_at_by_rule[rule.id] = now.isoformat()

        result = PreflightService(
            secrets=secrets,
            market_data=FakeMarketData(exists=True),
            account=FakeAccount(fail=True),
        ).check_rule(
            state=state,
            rule=rule,
            account_name="trader",
            dry_run_events_count=1,
            confirmation_phrase=MAINNET_CONFIRMATION_PHRASE,
            now=now,
        )

        self.assertFalse(result.passed)
        self.assertFalse(result.account_snapshot_available)
        self.assertIn("account snapshot is not available", result.reasons)


def trailing_rule() -> TrailingStopRule:
    return TrailingStopRule(
        id="rule_123",
        coin="ETH",
        side=PositionSide.LONG,
        size=Decimal("1"),
        trail_mode=TrailMode.ABSOLUTE,
        trail_value=Decimal("50"),
    )


if __name__ == "__main__":
    unittest.main()
