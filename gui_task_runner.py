"""Tk-agnostic background task execution for the GUI.

``TaskRunner`` deliberately has no Tk dependency.  A worker runs in one
daemon thread and communicates only by putting :class:`TaskEvent` instances
on an internal queue.  The thread that created the runner (normally Tk's
main thread) calls :meth:`TaskRunner.drain` from its regular ``after`` poll.

Workers receive a :class:`TaskContext`, which contains the task identity and
cooperative cancellation event.  They must not touch Tk objects; copy any
input they need before starting the task and send display updates through
``context.report_progress``.
"""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, TypeVar


TaskEventKind = Literal["progress", "success", "error", "cancelled"]
_ResultT = TypeVar("_ResultT")
_ProgressReporter = Callable[[Any], bool]


class TaskRunnerError(RuntimeError):
    """Base class for task-runner state errors."""


class TaskRunnerClosedError(TaskRunnerError):
    """Raised when a new task is submitted after ``shutdown``."""


class TaskAlreadyRunningError(TaskRunnerError):
    """Raised when a second task is submitted before the first is complete."""


class TaskCancelled(Exception):
    """Exception workers may raise after observing cancellation."""


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """A message delivered to the owner thread by :meth:`TaskRunner.drain`.

    ``payload`` contains progress data for ``progress`` events and the
    worker's return value for ``success`` events.  For ``error`` events,
    ``exception`` is the original exception object, not a stringified copy.
    A ``cancelled`` event may carry a :class:`TaskCancelled` in ``exception``
    when the worker explicitly raised one. A late cancellation request does
    not rewrite a normally returned result into ``cancelled``.
    """

    kind: TaskEventKind
    token: str
    name: str
    payload: Any = None
    exception: BaseException | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether this event ends the task lifecycle."""

        return self.kind != "progress"


@dataclass(frozen=True, slots=True)
class TaskHandle:
    """Identity and cancellation handle for a submitted task."""

    token: str
    name: str
    cancel_event: threading.Event = field(repr=False, compare=False)
    thread: threading.Thread = field(repr=False, compare=False)

    @property
    def daemon(self) -> bool:
        """Expose the thread's daemon setting without exposing Tk concerns."""

        return self.thread.daemon

    @property
    def alive(self) -> bool:
        """Return whether the worker thread is still running."""

        return self.thread.is_alive()

    def cancel(self) -> bool:
        """Request cooperative cancellation, returning ``True`` on first set."""

        if self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        return True


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Context passed to a worker function.

    The context is safe to use from the worker thread.  ``report_progress``
    only queues data; it never invokes a GUI callback.
    """

    token: str
    name: str
    cancel_event: threading.Event = field(repr=False, compare=False)
    _reporter: _ProgressReporter = field(repr=False, compare=False)

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""

        return self.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`TaskCancelled` when the worker should stop."""

        if self.cancelled:
            raise TaskCancelled(f"Task {self.name!r} was cancelled")

    def report_progress(self, payload: Any = None) -> bool:
        """Queue one progress payload and return whether it was accepted.

        Returning ``False`` is useful during shutdown: it tells a worker that
        the runner is closed without requiring it to know anything about Tk.
        Payloads are intentionally unconstrained so callers can send a
        percentage, a message, or a small dictionary containing both.
        """

        return self._reporter(payload)


