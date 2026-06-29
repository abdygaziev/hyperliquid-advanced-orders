from __future__ import annotations

from typing import Protocol

from .audit import AuditEvent, JsonlAuditLog
from .hyperliquid_client import FillEvent, PositionSnapshot
from .models import ExecutionMode, PositionSide, PriceTick, RuleStatus, TriggeredExit
from .storage import LocalDaemonState, LocalStateStore
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
    ) -> None:
        self.store = store
        self.audit = audit
        self.market_data = market_data
        self.account = account
        self.engine = engine or TrailingStopEngine()

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
                self._handle_triggered_exit(triggered, state)

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

    def _handle_triggered_exit(self, triggered: TriggeredExit, state: LocalDaemonState) -> None:
        if triggered.execution_mode == ExecutionMode.DRY_RUN:
            self.audit.append(
                AuditEvent.create(
                    "dry_run_exit",
                    "Trailing stop would submit a reduce-only exit.",
                    rule_id=triggered.rule_id,
                    payload=self._trigger_payload(triggered, outcome="dry_run"),
                )
            )
            return

        if state.kill_switch_active:
            self.audit.append(
                AuditEvent.create(
                    "live_submission_blocked",
                    "Live submission blocked by readiness policy.",
                    rule_id=triggered.rule_id,
                    payload={
                        **self._trigger_payload(triggered, outcome="blocked"),
                        "reasons": ["kill switch is active"],
                    },
                )
            )

    def _trigger_payload(self, triggered: TriggeredExit, *, outcome: str) -> dict[str, str]:
        return {
            "rule_id": triggered.rule_id,
            "coin": triggered.coin,
            "side": triggered.side,
            "size": str(triggered.size),
            "mark_price": str(triggered.mark_price),
            "stop_price": str(triggered.stop_price),
            "execution_mode": triggered.execution_mode.value,
            "exit_order_type": triggered.exit_order_type.value,
            "reason": triggered.reason,
            "outcome": outcome,
        }
