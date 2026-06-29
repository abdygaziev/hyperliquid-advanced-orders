from __future__ import annotations

from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path

import typer

from . import __version__
from .audit import AuditEvent, JsonlAuditLog
from .models import PositionSide, TrailMode, TrailingStopRule

app = typer.Typer(
    help="Local Hyperliquid advanced order daemon.",
    invoke_without_command=True,
)
rule_app = typer.Typer(help="Manage advanced order rules.")
app.add_typer(rule_app, name="rule")


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "HyperliquidAdvancedOrders"


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
    typer.echo(f"Initialized {data_dir}")


@app.command()
def run(
    dry_run: bool = typer.Option(True, help="Start without live submissions."),
) -> None:
    mode = "dry_run" if dry_run else "live-capable"
    typer.echo(f"Daemon scaffold started in {mode} mode. Live Hyperliquid loop is not wired yet.")


@rule_app.command("create-trailing")
def create_trailing_rule(
    coin: str = typer.Option(..., help="Hyperliquid market, such as ETH."),
    side: PositionSide = typer.Option(..., help="Position side to protect."),
    size: str = typer.Option(..., help="Close size chosen by the trader."),
    trail_mode: TrailMode = typer.Option(..., help="Trailing mode."),
    trail_value: str = typer.Option(..., help="Percent, absolute value, or MA offset."),
) -> None:
    rule = TrailingStopRule(
        coin=coin.upper(),
        side=side,
        size=parse_decimal(size, "size"),
        trail_mode=trail_mode,
        trail_value=parse_decimal(trail_value, "trail_value"),
    )
    audit = JsonlAuditLog(default_data_dir() / "audit.jsonl")
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
            },
        )
    )
    typer.echo(f"Created {rule.id} for {rule.coin} {rule.side.value} in dry_run")


@app.command("kill-switch")
def kill_switch() -> None:
    audit = JsonlAuditLog(default_data_dir() / "audit.jsonl")
    audit.append(AuditEvent.create("kill_switch_enabled", "Global kill switch enabled."))
    typer.echo("Kill switch enabled. Automated submissions are blocked.")
