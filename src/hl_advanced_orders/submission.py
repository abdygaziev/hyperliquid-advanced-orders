from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from .audit import AuditEvent, JsonlAuditLog
from .models import ExecutionMode, TrailingStopRule, TriggeredExit
from .readiness import ReadinessChecker, ReadinessContext

EXCHANGE_ERROR_STATUSES = frozenset({"error", "err", "rejected", "failed", "failure"})


class ExchangeGateway(Protocol):
    def submit_market_close(self, coin: str, size: Decimal) -> dict[str, Any]:
        pass


class SubmissionOutcome(StrEnum):
    DRY_RUN_RECORDED = "dry_run_recorded"
    LIVE_BLOCKED = "live_blocked"
    LIVE_FAILED = "live_failed"
    LIVE_SUBMITTED = "live_submitted"


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
    ) -> SubmissionOutcome:
        if triggered.execution_mode == ExecutionMode.DRY_RUN:
            self.audit.append(
                AuditEvent.create(
                    "dry_run_exit",
                    "Trailing stop would submit a reduce-only exit.",
                    rule_id=triggered.rule_id,
                    payload=trigger_payload(triggered, outcome="dry_run"),
                )
            )
            return SubmissionOutcome.DRY_RUN_RECORDED

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
            return SubmissionOutcome.LIVE_BLOCKED

        assert self.exchange is not None
        try:
            self.audit.append(
                AuditEvent.create(
                    "live_submission_attempted",
                    "Live submission attempted.",
                    rule_id=triggered.rule_id,
                    payload=trigger_payload(triggered, outcome="attempted"),
                )
            )
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
            return SubmissionOutcome.LIVE_FAILED

        response_error = exchange_response_error(response)
        if response_error is not None:
            self.audit.append(
                AuditEvent.create(
                    "live_submission_failed",
                    "Live submission was rejected.",
                    rule_id=triggered.rule_id,
                    payload={
                        **trigger_payload(triggered, outcome="rejected"),
                        "error": response_error,
                        "exchange_response": response,
                    },
                )
            )
            return SubmissionOutcome.LIVE_FAILED

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
        return SubmissionOutcome.LIVE_SUBMITTED

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


def exchange_response_error(response: dict[str, Any]) -> str | None:
    status = str(response.get("status", "")).lower()
    if _is_error_status(status):
        return str(response.get("response") or response.get("message") or status)

    nested_error = _find_response_error(response)
    if nested_error is not None:
        return nested_error

    if status and status != "ok":
        return f"unexpected exchange status: {status}"
    return None


def _find_response_error(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "error" and item:
                return str(item)
            if lowered == "status" and _is_error_status(str(item).lower()):
                return str(item)
            nested = _find_response_error(item)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _find_response_error(item)
            if nested is not None:
                return nested
    return None


def _is_error_status(status: str) -> bool:
    return status in EXCHANGE_ERROR_STATUSES
