from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from hl_advanced_orders.audit import JsonlAuditLog
from hl_advanced_orders.models import (
    ExecutionMode,
    ExitOrderType,
    LiveEnablementStatus,
    PositionSide,
    TrailMode,
    TrailingStopRule,
    TriggeredExit,
)
from hl_advanced_orders.readiness import MAINNET_CONFIRMATION_PHRASE, ReadinessChecker, ReadinessContext
from hl_advanced_orders.secrets import InMemorySecrets
from hl_advanced_orders.submission import SubmissionOutcome, SubmissionPolicy


class FakeExchange:
    def __init__(self, *, fail: bool = False, response: dict[str, object] | None = None) -> None:
        self.fail = fail
        self.response = response
        self.calls: list[tuple[str, Decimal]] = []

    def submit_market_close(self, coin: str, size: Decimal) -> dict[str, object]:
        self.calls.append((coin, size))
        if self.fail:
            raise RuntimeError("exchange unavailable")
        if self.response is not None:
            return self.response
        return {"status": "ok", "coin": coin, "size": str(size)}


class SubmissionPolicyTest(unittest.TestCase):
    def test_dry_run_trigger_appends_audit_and_does_not_call_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exchange = FakeExchange()
            policy = SubmissionPolicy(audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"), exchange=exchange)

            policy.handle(triggered=dry_run_exit(), rule=rule())

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(exchange.calls, [])
            self.assertEqual(events[-1]["event_type"], "dry_run_exit")
            self.assertEqual(events[-1]["payload"]["outcome"], "dry_run")

    def test_auto_submit_blocks_each_readiness_failure_and_does_not_call_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=exchange,
                readiness_checker=ReadinessChecker(secrets),
            )
            context = ReadinessContext(
                account="trader",
                market_exists=False,
                observed_live_mark_price=False,
                kill_switch_available=True,
                kill_switch_active=True,
                dry_run_events_count=0,
                confirmation_phrase="enable mainnet auto submit",
            )

            policy.handle(triggered=live_exit(), rule=rule(ExecutionMode.AUTO_SUBMIT), context=context)

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(exchange.calls, [])
            reasons = events[-1]["payload"]["reasons"]
            self.assertIn("missing private key in macOS Keychain", reasons)
            self.assertIn("market does not exist: ETH", reasons)
            self.assertIn("rule has not observed live mark prices", reasons)
            self.assertIn("kill switch is active", reasons)
            self.assertIn("rule has not produced a dry-run audit event", reasons)
            self.assertIn("confirmation phrase did not match", reasons)

    def test_exact_confirmation_phrase_allows_live_submission_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=exchange,
                readiness_checker=ReadinessChecker(secrets),
            )

            policy.handle(
                triggered=live_exit(),
                rule=rule(ExecutionMode.AUTO_SUBMIT, live_status=LiveEnablementStatus.NORMAL_LIVE),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(exchange.calls, [("ETH", Decimal("0.4"))])
            self.assertEqual(events[-1]["event_type"], "live_submission_succeeded")
            self.assertEqual(events[-1]["payload"]["exchange_response"]["status"], "ok")

    def test_live_submission_failure_records_failure_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=FakeExchange(fail=True),
                readiness_checker=ReadinessChecker(secrets),
            )

            policy.handle(
                triggered=live_exit(),
                rule=rule(ExecutionMode.AUTO_SUBMIT, live_status=LiveEnablementStatus.NORMAL_LIVE),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(events[-1]["event_type"], "live_submission_failed")
            self.assertIn("exchange unavailable", events[-1]["payload"]["error"])

    def test_rejected_exchange_response_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=FakeExchange(
                    response={
                        "status": "ok",
                        "response": {
                            "type": "order",
                            "data": {"statuses": [{"error": "Insufficient margin"}]},
                        },
                    }
                ),
                readiness_checker=ReadinessChecker(secrets),
            )

            outcome = policy.handle(
                triggered=live_exit(),
                rule=rule(ExecutionMode.AUTO_SUBMIT, live_status=LiveEnablementStatus.NORMAL_LIVE),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(outcome, SubmissionOutcome.LIVE_FAILED)
            self.assertEqual(events[-1]["event_type"], "live_submission_failed")
            self.assertEqual(events[-1]["payload"]["outcome"], "rejected")
            self.assertIn("Insufficient margin", events[-1]["payload"]["error"])

    def test_ambiguous_exchange_response_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=FakeExchange(response={"response": {"type": "order"}}),
                readiness_checker=ReadinessChecker(secrets),
            )

            outcome = policy.handle(
                triggered=live_exit(),
                rule=rule(ExecutionMode.AUTO_SUBMIT, live_status=LiveEnablementStatus.NORMAL_LIVE),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(outcome, SubmissionOutcome.LIVE_FAILED)
            self.assertIn("missing exchange status", events[-1]["payload"]["error"])

    def test_stale_mark_observation_blocks_live_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=exchange,
                readiness_checker=ReadinessChecker(secrets),
                max_mark_age=timedelta(minutes=5),
            )

            outcome = policy.handle(
                triggered=live_exit(
                    mark_observed_at=datetime.now(timezone.utc) - timedelta(minutes=10)
                ),
                rule=rule(ExecutionMode.AUTO_SUBMIT, live_status=LiveEnablementStatus.NORMAL_LIVE),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(outcome, SubmissionOutcome.LIVE_BLOCKED)
            self.assertEqual(exchange.calls, [])
            self.assertIn("mark price observation is stale", events[-1]["payload"]["reasons"])

    def test_auto_submit_without_canary_evidence_blocks_normal_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=exchange,
                readiness_checker=ReadinessChecker(secrets),
            )

            outcome = policy.handle(
                triggered=live_exit(),
                rule=rule(ExecutionMode.AUTO_SUBMIT),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(outcome, SubmissionOutcome.LIVE_BLOCKED)
            self.assertEqual(exchange.calls, [])
            self.assertIn(
                "canary evidence is required before normal live auto_submit",
                events[-1]["payload"]["reasons"],
            )

    def test_canary_mode_allows_pending_rule_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=exchange,
                readiness_checker=ReadinessChecker(secrets),
                canary_mode=True,
            )

            outcome = policy.handle(
                triggered=live_exit(),
                rule=rule(ExecutionMode.AUTO_SUBMIT),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(outcome, SubmissionOutcome.LIVE_SUBMITTED)
            self.assertEqual(exchange.calls, [("ETH", Decimal("0.4"))])
            self.assertTrue(events[-1]["payload"]["canary_mode"])
            self.assertEqual(events[-1]["payload"]["live_status"], "canary_pending")

    def test_manual_review_rule_blocks_live_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            secrets = InMemorySecrets()
            secrets.set_private_key("trader", "private-key")
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                exchange=exchange,
                readiness_checker=ReadinessChecker(secrets),
                canary_mode=True,
            )

            outcome = policy.handle(
                triggered=live_exit(),
                rule=rule(
                    ExecutionMode.AUTO_SUBMIT,
                    live_status=LiveEnablementStatus.MANUAL_REVIEW,
                ),
                context=ready_context(),
            )

            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(outcome, SubmissionOutcome.LIVE_BLOCKED)
            self.assertEqual(exchange.calls, [])
            self.assertIn("rule requires manual review", events[-1]["payload"]["reasons"])


