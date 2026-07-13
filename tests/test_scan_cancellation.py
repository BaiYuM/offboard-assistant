from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import offboard_assistant as core


class ScanCancellationTests(unittest.TestCase):
    def test_sensitive_scan_honors_cancellation_between_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(8):
                (root / f"{index:02d}.env").write_text("TOKEN=value\n", encoding="utf-8")

            checks = 0

            def cancel_after_a_few_checks() -> bool:
                nonlocal checks
                checks += 1
                return checks >= 4

            with self.assertRaises(core.ScanCancelled):
                core.scan_sensitive_locations(
                    [root],
                    cancellation=cancel_after_a_few_checks,
                )
            self.assertGreaterEqual(checks, 4)

    def test_collect_snapshot_reports_stage_progress(self) -> None:
        progress: list[tuple[str, int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                mock.patch.object(core, "scan_environment", return_value=[]),
                mock.patch.object(core, "scan_installed_apps_from_registry", return_value=[]),
                mock.patch.object(core, "scan_browser_logins", return_value=[]),
                mock.patch.object(core, "scan_sensitive_locations", return_value=[]),
                mock.patch.object(core, "scan_chat_locations", return_value=[]),
                mock.patch.object(core, "scan_recent_ide_projects", return_value=[]),
            ):
                snapshot = core.collect_snapshot(
                    state_dir,
                    [],
                    config={"ide_scan_enabled": True},
                    progress=lambda stage, completed, total: progress.append((stage, completed, total)),
                )

        self.assertEqual(snapshot["items"], [])
        self.assertEqual(progress[0], ("环境变量", 0, 7))
        self.assertEqual(progress[-1], ("整理候选项", 7, 7))
        self.assertIn(("IDE 最近项目", 5, 7), progress)

    def test_collect_snapshot_uses_six_stages_when_ide_scan_is_disabled(self) -> None:
        progress: list[tuple[str, int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                mock.patch.object(core, "scan_environment", return_value=[]),
                mock.patch.object(core, "scan_installed_apps_from_registry", return_value=[]),
                mock.patch.object(core, "scan_browser_logins", return_value=[]),
                mock.patch.object(core, "scan_sensitive_locations", return_value=[]),
                mock.patch.object(core, "scan_chat_locations", return_value=[]),
            ):
                core.collect_snapshot(
                    state_dir,
                    [],
                    config={"ide_scan_enabled": False},
                    progress=lambda stage, completed, total: progress.append((stage, completed, total)),
                )

        self.assertNotIn(("IDE 最近项目", 5, 6), progress)
        self.assertEqual(progress[-2:], [("整理候选项", 5, 6), ("整理候选项", 6, 6)])

    def test_collect_snapshot_cancellation_stops_before_next_stage(self) -> None:
        calls: list[str] = []

        def cancel() -> bool:
            return True

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "scan_environment", side_effect=lambda: calls.append("env") or []):
                with self.assertRaises(core.ScanCancelled):
                    core.collect_snapshot(Path(tmp), [], cancellation=cancel)

        self.assertEqual(calls, [])

    def test_collect_snapshot_forwards_event_and_stops_after_browser_stage(self) -> None:
        cancellation = threading.Event()

        def browser_scan(_state_dir: Path, *, cancellation: threading.Event):
            cancellation.set()
            return []

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(core, "scan_environment", return_value=[]) as env_scan,
                mock.patch.object(core, "scan_installed_apps_from_registry", return_value=[]) as app_scan,
                mock.patch.object(core, "scan_browser_logins", side_effect=browser_scan) as browser,
                mock.patch.object(core, "scan_sensitive_locations", return_value=[]) as sensitive,
            ):
                with self.assertRaises(core.ScanCancelled):
                    core.collect_snapshot(Path(tmp), [], cancellation=cancellation)

        env_scan.assert_called_once_with(cancellation=cancellation)
        app_scan.assert_called_once_with(cancellation=cancellation)
        browser.assert_called_once_with(Path(tmp), cancellation=cancellation)
        sensitive.assert_not_called()

    def test_final_progress_is_reported_after_item_organization(self) -> None:
        order: list[str] = []
        item = {"id": "one", "type": "environment_variable", "name": "TOKEN"}

        def progress(_stage: str, completed: int, total: int) -> None:
            if completed == total:
                order.append("complete")

        def infer(*_args, **_kwargs) -> str:
            order.append("organized")
            return "unknown"

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(core, "scan_environment", return_value=[item]),
                mock.patch.object(core, "scan_installed_apps_from_registry", return_value=[]),
                mock.patch.object(core, "scan_browser_logins", return_value=[]),
                mock.patch.object(core, "scan_sensitive_locations", return_value=[]),
                mock.patch.object(core, "scan_chat_locations", return_value=[]),
                mock.patch.object(core, "scan_recent_ide_projects", return_value=[]),
                mock.patch.object(core, "infer_account_owner_hint", side_effect=infer),
            ):
                core.collect_snapshot(Path(tmp), [], progress=progress)

        self.assertEqual(order, ["organized", "complete"])


if __name__ == "__main__":
    unittest.main()
