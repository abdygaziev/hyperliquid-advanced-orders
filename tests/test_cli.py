from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from hl_advanced_orders.cli import app


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

        result = self.invoke(["readiness", rule_id, "--account", "trader"])

        self.assertEqual(result.exit_code, 1)
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


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
