from __future__ import annotations

from dataclasses import dataclass
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


class ObservationSource(StrEnum):
    LIVE_MARK = "live_mark"
    MID_PRICE_FALLBACK = "mid_price_fallback"


@dataclass(frozen=True)
class TrailingStopRule:
    coin: str
    side: PositionSide
    size: Decimal
    trail_mode: TrailMode
    trail_value: Decimal
    protect_existing: bool = True
    opening_order_id: str | None = None
    disabled: bool = False
    exit_order_type: ExitOrderType = ExitOrderType.MARKET
    execution_mode: ExecutionMode = ExecutionMode.DRY_RUN
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"rule_{uuid4().hex[:12]}")
        if self.size <= 0:
            raise ValueError("size must be positive")
        if self.trail_value <= 0:
            raise ValueError("trail_value must be positive")
        if self.trail_mode == TrailMode.PERCENT and self.trail_value >= 100:
            raise ValueError("percent trail_value must be less than 100")
        object.__setattr__(self, "coin", self.coin.upper())


@dataclass(frozen=True)
class PriceTick:
    coin: str
    mark_price: Decimal
    observed_at: datetime
    source: ObservationSource = ObservationSource.LIVE_MARK

    @classmethod
    def now(
        cls,
        coin: str,
        mark_price: Decimal,
        source: ObservationSource = ObservationSource.LIVE_MARK,
    ) -> "PriceTick":
        return cls(
            coin=coin.upper(),
            mark_price=mark_price,
            observed_at=datetime.now(timezone.utc),
            source=source,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", self.coin.upper())


@dataclass(frozen=True)
class ExistingPosition:
    coin: str
    side: PositionSide
    size: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", self.coin.upper())


@dataclass(frozen=True)
class FillEvent:
    coin: str
    side: PositionSide
    size: Decimal
    order_id: str | None = None
    fill_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "coin", self.coin.upper())


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
