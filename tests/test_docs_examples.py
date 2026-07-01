from __future__ import annotations

import unittest
from pathlib import Path

from typer.testing import CliRunner

from hl_advanced_orders.cli import app


class DocumentationExamplesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_readme_references_existing_top_level_commands(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        help_result = self.runner.invoke(app, ["--help"])

        for command in [
            "init",
            "run",
            "health",
            "state-validate",
            "diagnostics",
            "emergency-cancel",
            "preflight",
            "kill-switch",
        ]:
            self.assertIn(f"hl-advanced-orders {command}", readme)
            self.assertIn(command, help_result.output)

    def test_readme_references_existing_rule_commands_and_confirmation_phrase(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        help_result = self.runner.invoke(app, ["rule", "--help"])

        for command in [
            "create-trailing",
            "promote-live",
            "manual-review",
            "reset-triggered",
            "disable",
        ]:
            self.assertIn(f"hl-advanced-orders rule {command}", readme)
            self.assertIn(command, help_result.output)
        self.assertIn("ENABLE MAINNET AUTO SUBMIT", readme)

    def test_runbook_covers_operator_workflow(self) -> None:
        runbook = Path("docs/runbooks/trader-readiness.md").read_text(encoding="utf-8")

        for phrase in [
            "Dry-Run Burn-In",
            "Preflight",
            "Canary",
            "Normal Live",
            "Kill Switch",
            "Recovery",
            "Emergency Cancel",
            "macOS Launch",
            "official Hyperliquid docs",
        ]:
            self.assertIn(phrase, runbook)

    def test_launchd_template_uses_placeholders_and_no_private_key(self) -> None:
        plist = Path("packaging/launchd/com.hyperliquid-advanced-orders.plist").read_text(
            encoding="utf-8"
        )

        self.assertIn("0xREPLACE_WITH_ACCOUNT_ADDRESS", plist)
        self.assertIn("0xREPLACE_WITH_WALLET_ADDRESS", plist)
        self.assertNotIn("private-key", plist.lower())
        self.assertNotIn("secret", plist.lower())


if __name__ == "__main__":
    unittest.main()
