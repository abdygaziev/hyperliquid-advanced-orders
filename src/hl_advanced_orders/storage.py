from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import ExecutionMode, ExitOrderType, PositionSide, TrailMode, TrailingStopRule
from .trailing import TrailingStopState


SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    pass


@dataclass
class StoredRuleState:
    protected_size: Decimal = Decimal("0")
    favorable_price: Decimal | None = None
    stop_price: Decimal | None = None
    moving_window: deque[Decimal] = field(default_factory=deque)
    triggered: bool = False
    observed_live_mark_price: bool = False
    processed_fill_ids: set[str] = field(default_factory=set)
    last_checkpoint_at: str | None = None


@dataclass
class RuleStoreSnapshot:
    rules: dict[str, TrailingStopRule] = field(default_factory=dict)
    states: dict[str, StoredRuleState] = field(default_factory=dict)
    kill_switch_active: bool = False


class JsonRuleStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RuleStoreSnapshot:
        if not self.path.exists():
            return RuleStoreSnapshot()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"Malformed state file: {self.path}") from exc
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise StorageError(f"Unsupported state schema version in {self.path}")

        rules = {
            rule_id: _rule_from_json(rule_raw)
            for rule_id, rule_raw in raw.get("rules", {}).items()
        }
        states = {
            rule_id: _state_from_json(state_raw)
            for rule_id, state_raw in raw.get("states", {}).items()
        }
        for rule_id in rules:
            states.setdefault(rule_id, StoredRuleState())
        return RuleStoreSnapshot(
            rules=rules,
            states=states,
            kill_switch_active=bool(raw.get("kill_switch_active", False)),
        )

    def save(self, snapshot: RuleStoreSnapshot) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kill_switch_active": snapshot.kill_switch_active,
            "rules": {
                rule_id: _rule_to_json(rule)
                for rule_id, rule in sorted(snapshot.rules.items())
            },
            "states": {
                rule_id: _state_to_json(state)
                for rule_id, state in sorted(snapshot.states.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    def add_rule(self, rule: TrailingStopRule) -> None:
        snapshot = self.load()
        snapshot.rules[rule.id] = rule
        snapshot.states.setdefault(rule.id, StoredRuleState())
        self.save(snapshot)

    def set_rule(self, rule: TrailingStopRule, state: StoredRuleState) -> None:
        snapshot = self.load()
        snapshot.rules[rule.id] = rule
        snapshot.states[rule.id] = state
        self.save(snapshot)

    def set_kill_switch(self, active: bool) -> None:
        snapshot = self.load()
        snapshot.kill_switch_active = active
        self.save(snapshot)


def state_from_trailing(state: TrailingStopState, *, observed_live_mark_price: bool) -> StoredRuleState:
    return StoredRuleState(
        protected_size=state.protected_size,
        favorable_price=state.favorable_price,
        stop_price=state.stop_price,
        moving_window=deque(state.moving_window),
        triggered=state.triggered,
        observed_live_mark_price=observed_live_mark_price,
        processed_fill_ids=set(state.processed_fill_ids),
        last_checkpoint_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
    )


def trailing_from_state(rule: TrailingStopRule, state: StoredRuleState) -> TrailingStopState:
    return TrailingStopState(
        rule=rule,
        protected_size=state.protected_size,
        favorable_price=state.favorable_price,
        stop_price=state.stop_price,
        moving_window=deque(state.moving_window),
        triggered=state.triggered,
        processed_fill_ids=set(state.processed_fill_ids),
    )


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _rule_to_json(rule: TrailingStopRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "coin": rule.coin,
        "side": rule.side.value,
        "size": str(rule.size),
        "trail_mode": rule.trail_mode.value,
        "trail_value": str(rule.trail_value),
        "protect_existing": rule.protect_existing,
        "opening_order_id": rule.opening_order_id,
        "disabled": rule.disabled,
        "exit_order_type": rule.exit_order_type.value,
        "execution_mode": rule.execution_mode.value,
    }


def _rule_from_json(raw: dict[str, Any]) -> TrailingStopRule:
    return TrailingStopRule(
        id=raw["id"],
        coin=raw["coin"],
        side=PositionSide(raw["side"]),
        size=_decimal(raw["size"]),
        trail_mode=TrailMode(raw["trail_mode"]),
        trail_value=_decimal(raw["trail_value"]),
        protect_existing=bool(raw.get("protect_existing", True)),
        opening_order_id=raw.get("opening_order_id"),
        disabled=bool(raw.get("disabled", False)),
        exit_order_type=ExitOrderType(raw.get("exit_order_type", ExitOrderType.MARKET.value)),
        execution_mode=ExecutionMode(raw.get("execution_mode", ExecutionMode.DRY_RUN.value)),
    )


def _state_to_json(state: StoredRuleState) -> dict[str, Any]:
    return {
        "protected_size": str(state.protected_size),
        "favorable_price": None if state.favorable_price is None else str(state.favorable_price),
        "stop_price": None if state.stop_price is None else str(state.stop_price),
        "moving_window": [str(value) for value in state.moving_window],
        "triggered": state.triggered,
        "observed_live_mark_price": state.observed_live_mark_price,
        "processed_fill_ids": sorted(state.processed_fill_ids),
        "last_checkpoint_at": state.last_checkpoint_at,
    }


def _state_from_json(raw: dict[str, Any]) -> StoredRuleState:
    return StoredRuleState(
        protected_size=_decimal(raw.get("protected_size", "0")),
        favorable_price=None
        if raw.get("favorable_price") is None
        else _decimal(raw["favorable_price"]),
        stop_price=None if raw.get("stop_price") is None else _decimal(raw["stop_price"]),
        moving_window=deque(_decimal(value) for value in raw.get("moving_window", [])),
        triggered=bool(raw.get("triggered", False)),
        observed_live_mark_price=bool(raw.get("observed_live_mark_price", False)),
        processed_fill_ids=set(raw.get("processed_fill_ids", [])),
        last_checkpoint_at=raw.get("last_checkpoint_at"),
    )
