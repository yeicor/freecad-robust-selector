"""Background execution for slow geometry work.

Planning semantic routes and auditing selectors over complex geometries can
take seconds; running that on the GUI thread freezes FreeCAD.  Everything
submitted here runs on Qt's global thread pool while the caller stays
responsive.

Thread-safety contract (load-bearing, read carefully): worker callables must
only touch data that is detached from the live document — shape copies,
name strings, plain parameters.  They must never touch document objects,
their properties, the selection, or any GUI widget.  Callers snapshot what
the worker needs on the GUI thread (see ``fs_bindings.snapshot_source``)
and freeze recomputes for the source document while the task runs, so the
snapshot cannot be invalidated mid-flight.  Results are delivered back on
the GUI thread through queued signals; only the result handlers may touch
the document or the UI.

Superseded tasks are not cancelled (a QRunnable cannot be stopped safely);
callers tag submissions with a generation counter and drop stale results.
A dropped result is a correctness rule, and it is logged, never silent.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

try:  # pragma: no cover - exercised in FreeCAD
    from PySide import QtCore
except ImportError:  # pragma: no cover - FreeCAD 1.x wheels
    from PySide6 import QtCore


class PendingTask(QtCore.QObject):
    """One background computation with an explicit settlement contract."""

    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self._fn = fn
        self._settled: str = ""
        self._result: Any = None
        self._error: str = ""
        # The pool's runnable releases its reference when run() returns, which
        # can precede queued delivery.  Holding ourselves until emission keeps
        # fire-and-forget submissions alive; the reference is released right
        # after emitting so nothing leaks.
        self._selfref: Any = self

    def _execute(self) -> None:
        # Emission precedes the settlement flag on purpose: anyone observing
        # the flag is guaranteed the delivery is already queued, so a final
        # event drain cannot miss it.
        try:
            result = self._fn()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self.failed.emit(self._error)
            self._settled = "failed"
        else:
            self._result = result
            self.finished.emit(result)
            self._settled = "settled"
        finally:
            self._selfref = None

    def wait(self, timeout_ms: int = 120000) -> Any:
        """Block until settlement; for tests and value-producing commands.

        Returns the result, re-raises a worker failure as RuntimeError, and
        raises TimeoutError past the deadline.

        Deliberately pumps NO events while waiting: tests must stay
        deterministic (no timer/paint reentrancy from nested loops).  Once the
        settlement flag is observed, a single queue drain delivers the
        already-posted slot calls (emission always precedes flagging in
        ``_execute``).  ``time.sleep`` always releases the GIL, so the Python
        worker underneath can never starve.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while not self._settled and time.monotonic() < deadline:
            time.sleep(0.005)
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents, 100)
        if self._settled == "settled":
            return self._result
        if self._settled == "failed":
            raise RuntimeError(self._error)
        raise TimeoutError(f"Background task did not settle within {timeout_ms} ms")


class _Runner(QtCore.QRunnable):
    def __init__(self, task: PendingTask):
        super().__init__()
        self._task = task
        self.setAutoDelete(True)

    def run(self) -> None:
        self._task._execute()


_SYNC_ONLY = bool(os.environ.get("FS_TASKS_SYNC", ""))


def submit(fn: Callable[[], Any], on_done=None, on_failed=None) -> PendingTask:
    """Run ``fn`` on the global thread pool and return its pending handle.

    Result handlers are connected BEFORE the worker starts: a fast task could
    otherwise settle between ``start()`` and a later ``connect()``, and the
    queued delivery would miss the application slots entirely.
    """
    task = PendingTask(fn)
    if on_done is not None:
        task.finished.connect(on_done)
    if on_failed is not None:
        task.failed.connect(on_failed)
    if _SYNC_ONLY:
        task._execute()
        return task
    QtCore.QThreadPool.globalInstance().start(_Runner(task))
    return task


_frozen: dict[str, list[Any]] = {}


def freeze_recomputes(doc: Any) -> None:
    """Freeze ``doc`` recomputes while background work reads its geometry.

    Reference-counted per document, so overlapping tasks cannot unfreeze each
    other early.  A document without a name cannot be tracked and is refused
    loudly instead of being left frozen by mistake.
    """
    if doc is None:
        return
    name = getattr(doc, "Name", "")
    if not name:
        raise ValueError("Cannot freeze recomputes for a document without a name")
    entry = _frozen.get(name)
    if entry is None:
        doc.RecomputesFrozen = True
        _frozen[name] = [doc, 1]
    else:
        entry[1] += 1


def unfreeze_recomputes(doc: Any) -> None:
    """Release one freeze claim; unfreezes when the last claim is released."""
    if doc is None:
        return
    name = getattr(doc, "Name", "")
    entry = _frozen.pop(name, None)
    if entry is None:
        return
    entry[1] -= 1
    if entry[1] <= 0:
        if hasattr(entry[0], "RecomputesFrozen"):
            try:
                entry[0].RecomputesFrozen = False
            except (ReferenceError, RuntimeError):
                # Document C++ object was destroyed during background execution
                pass
    else:
        _frozen[name] = entry
