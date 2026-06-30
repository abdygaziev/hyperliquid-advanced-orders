from __future__ import annotations

from decimal import Decimal
from decimal import InvalidOperation
import os
from pathlib import Path

import typer

from . import __version__
from .audit import AuditEvent, JsonlAuditLog
from .daemon import DaemonService, EmptyAccountGateway, StaticMarketDataGateway
from .hyperliquid_client import HyperliquidConfig, build_exchange_gateway, build_info_gateway
from .models import ExecutionMode, PositionSide, TrailMode, TrailingStopRule
from .readiness import ReadinessChecker, ReadinessContext
from .secrets import KeychainSecrets
from .storage import JsonRuleStore
from .submission import SubmissionPolicy

app = typer.Typer(
    help="Local Hyperliquid advanced order daemon.",
    invoke_without_command=True,
)
rule_app = typer.Typer(help="Manage advanced order rules.")
app.add_typer(rule_app, name="rule")
secret_app = typer.Typer(help="Manage local account secrets.")
app.add_typer(secret_app, name="secret")


def default_data_dir() -> Path:
    if os.environ.get("HLAO_DATA_DIR"):
        return Path(os.environ["HLAO_DATA_DIR"])
    return Path.home() / "Library" / "Application Support" / "HyperliquidAdvancedOrders"


def _store(data_dir: Path | None = None) -> JsonRuleStore:
    root = data_dir or default_data_dir()
    return JsonRuleStore(root / "state.json")


def _audit(data_dir: Path | None = None) -> JsonlAuditLog:
    root = data_dir or default_data_dir()
    return JsonlAuditLog(root / "audit.jsonl")


def parse_decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise typer.BadParameter(f"{field_name} must be a decimal number") from exc
    if parsed <= 0:
        raise typer.BadParameter(f"{field_name} must be positive")
    return parsed


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit.",
        is_eager=True,
    ),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def init() -> None:
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _store(data_dir).save(_store(data_dir).load())
    typer.echo(f"Initialized {data_dir}")


@app.command()
def run(
    dry_run: bool = typer.Option(True, help="Start without live submissions."),
    account: str = typer.Option("", help="Hyperliquid account address for live gateways."),
    once: bool = typer.Option(False, "--once", help="Run one deterministic daemon tick."),
    mark_price: str | None = typer.Option(
        None,
        help="Offline test mark price for all stored rules; avoids network access.",
    ),
    confirmation_phrase: str = typer.Option("", help="Live auto-submit confirmation phrase."),
    base_url: str | None = typer.Option(None, help="Optional Hyperliquid API base URL."),
) -> None:
    snapshot = _store().load()
    prices = {}
    if mark_price is not None:
        price = parse_decimal(mark_price, "mark_price")
        prices = {rule.coin: price for rule in snapshot.rules.values()}
    config = HyperliquidConfig(base_url=base_url)
    offline_mode = mark_price is not None
    market_data = StaticMarketDataGateway(prices) if offline_mode else build_info_gateway(config)
    account_gateway = EmptyAccountGateway() if offline_mode else market_data
    exchange = None
    if not dry_run:
        exchange = build_exchange_gateway(account, KeychainSecrets(), config)
    audit = _audit()
    policy = SubmissionPolicy(
        audit=audit,
        readiness=ReadinessChecker(KeychainSecrets()),
        market_data=market_data,
        exchange=exchange,
        account=account,
        confirmation_phrase=confirmation_phrase,
    )
    service = DaemonService(
        store=_store(),
        audit=audit,
        market_data=market_data,
        account_gateway=account_gateway,
        submission_policy=policy,
        account=account,
    )
    ticks = service.run(max_ticks=1 if once else None)
    mode = "dry_run" if dry_run else "live-capable"
    typer.echo(f"Daemon ran {ticks} tick(s) in {mode} mode.")


@rule_app.command("create-trailing")
def create_trailing_rule(
    coin: str = typer.Option(..., help="Hyperliquid market, such as ETH."),
    side: PositionSide = typer.Option(..., help="Position side to protect."),
    size: str = typer.Option(..., help="Close size chosen by the trader."),
    trail_mode: TrailMode = typer.Option(..., help="Trailing mode."),
    trail_value: str = typer.Option(..., help="Percent, absolute value, or MA offset."),
    protect_existing: bool = typer.Option(True, help="Protect existing matching position."),
    opening_order_id: str | None = typer.Option(None, help="Attach protection to opening order id."),
    auto_submit: bool = typer.Option(False, help="Store rule in readiness-gated auto_submit mode."),
) -> None:
    rule = TrailingStopRule(
        coin=coin.upper(),
        side=side,
        size=parse_decimal(size, "size"),
        trail_mode=trail_mode,
        trail_value=parse_decimal(trail_value, "trail_value"),
        protect_existing=protect_existing,
        opening_order_id=opening_order_id,
        execution_mode=ExecutionMode.AUTO_SUBMIT if auto_submit else ExecutionMode.DRY_RUN,
    )
    _store().add_rule(rule)
    audit = _audit()
    audit.append(
        AuditEvent.create(
            "rule_created",
            "Created trailing stop rule in dry_run.",
            rule_id=rule.id,
            payload={
                "coin": rule.coin,
                "side": rule.side.value,
                "size": str(rule.size),
                "trail_mode": rule.trail_mode.value,
                "trail_value": str(rule.trail_value),
                "execution_mode": rule.execution_mode.value,
                "protect_existing": rule.protect_existing,
                "opening_order_id": rule.opening_order_id,
            },
        )
    )
    typer.echo(f"Created {rule.id} for {rule.coin} {rule.side.value} in {rule.execution_mode.value}")


