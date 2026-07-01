from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

import typer

from . import __version__
from .audit import AuditEvent, JsonlAuditLog
from .daemon import DaemonRunner, DaemonService, ReadinessContextFactory
from .hyperliquid_client import (
    HyperliquidAccountGateway,
    HyperliquidExchangeGateway,
    HyperliquidMarketDataGateway,
)
from .models import ExecutionMode, PositionSide, RuleStatus, TrailMode, TrailingStopRule
from .readiness import ReadinessChecker, ReadinessContext
from .secrets import InMemorySecrets, KeychainSecrets, SecretStore
from .storage import LocalStateStore
from .submission import SubmissionPolicy

app = typer.Typer(
    help="Local Hyperliquid advanced order daemon.",
    invoke_without_command=True,
)
rule_app = typer.Typer(help="Manage advanced order rules.")
secret_app = typer.Typer(help="Manage local signing secrets.")
app.add_typer(rule_app, name="rule")
app.add_typer(secret_app, name="secret")


class LiveRunSetup(NamedTuple):
    submission_policy: SubmissionPolicy | None
    readiness_context_factory: ReadinessContextFactory | None


class MarketVerification(NamedTuple):
    exists: bool
    source: str
    error: str | None = None


class LazyHyperliquidExchangeGateway:
    def __init__(
        self,
        *,
        account: str,
        wallet_address: str,
        secrets: SecretStore,
        base_url: str | None,
    ) -> None:
        self.account = account
        self.wallet_address = wallet_address
        self.secrets = secrets
        self.base_url = base_url
        self.gateway: HyperliquidExchangeGateway | None = None

    def submit_market_close(self, coin: str, size: Decimal) -> dict[str, Any]:
        if self.gateway is None:
            self.gateway = HyperliquidExchangeGateway.from_keychain(
                account=self.account,
                wallet_address=self.wallet_address,
                secrets=self.secrets,
                base_url=self.base_url,
            )
        return self.gateway.submit_market_close(coin, size)


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
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit.",
        is_eager=True,
    ),
    data_dir: Path = typer.Option(
        default_data_dir(),
        "--data-dir",
        help="Local daemon state directory.",
    ),
) -> None:
    ctx.obj = {"data_dir": data_dir}
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def init(ctx: typer.Context) -> None:
    store = state_store(ctx)
    state = store.load()
    store.save(state)
    typer.echo(f"Initialized {data_dir(ctx)}")


