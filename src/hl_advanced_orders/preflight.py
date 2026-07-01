from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .hyperliquid_client import PositionSnapshot
from .models import TrailingStopRule
from .readiness import ReadinessChecker, ReadinessContext
from .secrets import SecretStore
from .storage import LocalDaemonState


class MarketMetadataGateway(Protocol):
    def get_market_metadata(self, coin: str):
        pass


class AccountSnapshotGateway(Protocol):
    def get_positions(self) -> list[PositionSnapshot]:
        pass


@dataclass(frozen=True)
class PreflightResult:
    rule_id: str
    context: ReadinessContext
    account_snapshot_available: bool
    market_metadata_source: str
    mark_observed_at: str | None
    reasons: list[str]

    @property
    def passed(self) -> bool:
        return not self.reasons


class PreflightService:
    def __init__(
        self,
        *,
        secrets: SecretStore,
        market_data: MarketMetadataGateway,
        account: AccountSnapshotGateway | None = None,
        freshness_window: timedelta = timedelta(minutes=5),
    ) -> None:
        self.secrets = secrets
        self.market_data = market_data
        self.account = account
        self.freshness_window = freshness_window

    def check_rule(
        self,
        *,
        state: LocalDaemonState,
        rule: TrailingStopRule,
        account_name: str,
        dry_run_events_count: int,
        confirmation_phrase: str,
        now: datetime | None = None,
    ) -> PreflightResult:
        observed_at = state.live_mark_observed_at_by_rule.get(rule.id)
        observed_live_mark_price = self._mark_is_fresh(observed_at, now or datetime.now(timezone.utc))
        market_metadata = self.market_data.get_market_metadata(rule.coin)
        account_snapshot_available = self._account_snapshot_available()
        context = ReadinessContext(
            account=account_name,
            market_exists=bool(market_metadata.exists),
            observed_live_mark_price=observed_live_mark_price,
            kill_switch_available=True,
            kill_switch_active=state.kill_switch_active,
            dry_run_events_count=dry_run_events_count,
            confirmation_phrase=confirmation_phrase,
        )
        reasons = ReadinessChecker(self.secrets).check_mainnet_auto_submit(rule, context).reasons
        if not account_snapshot_available:
            reasons.append("account snapshot is not available")
        return PreflightResult(
            rule_id=rule.id,
            context=context,
            account_snapshot_available=account_snapshot_available,
            market_metadata_source=str(market_metadata.source),
            mark_observed_at=observed_at,
            reasons=reasons,
        )

    def _mark_is_fresh(self, observed_at: str | None, now: datetime) -> bool:
        if observed_at is None:
            return False
        try:
            parsed = datetime.fromisoformat(observed_at)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return now - parsed <= self.freshness_window

    def _account_snapshot_available(self) -> bool:
        if self.account is None:
            return True
        try:
            self.account.get_positions()
        except Exception:
            return False
        return True