@rule_app.command("list")
def list_rules() -> None:
    snapshot = _store().load()
    if not snapshot.rules:
        typer.echo("No rules configured.")
        return
    for rule in snapshot.rules.values():
        state = snapshot.states[rule.id]
        status = "disabled" if rule.disabled else "active"
        if state.triggered:
            status = f"{status}, triggered"
        typer.echo(
            " ".join(
                [
                    rule.id,
                    rule.coin,
                    rule.side.value,
                    f"size={rule.size}",
                    f"protected={state.protected_size}",
                    f"mode={rule.execution_mode.value}",
                    f"status={status}",
                ]
            )
        )


@rule_app.command("disable")
def disable_rule(rule_id: str = typer.Argument(...)) -> None:
    snapshot = _store().load()
    if rule_id not in snapshot.rules:
        raise typer.BadParameter(f"unknown rule id: {rule_id}")
    rule = snapshot.rules[rule_id]
    disabled_rule = TrailingStopRule(
        id=rule.id,
        coin=rule.coin,
        side=rule.side,
        size=rule.size,
        trail_mode=rule.trail_mode,
        trail_value=rule.trail_value,
        protect_existing=rule.protect_existing,
        opening_order_id=rule.opening_order_id,
        disabled=True,
        exit_order_type=rule.exit_order_type,
        execution_mode=rule.execution_mode,
    )
    snapshot.rules[rule_id] = disabled_rule
    _store().save(snapshot)
    _audit().append(AuditEvent.create("rule_disabled", "Rule disabled.", rule_id=rule_id))
    typer.echo(f"Disabled {rule_id}")


@rule_app.command("readiness")
def inspect_readiness(
    rule_id: str = typer.Argument(...),
    account: str = typer.Option(..., help="Hyperliquid account address."),
    confirmation_phrase: str = typer.Option("", help="Typed live-submission phrase."),
) -> None:
    snapshot = _store().load()
    if rule_id not in snapshot.rules:
        raise typer.BadParameter(f"unknown rule id: {rule_id}")
    rule = snapshot.rules[rule_id]
    state = snapshot.states[rule_id]
    market_data = StaticMarketDataGateway({rule.coin: state.stop_price or Decimal("1")})
    result = ReadinessChecker(KeychainSecrets()).check_mainnet_auto_submit(
        rule,
        ReadinessContext(
            account=account,
            market_exists=market_data.market_exists(rule.coin),
            observed_live_mark_price=state.observed_live_mark_price,
            kill_switch_available=True,
            kill_switch_active=snapshot.kill_switch_active,
            dry_run_events_count=_audit().count_rule_events(rule.id, "dry_run_triggered"),
            confirmation_phrase=confirmation_phrase,
        ),
    )
    _audit().append(
        AuditEvent.create(
            "readiness_checked",
            "Readiness inspected.",
            rule_id=rule.id,
            payload={"passed": result.passed, "reasons": result.reasons},
        )
    )
    if result.passed:
        typer.echo("Ready for auto_submit.")
        return
    typer.echo("Not ready for auto_submit:")
    for reason in result.reasons:
        typer.echo(f"- {reason}")


@secret_app.command("store-key")
def store_key(
    account: str = typer.Option(..., help="Hyperliquid account address."),
    private_key: str = typer.Option(
        ...,
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
        help="Private key stored in macOS Keychain.",
    ),
) -> None:
    KeychainSecrets().set_private_key(account, private_key)
    typer.echo(f"Stored private key for {account} in macOS Keychain.")


@secret_app.command("check-key")
def check_key(account: str = typer.Option(..., help="Hyperliquid account address.")) -> None:
    if KeychainSecrets().has_private_key(account):
        typer.echo(f"Private key is present for {account}.")
    else:
        raise typer.Exit(code=1)


@app.command("kill-switch")
def kill_switch(
    active: bool = typer.Option(True, "--active/--inactive", help="Persist kill-switch state."),
) -> None:
    _store().set_kill_switch(active)
    event_type = "kill_switch_enabled" if active else "kill_switch_disabled"
    message = (
        "Global kill switch enabled."
        if active
        else "Global kill switch disabled; readiness gates still apply."
    )
    _audit().append(AuditEvent.create(event_type, message))
    typer.echo(
        "Kill switch enabled. Automated submissions are blocked."
        if active
        else "Kill switch disabled. Readiness gates still apply."
    )
