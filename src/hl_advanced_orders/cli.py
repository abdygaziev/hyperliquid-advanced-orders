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
from .models import (
    ExecutionMode,
    LiveEnablementStatus,
    PositionSide,
    RuleStatus,
    TrailMode,
    TrailingStopRule,
)
from .preflight import PreflightService
from .readiness import ReadinessChecker, ReadinessContext
from .recovery import diagnostics_payload, manual_review_rule_ids, reset_triggered_rule, validate_state
from .secrets import KeychainSecrets, SecretStore
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

    def schedule_cancel(self, time_ms: int | None) -> dict[str, Any]:
        if self.gateway is None:
            self.gateway = HyperliquidExchangeGateway.from_keychain(
                account=self.account,
                wallet_address=self.wallet_address,
                secrets=self.secrets,
                base_url=self.base_url,
            )
        return self.gateway.schedule_cancel(time_ms)


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
    poll_interval_seconds: float = typer.Option(
        5.0,
        "--poll-interval-seconds",
        "--interval-seconds",
        help="Seconds between daemon ticks in continuous mode.",
    ),
    max_iterations: int | None = typer.Option(
        None,
        "--max-iterations",
        "--max-ticks",
        help="Stop after this many ticks; primarily useful for supervised smoke tests.",
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
    canary: bool = typer.Option(
        False,
        "--canary",
        help="Run pending live rules as canary submissions before normal live promotion.",
    ),
    market_exists: bool = typer.Option(
        False,
        "--market-exists",
        help="Deprecated manual hint; live readiness verifies markets through Hyperliquid metadata.",
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional Hyperliquid API URL."),
) -> None:
    if poll_interval_seconds <= 0:
        raise typer.BadParameter("--poll-interval-seconds must be positive")
    if max_iterations is not None and max_iterations <= 0:
        raise typer.BadParameter("--max-iterations must be positive")
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
    account = HyperliquidAccountGateway(info=market_data.info, address=account_address)
    live_setup = configure_live_run(
        ctx=ctx,
        audit=audit,
        has_auto_submit=has_auto_submit,
        keychain_account=keychain_account,
        wallet_address=wallet_address,
        confirmation_phrase=confirmation_phrase,
        market_exists=market_exists,
        base_url=base_url,
        market_data=market_data,
        account=account,
        canary_mode=canary,
    )
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
        if canary:
            typer.echo(f"Canary mode target={base_url or 'default-mainnet'}")
        return
    iterations = DaemonRunner(
        daemon=daemon,
        store=store,
        audit=audit,
        poll_interval_seconds=poll_interval_seconds,
        max_iterations=max_iterations,
    ).run()
    typer.echo(f"Daemon stopped after {iterations} tick{'s' if iterations != 1 else ''}.")
    if canary:
        typer.echo(f"Canary mode target={base_url or 'default-mainnet'}")


@app.command()
def health(ctx: typer.Context) -> None:
    state = state_store(ctx).load()
    health_state = state.health
    typer.echo(f"mode={health_state.mode}")
    typer.echo(f"active_rules={health_state.active_rules_count}")
    typer.echo(f"last_tick_started_at={health_state.last_tick_started_at}")
    typer.echo(f"last_tick_completed_at={health_state.last_tick_completed_at}")
    typer.echo(f"last_successful_account_snapshot_at={health_state.last_successful_account_snapshot_at}")
    typer.echo(f"last_successful_market_snapshot_at={health_state.last_successful_market_snapshot_at}")
    typer.echo(f"consecutive_failures={health_state.consecutive_failures}")
    typer.echo(f"active_error={health_state.active_error}")
    if health_state.last_blocked_reasons:
        typer.echo("last_blocked_reasons=" + "; ".join(health_state.last_blocked_reasons))


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
                "live_status": rule.live_status.value,
                "attached_order_id": rule.attached_order_id,
            },
        )
    )
    typer.echo(
        f"Created {rule.id} for {rule.coin} {rule.side.value} "
        f"in {rule.execution_mode.value} live_status={rule.live_status.value}"
    )


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
                    f"live_status={rule.live_status.value}",
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


@rule_app.command("promote-live")
def promote_rule_live(ctx: typer.Context, rule_id: str) -> None:
    store = state_store(ctx)
    state = store.load()
    rule = require_rule(state.rules, rule_id)
    if rule.live_status != LiveEnablementStatus.CANARY_SUCCEEDED:
        raise typer.BadParameter("rule must have canary_succeeded live_status before promotion")
    promoted = replace(rule, live_status=LiveEnablementStatus.NORMAL_LIVE)
    state.rules[rule_id] = promoted
    state.rule_states[rule_id].rule = promoted
    store.save(state)
    audit_log(ctx).append(
        AuditEvent.create(
            "rule_live_promoted",
            "Promoted rule to normal live auto_submit.",
            rule_id=rule_id,
            payload={"live_status": promoted.live_status.value},
        )
    )
    typer.echo(f"Promoted {rule_id} to normal_live")


@rule_app.command("manual-review")
def list_manual_review_rules(ctx: typer.Context) -> None:
    rule_ids = manual_review_rule_ids(state_store(ctx))
    if not rule_ids:
        typer.echo("No manual-review rules.")
        return
    for rule_id in rule_ids:
        typer.echo(rule_id)


