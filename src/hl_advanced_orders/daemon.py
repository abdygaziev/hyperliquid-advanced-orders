from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from .audit import AuditEvent, JsonlAuditLog
from .hyperliquid_client import FillEvent, PositionSnapshot
from .models import ExecutionMode, PositionSide, PriceSource, PriceTick, RuleStatus, TrailingStopRule
from .readiness import ReadinessContext
from .storage import LocalDaemonState, LocalStateStore
from .submission import SubmissionOutcome, SubmissionPolicy
from .trailing import TrailingStopEngine, TrailingStopState


class MarketDataGateway(Protocol):
    def get_mark_price(self, coin: str) -> PriceTick:
        pass


class AccountGateway(Protocol):
    def get_positions(self) -> list[PositionSnapshot]:
        pass

    def get_fills(self) -> list[FillEvent]:
        pass


ReadinessContextFactory = Callable[[LocalDaemonState, TrailingStopRule], ReadinessContext | None]


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
        readiness_context_factory: ReadinessContextFactory | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.market_data = market_data
        self.account = account
        self.engine = engine or TrailingStopEngine()
        self.submission_policy = submission_policy or SubmissionPolicy(audit=audit)
        self.readiness_context = readiness_context
        self.readiness_context_factory = readiness_context_factory

    def run_once(self) -> None:
        state = self.store.load()
        try:
            positions = self.account.get_positions()
            fills = self.account.get_fills()
        except Exception as exc:
            self.audit.append(
                AuditEvent.create(
                    "account_snapshot_failed",
                    "Failed to load account snapshot.",
                    payload={"error": str(exc)},
                )
            )
            return

        for rule in list(state.rules.values()):
            if rule.status == RuleStatus.DISABLED:
                continue
            runtime = state.ensure_rule_state(rule)
            self._update_protected_size(state, runtime, positions, fills)
            try:
                tick = self.market_data.get_mark_price(rule.coin)
            except Exception as exc:
                self.audit.append(
                    AuditEvent.create(
                        "market_data_failed",
                        "Failed to load market data.",
                        rule_id=rule.id,
                        payload={"coin": rule.coin, "error": str(exc)},
                    )
                )
                continue
            if tick.coin != rule.coin or tick.source != PriceSource.MARK:
                continue
            state.live_mark_observed_rule_ids.add(rule.id)
            triggered = self.engine.observe(runtime, tick)
            if triggered is not None:
                if triggered.execution_mode == ExecutionMode.AUTO_SUBMIT:
                    self.store.save_preserving_active_kill_switch(state)
                context = self._readiness_context_for(state, rule)
                outcome = self.submission_policy.handle(triggered=triggered, rule=rule, context=context)
                if outcome == SubmissionOutcome.LIVE_BLOCKED:
                    runtime.triggered = False

        self.store.save_preserving_active_kill_switch(state)

    def _update_protected_size(
        self,
        state: LocalDaemonState,
        runtime: TrailingStopState,
        positions: list[PositionSnapshot],
        fills: list[FillEvent],
    ) -> None:
        rule = runtime.rule
        if rule.attached_order_id:
            order_key = f"{rule.id}:{rule.attached_order_id}"
            total_filled = Decimal("0")
            for fill in fills:
                if not self._fill_matches_rule(fill, rule.side, rule.coin, rule.attached_order_id):
                    continue
                total_filled += fill.size
                fill_key = fill.fill_id or f"{fill.order_id}:{fill.coin}:{fill.side.value}:{fill.size}"
                if state.last_fill_seen_by_order.get(fill_key):
                    continue
                runtime.increase_protected_size(fill.size)
                state.last_fill_seen_by_order[fill_key] = rule.id
            if total_filled > 0:
                previous_total = state.filled_size_by_order.get(order_key, Decimal("0"))
                cumulative_total = max(previous_total, total_filled)
                runtime.protected_size = min(rule.size, cumulative_total)
                state.filled_size_by_order[order_key] = cumulative_total
            return

        for position in positions:
            if position.coin == rule.coin and position.side == rule.side:
                runtime.protected_size = min(rule.size, position.size)
                return
        runtime.protected_size = Decimal("0")

    def _fill_matches_rule(
        self,
        fill: FillEvent,
        side: PositionSide,
        coin: str,
        order_id: str,
    ) -> bool:
        return fill.coin == coin and fill.side == side and fill.order_id == order_id

    def _readiness_context_for(
        self,
        state: LocalDaemonState,
        rule: TrailingStopRule,
    ) -> ReadinessContext | None:
        context = (
            self.readiness_context_factory(state, rule)
            if self.readiness_context_factory is not None
            else self.readiness_context
        )
        if context is not None:
            return replace(
                context,
                observed_live_mark_price=(
                    context.observed_live_mark_price or rule.id in state.live_mark_observed_rule_ids
                ),
                kill_switch_active=state.kill_switch_active,
            )
        if state.kill_switch_active:
            return ReadinessContext(
                account="",
                market_exists=True,
                observed_live_mark_price=rule.id in state.live_mark_observed_rule_ids,
                kill_switch_available=True,
                kill_switch_active=True,
                dry_run_events_count=1,
                confirmation_phrase="",
            )
        return None
