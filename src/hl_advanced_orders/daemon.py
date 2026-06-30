from __future__ import annotations

import time
from decimal import Decimal
from typing import Protocol

from .audit import AuditEvent, JsonlAuditLog
from .hyperliquid_client import AccountGateway, MarketDataGateway
from .models import FillEvent, ObservationSource, PositionSide
from .storage import JsonRuleStore, state_from_trailing, trailing_from_state
from .submission import SubmissionPolicy
from .trailing import TrailingStopEngine


class ExitPolicy(Protocol):
    def handle(self, exit_order, snapshot):
        pass


class DaemonService:
    def __init__(
        self,
        *,
        store: JsonRuleStore,
        audit: JsonlAuditLog,
        market_data: MarketDataGateway,
        account_gateway: AccountGateway,
        submission_policy: SubmissionPolicy | ExitPolicy,
        account: str = "",
        engine: TrailingStopEngine | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.market_data = market_data
        self.account_gateway = account_gateway
        self.submission_policy = submission_policy
        self.account = account
        self.engine = engine or TrailingStopEngine()

    def tick(self) -> int:
        snapshot = self.store.load()
        positions = self.account_gateway.positions(self.account) if self.account else []
        fills = self.account_gateway.fills(self.account) if self.account else []
        changes = 0

        for rule_id, rule in list(snapshot.rules.items()):
            if rule.disabled:
                continue
            stored_state = snapshot.states[rule_id]
            trailing_state = trailing_from_state(rule, stored_state)
            before_protected = trailing_state.protected_size

            if rule.protect_existing:
                for position in positions:
                    if position.coin == rule.coin and position.side == rule.side:
                        trailing_state.increase_protected_size(
                            min(rule.size, position.size) - trailing_state.protected_size
                        )

            for fill in fills:
                if _fill_matches_rule(fill, rule.coin, rule.side, rule.opening_order_id):
                    fill_id = _stable_fill_id(fill)
                    if fill_id in trailing_state.processed_fill_ids:
                        continue
                    trailing_state.increase_protected_size(fill.size)
                    trailing_state.processed_fill_ids.add(fill_id)

            if trailing_state.protected_size != before_protected:
                self.audit.append(
                    AuditEvent.create(
                        "rule_state_changed",
                        "Protected size updated.",
                        rule_id=rule_id,
                        payload={
                            "coin": rule.coin,
                            "protected_size": str(trailing_state.protected_size),
                            "size": str(rule.size),
                        },
                    )
                )
                changes += 1

            tick = self.market_data.latest_price(rule.coin)
            if tick is None:
                snapshot.states[rule_id] = state_from_trailing(
                    trailing_state,
                    observed_live_mark_price=stored_state.observed_live_mark_price,
                )
                continue

            observed_live_mark_price = (
                stored_state.observed_live_mark_price
                or tick.source == ObservationSource.LIVE_MARK
            )
            exit_order = self.engine.observe(trailing_state, tick)
            snapshot.states[rule_id] = state_from_trailing(
                trailing_state,
                observed_live_mark_price=observed_live_mark_price,
            )
            if exit_order is not None:
                self.submission_policy.handle(exit_order, snapshot)
                changes += 1

        self.store.save(snapshot)
        return changes

    def run(self, *, interval_seconds: float = 1.0, max_ticks: int | None = None) -> int:
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            self.tick()
            ticks += 1
            if max_ticks is None:
                time.sleep(interval_seconds)
        return ticks


class EmptyAccountGateway:
    def positions(self, account: str):
        return []

    def fills(self, account: str):
        return []


class StaticMarketDataGateway:
    def __init__(self, prices: dict[str, Decimal] | None = None) -> None:
        self.prices = {coin.upper(): price for coin, price in (prices or {}).items()}

    def latest_price(self, coin: str):
        from .models import PriceTick

        price = self.prices.get(coin.upper())
        if price is None:
            return None
        return PriceTick.now(coin, price)

    def market_exists(self, coin: str) -> bool:
        return coin.upper() in self.prices


def _fill_matches_rule(
    fill: FillEvent,
    coin: str,
    side: PositionSide,
    opening_order_id: str | None,
) -> bool:
    if fill.coin != coin or fill.side != side:
        return False
    if opening_order_id is None:
        return True
    return fill.order_id == opening_order_id


def _stable_fill_id(fill: FillEvent) -> str:
    if fill.fill_id is not None:
        return fill.fill_id
    return f"{fill.coin}:{fill.side.value}:{fill.order_id}:{fill.size}"