@app.command()
def run(
    ctx: typer.Context,
    once: bool = typer.Option(False, "--once", help="Run one bounded daemon tick."),
    interval_seconds: float = typer.Option(
        5.0,
        "--interval-seconds",
        min=0.001,
        help="Seconds between daemon ticks when running continuously.",
    ),
    max_ticks: int | None = typer.Option(
        None,
        "--max-ticks",
        min=1,
        help="Stop after this many ticks; useful for smoke tests.",
    ),
    account_address: str | None = typer.Option(
        None,
        "--account-address",
        help="Hyperliquid wallet address for account state and fills.",
    ),
    keychain_account: str | None = typer.Option(
        None,
        "--keychain-account",
        help="Keychain account name for live auto-submit signing.",
    ),
    wallet_address: str | None = typer.Option(
        None,
        "--wallet-address",
        help="Hyperliquid wallet address for live auto-submit signing.",
    ),
    confirmation_phrase: str = typer.Option(
        "",
        "--confirmation-phrase",
        help="Mainnet auto-submit confirmation phrase.",
    ),
    market_exists: bool = typer.Option(
        False,
        "--market-exists",
        help="Deprecated manual hint; live readiness verifies markets through Hyperliquid metadata.",
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional Hyperliquid API URL."),
) -> None:
    if account_address is None:
        raise typer.BadParameter("--account-address is required")
    store = state_store(ctx)
    audit = audit_log(ctx)
    state = store.load()
    has_auto_submit = any(
        rule.execution_mode == ExecutionMode.AUTO_SUBMIT and rule.status == RuleStatus.ACTIVE
        for rule in state.rules.values()
    )
    market_data = HyperliquidMarketDataGateway(base_url=base_url)
    live_setup = configure_live_run(
        ctx=ctx,
        audit=audit,
        has_auto_submit=has_auto_submit,
        keychain_account=keychain_account,
        wallet_address=wallet_address,
        confirmation_phrase=confirmation_phrase,
        manual_market_exists=market_exists,
        market_data=market_data,
        base_url=base_url,
    )
    account = HyperliquidAccountGateway(info=market_data.info, address=account_address)
    daemon = DaemonService(
        store=store,
        audit=audit,
        market_data=market_data,
        account=account,
        submission_policy=live_setup.submission_policy,
        readiness_context_factory=live_setup.readiness_context_factory,
    )
    if once:
        daemon.run_once()
        typer.echo("Completed one daemon tick.")
        return

    ticks = DaemonRunner(
        daemon=daemon,
        audit=audit,
        interval_seconds=interval_seconds,
        max_ticks=max_ticks,
    ).run()
    typer.echo(f"Daemon stopped after {ticks} tick{'s' if ticks != 1 else ''}.")


@rule_app.command("create-trailing")
def create_trailing_rule(
    ctx: typer.Context,
    coin: str = typer.Option(..., help="Hyperliquid market, such as ETH."),
    side: PositionSide = typer.Option(..., help="Position side to protect."),
    size: str = typer.Option(..., help="Close size chosen by the trader."),
    trail_mode: TrailMode = typer.Option(..., help="Trailing mode."),
    trail_value: str = typer.Option(..., help="Percent, absolute value, or MA offset."),
    execution_mode: ExecutionMode = typer.Option(
        ExecutionMode.DRY_RUN,
        help="Per-rule execution mode.",
    ),
    attached_order_id: str | None = typer.Option(
        None,
        help="Opening order identity to attach protection to.",
    ),
) -> None:
    try:
        rule = TrailingStopRule(
            coin=coin.upper(),
            side=side,
            size=parse_decimal(size, "size"),
            trail_mode=trail_mode,
            trail_value=parse_decimal(trail_value, "trail_value"),
            execution_mode=execution_mode,
            attached_order_id=attached_order_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    store = state_store(ctx)
    state = store.load()
    state.ensure_rule_state(rule)
    store.save(state)
    audit_log(ctx).append(
        AuditEvent.create(
            "rule_created",
            "Created trailing stop rule.",
            rule_id=rule.id,
            payload={
                "coin": rule.coin,
                "side": rule.side.value,
                "size": str(rule.size),
                "trail_mode": rule.trail_mode.value,
                "trail_value": str(rule.trail_value),
                "execution_mode": rule.execution_mode.value,
                "attached_order_id": rule.attached_order_id,
            },
        )
    )
    typer.echo(f"Created {rule.id} for {rule.coin} {rule.side.value} in {rule.execution_mode.value}")


@rule_app.command("list")
def list_rules(ctx: typer.Context) -> None:
    state = state_store(ctx).load()
    if not state.rules:
        typer.echo("No rules.")
        return
    for rule in state.rules.values():
        runtime = state.ensure_rule_state(rule)
        typer.echo(
            " ".join(
                [
                    rule.id,
                    rule.coin,
                    rule.side.value,
                    rule.execution_mode.value,
                    rule.status.value,
                    f"size={rule.size}",
                    f"protected={runtime.protected_size}",
                    f"triggered={runtime.triggered}",
                ]
            )
        )


@rule_app.command("disable")
def disable_rule(ctx: typer.Context, rule_id: str) -> None:
    store = state_store(ctx)
    state = store.load()
    rule = require_rule(state.rules, rule_id)
    disabled = replace(rule, status=RuleStatus.DISABLED)
    state.rules[rule_id] = disabled
    state.rule_states[rule_id].rule = disabled
    store.save(state)
    audit_log(ctx).append(
        AuditEvent.create("rule_disabled", "Disabled trailing stop rule.", rule_id=rule_id)
    )
    typer.echo(f"Disabled {rule_id}")


@app.command()
def readiness(
    ctx: typer.Context,
    rule_id: str,
    account: str = typer.Option(..., help="Keychain account name."),
    confirmation_phrase: str = typer.Option("", help="Mainnet auto-submit confirmation phrase."),
    market_exists: bool = typer.Option(
        False,
        "--market-exists",
        help="Deprecated manual hint; live readiness verifies markets through Hyperliquid metadata.",
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional Hyperliquid API URL."),
) -> None:
    state = state_store(ctx).load()
    rule = require_rule(state.rules, rule_id)
    checker = ReadinessChecker(KeychainSecrets())
    market_data = HyperliquidMarketDataGateway(base_url=base_url)
    context = build_readiness_context(
        ctx=ctx,
        state=state,
        rule=rule,
        account=account,
        market_verification=verify_rule_market(
            market_data=market_data,
            coin=rule.coin,
            manual_market_exists=market_exists,
        ),
        confirmation_phrase=confirmation_phrase,
    )
    result = checker.check_mainnet_auto_submit(rule, context)
    audit_log(ctx).append(
        AuditEvent.create(
            "readiness_checked",
            "Checked mainnet auto-submit readiness.",
            rule_id=rule_id,
            payload={
                "passed": result.passed,
                "reasons": result.reasons,
                "market_verification": context.market_verification,
                "market_verification_error": context.market_verification_error,
            },
        )
    )
    typer.echo(f"market verification: {context.market_verification}")
    if result.passed:
        typer.echo(f"{rule_id} is ready for auto_submit.")
        return
    for reason in result.reasons:
        typer.echo(reason)
    raise typer.Exit(1)


@app.command("kill-switch")
def kill_switch(
    ctx: typer.Context,
    enable: bool = typer.Option(False, "--enable", help="Enable the kill switch."),
    disable: bool = typer.Option(False, "--disable", help="Disable the kill switch."),
) -> None:
    if enable == disable:
        raise typer.BadParameter("choose exactly one of --enable or --disable")
    store = state_store(ctx)
    state = store.load()
    state.kill_switch_active = enable
    store.save(state)
    audit_log(ctx).append(
        AuditEvent.create(
            "kill_switch_enabled" if enable else "kill_switch_disabled",
            "Kill switch enabled." if enable else "Kill switch disabled.",
        )
    )
    typer.echo(f"Kill switch {'enabled' if enable else 'disabled'}.")


@app.command()
def preflight(
    ctx: typer.Context,
    coin: str | None = typer.Option(
        None,
        "--coin",
        help="Optional Hyperliquid market to verify without submitting orders.",
    ),
    rule_id: str | None = typer.Option(
        None,
        "--rule-id",
        help="Optional local rule to include in readiness preflight.",
    ),
    account_address: str | None = typer.Option(
        None,
        "--account-address",
        help="Optional wallet address for read-only account snapshot checks.",
    ),
    keychain_account: str | None = typer.Option(
        None,
        "--keychain-account",
        help="Optional Keychain account name for readiness reporting.",
    ),
    verify_keychain: bool = typer.Option(
        False,
        "--verify-keychain",
        help="Actually check macOS Keychain key presence during preflight.",
    ),
    confirmation_phrase: str = typer.Option(
        "",
        "--confirmation-phrase",
        help="Optional phrase to include in readiness reporting.",
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional Hyperliquid API URL."),
) -> None:
    typer.echo("preflight: read-only; no orders will be submitted")
    typer.echo(f"state directory: {data_dir(ctx)}")
    typer.echo(f"base url: {base_url or 'default'}")
    typer.echo("entrypoint: ok")

    store = state_store(ctx)
    state = store.load()
    rule = require_rule(state.rules, rule_id) if rule_id is not None else None
    if rule is not None and coin is not None and coin.upper() != rule.coin:
        raise typer.BadParameter("--coin must match the selected rule coin")
    target_coin = (coin or (rule.coin if rule is not None else "")).upper()
    market_data = HyperliquidMarketDataGateway(base_url=base_url)

    market_verification = MarketVerification(False, "not_checked")
    if target_coin:
        market_verification = verify_rule_market(
            market_data=market_data,
            coin=target_coin,
            manual_market_exists=False,
        )
        typer.echo(f"market {target_coin}: {market_verification.source}")
        if market_verification.error is not None:
            typer.echo(f"market error: {market_verification.error}")
        try:
            tick = market_data.get_mark_price(target_coin)
            typer.echo(f"price {target_coin}: {tick.source.value} {tick.mark_price}")
        except Exception as exc:
            typer.echo(f"price {target_coin}: unavailable ({exc})")

    if account_address is not None:
        account = HyperliquidAccountGateway(info=market_data.info, address=account_address)
        try:
            positions = account.get_positions()
            fills = account.get_fills()
            typer.echo(f"account snapshot: positions={len(positions)} fills={len(fills)}")
        except Exception as exc:
            typer.echo(f"account snapshot: unavailable ({exc})")

    if rule is not None:
        secrets = KeychainSecrets() if verify_keychain else InMemorySecrets()
        checker = ReadinessChecker(secrets)
        context = build_readiness_context(
            ctx=ctx,
            state=state,
            rule=rule,
            account=keychain_account or "",
            market_verification=market_verification,
            confirmation_phrase=confirmation_phrase,
        )
        result = checker.check_mainnet_auto_submit(rule, context)
        audit_log(ctx).append(
            AuditEvent.create(
                "preflight_checked",
                "Ran read-only preflight.",
                rule_id=rule.id,
                payload={
                    "passed": result.passed,
                    "reasons": result.reasons,
                    "market_verification": context.market_verification,
                    "read_only": True,
                    "keychain_checked": verify_keychain,
                },
            )
        )
        typer.echo(f"readiness: {'passed' if result.passed else 'blocked'}")
        for reason in result.reasons:
            typer.echo(reason)
    elif keychain_account is not None and not verify_keychain:
        typer.echo("keychain: not checked; pass --verify-keychain to check local key presence")


@secret_app.command("store-key")
def store_key(
    account: str = typer.Option(..., help="Keychain account name."),
) -> None:
    private_key = typer.prompt("Private key", hide_input=True, confirmation_prompt=False)
    try:
        KeychainSecrets().set_private_key(account, private_key)
    except ImportError as exc:
        raise typer.ClickException("keyring is not installed; cannot access macOS Keychain") from exc
    typer.echo(f"Stored private key for {account}.")


@secret_app.command("verify-key")
def verify_key(account: str = typer.Option(..., help="Keychain account name.")) -> None:
    if KeychainSecrets().has_private_key(account):
        typer.echo(f"Private key exists for {account}.")
        return
    typer.echo(f"No private key found for {account}.")
    raise typer.Exit(1)


def data_dir(ctx: typer.Context) -> Path:
    return ctx.obj["data_dir"]


def state_store(ctx: typer.Context) -> LocalStateStore:
    return LocalStateStore(data_dir(ctx) / "state.json")


def audit_log(ctx: typer.Context) -> JsonlAuditLog:
    return JsonlAuditLog(data_dir(ctx) / "audit.jsonl")


def configure_live_run(
    *,
    ctx: typer.Context,
    audit: JsonlAuditLog,
    has_auto_submit: bool,
    keychain_account: str | None,
    wallet_address: str | None,
    confirmation_phrase: str,
    manual_market_exists: bool,
    market_data: Any,
    base_url: str | None,
) -> LiveRunSetup:
    if not has_auto_submit:
        return LiveRunSetup(None, None)

    require_live_run_options(
        keychain_account=keychain_account,
        wallet_address=wallet_address,
        confirmation_phrase=confirmation_phrase,
    )
    assert keychain_account is not None
    assert wallet_address is not None

    secrets = KeychainSecrets()
    checker = ReadinessChecker(secrets)
    exchange = LazyHyperliquidExchangeGateway(
        account=keychain_account,
        wallet_address=wallet_address,
        secrets=secrets,
        base_url=base_url,
    )
    submission_policy = SubmissionPolicy(
        audit=audit,
        exchange=exchange,
        readiness_checker=checker,
    )

    def readiness_context_factory(current_state, rule: TrailingStopRule) -> ReadinessContext:
        return build_readiness_context(
            ctx=ctx,
            state=current_state,
            rule=rule,
            account=keychain_account,
            market_verification=verify_rule_market(
                market_data=market_data,
                coin=rule.coin,
                manual_market_exists=manual_market_exists,
            ),
            confirmation_phrase=confirmation_phrase,
        )

    return LiveRunSetup(submission_policy, readiness_context_factory)


def require_live_run_options(
    *,
    keychain_account: str | None,
    wallet_address: str | None,
    confirmation_phrase: str,
) -> None:
    missing_options = []
    if keychain_account is None:
        missing_options.append("--keychain-account")
    if wallet_address is None:
        missing_options.append("--wallet-address")
    if not confirmation_phrase:
        missing_options.append("--confirmation-phrase")
    if missing_options:
        raise typer.BadParameter(
            "auto_submit rules require "
            + ", ".join(missing_options)
            + " for live daemon runs"
        )


def build_readiness_context(
    *,
    ctx: typer.Context,
    state: Any,
    rule: TrailingStopRule,
    account: str,
    market_verification: MarketVerification,
    confirmation_phrase: str,
) -> ReadinessContext:
    return ReadinessContext(
        account=account,
        market_exists=market_verification.exists,
        observed_live_mark_price=rule.id in state.live_mark_observed_rule_ids,
        kill_switch_available=True,
        kill_switch_active=state.kill_switch_active,
        dry_run_events_count=count_rule_events(ctx, rule.id, "dry_run_exit"),
        confirmation_phrase=confirmation_phrase,
        market_verification=market_verification.source,
        market_verification_error=market_verification.error,
    )


def verify_rule_market(
    *,
    market_data: Any,
    coin: str,
    manual_market_exists: bool,
) -> MarketVerification:
    try:
        if market_data.market_exists(coin):
            return MarketVerification(True, "hyperliquid_metadata")
        return MarketVerification(False, "hyperliquid_metadata")
    except Exception as exc:
        if manual_market_exists:
            return MarketVerification(
                False,
                "manual_hint_ignored_after_metadata_failure",
                str(exc),
            )
        return MarketVerification(False, "hyperliquid_metadata_unavailable", str(exc))


def require_rule(rules: dict[str, TrailingStopRule], rule_id: str) -> TrailingStopRule:
    try:
        return rules[rule_id]
    except KeyError as exc:
        raise typer.BadParameter(f"unknown rule_id: {rule_id}") from exc


def count_rule_events(ctx: typer.Context, rule_id: str, event_type: str) -> int:
    path = data_dir(ctx) / "audit.jsonl"
    if not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            event: dict[str, Any] = json.loads(line)
            if event.get("rule_id") == rule_id and event.get("event_type") == event_type:
                count += 1
    return count


if __name__ == "__main__":
    app()
