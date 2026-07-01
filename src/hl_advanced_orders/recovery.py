from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from .audit import AuditEvent, JsonlAuditLog, redact_payload
from .hyperliquid_client import PositionSnapshot
from .models import ExecutionMode, LiveEnablementStatus, RuleStatus
from .storage import LocalStateStore, StorageError


class ReconciliationAccount(Protocol):
    def get_positions(self) -> list[PositionSnapshot]:
        pass


def validate_state(store: LocalStateStore) -> tuple[bool, str]:
    try:
        store.load()
    except StorageError as exc:
        return False, str(exc)
    return True, "state is valid"


def manual_review_rule_ids(store: LocalStateStore) -> list[str]:
    state = store.load()
    return [
        rule.id
        for rule in state.rules.values()
        if rule.live_status == LiveEnablementStatus.MANUAL_REVIEW and rule.status == RuleStatus.ACTIVE
    ]


def diagnostics_payload(store: LocalStateStore, audit_path: Path, *, recent_events: int = 20) -> dict[str, Any]:
    state = store.load()
    events = _read_recent_events(audit_path, recent_events)
    return redact_payload(
        {
            "state_schema_version": 2,
            "kill_switch_active": state.kill_switch_active,
            "health": {
                "mode": state.health.mode,
                "last_tick_started_at": state.health.last_tick_started_at,
                "last_tick_completed_at": state.health.last_tick_completed_at,
                "consecutive_failures": state.health.consecutive_failures,
                "active_error": state.health.active_error,
            },
            "rules": [
                {
                    "id": rule.id,
                    "coin": rule.coin,
                    "side": rule.side.value,
                    "execution_mode": rule.execution_mode.value,
                    "status": rule.status.value,
                    "live_status": rule.live_status.value,
                    "triggered": state.rule_states[rule.id].triggered,
                    "protected_size": str(state.rule_states[rule.id].protected_size),
                }
                for rule in state.rules.values()
            ],
            "recent_events": [
                {
                    "event_type": event.get("event_type"),
                    "rule_id": event.get("rule_id"),
                    "created_at": event.get("created_at"),
                    "payload": event.get("payload", {}),
                }
                for event in events
            ],
        }
    )


def reset_triggered_rule(
    *,
    store: LocalStateStore,
    audit: JsonlAuditLog,
    account: ReconciliationAccount,
    rule_id: str,
    reason: str,
) -> None:
    if not reason.strip():
        raise ValueError("reset reason is required")
    state = store.load()
    rule = state.rules[rule_id]
    runtime = state.rule_states[rule_id]
    positions = account.get_positions()
    matching_position = next(
        (
            position
            for position in positions
            if position.coin == rule.coin and position.side == rule.side and position.size > 0
        ),
        None,
    )
    if matching_position is None:
        raise ValueError("cannot reset without matching current account position")

    previous_live_status = rule.live_status
    runtime.triggered = False
    runtime.protected_size = min(rule.size, matching_position.size)
    if rule.execution_mode == ExecutionMode.AUTO_SUBMIT:
        updated = replace(rule, live_status=LiveEnablementStatus.CANARY_PENDING)
        state.rules[rule_id] = updated
        runtime.rule = updated
    store.save_preserving_active_kill_switch(state)
    audit.append(
        AuditEvent.create(
            "rule_trigger_reset",
            "Reset triggered rule after account reconciliation.",
            rule_id=rule_id,
            payload={
                "reason": reason,
                "previous_live_status": previous_live_status.value,
                "new_live_status": state.rules[rule_id].live_status.value,
                "protected_size": str(runtime.protected_size),
                "reconciled_position_size": str(matching_position.size),
            },
        )
    )


def _read_recent_events(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return events[-limit:]