class TaskRunner:
    """Run at most one cooperative background task at a time.

    The runner is thread-safe for worker-to-queue communication.  ``drain``
    is intended to be called by the owner/UI thread and returns a snapshot of
    all currently queued events.  It never waits for a worker.
    """

    def __init__(self) -> None:
        self._events: queue.Queue[TaskEvent] = queue.Queue()
        self._lock = threading.RLock()
        self._active: TaskHandle | None = None
        self._closed = False
        self._owner_thread_id = threading.get_ident()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def busy(self) -> bool:
        """Whether a task has been submitted and not reached a terminal event."""

        with self._lock:
            return self._active is not None

    @property
    def active(self) -> TaskHandle | None:
        """Return the current handle, or ``None`` when no task is active."""

        with self._lock:
            return self._active

    def start(
        self,
        name: str,
        worker: Callable[..., _ResultT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> TaskHandle:
        """Submit one worker and return its unique :class:`TaskHandle`.

        ``worker`` is called as ``worker(context, *args, **kwargs)``.  The
        returned value appears in a ``success`` event.  Exceptions are caught
        in the worker thread and delivered as ``error`` events.
        """

        if not isinstance(name, str) or not name.strip():
            raise ValueError("task name must be a non-empty string")
        if not callable(worker):
            raise TypeError("worker must be callable")

        token = uuid.uuid4().hex
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(token, name, cancel_event, worker, args, kwargs),
            name=f"gui-task-{name}-{token[:8]}",
            daemon=True,
        )
        handle = TaskHandle(token, name, cancel_event, thread)

        with self._lock:
            if self._closed:
                raise TaskRunnerClosedError("task runner is shut down")
            if self._active is not None:
                raise TaskAlreadyRunningError(
                    f"task {self._active.name!r} ({self._active.token}) is still running"
                )
            self._active = handle
            try:
                # Starting while holding the lock closes the small race where
                # shutdown could otherwise cancel a handle before its thread
                # has been started.
                thread.start()
            except BaseException:
                self._active = None
                raise
        return handle

    def cancel(self, token: str | None = None) -> bool:
        """Request cancellation for the active task.

        If ``token`` is supplied, it must match the active task. No terminal
        event is synthesized here; the worker must observe the request and
        raise :class:`TaskCancelled` when it can safely abort, so there is
        exactly one terminal event and its ordering is deterministic.
        """

        with self._lock:
            if self._closed or self._active is None:
                return False
            if token is not None and token != self._active.token:
                return False
            return self._active.cancel()

    def drain(self, limit: int | None = None) -> list[TaskEvent]:
        """Return queued events without blocking.

        ``drain`` is intentionally owner-thread-only.  This prevents an
        accidental worker call from turning a Tk callback into a cross-thread
        GUI access pattern.  Call it from the GUI's periodic ``after`` hook.
        """

        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("TaskRunner.drain() must be called by its owner thread")
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative or None")

        events: list[TaskEvent] = []
        with self._lock:
            if self._closed:
                return events
            while limit is None or len(events) < limit:
                try:
                    events.append(self._events.get_nowait())
                except queue.Empty:
                    break
        return events

    def shutdown(self) -> None:
        """Close the runner, request cancellation, and discard queued events.

        Shutdown is non-blocking.  The daemon worker may finish later, but all
        events it emits after closure are ignored.  Calling shutdown again is
        harmless.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._active is not None:
                self._active.cancel()
            self._clear_events_locked()

    close = shutdown

    def _run(
        self,
        token: str,
        name: str,
        cancel_event: threading.Event,
        worker: Callable[..., _ResultT],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        context = TaskContext(token, name, cancel_event, lambda payload: self._emit_progress(token, name, payload))
        try:
            context.raise_if_cancelled()
            result = worker(context, *args, **kwargs)
        except TaskCancelled as exc:
            self._finish(TaskEvent("cancelled", token, name, exception=exc))
        except BaseException as exc:
            # Preserve the original object for callers that need its type or
            # attributes; formatting/logging belongs on the owner thread.
            self._finish(TaskEvent("error", token, name, exception=exc))
        else:
            # A cancellation request is cooperative.  If the worker returns
            # normally, its work (including any side effects) completed and
            # must be reported as success even when the request arrived at
            # the end of the operation.  Workers that can safely abort should
            # call ``raise_if_cancelled`` and take the explicit cancelled path.
            self._finish(TaskEvent("success", token, name, payload=result))

    def _emit_progress(self, token: str, name: str, payload: Any) -> bool:
        event = TaskEvent("progress", token, name, payload=payload)
        with self._lock:
            if self._closed:
                return False
            # A worker can only emit for its own active token.  This guard also
            # suppresses a stale callback if a future implementation reuses a
            # worker object after its task has ended.
            if self._active is None or self._active.token != token:
                return False
            self._events.put_nowait(event)
            return True

    def _finish(self, event: TaskEvent) -> None:
        with self._lock:
            if self._active is None or self._active.token != event.token:
                return
            if not self._closed:
                self._events.put_nowait(event)
            # Mark the logical task complete atomically with terminal-event
            # enqueueing, so cancel/start cannot interleave with completion.
            self._active = None

    def _clear_events_locked(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return


__all__ = [
    "TaskAlreadyRunningError",
    "TaskCancelled",
    "TaskContext",
    "TaskEvent",
    "TaskEventKind",
    "TaskHandle",
    "TaskRunner",
    "TaskRunnerClosedError",
    "TaskRunnerError",
]
