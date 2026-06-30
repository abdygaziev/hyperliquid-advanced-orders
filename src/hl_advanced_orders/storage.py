from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import (
    ExecutionMode,
    ExitOrderType,
    PositionSide,
    RuleStatus,
    TrailMode,
    TrailingStopRule,
)
from .trailing import TrailingStopState


STATE_SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    pass


@dataclass
class LocalDaemonState:
    rules: dict[str, TrailingStopRule] = field(default_factory=dict)
    rule_states: dict[str, TrailingStopState] = field(default_factory=dict)
    kill_switch_active: bool = False
    last_fill_seen_by_order: dict[str, str] = field(default_factory=dict)
    filled_size_by_order: dict[str, Decimal] = field(default_factory=dict)
    live_mark_observed_rule_ids: set[str] = field(default_factory=set)

    def ensure_rule_state(self, rule: TrailingStopRule) -> TrailingStopState:
        self.rules[rule.id] = rule
        if rule.id not in self.rule_states:
            self.rule_states[rule.id] = TrailingStopState(rule=rule)
        return self.rule_states[rule.id]


class LocalStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> LocalDaemonState:
        if not self.path.exists():
            return LocalDaemonState()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._decode_state(raw)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"failed to load local state from {self.path}: {exc}") from exc

    def save(self, state: LocalDaemonState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._encode_state(state), sort_keys=True, indent=2)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace(temp_path)
            self._fsync_parent_dir()
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _replace(self, temp_path: Path) -> None:
        temp_path.replace(self.path)

    def _fsync_parent_dir(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save_preserving_active_kill_switch(self, state: LocalDaemonState) -> None:
        latest = self.load()
        state.kill_switch_active = state.kill_switch_active or latest.kill_switch_active
        self.save(state)

    def _encode_state(self, state: LocalDaemonState) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "kill_switch_active": state.kill_switch_active,
            "last_fill_seen_by_order": state.last_fill_seen_by_order,
            "filled_size_by_order": {
                key: str(value) for key, value in state.filled_size_by_order.items()
            },
            "live_mark_observed_rule_ids": sorted(state.live_mark_observed_rule_ids),
            "rules": [self._encode_rule(rule) for rule in state.rules.values()],
            "rule_states": [
                self._encode_rule_state(rule_id, rule_state)
                for rule_id, rule_state in state.rule_states.items()
            ],
        }

    def _decode_state(self, raw: dict[str, Any]) -> LocalDaemonState:
        schema_version = raw.get("schema_version")
        if schema_version != STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {schema_version}")

        rules = {rule.id: rule for rule in (self._decode_rule(item) for item in raw["rules"])}
        state = LocalDaemonState(
            rules=rules,
            kill_switch_active=bool(raw.get("kill_switch_active", False)),
            last_fill_seen_by_order=dict(raw.get("last_fill_seen_by_order", {})),
            filled_size_by_order={
                str(key): Decimal(str(value))
                for key, value in raw.get("filled_size_by_order", {}).items()
            },
            live_mark_observed_rule_ids=set(raw.get("live_mark_observed_rule_ids", [])),
        )
        for item in raw.get("rule_states", []):
            rule_id, rule_state = self._decode_rule_state(item, rules)
            state.rule_states[rule_id] = rule_state
        for rule in rules.values():
            state.ensure_rule_state(rule)
        return state

    def _encode_rule(self, rule: TrailingStopRule) -> dict[str, str | None]:
        return {
            "id": rule.id,
            "coin": rule.coin,
            "side": rule.side.value,
            "size": str(rule.size),
            "trail_mode": rule.trail_mode.value,
            "trail_value": str(rule.trail_value),
            "exit_order_type": rule.exit_order_type.value,
            "execution_mode": rule.execution_mode.value,
            "status": rule.status.value,
            "attached_order_id": rule.attached_order_id,
        }

    def _decode_rule(self, raw: dict[str, Any]) -> TrailingStopRule:
        return TrailingStopRule(
            id=str(raw["id"]),
            coin=str(raw["coin"]),
            side=PositionSide(str(raw["side"])),
            size=Decimal(str(raw["size"])),
            trail_mode=TrailMode(str(raw["trail_mode"])),
            trail_value=Decimal(str(raw["trail_value"])),
            exit_order_type=ExitOrderType(str(raw.get("exit_order_type", ExitOrderType.MARKET))),
            execution_mode=ExecutionMode(str(raw.get("execution_mode", ExecutionMode.DRY_RUN))),
            status=RuleStatus(str(raw.get("status", RuleStatus.ACTIVE))),
            attached_order_id=raw.get("attached_order_id"),
        )

    def _encode_rule_state(self, rule_id: str, state: TrailingStopState) -> dict[str, Any]:
        return {
            "rule_id": rule_id,
            "protected_size": str(state.protected_size),
            "favorable_price": str(state.favorable_price) if state.favorable_price is not None else None,
            "stop_price": str(state.stop_price) if state.stop_price is not None else None,
            "moving_window": [str(value) for value in state.moving_window],
            "triggered": state.triggered,
        }

    def _decode_rule_state(
        self,
        raw: dict[str, Any],
        rules: dict[str, TrailingStopRule],
    ) -> tuple[str, TrailingStopState]:
        rule_id = str(raw["rule_id"])
        rule = rules[rule_id]
        return rule_id, TrailingStopState(
            rule=rule,
            protected_size=Decimal(str(raw.get("protected_size", "0"))),
            favorable_price=self._optional_decimal(raw.get("favorable_price")),
            stop_price=self._optional_decimal(raw.get("stop_price")),
            moving_window=deque(Decimal(str(value)) for value in raw.get("moving_window", [])),
            triggered=bool(raw.get("triggered", False)),
        )

    def _optional_decimal(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))
