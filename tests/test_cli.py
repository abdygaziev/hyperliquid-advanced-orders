from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from hl_advanced_orders.audit import AuditEvent, JsonlAuditLog
from hl_advanced_orders.cli import app
from hl_advanced_orders.hyperliquid_client import MarketMetadata, PositionSnapshot
from hl_advanced_orders.models import LiveEnablementStatus, PositionSide, PriceTick
from hl_advanced_orders.storage import LocalStateStore


class MissingMarketData:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url

    def get_market_metadata(self, coin: str) -> MarketMetadata:
        return MarketMetadata(coin=coin.upper(), exists=False, source="hyperliquid_metadata")


class CliWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def invoke(self, args: list[str], input_text: str | None = None):
        return self.runner.invoke(
            app,
            ["--data-dir", str(self.data_dir), *args],
            input=input_text,
        )

    def test_create_trailing_rule_stores_dry_run_rule_and_audit_event(self) -> None:
        result = self.invoke(
            [
                "rule",
                "create-trailing",
                "--coin",
                "eth",
                "--side",
                "long",
                "--size",
                "1",
                "--trail-mode",
                "percent",
                "--trail-value",
                "5",
            ]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("dry_run", result.output)
        rule_id = result.output.split()[1]
        state = json.loads((self.data_dir / "state.json").read_text(encoding="utf-8"))
        events = read_events(self.data_dir / "audit.jsonl")
        self.assertEqual(state["rules"][0]["execution_mode"], "dry_run")
        self.assertEqual(state["rules"][0]["live_status"], "dry_run")
        self.assertEqual(state["rules"][0]["id"], rule_id)
        self.assertEqual(events[-1]["event_type"], "rule_created")

    def test_invalid_percent_trail_value_returns_parameter_error(self) -> None:
        result = self.invoke(
            [
                "rule",
                "create-trailing",
                "--coin",
                "ETH",
                "--side",
                "long",
                "--size",
                "1",
                "--trail-mode",
                "percent",
                "--trail-value",
                "100",
            ]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("percent trail_value must be less than 100", result.output)

    def test_list_rules_shows_mode_side_sizes_and_status(self) -> None:
        self.test_create_trailing_rule_stores_dry_run_rule_and_audit_event()

        result = self.invoke(["rule", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ETH", result.output)
        self.assertIn("long", result.output)
        self.assertIn("dry_run", result.output)
        self.assertIn("live_status=dry_run", result.output)
        self.assertIn("protected=0", result.output)
        self.assertIn("active", result.output)

    def test_readiness_prints_all_failing_reasons(self) -> None:
        create_result = self.invoke(
            [
                "rule",
                "create-trailing",
                "--coin",
                "ETH",
                "--side",
                "long",
                "--size",
                "1",
                "--trail-mode",
                "absolute",
                "--trail-value",
                "50",
                "--execution-mode",
                "auto_submit",
            ]
        )
        rule_id = create_result.output.split()[1]

        with patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", MissingMarketData):
            result = self.invoke(["readiness", rule_id, "--account", "trader"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("market verification: hyperliquid_metadata", result.output)
        self.assertIn("missing private key in macOS Keychain", result.output)
        self.assertIn("market does not exist: ETH", result.output)
        self.assertIn("rule has not observed live mark prices", result.output)
        self.assertIn("rule has not produced a dry-run audit event", result.output)
        self.assertIn("confirmation phrase did not match", result.output)

    def test_kill_switch_enable_persists_and_blocks_readiness(self) -> None:
        create_result = self.invoke(
            [
                "rule",
                "create-trailing",
                "--coin",
                "ETH",
                "--side",
                "long",
                "--size",
                "1",
                "--trail-mode",
                "absolute",
                "--trail-value",
                "50",
            ]
        )
        rule_id = create_result.output.split()[1]

        switch_result = self.invoke(["kill-switch", "--enable"])
        with patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", MissingMarketData):
            readiness_result = self.invoke(["readiness", rule_id, "--account", "trader"])

        self.assertEqual(switch_result.exit_code, 0, switch_result.output)
        self.assertIn("enabled", switch_result.output)
        self.assertIn("kill switch is active", readiness_result.output)

    def test_secret_storage_does_not_echo_private_key_material(self) -> None:
        class FakeSecrets:
            saved: tuple[str, str] | None = None

            def set_private_key(self, account: str, private_key: str) -> None:
                FakeSecrets.saved = (account, private_key)

        with patch("hl_advanced_orders.cli.KeychainSecrets", FakeSecrets):
            result = self.invoke(["secret", "store-key", "--account", "trader"], "super-secret\n")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(FakeSecrets.saved, ("trader", "super-secret"))
        self.assertNotIn("super-secret", result.output)
        self.assertFalse((self.data_dir / "state.json").exists())

    def test_secret_key_cannot_be_supplied_as_command_line_option(self) -> None:
        help_result = self.invoke(["secret", "store-key", "--help"])
        option_result = self.invoke(
            ["secret", "store-key", "--account", "trader", "--private-key", "super-secret"]
        )

        self.assertEqual(help_result.exit_code, 0, help_result.output)
        self.assertNotIn("--private-key", help_result.output)
        self.assertNotEqual(option_result.exit_code, 0)
        self.assertNotIn("super-secret", option_result.output)

    def test_module_help_matches_console_entrypoint_surface(self) -> None:
        result = run_module_cli(["--help"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local Hyperliquid advanced order daemon.", result.stdout)
        for command in ["init", "run", "readiness", "kill-switch", "rule", "secret"]:
            self.assertIn(command, result.stdout)

    def test_module_version_prints_package_version(self) -> None:
        result = run_module_cli(["--version"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^\d+\.\d+\.\d+$")

    def test_importing_cli_module_does_not_execute_app_or_create_state(self) -> None:
        script = "import hl_advanced_orders.cli; print('imported')"

        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            env=pythonpath_env(),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "imported")
        self.assertFalse((self.data_dir / "state.json").exists())

    def test_run_once_executes_daemon_tick_with_live_gateways(self) -> None:
        calls: list[str] = []

        class FakeMarketData:
            info = object()

            def __init__(self, base_url: str | None = None) -> None:
                calls.append(f"market:{base_url}")

        class FakeAccountGateway:
            def __init__(self, info: object, address: str) -> None:
                calls.append(f"account:{address}:{info is FakeMarketData.info}")

        class FakeDaemon:
            def __init__(self, **kwargs: object) -> None:
                calls.append("daemon:init")

            def run_once(self) -> None:
                calls.append("daemon:run_once")

        with (
            patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", FakeMarketData),
            patch("hl_advanced_orders.cli.HyperliquidAccountGateway", FakeAccountGateway),
            patch("hl_advanced_orders.cli.DaemonService", FakeDaemon),
        ):
            result = self.invoke(
                ["run", "--once", "--account-address", "0xabc", "--base-url", "http://example"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Completed one daemon tick.", result.output)
        self.assertEqual(
            calls,
            ["market:http://example", "account:0xabc:True", "daemon:init", "daemon:run_once"],
        )

    def test_run_continuous_uses_runner_and_max_iterations(self) -> None:
        calls: list[str] = []

        class FakeMarketData:
            info = object()

            def __init__(self, base_url: str | None = None) -> None:
                calls.append(f"market:{base_url}")

        class FakeAccountGateway:
            def __init__(self, info: object, address: str) -> None:
                calls.append(f"account:{address}:{info is FakeMarketData.info}")

        class FakeDaemon:
            def __init__(self, **kwargs: object) -> None:
                calls.append("daemon:init")

        class FakeRunner:
            def __init__(self, **kwargs: object) -> None:
                calls.append(f"runner:interval={kwargs['poll_interval_seconds']}")
                calls.append(f"runner:max={kwargs['max_iterations']}")

            def run(self) -> int:
                calls.append("runner:run")
                return 2

        with (
            patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", FakeMarketData),
            patch("hl_advanced_orders.cli.HyperliquidAccountGateway", FakeAccountGateway),
            patch("hl_advanced_orders.cli.DaemonService", FakeDaemon),
            patch("hl_advanced_orders.cli.DaemonRunner", FakeRunner),
        ):
            result = self.invoke(
                [
                    "run",
                    "--account-address",
                    "0xabc",
                    "--poll-interval-seconds",
                    "0.5",
                    "--max-iterations",
                    "2",
                ]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Daemon stopped after 2 ticks.", result.output)
        self.assertEqual(
            calls,
            [
                "market:None",
                "account:0xabc:True",
                "daemon:init",
                "runner:interval=0.5",
                "runner:max=2",
                "runner:run",
            ],
        )

    def test_run_continuous_rejects_non_positive_poll_interval(self) -> None:
        result = self.invoke(["run", "--account-address", "0xabc", "--poll-interval-seconds", "0"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--poll-interval-seconds must be positive", result.output)

    def test_health_command_prints_persisted_health_state(self) -> None:
        store = LocalStateStore(self.data_dir / "state.json")
        state = store.load()
        state.health.mode = "running"
        state.health.active_rules_count = 2
        state.health.last_tick_started_at = "2026-06-30T10:00:00+00:00"
        state.health.last_tick_completed_at = "2026-06-30T10:00:01+00:00"
        state.health.last_successful_account_snapshot_at = "2026-06-30T10:00:01+00:00"
        state.health.last_successful_market_snapshot_at = "2026-06-30T10:00:01+00:00"
        state.health.consecutive_failures = 1
        state.health.active_error = "market unavailable"
        state.health.last_blocked_reasons = ["kill switch is active"]
        store.save(state)

        result = self.invoke(["health"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("mode=running", result.output)
        self.assertIn("active_rules=2", result.output)
        self.assertIn("last_tick_started_at=2026-06-30T10:00:00+00:00", result.output)
        self.assertIn("consecutive_failures=1", result.output)
        self.assertIn("active_error=market unavailable", result.output)
        self.assertIn("last_blocked_reasons=kill switch is active", result.output)

    def test_run_once_rejects_auto_submit_without_live_options(self) -> None:
        self.create_auto_submit_rule()

        result = self.invoke(["run", "--once", "--account-address", "0xabc"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("auto_submit rules require", result.output)
        self.assertIn("--keychain-account", result.output)
        self.assertIn("--wallet-address", result.output)
        self.assertNotIn("--market-exists", result.output)

    def test_run_once_wires_live_submission_for_auto_submit_rules(self) -> None:
        self.create_auto_submit_rule()
        calls: list[str] = []

        class FakeMarketData:
            info = object()

            def __init__(self, base_url: str | None = None) -> None:
                calls.append(f"market:{base_url}")

            def get_market_metadata(self, coin: str):
                return MarketMetadata(coin=coin.upper(), exists=True, source="fake")

        class FakeAccountGateway:
            def __init__(self, info: object, address: str) -> None:
                calls.append(f"account:{address}:{info is FakeMarketData.info}")

        class FakeExchangeGateway:
            @classmethod
            def from_keychain(cls, **kwargs: object) -> object:
                calls.append(
                    f"exchange:{kwargs['account']}:{kwargs['wallet_address']}:{kwargs['base_url']}"
                )
                return object()

        class FakeDaemon:
            def __init__(self, **kwargs: object) -> None:
                calls.append(f"policy:{kwargs['submission_policy'] is not None}")
                calls.append(f"context:{kwargs['readiness_context_factory'] is not None}")

            def run_once(self) -> None:
                calls.append("daemon:run_once")

        with (
            patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", FakeMarketData),
            patch("hl_advanced_orders.cli.HyperliquidAccountGateway", FakeAccountGateway),
            patch("hl_advanced_orders.cli.HyperliquidExchangeGateway", FakeExchangeGateway),
            patch("hl_advanced_orders.cli.DaemonService", FakeDaemon),
        ):
            result = self.invoke(
                [
                    "run",
                    "--once",
                    "--account-address",
                    "0xabc",
                    "--keychain-account",
                    "trader",
                    "--wallet-address",
                    "0xwallet",
                    "--confirmation-phrase",
                    "enable mainnet auto submit",
                    "--canary",
                    "--base-url",
                    "http://example",
                ]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Canary mode target=http://example", result.output)
        self.assertEqual(
            calls,
            [
                "market:http://example",
                "account:0xabc:True",
                "policy:True",
                "context:True",
                "daemon:run_once",
            ],
        )

    def test_promote_live_requires_canary_succeeded_status(self) -> None:
        rule_id = self.create_auto_submit_rule()

        blocked = self.invoke(["rule", "promote-live", rule_id])

        self.assertNotEqual(blocked.exit_code, 0)
        self.assertIn("canary_succeeded", blocked.output)

        store = LocalStateStore(self.data_dir / "state.json")
        state = store.load()
        rule = state.rules[rule_id]
        promoted_candidate = rule.__class__(
            id=rule.id,
            coin=rule.coin,
            side=rule.side,
            size=rule.size,
            trail_mode=rule.trail_mode,
            trail_value=rule.trail_value,
            exit_order_type=rule.exit_order_type,
            execution_mode=rule.execution_mode,
            status=rule.status,
            live_status=LiveEnablementStatus.CANARY_SUCCEEDED,
            attached_order_id=rule.attached_order_id,
        )
        state.rules[rule_id] = promoted_candidate
        state.rule_states[rule_id].rule = promoted_candidate
        store.save(state)

        result = self.invoke(["rule", "promote-live", rule_id])
        loaded = store.load()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("normal_live", result.output)
        self.assertEqual(loaded.rules[rule_id].live_status, LiveEnablementStatus.NORMAL_LIVE)

    def test_manual_review_listing_and_diagnostics_are_redacted(self) -> None:
        rule_id = self.create_auto_submit_rule()
        store = LocalStateStore(self.data_dir / "state.json")
        state = store.load()
        rule = state.rules[rule_id]
        updated = rule.__class__(
            id=rule.id,
            coin=rule.coin,
            side=rule.side,
            size=rule.size,
            trail_mode=rule.trail_mode,
            trail_value=rule.trail_value,
            exit_order_type=rule.exit_order_type,
            execution_mode=rule.execution_mode,
            status=rule.status,
            live_status=LiveEnablementStatus.MANUAL_REVIEW,
            attached_order_id=rule.attached_order_id,
        )
        state.rules[rule_id] = updated
        state.rule_states[rule_id].rule = updated
        store.save(state)
        JsonlAuditLog(self.data_dir / "audit.jsonl").append(
            AuditEvent.create("debug", "Debug.", payload={"private_key": "super-secret"})
        )

        review_result = self.invoke(["rule", "manual-review"])
        diagnostics_result = self.invoke(["diagnostics"])

        self.assertEqual(review_result.exit_code, 0, review_result.output)
        self.assertIn(rule_id, review_result.output)
        self.assertEqual(diagnostics_result.exit_code, 0, diagnostics_result.output)
        self.assertIn("[REDACTED]", diagnostics_result.output)
        self.assertNotIn("super-secret", diagnostics_result.output)

    def test_state_validate_reports_malformed_state(self) -> None:
        (self.data_dir / "state.json").write_text("{not-json", encoding="utf-8")

        result = self.invoke(["state-validate"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("failed to load local state", result.output)

    def test_reset_triggered_rule_uses_account_reconciliation(self) -> None:
        rule_id = self.create_auto_submit_rule()
        store = LocalStateStore(self.data_dir / "state.json")
        state = store.load()
        rule = state.rules[rule_id]
        updated = rule.__class__(
            id=rule.id,
            coin=rule.coin,
            side=rule.side,
            size=rule.size,
            trail_mode=rule.trail_mode,
            trail_value=rule.trail_value,
            exit_order_type=rule.exit_order_type,
            execution_mode=rule.execution_mode,
            status=rule.status,
            live_status=LiveEnablementStatus.MANUAL_REVIEW,
            attached_order_id=rule.attached_order_id,
        )
        state.rules[rule_id] = updated
        state.rule_states[rule_id].rule = updated
        state.rule_states[rule_id].triggered = True
        store.save(state)

        class FakeMarketData:
            info = object()

            def __init__(self, base_url: str | None = None) -> None:
                pass

        class FakeAccountGateway:
            def __init__(self, info: object, address: str) -> None:
                pass

            def get_positions(self):
                return [PositionSnapshot("ETH", PositionSide.LONG, Decimal("0.5"))]

        with (
            patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", FakeMarketData),
            patch("hl_advanced_orders.cli.HyperliquidAccountGateway", FakeAccountGateway),
        ):
            result = self.invoke(
                [
                    "rule",
                    "reset-triggered",
                    rule_id,
                    "--reason",
                    "operator reviewed exchange fill",
                    "--account-address",
                    "0xabc",
                ]
            )

        loaded = store.load()
        events = read_events(self.data_dir / "audit.jsonl")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(loaded.rule_states[rule_id].triggered)
        self.assertEqual(loaded.rules[rule_id].live_status, LiveEnablementStatus.CANARY_PENDING)
        self.assertEqual(events[-1]["event_type"], "rule_trigger_reset")

    def test_emergency_cancel_invokes_gateway_and_audits(self) -> None:
        calls: list[int | None] = []

        class FakeExchangeGateway:
            @classmethod
            def from_keychain(cls, **kwargs: object):
                return cls()

            def schedule_cancel(self, time_ms: int | None):
                calls.append(time_ms)
                return {"status": "ok", "time": time_ms}

        with patch("hl_advanced_orders.cli.HyperliquidExchangeGateway", FakeExchangeGateway):
            result = self.invoke(
                [
                    "emergency-cancel",
                    "--keychain-account",
                    "trader",
                    "--wallet-address",
                    "0xwallet",
                    "--time-ms",
                    "123456",
                ]
            )

        events = read_events(self.data_dir / "audit.jsonl")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls, [123456])
        self.assertEqual(events[-1]["event_type"], "emergency_cancel_scheduled")
        self.assertEqual(events[-1]["payload"]["time_ms"], 123456)

    def test_run_once_missing_key_is_blocked_without_constructing_exchange(self) -> None:
        rule_id = self.create_auto_submit_rule()
        store = LocalStateStore(self.data_dir / "state.json")
        state = store.load()
        runtime = state.rule_states[rule_id]
        runtime.protected_size = Decimal("1")
        runtime.favorable_price = Decimal("100")
        runtime.stop_price = Decimal("95")
        store.save(state)
        JsonlAuditLog(self.data_dir / "audit.jsonl").append(
            AuditEvent.create("dry_run_exit", "Dry run.", rule_id=rule_id)
        )

        class FakeSecrets:
            def has_private_key(self, account: str) -> bool:
                return False

            def get_private_key(self, account: str) -> str | None:
                return None

        class FakeMarketData:
            info = object()

            def __init__(self, base_url: str | None = None) -> None:
                pass

            def get_mark_price(self, coin: str):
                return PriceTick.now(coin.upper(), Decimal("49"))

            def get_market_metadata(self, coin: str):
                return MarketMetadata(coin=coin.upper(), exists=True, source="fake")

        class FakeAccountGateway:
            def __init__(self, info: object, address: str) -> None:
                pass

            def get_positions(self):
                return [PositionSnapshot("ETH", PositionSide.LONG, Decimal("1"))]

            def get_fills(self):
                return []

        with (
            patch("hl_advanced_orders.cli.KeychainSecrets", FakeSecrets),
            patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", FakeMarketData),
            patch("hl_advanced_orders.cli.HyperliquidAccountGateway", FakeAccountGateway),
            patch(
                "hl_advanced_orders.cli.HyperliquidExchangeGateway.from_keychain",
                side_effect=AssertionError("exchange should not be constructed"),
            ),
        ):
            result = self.invoke(
                [
                    "run",
                    "--once",
                    "--account-address",
                    "0xabc",
                    "--keychain-account",
                    "trader",
                    "--wallet-address",
                    "0xwallet",
                    "--confirmation-phrase",
                    "ENABLE MAINNET AUTO SUBMIT",
                    "--market-exists",
                ]
            )

        events = read_events(self.data_dir / "audit.jsonl")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(events[-1]["event_type"], "live_submission_blocked")
        self.assertIn("missing private key in macOS Keychain", events[-1]["payload"]["reasons"])

    def test_preflight_prints_all_failing_reasons_for_active_rules(self) -> None:
        rule_id = self.create_auto_submit_rule()

        class FakeMarketData:
            def __init__(self, base_url: str | None = None) -> None:
                pass

            def get_market_metadata(self, coin: str):
                return MarketMetadata(coin=coin.upper(), exists=False, source="fake_metadata")

        class FakeSecrets:
            def has_private_key(self, account: str) -> bool:
                return False

        with (
            patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", FakeMarketData),
            patch("hl_advanced_orders.cli.KeychainSecrets", FakeSecrets),
        ):
            result = self.invoke(["preflight", "--account", "trader"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"{rule_id} ETH blocked market_source=fake_metadata", result.output)
        self.assertIn("missing private key in macOS Keychain", result.output)
        self.assertIn("market does not exist: ETH", result.output)
        self.assertIn("rule has not observed live mark prices", result.output)
        self.assertIn("rule has not produced a dry-run audit event", result.output)
        self.assertIn("confirmation phrase did not match", result.output)

    def create_auto_submit_rule(self) -> str:
        result = self.invoke(
            [
                "rule",
                "create-trailing",
                "--coin",
                "ETH",
                "--side",
                "long",
                "--size",
                "1",
                "--trail-mode",
                "absolute",
                "--trail-value",
                "50",
                "--execution-mode",
                "auto_submit",
            ]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output.split()[1]


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_module_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hl_advanced_orders.cli", *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        env=pythonpath_env(),
    )


def pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing else f"{src_path}{os.pathsep}{existing}"
    return env


if __name__ == "__main__":
    unittest.main()
