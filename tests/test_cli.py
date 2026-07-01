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
from hl_advanced_orders.hyperliquid_client import PositionSnapshot
from hl_advanced_orders.models import PositionSide, PriceTick
from hl_advanced_orders.storage import LocalStateStore


class MissingMarketData:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url

    def market_exists(self, coin: str) -> bool:
        return False


class FailingMarketData:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url

    def market_exists(self, coin: str) -> bool:
        raise RuntimeError("metadata unavailable")


class PreflightInfo:
    def user_state(self, address: str) -> dict[str, object]:
        self.address = address
        return {"assetPositions": [{"position": {"coin": "ETH", "szi": "1"}}]}

    def user_fills(self, address: str) -> list[dict[str, object]]:
        self.address = address
        return [{"coin": "ETH", "oid": 1, "side": "B", "sz": "0.5", "tid": 99}]


class PreflightMarketData:
    info = PreflightInfo()
    base_urls: list[str | None] = []

    def __init__(self, base_url: str | None = None) -> None:
        self.base_urls.append(base_url)

    def market_exists(self, coin: str) -> bool:
        return coin.upper() == "ETH"

    def get_mark_price(self, coin: str):
        return PriceTick.now(coin.upper(), Decimal("2500"))


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

    def test_manual_market_hint_does_not_satisfy_failed_metadata_check(self) -> None:
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

        with patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", FailingMarketData):
            result = self.invoke(
                ["readiness", rule_id, "--account", "trader", "--market-exists"]
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            "market verification: manual_hint_ignored_after_metadata_failure",
            result.output,
        )
        self.assertIn("market verification unavailable for ETH: metadata unavailable", result.output)

    def test_preflight_reports_read_only_market_account_and_readiness(self) -> None:
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

        with (
            patch("hl_advanced_orders.cli.HyperliquidMarketDataGateway", PreflightMarketData),
            patch(
                "hl_advanced_orders.cli.KeychainSecrets",
                side_effect=AssertionError("preflight should not check Keychain by default"),
            ),
            patch(
                "hl_advanced_orders.cli.HyperliquidExchangeGateway.from_keychain",
                side_effect=AssertionError("preflight should not construct exchange gateway"),
            ),
        ):
            result = self.invoke(
                [
                    "preflight",
                    "--rule-id",
                    rule_id,
                    "--account-address",
                    "0xabc",
                    "--keychain-account",
                    "trader",
                    "--base-url",
                    "https://api.hyperliquid-testnet.xyz",
                ]
            )

        events = read_events(self.data_dir / "audit.jsonl")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("preflight: read-only; no orders will be submitted", result.output)
        self.assertIn("base url: https://api.hyperliquid-testnet.xyz", result.output)
        self.assertIn("market ETH: hyperliquid_metadata", result.output)
        self.assertIn("price ETH: mark 2500", result.output)
        self.assertIn("account snapshot: positions=1 fills=1", result.output)
        self.assertIn("readiness: blocked", result.output)
        self.assertIn("missing private key in macOS Keychain", result.output)
        self.assertEqual(PreflightMarketData.base_urls[-1], "https://api.hyperliquid-testnet.xyz")
        self.assertEqual(events[-1]["event_type"], "preflight_checked")
        self.assertTrue(events[-1]["payload"]["read_only"])

    def test_preflight_help_states_no_order_submission(self) -> None:
        result = self.invoke(["preflight", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--verify-keychain", result.output)
        self.assertIn("without submitting orders", result.output)

    def test_preflight_rejects_mismatched_rule_and_coin(self) -> None:
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

        result = self.invoke(["preflight", "--rule-id", rule_id, "--coin", "SOL"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--coin must match the selected rule coin", result.output)

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

    def test_run_continuous_delegates_to_runner_with_max_ticks(self) -> None:
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
                calls.append(f"runner:{kwargs['interval_seconds']}:{kwargs['max_ticks']}")

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
                    "--base-url",
                    "http://example",
                    "--interval-seconds",
                    "0.5",
                    "--max-ticks",
                    "2",
                ]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Daemon stopped after 2 ticks.", result.output)
        self.assertEqual(
            calls,
            [
                "market:http://example",
                "account:0xabc:True",
                "daemon:init",
                "runner:0.5:2",
                "runner:run",
            ],
        )

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
                    "--base-url",
                    "http://example",
                ]
            )

        self.assertEqual(result.exit_code, 0, result.output)
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

            def market_exists(self, coin: str) -> bool:
                return True

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
