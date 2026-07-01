from __future__ import annotations

import unittest
from pathlib import Path

from typer.testing import CliRunner

from hl_advanced_orders.cli import app
from hl_advanced_orders.readiness import MAINNET_CONFIRMATION_PHRASE


class DocumentationExamplesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = Path("README.md").read_text(encoding="utf-8")
        self.runner = CliRunner()

    def test_readme_preserves_test_command_and_confirmation_phrase(self) -> None:
        self.assertIn("python -m unittest discover -s tests", self.readme)
        self.assertIn(MAINNET_CONFIRMATION_PHRASE, self.readme)
        self.assertIn("dry-run/private-pilot", self.readme)

    def test_readme_routes_through_preflight_before_mainnet_automation(self) -> None:
        preflight_index = self.readme.index("hl-advanced-orders preflight")
        auto_submit_index = self.readme.index("--execution-mode auto_submit")

        self.assertLess(preflight_index, auto_submit_index)
        self.assertIn("read-only preflight", self.readme)
        self.assertIn("submitting orders", self.readme)
        self.assertNotIn("Pass `--market-exists`", self.readme)

    def test_documented_top_level_commands_exist(self) -> None:
        result = self.runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        for command in ["init", "run", "readiness", "kill-switch", "preflight"]:
            self.assertIn(command, result.output)

    def test_preflight_help_documents_no_order_submission(self) -> None:
        result = self.runner.invoke(app, ["preflight", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("without submitting orders", result.output)


if __name__ == "__main__":
    unittest.main()
