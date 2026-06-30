from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from hl_advanced_orders.cli import app
from hl_advanced_orders.storage import JsonRuleStore


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_create_trailing_rule_persists_rule_and_writes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._invoke(
                tmp,
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
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            snapshot = JsonRuleStore(Path(tmp) / "state.json").load()
            self.assertEqual(len(snapshot.rules), 1)
            self.assertIn("dry_run", result.output)
            self.assertIn("rule_created", (Path(tmp) / "audit.jsonl").read_text(encoding="utf-8"))

    def test_invalid_percent_trail_value_returns_parameter_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._invoke(
                tmp,
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
                ],
            )

            self.assertNotEqual(result.exit_code, 0)

    def test_list_readiness_kill_switch_and_bounded_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            create = self._invoke(
                tmp,
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
                    "5",
                ],
            )
            rule_id = create.output.split()[1]

            listed = self._invoke(tmp, ["rule", "list"])
            readiness = self._invoke(tmp, ["rule", "readiness", rule_id, "--account", "0xabc"])
            kill = self._invoke(tmp, ["kill-switch"])
            run = self._invoke(tmp, ["run", "--once", "--mark-price", "100"])

            self.assertEqual(listed.exit_code, 0, listed.output)
            self.assertIn(rule_id, listed.output)
            self.assertIn("missing private key", readiness.output)
            self.assertIn("Kill switch enabled", kill.output)
            self.assertIn("Daemon ran 1 tick", run.output)

    def _invoke(self, data_dir: str, args: list[str]):
        old_data_dir = os.environ.get("HLAO_DATA_DIR")
        os.environ["HLAO_DATA_DIR"] = data_dir
        try:
            return self.runner.invoke(app, args)
        finally:
            if old_data_dir is None:
                os.environ.pop("HLAO_DATA_DIR", None)
            else:
                os.environ["HLAO_DATA_DIR"] = old_data_dir


if __name__ == "__main__":
    unittest.main()