@rule_app.command("reset-triggered")
def reset_triggered(
    ctx: typer.Context,
    rule_id: str,
    reason: str = typer.Option(..., "--reason", help="Operator reason for the reset."),
    account_address: str = typer.Option(..., "--account-address", help="Hyperliquid wallet address."),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional Hyperliquid API URL."),
) -> None:
    market_data = HyperliquidMarketDataGateway(base_url=base_url)
    account = HyperliquidAccountGateway(info=market_data.info, address=account_address)
    try:
        reset_triggered_rule(
            store=state_store(ctx),
            audit=audit_log(ctx),
            account=account,
            rule_id=rule_id,
            reason=reason,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Reset {rule_id}")


@app.command("state-validate")
def state_validate(ctx: typer.Context) -> None:
    valid, message = validate_state(state_store(ctx))
    typer.echo(message)
    if not valid:
        raise typer.Exit(1)


@app.command("diagnostics")
def diagnostics(ctx: typer.Context) -> None:
    payload = diagnostics_payload(state_store(ctx), data_dir(ctx) / "audit.jsonl")
    typer.echo(json.dumps(payload, sort_keys=True, indent=2))


@app.command("emergency-cancel")
def emergency_cancel(
    ctx: typer.Context,
    keychain_account: str = typer.Option(..., "--keychain-account", help="Keychain account name."),
    wallet_address: str = typer.Option(..., "--wallet-address", help="Hyperliquid wallet address."),
    time_ms: int | None = typer.Option(
        None,
        "--time-ms",
        help="UTC millis for scheduled cancel; omit to cancel a pending schedule.",
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional Hyperliquid API URL."),
) -> None:
    gateway = LazyHyperliquidExchangeGateway(
        account=keychain_account,
        wallet_address=wallet_address,
        secrets=KeychainSecrets(),
        base_url=base_url,
    )
    response = gateway.schedule_cancel(time_ms)
    audit_log(ctx).append(
        AuditEvent.create(
            "emergency_cancel_scheduled",
            "Operator invoked exchange emergency cancel.",
            payload={"time_ms": time_ms, "exchange_response": response},
        )
    )
    typer.echo("Emergency cancel request submitted.")


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
    market_metadata = market_data.get_market_metadata(rule.coin)
    context = build_readiness_context(
        ctx=ctx,
        state=state,
        rule=rule,
        account=account,
        market_exists=bool(market_metadata.exists),
        confirmation_phrase=confirmation_phrase,
        market_verification=str(market_metadata.source),
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
                "manual_market_hint_ignored": market_exists,
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


@app.command()
def preflight(
    ctx: typer.Context,
    account: str = typer.Option(..., help="Keychain account name."),
    account_address: str | None = typer.Option(
        None,
        "--account-address",
        help="Hyperliquid wallet address for account snapshot validation.",
    ),
    confirmation_phrase: str = typer.Option("", help="Mainnet auto-submit confirmation phrase."),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional Hyperliquid API URL."),
) -> None:
    typer.echo("preflight: read-only; no orders will be submitted")
    state = state_store(ctx).load()
    if not state.rules:
        typer.echo("No rules.")
        return
    market_data = HyperliquidMarketDataGateway(base_url=base_url)
    account_gateway = (
        HyperliquidAccountGateway(info=market_data.info, address=account_address)
        if account_address is not None
        else None
    )
    service = PreflightService(
        secrets=KeychainSecrets(),
        market_data=market_data,
        account=account_gateway,
    )
    failed = False
    for rule in state.rules.values():
        result = service.check_rule(
            state=state,
            rule=rule,
            account_name=account,
            dry_run_events_count=count_rule_events(ctx, rule.id, "dry_run_exit"),
            confirmation_phrase=confirmation_phrase,
        )
        status = "ready" if result.passed else "blocked"
        typer.echo(
            f"{rule.id} {rule.coin} {status} market_source={result.market_metadata_source} "
            f"mark_observed_at={result.mark_observed_at}"
        )
        for reason in result.reasons:
            typer.echo(f"{rule.id}: {reason}")
        failed = failed or not result.passed
    if failed:
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
    market_exists: bool,
    base_url: str | None,
    market_data: Any,
    account: Any,
    canary_mode: bool,
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
        canary_mode=canary_mode,
    )

    def readiness_context_factory(current_state, rule: TrailingStopRule) -> ReadinessContext:
        return PreflightService(
            secrets=secrets,
            market_data=market_data,
            account=account,
        ).check_rule(
            state=current_state,
            rule=rule,
            account_name=keychain_account,
            dry_run_events_count=count_rule_events(ctx, rule.id, "dry_run_exit"),
            confirmation_phrase=confirmation_phrase,
        ).context

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
            "auto_submit rules require " + ", ".join(missing_options) + " with --once"
        )


def build_readiness_context(
    *,
    ctx: typer.Context,
    state: Any,
    rule: TrailingStopRule,
    account: str,
    market_exists: bool,
    confirmation_phrase: str,
    market_verification: str = "unknown",
) -> ReadinessContext:
    return ReadinessContext(
        account=account,
        market_exists=market_exists,
        observed_live_mark_price=rule.id in state.live_mark_observed_rule_ids,
        kill_switch_available=True,
        kill_switch_active=state.kill_switch_active,
        dry_run_events_count=count_rule_events(ctx, rule.id, "dry_run_exit"),
        confirmation_phrase=confirmation_phrase,
        market_verification=market_verification,
    )


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
