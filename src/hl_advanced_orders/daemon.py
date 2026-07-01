from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from time import sleep as default_sleep
from typing import Protocol

from .audit import AuditEvent, JsonlAuditLog
from .hyperliquid_client import FillEvent, PositionSnapshot
from .models import (
    ExecutionMode,
    LiveEnablementStatus,
    PositionSide,
    PriceSource,
    PriceTick,
    RuleStatus,
    TrailingStopRule,
)
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
StopPredicate = Callable[[], bool]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class DaemonTickResult:
    active_rules_count: int
    account_snapshot_succeeded: bool
    market_snapshot_succeeded: bool
    failure_messages: list[str]
    blocked_reasons: list[str]

    @property
    def succeeded(self) -> bool:
        return self.account_snapshot_succeeded and self.market_snapshot_succeeded


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

    def run_once(self) -> DaemonTickResult:
        state = self.store.load()
        active_rules_count = sum(1 for rule in state.rules.values() if rule.status == RuleStatus.ACTIVE)
        market_snapshot_succeeded = True
        failure_messages: list[str] = []
        blocked_reasons: list[str] = []
        try:
            positions = self.account.get_positions()
            fills = self.account.get_fills()
        except Exception as exc:
            failure_messages.append(str(exc))
            self.audit.append(
                AuditEvent.create(
                    "account_snapshot_failed",
                    "Failed to load account snapshot.",
                    payload={"error": str(exc)},
                )
            )
            return DaemonTickResult(
                active_rules_count=active_rules_count,
                account_snapshot_succeeded=False,
                market_snapshot_succeeded=False,
                failure_messages=failure_messages,
                blocked_reasons=blocked_reasons,
            )

        for rule in list(state.rules.values()):
            if rule.status == RuleStatus.DISABLED:
                continue
            runtime = state.ensure_rule_state(rule)
            self._update_protected_size(state, runtime, positions, fills)
            try:
                tick = self.market_data.get_mark_price(rule.coin)
            except Exception as exc:
                market_snapshot_succeeded = False
                failure_messages.append(str(exc))
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
            state.live_mark_observed_at_by_rule[rule.id] = tick.observed_at.isoformat()
            triggered = self.engine.observe(runtime, tick)
            if triggered is not None:
                if triggered.execution_mode == ExecutionMode.AUTO_SUBMIT:
                    self.store.save_preserving_active_kill_switch(state)
                context = self._readiness_context_for(state, rule)
                outcome = self.submission_policy.handle(triggered=triggered, rule=rule, context=context)
                if outcome == SubmissionOutcome.LIVE_BLOCKED:
                    runtime.triggered = False
                    if context is not None:
                        blocked_reasons = self._blocked_reasons(context)
                elif outcome == SubmissionOutcome.LIVE_SUBMITTED:
                    self._record_live_success(state, runtime)
                elif outcome == SubmissionOutcome.LIVE_FAILED:
                    self._record_manual_review(state, runtime)

        self.store.save_preserving_active_kill_switch(state)
        return DaemonTickResult(
            active_rules_count=active_rules_count,
            account_snapshot_succeeded=True,
            market_snapshot_succeeded=market_snapshot_succeeded,
            failure_messages=failure_messages,
            blocked_reasons=blocked_reasons,
        )

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
                observed_live_mark_price=context.observed_live_mark_price,
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

    def _record_live_success(
        self,
        state: LocalDaemonState,
        runtime: TrailingStopState,
    ) -> None:
        rule = runtime.rule
        if rule.live_status == LiveEnablementStatus.CANARY_PENDING:
            updated = replace(rule, live_status=LiveEnablementStatus.CANARY_SUCCEEDED)
            state.rules[rule.id] = updated
            runtime.rule = updated

    def _record_manual_review(
        self,
        state: LocalDaemonState,
        runtime: TrailingStopState,
    ) -> None:
        rule = runtime.rule
        if rule.execution_mode == ExecutionMode.AUTO_SUBMIT:
            updated = replace(rule, live_status=LiveEnablementStatus.MANUAL_REVIEW)
            state.rules[rule.id] = updated
            runtime.rule = updated

    def _blocked_reasons(self, context: ReadinessContext) -> list[str]:
        reasons: list[str] = []
        if context.kill_switch_active:
            reasons.append("kill switch is active")
        if not context.market_exists:
            reasons.append("market does not exist")
        if not context.observed_live_mark_price:
            reasons.append("rule has not observed live mark prices")
        if context.dry_run_events_count <= 0:
            reasons.append("rule has not produced a dry-run audit event")
        if context.confirmation_phrase == "":
            reasons.append("confirmation phrase did not match")
        return reasons


