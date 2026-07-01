from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from .models import (
    ExecutionMode,
    PositionSide,
    PriceTick,
    TrailMode,
    TrailingStopRule,
    TriggeredExit,
)


@dataclass
class TrailingStopState:
    rule: TrailingStopRule
    protected_size: Decimal = Decimal("0")
    favorable_price: Decimal | None = None
    stop_price: Decimal | None = None
    moving_window: deque[Decimal] = field(default_factory=deque)
    triggered: bool = False
    processed_fill_ids: set[str] = field(default_factory=set)

    def increase_protected_size(self, filled_size: Decimal) -> None:
        if filled_size <= 0:
            return
        self.protected_size = min(self.rule.size, self.protected_size + filled_size)


class TrailingStopEngine:
    def __init__(self, moving_average_window: int = 20) -> None:
        if moving_average_window <= 0:
            raise ValueError("moving_average_window must be positive")
        self.moving_average_window = moving_average_window

    def observe(self, state: TrailingStopState, tick: PriceTick) -> TriggeredExit | None:
        if tick.coin != state.rule.coin:
            return None
        if state.triggered or state.protected_size <= 0:
            return None

        signal_price = self._signal_price(state, tick.mark_price)
        if signal_price is None:
            return None

        self._update_trail(state, signal_price)
        if state.stop_price is None:
            return None

        if self._is_triggered(state.rule.side, tick.mark_price, state.stop_price):
            state.triggered = True
            return self._exit_for(state, tick)
        return None

    def _signal_price(self, state: TrailingStopState, mark_price: Decimal) -> Decimal | None:
        if state.rule.trail_mode != TrailMode.MOVING_AVERAGE:
            return mark_price

        state.moving_window.append(mark_price)
        while len(state.moving_window) > self.moving_average_window:
            state.moving_window.popleft()
        total = sum(state.moving_window, Decimal("0"))
        return total / Decimal(len(state.moving_window))

    def _update_trail(self, state: TrailingStopState, signal_price: Decimal) -> None:
        if state.favorable_price is None:
            state.favorable_price = signal_price
        elif state.rule.side == PositionSide.LONG and signal_price > state.favorable_price:
            state.favorable_price = signal_price
        elif state.rule.side == PositionSide.SHORT and signal_price < state.favorable_price:
            state.favorable_price = signal_price

        state.stop_price = self._stop_price(state.rule, state.favorable_price)

    def _stop_price(self, rule: TrailingStopRule, favorable_price: Decimal) -> Decimal:
        if rule.trail_mode == TrailMode.PERCENT:
            distance = favorable_price * (rule.trail_value / Decimal("100"))
        else:
            distance = rule.trail_value

        if rule.side == PositionSide.LONG:
            return favorable_price - distance
        return favorable_price + distance

    def _is_triggered(
        self,
        side: PositionSide,
        mark_price: Decimal,
        stop_price: Decimal,
    ) -> bool:
        if side == PositionSide.LONG:
            return mark_price <= stop_price
        return mark_price >= stop_price

    def _exit_for(self, state: TrailingStopState, tick: PriceTick) -> TriggeredExit:
        close_side = "sell" if state.rule.side == PositionSide.LONG else "buy"
        assert state.stop_price is not None
        return TriggeredExit(
            rule_id=state.rule.id,
            coin=state.rule.coin,
            side=close_side,
            size=state.protected_size,
            reason="trailing_stop_triggered",
            mark_price=tick.mark_price,
            stop_price=state.stop_price,
            execution_mode=state.rule.execution_mode or ExecutionMode.DRY_RUN,
            exit_order_type=state.rule.exit_order_type,
            mark_observed_at=tick.observed_at,
        )
