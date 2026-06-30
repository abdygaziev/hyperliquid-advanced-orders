from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .models import ExistingPosition, FillEvent, ObservationSource, PositionSide, PriceTick, TriggeredExit
from .secrets import KeychainSecrets


class MarketDataGateway(Protocol):
    def latest_price(self, coin: str) -> PriceTick | None:
        pass

    def market_exists(self, coin: str) -> bool:
        pass


class AccountGateway(Protocol):
    def positions(self, account: str) -> list[ExistingPosition]:
        pass

    def fills(self, account: str) -> list[FillEvent]:
        pass


class ExchangeGateway(Protocol):
    def close_position(self, exit_order: TriggeredExit) -> dict[str, Any]:
        pass


@dataclass(frozen=True)
class HyperliquidConfig:
    base_url: str | None = None
    skip_ws: bool = True


class HyperliquidInfoGateway:
    def __init__(self, info: Any) -> None:
        self.info = info

    def latest_price(self, coin: str) -> PriceTick | None:
        coin = coin.upper()
        if hasattr(self.info, "meta_and_asset_ctxs"):
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            tick = _tick_from_meta_and_asset_ctxs(coin, meta_and_ctxs)
            if tick is not None:
                return tick
        if hasattr(self.info, "all_mids"):
            mids = self.info.all_mids()
            if coin in mids:
                return PriceTick.now(
                    coin,
                    Decimal(str(mids[coin])),
                    source=ObservationSource.MID_PRICE_FALLBACK,
                )
        return None

    def market_exists(self, coin: str) -> bool:
        coin = coin.upper()
        if hasattr(self.info, "meta"):
            meta = self.info.meta()
            return any(asset.get("name", "").upper() == coin for asset in meta.get("universe", []))
        return self.latest_price(coin) is not None

    def positions(self, account: str) -> list[ExistingPosition]:
        state = self.info.user_state(account)
        positions: list[ExistingPosition] = []
        for entry in state.get("assetPositions", []):
            position = entry.get("position", entry)
            size = Decimal(str(position.get("szi", "0")))
            if size == 0:
                continue
            positions.append(
                ExistingPosition(
                    coin=position.get("coin", ""),
                    side=PositionSide.LONG if size > 0 else PositionSide.SHORT,
                    size=abs(size),
                )
            )
        return positions

    def fills(self, account: str) -> list[FillEvent]:
        fills: list[FillEvent] = []
        for raw in self.info.user_fills(account):
            size = Decimal(str(raw.get("sz", "0")))
            if size <= 0:
                continue
            side = str(raw.get("side", "")).lower()
            fills.append(
                FillEvent(
                    coin=raw.get("coin", ""),
                    side=PositionSide.LONG if side in {"buy", "b"} else PositionSide.SHORT,
                    size=size,
                    order_id=str(raw.get("oid")) if raw.get("oid") is not None else None,
                    fill_id=_fill_id(raw),
                )
            )
        return fills


class HyperliquidExchangeGateway:
    def __init__(self, exchange: Any) -> None:
        self.exchange = exchange

    def close_position(self, exit_order: TriggeredExit) -> dict[str, Any]:
        response = self.exchange.market_close(exit_order.coin, float(exit_order.size))
        return response if isinstance(response, dict) else {"response": response}


def build_exchange_gateway(account: str, secrets: KeychainSecrets, config: HyperliquidConfig) -> ExchangeGateway:
    private_key = secrets.get_private_key(account)
    if private_key is None:
        raise RuntimeError("missing private key in macOS Keychain")
    try:
        from eth_account import Account  # type: ignore[import-not-found]
        from hyperliquid.exchange import Exchange  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("hyperliquid exchange dependencies are not installed") from exc
    wallet = Account.from_key(private_key)
    return HyperliquidExchangeGateway(Exchange(wallet, base_url=config.base_url))


def build_info_gateway(config: HyperliquidConfig) -> HyperliquidInfoGateway:
    try:
        from hyperliquid.info import Info  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("hyperliquid info dependency is not installed") from exc
    return HyperliquidInfoGateway(Info(base_url=config.base_url, skip_ws=config.skip_ws))


def _tick_from_meta_and_asset_ctxs(coin: str, payload: Any) -> PriceTick | None:
    if not isinstance(payload, list) or len(payload) < 2:
        return None
    meta, contexts = payload[0], payload[1]
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    for asset, ctx in zip(universe, contexts, strict=False):
        name = asset.get("name", "").upper()
        if name == coin and isinstance(ctx, dict) and ctx.get("markPx") is not None:
            return PriceTick.now(coin, Decimal(str(ctx["markPx"])))
    return None


def _fill_id(raw: dict[str, Any]) -> str:
    for key in ("hash", "tid", "time", "oid"):
        if raw.get(key) is not None:
            return f"{key}:{raw[key]}"
    return f"{raw.get('coin')}:{raw.get('side')}:{raw.get('sz')}"
