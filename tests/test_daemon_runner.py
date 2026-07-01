from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hl_advanced_orders.audit import JsonlAuditLog
from hl_advanced_orders.daemon import DaemonRunner, DaemonTickResult
from hl_advanced_orders.storage import LocalStateStore


class FakeDaemon:
    def __init__(self, results: list[DaemonTickResult]) -> None:
        self.results = list(results)
        self.calls = 0

    def run_once(self) -> DaemonTickResult:
        self.calls += 1
        return self.results.pop(0)


class StopAfterFirstSleep:
    def __init__(self) -> None:
        self.sleep_calls = 0

    def should_stop(self) -> bool:
        return self.sleep_calls > 0

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1


class DaemonRunnerTest(unittest.TestCase):
    def test_runner_records_heartbeat_for_two_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            audit = JsonlAuditLog(Path(temp_dir) / "audit.jsonl")
            daemon = FakeDaemon(
                [
                    DaemonTickResult(2, True, True, [], []),
                    DaemonTickResult(2, True, True, [], []),
                ]
            )

            iterations = DaemonRunner(
                daemon=daemon,
                store=store,
                audit=audit,
                poll_interval_seconds=1,
                max_iterations=2,
                sleep=lambda _: None,
            ).run()

            health = store.load().health
            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(iterations, 2)
            self.assertEqual(daemon.calls, 2)
            self.assertEqual(health.mode, "stopped")
            self.assertEqual(health.active_rules_count, 2)
            self.assertIsNotNone(health.last_tick_started_at)
            self.assertIsNotNone(health.last_tick_completed_at)
            self.assertIsNotNone(health.last_successful_account_snapshot_at)
            self.assertIsNotNone(health.last_successful_market_snapshot_at)
            self.assertEqual(health.consecutive_failures, 0)
            self.assertIsNone(health.active_error)
            self.assertEqual([event["event_type"] for event in events], ["daemon_heartbeat", "daemon_heartbeat"])

    def test_transient_failure_records_count_then_success_clears_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")
            audit = JsonlAuditLog(Path(temp_dir) / "audit.jsonl")
            daemon = FakeDaemon(
                [
                    DaemonTickResult(1, True, False, ["market unavailable"], []),
                    DaemonTickResult(1, True, True, [], []),
                ]
            )

            DaemonRunner(
                daemon=daemon,
                store=store,
                audit=audit,
                poll_interval_seconds=1,
                max_iterations=2,
                sleep=lambda _: None,
            ).run()

            health = store.load().health
            events = read_events(Path(temp_dir) / "audit.jsonl")
            self.assertEqual(health.consecutive_failures, 0)
            self.assertIsNone(health.active_error)
            self.assertEqual(events[0]["payload"]["consecutive_failures"], 1)
            self.assertEqual(events[0]["payload"]["active_error"], "market unavailable")
            self.assertEqual(events[1]["payload"]["consecutive_failures"], 0)

    def test_shutdown_request_between_ticks_leaves_state_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stopper = StopAfterFirstSleep()
            store = LocalStateStore(Path(temp_dir) / "state.json")
            daemon = FakeDaemon(
                [
                    DaemonTickResult(1, True, True, [], []),
                    DaemonTickResult(1, True, True, [], []),
                ]
            )

            iterations = DaemonRunner(
                daemon=daemon,
                store=store,
                audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                poll_interval_seconds=1,
                should_stop=stopper.should_stop,
                sleep=stopper.sleep,
            ).run()

            self.assertEqual(iterations, 1)
            self.assertEqual(daemon.calls, 1)
            self.assertEqual(store.load().health.mode, "stopped")

    def test_interrupt_during_sleep_still_marks_runner_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LocalStateStore(Path(temp_dir) / "state.json")

            def interrupt_sleep(_: float) -> None:
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                DaemonRunner(
                    daemon=FakeDaemon([DaemonTickResult(1, True, True, [], [])]),
                    store=store,
                    audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                    poll_interval_seconds=1,
                    sleep=interrupt_sleep,
                ).run()

            self.assertEqual(store.load().health.mode, "stopped")

    def test_runner_rejects_non_positive_poll_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "poll_interval_seconds must be positive"):
                DaemonRunner(
                    daemon=FakeDaemon([]),
                    store=LocalStateStore(Path(temp_dir) / "state.json"),
                    audit=JsonlAuditLog(Path(temp_dir) / "audit.jsonl"),
                    poll_interval_seconds=0,
                )


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
