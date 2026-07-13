from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest import mock


class GuiTaskIntegrationTests(unittest.TestCase):
    def _build_app(self, tmp_path: Path):
        import tkinter as tk
        import offboard_gui as gui

        gui.FirstRunWizard = mock.MagicMock()
        app = gui.OffboardGui(tmp_path / "state")
        app.withdraw()
        return app

    def _pump_until(self, root, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            root.update()
            if time.monotonic() >= deadline:
                self.fail("GUI task did not reach the expected state")
            time.sleep(0.005)

    def test_worker_and_success_callback_stay_on_their_expected_threads(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._build_app(Path(tmp))
            root = app
            owner_thread = threading.get_ident()
            observed: dict[str, int] = {}
            progress: list[str] = []

            def worker(context):
                observed["worker"] = threading.get_ident()
                context.report_progress("half")
                return "done"

            def success(value):
                observed["callback"] = threading.get_ident()
                progress.append(value)

            try:
                self.assertTrue(
                    app._start_background_task(
                        "integration",
                        worker,
                        success,
                        busy_text="working",
                        error_title="failed",
                        cancellable=True,
                    )
                )
                self._pump_until(root, lambda: app._active_task_state is None)
                self.assertEqual(progress, ["done"])
                self.assertNotEqual(observed["worker"], owner_thread)
                self.assertEqual(observed["callback"], owner_thread)
                self.assertFalse(app.task_cancel_button.instate(["!disabled"]))
            finally:
                app.destroy()

    def test_cancelled_task_restores_controls_without_success_callback(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._build_app(Path(tmp))
            started = threading.Event()
            success: list[object] = []

            def worker(context):
                started.set()
                while True:
                    context.raise_if_cancelled()
                    time.sleep(0.002)

            try:
                self.assertTrue(
                    app._start_background_task(
                        "cancellable",
                        worker,
                        success.append,
                        busy_text="working",
                        error_title="failed",
                        cancellable=True,
                    )
                )
                self.assertTrue(started.wait(1))
                app._cancel_active_task()
                self._pump_until(app, lambda: app._active_task_state is None)
                self.assertEqual(success, [])
                self.assertTrue(app.task_cancel_button.instate(["disabled"]))
            finally:
                app.destroy()

    def test_rescan_collects_off_main_thread_and_applies_snapshot(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._build_app(Path(tmp))
            baseline = app.state_dir / "baseline.json"
            baseline.write_text(json.dumps({"baseline_since": "2020-01-01", "items": []}), encoding="utf-8")
            observed: dict[str, int] = {}
            snapshot = {
                "schema_version": 1,
                "generated_at": "2026-07-13T00:00:00+00:00",
                "items": [
                    {
                        "id": "worker-item",
                        "type": "ide_recent_project",
                        "name": "worker project",
                        "path": "/tmp/worker-project",
                        "modified_at": "2026-07-13T00:00:00+00:00",
                    }
                ],
            }

            def collect(*args, **kwargs):
                observed["thread"] = threading.get_ident()
                kwargs["progress"]("测试扫描", 1, 1)
                return snapshot

            try:
                with mock.patch("offboard_gui.core.collect_snapshot", side_effect=collect) as collect_mock:
                    app.refresh_data(rescan=True)
                    self._pump_until(app, lambda: app._active_task_state is None)
                    collect_mock.assert_called_once()
                self.assertNotEqual(observed["thread"], threading.get_ident())
                self.assertEqual(app.candidates[0]["id"], "worker-item")
                self.assertTrue((app.state_dir / "latest-snapshot.json").exists())
            finally:
                app.destroy()

    def test_scan_detail_progress_keeps_the_stage_progress_scale(self) -> None:
        import tempfile
        from gui_task_runner import TaskEvent

        with tempfile.TemporaryDirectory() as tmp:
            app = self._build_app(Path(tmp))
            try:
                app._apply_task_progress(
                    TaskEvent(
                        "progress",
                        "token",
                        "扫描",
                        payload={"message": "敏感文件", "completed": 3, "total": 7},
                    )
                )
                app._apply_task_progress(
                    TaskEvent(
                        "progress",
                        "token",
                        "扫描",
                        payload={
                            "message": "敏感文件",
                            "completed": 100,
                            "total": 20000,
                            "detail": True,
                        },
                    )
                )

                self.assertEqual(float(app.task_progress["maximum"]), 7.0)
                self.assertEqual(float(app.task_progress["value"]), 3.0)
                self.assertIn("100/20000", app.status_var.get())
            finally:
                app.destroy()

    def test_query_background_tasks_submits_one_two_command_batch(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._build_app(Path(tmp))
            try:
                with mock.patch.object(app, "_run_schtasks_batch") as run_batch:
                    app.query_background_tasks()
                run_batch.assert_called_once_with(
                    [
                        ["/Query", "/TN", "OffboardAssistantInstallWatch"],
                        ["/Query", "/TN", "OffboardAssistantDailyScan"],
                    ]
                )
            finally:
                app.destroy()

    def test_close_waits_for_a_cooperative_worker_to_exit(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._build_app(Path(tmp))
            started = threading.Event()

            def worker(context):
                started.set()
                while True:
                    context.raise_if_cancelled()
                    time.sleep(0.002)

            self.assertTrue(
                app._start_background_task(
                    "close-test",
                    worker,
                    lambda _result: None,
                    busy_text="working",
                    error_title="failed",
                    cancellable=True,
                )
            )
            self.assertTrue(started.wait(1))
            handle = app._task_runner.active
            self.assertIsNotNone(handle)

            with mock.patch("offboard_gui.messagebox.askyesno", return_value=True):
                app._on_close()

            self.assertTrue(app._closing)
            self.assertFalse(handle.thread.is_alive())

    def test_baseline_commit_writes_snapshot_before_baseline(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = self._build_app(Path(tmp))
            app.baseline_since_var.set("2020-01-01")
            snapshot = {
                "schema_version": 1,
                "generated_at": "2026-07-13T00:00:00+00:00",
                "items": [],
            }
            writes: list[Path] = []

            try:
                with (
                    mock.patch("offboard_gui.messagebox.askyesno", return_value=True),
                    mock.patch("offboard_gui.core.collect_snapshot", return_value=snapshot),
                    mock.patch("offboard_gui.core.write_json", side_effect=lambda path, _data: writes.append(path)),
                ):
                    app.init_baseline_from_gui()
                    self._pump_until(app, lambda: app._active_task_state is None)

                self.assertEqual(
                    writes,
                    [
                        app.state_dir / "latest-snapshot.json",
                        app.state_dir / "baseline.json",
                    ],
                )
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main()
