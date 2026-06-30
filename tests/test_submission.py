from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from hl_advanced_orders.audit import AuditEvent, JsonlAuditLog
from hl_advanced_orders.models import (
    ExecutionMode,
    ExitOrderType,
    PositionSide,
    TrailMode,
    TrailingStopRule,
    TriggeredExit,
)
from hl_advanced_orders.readiness import MAINNET_CONFIRMATION_PHRASE, ReadinessChecker
from hl_advanced_orders.storage import RuleStoreSnapshot, StoredRuleState
from hl_advanced_orders.submission import SubmissionPolicy


class FakeSecrets:
    def __init__(self, present: bool) -> None:
        self.present = present

    def has_private_key(self, account: str) -> bool:
        return self.present


class FakeMarket:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists

    def latest_price(self, coin: str):
        return None

    def market_exists(self, coin: str) -> bool:
        return self.exists


class FakeExchange:
    def __init__(self) -> None:
        self.calls = 0

    def close_position(self, exit_order: TriggeredExit):
        self.calls += 1
        return {"status": "ok"}


class SubmissionPolicyTest(unittest.TestCase):
    def test_dry_run_writes_audit_without_exchange_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = JsonlAuditLog(Path(tmp) / "audit.jsonl")
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=audit,
                readiness=ReadinessChecker(FakeSecrets(True)),
                market_data=FakeMarket(),
                exchange=exchange,
                account="0xabc",
            )

            policy.handle(_exit(ExecutionMode.DRY_RUN), _snapshot(ExecutionMode.DRY_RUN))

            self.assertEqual(exchange.calls, 0)
            self.assertEqual(audit.events()[0].event_type, "dry_run_triggered")

    def test_auto_submit_blocks_all_readiness_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = JsonlAuditLog(Path(tmp) / "audit.jsonl")
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=audit,
                readiness=ReadinessChecker(FakeSecrets(False)),
                market_data=FakeMarket(False),
                exchange=exchange,
                account="0xabc",
                confirmation_phrase="wrong",
            )

            result = policy.handle(_exit(ExecutionMode.AUTO_SUBMIT), _snapshot(ExecutionMode.AUTO_SUBMIT))

            self.assertTrue(result.blocked)
            self.assertEqual(exchange.calls, 0)
            reasons = audit.events()[0].payload["reasons"]
            self.assertIn("missing private key in macOS Keychain", reasons)
            self.assertIn("confirmation phrase did not match", reasons)

    def test_auto_submit_calls_exchange_when_all_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = JsonlAuditLog(Path(tmp) / "audit.jsonl")
            audit.append(AuditEvent.create("dry_run_triggered", "prior dry run", rule_id="rule_1"))
            exchange = FakeExchange()
            policy = SubmissionPolicy(
                audit=audit,
                readiness=ReadinessChecker(FakeSecrets(True)),
                market_data=FakeMarket(True),
                exchange=exchange,
                account="0xabc",
                confirmation_phrase=MAINNET_CONFIRMATION_PHRASE,
            )

            result = policy.handle(_exit(ExecutionMode.AUTO_SUBMIT), _snapshot(ExecutionMode.AUTO_SUBMIT, True))

            self.assertTrue(result.submitted)
            self.assertEqual(exchange.calls, 1)
            self.assertEqual(audit.events()[-1].event_type, "live_submission_succeeded")


def _rule(mode: ExecutionMode) -> TrailingStopRule:
    return TrailingStopRule(
        id="rule_1",
        coin="ETH",
        side=PositionSide.LONG,
        size=Decimal("1"),
        trail_mode=TrailMode.ABSOLUTE,
        trail_value=Decimal("10"),
        execution_mode=mode,
    )


def _snapshot(mode: ExecutionMode, observed: bool = False) -> RuleStoreSnapshot:
    rule = _rule(mode)
    return RuleStoreSnapshot(
        rules={rule.id: rule},
        states={rule.id: StoredRuleState(protected_size=Decimal("1"), observed_live_mark_price=observed)},
    )


def _exit(mode: ExecutionMode) -> TriggeredExit:
    return TriggeredExit(
        rule_id="rule_1",
        coin="ETH",
        side="sell",
        size=Decimal("1"),
        reason="trailing_stop_triggered",
        mark_price=Decimal("90"),
        stop_price=Decimal("95"),
        execution_mode=mode,
        exit_order_type=ExitOrderType.MARKET,
    )


if __name__ == "__main__":
    unittest.main()
