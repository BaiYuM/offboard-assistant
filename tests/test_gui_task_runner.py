from __future__ import annotations

import threading
import time
import unittest

from gui_task_runner import (
    TaskAlreadyRunningError,
    TaskCancelled,
    TaskRunner,
    TaskRunnerClosedError,
)


def wait_for_thread(thread: threading.Thread, timeout: float = 2.0) -> None:
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError("worker thread did not finish")


class TaskRunnerTests(unittest.TestCase):
    def test_success_progress_context_and_daemon_thread(self) -> None:
        runner = TaskRunner()
        owner_id = threading.get_ident()
        observed: dict[str, object] = {}

        def worker(context, first, *, second):
            observed["worker_thread"] = threading.get_ident()
            observed["context_token"] = context.token
            observed["context_name"] = context.name
            observed["cancel_event"] = context.cancel_event
            context.report_progress({"percent": 50, "message": "half"})
            return first + second

        handle = runner.start("scan", worker, 2, second=3)
        self.assertTrue(handle.daemon)
        wait_for_thread(handle.thread)

        events = runner.drain()
        self.assertEqual([event.kind for event in events], ["progress", "success"])
        self.assertEqual(events[0].payload, {"percent": 50, "message": "half"})
        self.assertEqual(events[1].payload, 5)
        self.assertEqual(observed["worker_thread"], handle.thread.ident)
        self.assertNotEqual(observed["worker_thread"], owner_id)
        self.assertEqual(observed["context_token"], handle.token)
        self.assertEqual(observed["context_name"], "scan")
        self.assertIs(observed["cancel_event"], handle.cancel_event)
        self.assertFalse(runner.busy)

    def test_only_one_task_is_active_and_tokens_are_unique(self) -> None:
        runner = TaskRunner()
        started = threading.Event()
        release = threading.Event()

        def blocking_worker(context):
            started.set()
            release.wait(2)
            return "first"

        first = runner.start("same-name", blocking_worker)
        self.assertTrue(started.wait(1))
        with self.assertRaises(TaskAlreadyRunningError):
            runner.start("second", lambda context: "second")

        release.set()
        wait_for_thread(first.thread)
        self.assertEqual([event.kind for event in runner.drain()], ["success"])

        second = runner.start("same-name", lambda context: "second")
        wait_for_thread(second.thread)
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(runner.drain()[0].payload, "second")

    def test_cooperative_cancel_reports_cancelled_and_wrong_token_is_ignored(self) -> None:
        runner = TaskRunner()
        started = threading.Event()

        def cancellable_worker(context):
            started.set()
            while True:
                context.raise_if_cancelled()
                time.sleep(0.001)

        handle = runner.start("long-job", cancellable_worker)
        self.assertTrue(started.wait(1))
        self.assertFalse(runner.cancel("wrong-token"))
        self.assertTrue(runner.cancel(handle.token))
        self.assertFalse(runner.cancel(handle.token))
        wait_for_thread(handle.thread)

        events = runner.drain()
        self.assertEqual([event.kind for event in events], ["cancelled"])
        self.assertIsInstance(events[0].exception, TaskCancelled)
        self.assertFalse(runner.busy)

    def test_late_cancel_does_not_relabel_a_completed_worker(self) -> None:
        runner = TaskRunner()
        started = threading.Event()
        release = threading.Event()

        def committing_worker(context):
            started.set()
            release.wait(2)
            return "committed"

        handle = runner.start("commit", committing_worker)
        self.assertTrue(started.wait(1))
        self.assertTrue(runner.cancel(handle.token))
        release.set()
        wait_for_thread(handle.thread)

        events = runner.drain()
        self.assertEqual([event.kind for event in events], ["success"])
        self.assertEqual(events[0].payload, "committed")

    def test_exception_object_is_delivered_unchanged(self) -> None:
        runner = TaskRunner()
        expected = ValueError("keep this object")

        def failing_worker(context):
            raise expected

        handle = runner.start("failing", failing_worker)
        wait_for_thread(handle.thread)
        events = runner.drain()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "error")
        self.assertIs(events[0].exception, expected)
        self.assertIsNone(events[0].payload)

    def test_shutdown_discards_queued_and_late_events(self) -> None:
        runner = TaskRunner()
        started = threading.Event()
        release = threading.Event()

        def worker(context):
            context.report_progress("queued-before-close")
            started.set()
            release.wait(2)
            context.report_progress("late")
            return "late-result"

        handle = runner.start("closing", worker)
        self.assertTrue(started.wait(1))
        # A queued event is discarded immediately on close.
        self.assertTrue(runner.active is handle)
        runner.shutdown()
        self.assertTrue(runner.closed)
        self.assertFalse(runner.cancel(handle.token))
        self.assertEqual(runner.drain(), [])

        release.set()
        wait_for_thread(handle.thread)
        self.assertEqual(runner.drain(), [])
        self.assertFalse(runner.busy)
        self.assertIsNone(runner.active)
        with self.assertRaises(TaskRunnerClosedError):
            runner.start("after-close", lambda context: None)
        runner.shutdown()  # idempotent

    def test_drain_is_owner_thread_only(self) -> None:
        runner = TaskRunner()
        errors: list[BaseException] = []

        def other_thread() -> None:
            try:
                runner.drain()
            except BaseException as exc:  # test the public guard
                errors.append(exc)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        runner.shutdown()


if __name__ == "__main__":
    unittest.main()
