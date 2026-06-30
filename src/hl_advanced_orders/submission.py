from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .audit import AuditEvent, JsonlAuditLog
from .hyperliquid_client import ExchangeGateway, MarketDataGateway
from .models import ExecutionMode, TriggeredExit
from .readiness import ReadinessChecker, ReadinessContext
from .storage import RuleStoreSnapshot


@dataclass(frozen=True)
class SubmissionResult:
    event: AuditEvent
    submitted: bool = False
    blocked: bool = False


class SubmissionPolicy:
    def __init__(
        self,
        *,
        audit: JsonlAuditLog,
        readiness: ReadinessChecker,
        market_data: MarketDataGateway,
        exchange: ExchangeGateway | None,
        account: str,
        confirmation_phrase: str = "",
    ) -> None:
        self.audit = audit
        self.readiness = readiness
        self.market_data = market_data
        self.exchange = exchange
        self.account = account
        self.confirmation_phrase = confirmation_phrase

    def handle(self, exit_order: TriggeredExit, snapshot: RuleStoreSnapshot) -> SubmissionResult:
        if exit_order.execution_mode == ExecutionMode.DRY_RUN:
            event = self._dry_run_event(exit_order)
            self.audit.append(event)
            return SubmissionResult(event=event)

        rule = snapshot.rules[exit_order.rule_id]
        state = snapshot.states[exit_order.rule_id]
        readiness = self.readiness.check_mainnet_auto_submit(
            rule,
            ReadinessContext(
                account=self.account,
                market_exists=self.market_data.market_exists(rule.coin),
                observed_live_mark_price=state.observed_live_mark_price,
                kill_switch_available=True,
                kill_switch_active=snapshot.kill_switch_active,
                dry_run_events_count=self.audit.count_rule_events(rule.id, "dry_run_triggered"),
                confirmation_phrase=self.confirmation_phrase,
            ),
        )
        if not readiness.passed:
            event = AuditEvent.create(
                "live_submission_blocked",
                "Live submission blocked by readiness policy.",
                rule_id=exit_order.rule_id,
                payload={**_exit_payload(exit_order), "reasons": readiness.reasons},
            )
            self.audit.append(event)
            return SubmissionResult(event=event, blocked=True)

        if self.exchange is None:
            event = AuditEvent.create(
                "live_submission_blocked",
                "Live submission blocked because exchange gateway is unavailable.",
                rule_id=exit_order.rule_id,
                payload={**_exit_payload(exit_order), "reasons": ["exchange gateway unavailable"]},
            )
            self.audit.append(event)
            return SubmissionResult(event=event, blocked=True)

        self.audit.append(
            AuditEvent.create(
                "live_submission_attempted",
                "Attempting reduce-only live exit.",
                rule_id=exit_order.rule_id,
                payload=_exit_payload(exit_order),
            )
        )
        try:
            response = self.exchange.close_position(exit_order)
        except Exception as exc:  # pragma: no cover - exercised with fakes in tests
            event = AuditEvent.create(
                "live_submission_failed",
                "Live reduce-only exit failed.",
                rule_id=exit_order.rule_id,
                payload={**_exit_payload(exit_order), "error": str(exc)},
            )
            self.audit.append(event)
            return SubmissionResult(event=event, blocked=True)

        event = AuditEvent.create(
            "live_submission_succeeded",
            "Live reduce-only exit submitted.",
            rule_id=exit_order.rule_id,
            payload={**_exit_payload(exit_order), "response": _summarize(response)},
        )
        self.audit.append(event)
        return SubmissionResult(event=event, submitted=True)

    def _dry_run_event(self, exit_order: TriggeredExit) -> AuditEvent:
        return AuditEvent.create(
            "dry_run_triggered",
            "Dry-run trailing stop triggered; no live order submitted.",
            rule_id=exit_order.rule_id,
            payload=_exit_payload(exit_order),
        )


def _exit_payload(exit_order: TriggeredExit) -> dict[str, Any]:
    return {
        "rule_id": exit_order.rule_id,
        "coin": exit_order.coin,
        "side": exit_order.side,
        "size": str(exit_order.size),
        "mark_price": str(exit_order.mark_price),
        "stop_price": str(exit_order.stop_price),
        "execution_mode": exit_order.execution_mode.value,
        "exit_order_type": exit_order.exit_order_type.value,
        "outcome": exit_order.reason,
    }


def _summarize(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key.lower() not in {"private_key", "secret"}}
