from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from .audit import AuditEvent, JsonlAuditLog
from .models import ExecutionMode, TrailingStopRule, TriggeredExit
from .readiness import ReadinessChecker, ReadinessContext


class ExchangeGateway(Protocol):
    def submit_market_close(self, coin: str, size: Decimal) -> dict[str, Any]:
        pass


class SubmissionPolicy:
    def __init__(
        self,
        *,
        audit: JsonlAuditLog,
        exchange: ExchangeGateway | None = None,
        readiness_checker: ReadinessChecker | None = None,
    ) -> None:
        self.audit = audit
        self.exchange = exchange
        self.readiness_checker = readiness_checker

    def handle(
        self,
        *,
        triggered: TriggeredExit,
        rule: TrailingStopRule,
        context: ReadinessContext | None = None,
    ) -> None:
        if triggered.execution_mode == ExecutionMode.DRY_RUN:
            self.audit.append(
                AuditEvent.create(
                    "dry_run_exit",
                    "Trailing stop would submit a reduce-only exit.",
                    rule_id=triggered.rule_id,
                    payload=trigger_payload(triggered, outcome="dry_run"),
                )
            )
            return

        reasons = self._blocked_reasons(rule, context)
        if reasons:
            self.audit.append(
                AuditEvent.create(
                    "live_submission_blocked",
                    "Live submission blocked by readiness policy.",
                    rule_id=triggered.rule_id,
                    payload={**trigger_payload(triggered, outcome="blocked"), "reasons": reasons},
                )
            )
            return

        assert self.exchange is not None
        try:
            response = self.exchange.submit_market_close(triggered.coin, triggered.size)
        except Exception as exc:
            self.audit.append(
                AuditEvent.create(
                    "live_submission_failed",
                    "Live submission failed.",
                    rule_id=triggered.rule_id,
                    payload={**trigger_payload(triggered, outcome="failed"), "error": str(exc)},
                )
            )
            return

        self.audit.append(
            AuditEvent.create(
                "live_submission_succeeded",
                "Live submission succeeded.",
                rule_id=triggered.rule_id,
                payload={
                    **trigger_payload(triggered, outcome="submitted"),
                    "exchange_response": response,
                },
            )
        )

    def _blocked_reasons(
        self,
        rule: TrailingStopRule,
        context: ReadinessContext | None,
    ) -> list[str]:
        reasons: list[str] = []
        if self.exchange is None:
            reasons.append("exchange gateway is not configured")
        if self.readiness_checker is None:
            reasons.append("readiness checker is not configured")
        if context is None:
            reasons.append("readiness context is not available")
        elif context.kill_switch_active:
            reasons.append("kill switch is active")
        if self.readiness_checker is not None and context is not None:
            result = self.readiness_checker.check_mainnet_auto_submit(rule, context)
            for reason in result.reasons:
                if reason not in reasons:
                    reasons.append(reason)
        return reasons


def trigger_payload(triggered: TriggeredExit, *, outcome: str) -> dict[str, Any]:
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
