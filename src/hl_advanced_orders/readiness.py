from __future__ import annotations

from dataclasses import dataclass

from .models import TrailingStopRule
from .secrets import SecretStore


MAINNET_CONFIRMATION_PHRASE = "ENABLE MAINNET AUTO SUBMIT"


@dataclass(frozen=True)
class ReadinessResult:
    passed: bool
    reasons: list[str]


@dataclass(frozen=True)
class ReadinessContext:
    account: str
    market_exists: bool
    observed_live_mark_price: bool
    kill_switch_available: bool
    kill_switch_active: bool
    dry_run_events_count: int
    confirmation_phrase: str


class ReadinessChecker:
    def __init__(self, secrets: SecretStore) -> None:
        self.secrets = secrets

    def check_mainnet_auto_submit(
        self,
        rule: TrailingStopRule,
        context: ReadinessContext,
    ) -> ReadinessResult:
        reasons: list[str] = []
        if not self.secrets.has_private_key(context.account):
            reasons.append("missing private key in macOS Keychain")
        if not context.market_exists:
            reasons.append(f"market does not exist: {rule.coin}")
        if not context.observed_live_mark_price:
            reasons.append("rule has not observed live mark prices")
        if not context.kill_switch_available:
            reasons.append("kill switch is not available")
        if context.kill_switch_active:
            reasons.append("kill switch is active")
        if context.dry_run_events_count <= 0:
            reasons.append("rule has not produced a dry-run audit event")
        if context.confirmation_phrase != MAINNET_CONFIRMATION_PHRASE:
            reasons.append("confirmation phrase did not match")
        return ReadinessResult(passed=not reasons, reasons=reasons)
