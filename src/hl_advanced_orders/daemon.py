from __future__ import annotations

from typing import Protocol

from .audit import JsonlAuditLog
from .hyperliquid_client import FillEvent, PositionSnapshot
from .models import PositionSide, PriceTick, RuleStatus, TriggeredExit
from .readiness import ReadinessContext
from .storage import LocalDaemonState, LocalStateStore
from .submission import SubmissionPolicy
from .trailing import TrailingStopEngine, TrailingStopState


class MarketDataGateway(Protocol):
    def get_mark_price(self, coin: str) -> PriceTick:
        pass


class AccountGateway(Protocol):
    def get_positions(self) -> list[PositionSnapshot]:
        pass

    def get_fills(self) -> list[FillEvent]:
        pass


class DaemonService:
    def __init__(
        self,
        *,
        store: LocalStateStore,
        audit: JsonlAuditLog,
        market_data: MarketDataGateway,
        account: AccountGateway,
        engine: TrailingStopEngine | None = None,
        submission_policy: SubmissionPolicy | None = None,
        readiness_context: ReadinessContext | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.market_data = market_data
        self.account = account
        self.engine = engine or TrailingStopEngine()
        self.submission_policy = submission_policy or SubmissionPolicy(audit=audit)
        self.readiness_context = readiness_context

    def run_once(self) -> None:
        state = self.store.load()
        positions = self.account.get_positions()
        fills = self.account.get_fills()

        for rule in list(state.rules.values()):
            if rule.status == RuleStatus.DISABLED:
                continue
            runtime = state.ensure_rule_state(rule)
            self._update_protected_size(state, runtime, positions, fills)
            tick = self.market_data.get_mark_price(rule.coin)
            triggered = self.engine.observe(runtime, tick)
            if triggered is not None:
                context = self.readiness_context
                if context is not None:
                    context = ReadinessContext(
                        account=context.account,
                        market_exists=context.market_exists,
                        observed_live_mark_price=context.observed_live_mark_price,
                        kill_switch_available=context.kill_switch_available,
                        kill_switch_active=state.kill_switch_active,
                        dry_run_events_count=context.dry_run_events_count,
                        confirmation_phrase=context.confirmation_phrase,
                    )
                elif state.kill_switch_active:
                    context = ReadinessContext(
                        account="",
                        market_exists=True,
                        observed_live_mark_price=True,
                        kill_switch_available=True,
                        kill_switch_active=True,
                        dry_run_events_count=1,
                        confirmation_phrase="",
                    )
                self.submission_policy.handle(triggered=triggered, rule=rule, context=context)

        self.store.save(state)

    def process_tick(
        self,
        rule_id: str,
        state: LocalDaemonState,
        runtime: TrailingStopState,
        coin: str,
    ) -> TriggeredExit | None:
        tick = self.market_data.get_mark_price(coin)
        if runtime.rule.id != rule_id:
            return None
        return self.engine.observe(runtime, tick)

    def _update_protected_size(
        self,
        state: LocalDaemonState,
        runtime: TrailingStopState,
        positions: list[PositionSnapshot],
        fills: list[FillEvent],
    ) -> None:
        rule = runtime.rule
        if rule.attached_order_id:
            for fill in fills:
                if not self._fill_matches_rule(fill, rule.side, rule.coin, rule.attached_order_id):
                    continue
                fill_key = fill.fill_id or f"{fill.order_id}:{fill.coin}:{fill.side.value}:{fill.size}"
                if state.last_fill_seen_by_order.get(fill_key):
                    continue
                runtime.increase_protected_size(fill.size)
                state.last_fill_seen_by_order[fill_key] = rule.id
            return

        for position in positions:
            if position.coin == rule.coin and position.side == rule.side:
                runtime.protected_size = min(rule.size, position.size)
                return

    def _fill_matches_rule(
        self,
        fill: FillEvent,
        side: PositionSide,
        coin: str,
        order_id: str,
    ) -> bool:
        return fill.coin == coin and fill.side == side and fill.order_id == order_id