class DaemonRunner:
    def __init__(
        self,
        *,
        daemon: DaemonService,
        store: LocalStateStore,
        audit: JsonlAuditLog,
        poll_interval_seconds: float,
        max_iterations: int | None = None,
        should_stop: StopPredicate | None = None,
        sleep: SleepFn = default_sleep,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if max_iterations is not None and max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        self.daemon = daemon
        self.store = store
        self.audit = audit
        self.poll_interval_seconds = poll_interval_seconds
        self.max_iterations = max_iterations
        self.should_stop = should_stop or (lambda: False)
        self.sleep = sleep

    def run(self) -> int:
        iterations = 0
        try:
            while not self.should_stop():
                started_at = utc_now()
                self._mark_started(started_at)
                try:
                    result = self.daemon.run_once()
                except Exception as exc:
                    result = DaemonTickResult(
                        active_rules_count=0,
                        account_snapshot_succeeded=False,
                        market_snapshot_succeeded=False,
                        failure_messages=[str(exc)],
                        blocked_reasons=[],
                    )
                    self.audit.append(
                        AuditEvent.create(
                            "daemon_tick_failed",
                            "Daemon tick failed.",
                            payload={"error": str(exc)},
                        )
                    )
                self._mark_completed(started_at, result)
                iterations += 1
                if self.max_iterations is not None and iterations >= self.max_iterations:
                    break
                if self.should_stop():
                    break
                self.sleep(self.poll_interval_seconds)
        finally:
            self._mark_stopped()
        return iterations

    def _mark_started(self, started_at: str) -> None:
        state = self.store.load()
        state.health.mode = "running"
        state.health.last_tick_started_at = started_at
        self.store.save_preserving_active_kill_switch(state)

    def _mark_completed(self, started_at: str, result: DaemonTickResult) -> None:
        completed_at = utc_now()
        state = self.store.load()
        state.health.mode = "running"
        state.health.last_tick_started_at = started_at
        state.health.last_tick_completed_at = completed_at
        state.health.active_rules_count = result.active_rules_count
        state.health.last_blocked_reasons = result.blocked_reasons
        if result.account_snapshot_succeeded:
            state.health.last_successful_account_snapshot_at = completed_at
        if result.market_snapshot_succeeded:
            state.health.last_successful_market_snapshot_at = completed_at
        if result.succeeded:
            state.health.consecutive_failures = 0
            state.health.active_error = None
        else:
            state.health.consecutive_failures += 1
            state.health.active_error = "; ".join(result.failure_messages) or "daemon tick failed"
        self.store.save_preserving_active_kill_switch(state)
        self.audit.append(
            AuditEvent.create(
                "daemon_heartbeat",
                "Daemon tick completed.",
                payload={
                    "active_rules_count": result.active_rules_count,
                    "account_snapshot_succeeded": result.account_snapshot_succeeded,
                    "market_snapshot_succeeded": result.market_snapshot_succeeded,
                    "consecutive_failures": state.health.consecutive_failures,
                    "active_error": state.health.active_error,
                    "last_blocked_reasons": state.health.last_blocked_reasons,
                },
            )
        )

    def _mark_stopped(self) -> None:
        state = self.store.load()
        state.health.mode = "stopped"
        self.store.save_preserving_active_kill_switch(state)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
