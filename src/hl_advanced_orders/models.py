from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import uuid4


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class TrailMode(StrEnum):
    PERCENT = "percent"
    ABSOLUTE = "absolute"
    MOVING_AVERAGE = "moving_average"


class ExitOrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class ExecutionMode(StrEnum):
    DRY_RUN = "dry_run"
    AUTO_SUBMIT = "auto_submit"


class PriceSource(StrEnum):
    MARK = "mark"
    MID = "mid"


class RuleStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class LiveEnablementStatus(StrEnum):
    DRY_RUN = "dry_run"
    CANARY_PENDING = "canary_pending"
    CANARY_SUCCEEDED = "canary_succeeded"
    NORMAL_LIVE = "normal_live"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class TrailingStopRule:
    coin: str
    side: PositionSide
    size: Decimal
    trail_mode: TrailMode
    trail_value: Decimal
    exit_order_type: ExitOrderType = ExitOrderType.MARKET
    execution_mode: ExecutionMode = ExecutionMode.DRY_RUN
    status: RuleStatus = RuleStatus.ACTIVE
    live_status: LiveEnablementStatus = LiveEnablementStatus.DRY_RUN
    attached_order_id: str | None = None
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"rule_{uuid4().hex[:12]}")
        if (
            self.execution_mode == ExecutionMode.AUTO_SUBMIT
            and self.live_status == LiveEnablementStatus.DRY_RUN
        ):
            object.__setattr__(self, "live_status", LiveEnablementStatus.CANARY_PENDING)
        if self.execution_mode == ExecutionMode.DRY_RUN:
            object.__setattr__(self, "live_status", LiveEnablementStatus.DRY_RUN)
        if self.size <= 0:
            raise ValueError("size must be positive")
        if self.trail_value <= 0:
            raise ValueError("trail_value must be positive")
        if self.trail_mode == TrailMode.PERCENT and self.trail_value >= 100:
            raise ValueError("percent trail_value must be less than 100")


@dataclass(frozen=True)
class PriceTick:
    coin: str
    mark_price: Decimal
    observed_at: datetime
    source: PriceSource = PriceSource.MARK

    @classmethod
    def now(
        cls,
        coin: str,
        mark_price: Decimal,
        source: PriceSource = PriceSource.MARK,
    ) -> "PriceTick":
        return cls(
            coin=coin,
            mark_price=mark_price,
            observed_at=datetime.now(timezone.utc),
            source=source,
        )


@dataclass(frozen=True)
class TriggeredExit:
    rule_id: str
    coin: str
    side: Literal["sell", "buy"]
    size: Decimal
    reason: str
    mark_price: Decimal
    stop_price: Decimal
    execution_mode: ExecutionMode
    exit_order_type: ExitOrderType
    mark_observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
