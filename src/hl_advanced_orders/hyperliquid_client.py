from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .models import PositionSide, PriceTick
from .secrets import SecretStore


class MissingPrivateKeyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PositionSnapshot:
    coin: str
    side: PositionSide
    size: Decimal


@dataclass(frozen=True)
class FillEvent:
    coin: str
    side: PositionSide
    order_id: str
    size: Decimal


class InfoClient(Protocol):
    def all_mids(self) -> dict[str, Any]:
        pass

    def user_state(self, address: str) -> dict[str, Any]:
        pass

    def user_fills(self, address: str) -> list[dict[str, Any]]:
        pass


class ExchangeClient(Protocol):
    def market_close(self, coin: str, sz: Decimal) -> dict[str, Any]:
        pass


class HyperliquidMarketDataGateway:
    def __init__(self, info: InfoClient | None = None, *, base_url: str | None = None) -> None:
        self.info = info if info is not None else self._default_info(base_url)

    def get_mark_price(self, coin: str) -> PriceTick:
        normalized_coin = coin.upper()
        mids = self.info.all_mids()
        if normalized_coin not in mids:
            raise ValueError(f"missing mark price for {normalized_coin}")
        return PriceTick.now(normalized_coin, Decimal(str(mids[normalized_coin])))

    def _default_info(self, base_url: str | None) -> InfoClient:
        try:
            from hyperliquid.info import Info
        except ImportError as exc:
            raise RuntimeError("hyperliquid-python-sdk is required for live market data") from exc
        return Info(base_url=base_url, skip_ws=True) if base_url else Info(skip_ws=True)


class HyperliquidAccountGateway:
    def __init__(self, info: InfoClient, address: str) -> None:
        self.info = info
        self.address = address

    def get_positions(self) -> list[PositionSnapshot]:
        payload = self.info.user_state(self.address)
        positions: list[PositionSnapshot] = []
        for item in payload.get("assetPositions", []):
            raw_position = item.get("position", item)
            coin = str(raw_position["coin"]).upper()
            signed_size = Decimal(str(raw_position.get("szi", "0")))
            if signed_size == 0:
                continue
            side = PositionSide.LONG if signed_size > 0 else PositionSide.SHORT
            positions.append(PositionSnapshot(coin=coin, side=side, size=abs(signed_size)))
        return positions

    def get_fills(self) -> list[FillEvent]:
        fills: list[FillEvent] = []
        for item in self.info.user_fills(self.address):
            fills.append(
                FillEvent(
                    coin=str(item["coin"]).upper(),
                    side=self._fill_side(item),
                    order_id=str(item.get("oid", item.get("order_id", ""))),
                    size=Decimal(str(item.get("sz", item.get("size", "0")))),
                )
            )
        return fills

    def _fill_side(self, item: dict[str, Any]) -> PositionSide:
        raw_side = str(item.get("side", "")).lower()
        if raw_side in {"b", "buy", "long"}:
            return PositionSide.LONG
        if raw_side in {"a", "sell", "short"}:
            return PositionSide.SHORT
        raise ValueError(f"unsupported fill side: {item.get('side')}")


class HyperliquidExchangeGateway:
    def __init__(self, exchange: ExchangeClient) -> None:
        self.exchange = exchange

    @classmethod
    def from_keychain(
        cls,
        *,
        account: str,
        wallet_address: str,
        secrets: SecretStore,
        base_url: str | None = None,
    ) -> "HyperliquidExchangeGateway":
        private_key = secrets.get_private_key(account)
        if private_key is None:
            raise MissingPrivateKeyError(f"missing private key for account: {account}")
        exchange = cls._build_exchange(private_key, wallet_address, base_url)
        return cls(exchange=exchange)

    def submit_market_close(self, coin: str, size: Decimal) -> dict[str, Any]:
        return self.exchange.market_close(coin.upper(), size)

    @staticmethod
    def _build_exchange(
        private_key: str,
        wallet_address: str,
        base_url: str | None,
    ) -> ExchangeClient:
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except ImportError as exc:
            raise RuntimeError(
                "hyperliquid-python-sdk and eth-account are required for live submission"
            ) from exc

        wallet = Account.from_key(private_key)
        kwargs: dict[str, Any] = {"account_address": wallet_address}
        if base_url is not None:
            kwargs["base_url"] = base_url
        return Exchange(wallet, **kwargs)