def rule(
    execution_mode: ExecutionMode = ExecutionMode.DRY_RUN,
    *,
    live_status: LiveEnablementStatus = LiveEnablementStatus.DRY_RUN,
) -> TrailingStopRule:
    return TrailingStopRule(
        id="rule_123",
        coin="ETH",
        side=PositionSide.LONG,
        size=Decimal("1"),
        trail_mode=TrailMode.ABSOLUTE,
        trail_value=Decimal("50"),
        execution_mode=execution_mode,
        live_status=live_status,
    )


def dry_run_exit() -> TriggeredExit:
    return TriggeredExit(
        rule_id="rule_123",
        coin="ETH",
        side="sell",
        size=Decimal("0.4"),
        reason="trailing_stop_triggered",
        mark_price=Decimal("95"),
        stop_price=Decimal("100"),
        execution_mode=ExecutionMode.DRY_RUN,
        exit_order_type=ExitOrderType.MARKET,
    )


def live_exit(mark_observed_at=None) -> TriggeredExit:
    return TriggeredExit(
        rule_id="rule_123",
        coin="ETH",
        side="sell",
        size=Decimal("0.4"),
        reason="trailing_stop_triggered",
        mark_price=Decimal("95"),
        stop_price=Decimal("100"),
        execution_mode=ExecutionMode.AUTO_SUBMIT,
        exit_order_type=ExitOrderType.MARKET,
        mark_observed_at=mark_observed_at or datetime.now(timezone.utc),
    )


def ready_context() -> ReadinessContext:
    return ReadinessContext(
        account="trader",
        market_exists=True,
        observed_live_mark_price=True,
        kill_switch_available=True,
        kill_switch_active=False,
        dry_run_events_count=1,
        confirmation_phrase=MAINNET_CONFIRMATION_PHRASE,
    )


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
