# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

"""Bounded, FIFO persistence for immutable scan points."""

from __future__ import annotations

import queue
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar, Optional

from kiwi_scan.scan._point_frame import _PreparedPoint


@dataclass
class _WriteRequest:
    point: _PreparedPoint
    completed: Optional[threading.Event] = None
    error: Optional[BaseException] = None
    enqueued_at: float = field(default_factory=time.perf_counter)


class _ParallelPointWriter:
    """Write prepared scan points on one persistent background thread.

    Raw POSIX timestamps cross the scan/writer boundary. Timestamp rendering is
    deliberately performed only here so ISO-8601 conversion cannot consume scan
    loop CPU time.

    Rows from ``submit()`` are buffered and flushed at most once per
    ``flush_interval`` seconds, including when the scan goes idle. This keeps
    the file close to the scan without one flush per point on slow (e.g. NFS)
    storage. ``submit_and_wait()`` always flushes its own row.
    """

    _SENTINEL = object()
    _TIMESTAMP_FORMATS: ClassVar[frozenset[str]] = frozenset(
        {"iso8601", "unix"}
    )

    def __init__(
        self,
        format_value: Callable[[Any], str],
        *,
        timestamp_output_format: str = "iso8601",
        queue_size: int = 1024,
        flush_interval: float = 1.0,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be greater than zero")
        if flush_interval < 0:
            raise ValueError("flush_interval must not be negative")

        timestamp_output_format = str(timestamp_output_format).strip().lower()
        if timestamp_output_format not in self._TIMESTAMP_FORMATS:
            raise ValueError(
                "timestamp_output_format must be one of: iso8601, unix "
                f"(got {timestamp_output_format!r})"
            )

        self._format_value = format_value
        self._timestamp_output_format = timestamp_output_format
        self._flush_interval = float(flush_interval)
        self._queue = queue.Queue(maxsize=queue_size)
        self._lifecycle_lock = threading.RLock()
        self._error_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state = "new"
        self._output_file: Optional[str] = None
        self._first_error: Optional[BaseException] = None
        self._queue_high_water = 0
        self._maximum_queue_delay = 0.0

    @property
    def queue_high_water(self) -> int:
        with self._metrics_lock:
            return self._queue_high_water

    @property
    def maximum_queue_delay(self) -> float:
        with self._metrics_lock:
            return self._maximum_queue_delay

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._state == "running"

    @staticmethod
    def _timestamp_to_iso(timestamp: Any) -> str:
        if timestamp is None:
            return ""
        try:
            return datetime.fromtimestamp(float(timestamp)).astimezone().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return str(timestamp)

    @classmethod
    def format_point_line(
        cls,
        point: _PreparedPoint,
        format_value: Callable[[Any], str],
        *,
        timestamp_output_format: str,
    ) -> str:
        """Render one prepared point according to the file timestamp format."""
        timestamp_output_format = str(timestamp_output_format).strip().lower()
        if timestamp_output_format not in cls._TIMESTAMP_FORMATS:
            raise ValueError(
                "timestamp_output_format must be one of: iso8601, unix "
                f"(got {timestamp_output_format!r})"
            )

        def format_column(index: int, value: Any) -> str:
            if index not in point.timestamp_indices:
                return format_value(value)
            if timestamp_output_format == "iso8601":
                return cls._timestamp_to_iso(value)
            return format_value(value)

        return "\t".join(
            format_column(index, value)
            for index, value in enumerate(point.row_values)
        )

    def start(self, output_file: str) -> None:
        """Start the persistent writer for ``output_file``."""
        with self._lifecycle_lock:
            if self._state == "running":
                if self._output_file != output_file:
                    raise RuntimeError("Point writer already uses another file")
                return
            if self._state != "new":
                raise RuntimeError("A stopped point writer cannot be restarted")

            self._output_file = output_file
            self._state = "running"
            self._thread = threading.Thread(
                target=self._run,
                name="kiwi-scan-point-writer",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._output_file = None
                self._state = "new"
                raise

    def submit(self, point: _PreparedPoint) -> None:
        """Queue one point, blocking only when bounded backpressure is needed."""
        self._enqueue(_WriteRequest(point=point))

    def submit_and_wait(self, point: _PreparedPoint) -> None:
        """Queue one point and synchronously report its persistence result."""
        completed = threading.Event()
        request = _WriteRequest(point=point, completed=completed)
        self._enqueue(request)
        completed.wait()
        if request.error is not None:
            raise request.error

    def stop(self) -> None:
        """Stop accepting points, drain the queue, flush, and join the worker."""
        with self._lifecycle_lock:
            if self._state == "new":
                self._state = "stopped"
                return
            if self._state == "stopped":
                return
            if self._state == "running":
                self._state = "stopping"
                self._queue.put(self._SENTINEL)
            thread = self._thread

        self._queue.join()
        if thread is not None:
            thread.join()

        with self._lifecycle_lock:
            self._state = "stopped"

        self._raise_if_failed()

    def _enqueue(self, request: _WriteRequest) -> None:
        with self._lifecycle_lock:
            if self._state != "running":
                raise RuntimeError("Point writer is not running")
            self._raise_if_failed()
            self._queue.put(request)
            queued = self._queue.qsize()

        with self._metrics_lock:
            self._queue_high_water = max(self._queue_high_water, queued)

    def _run(self) -> None:
        try:
            with ExitStack() as stack:
                output = None

                try:
                    output = stack.enter_context(
                        open(str(self._output_file), "a", encoding="utf-8")
                    )
                except BaseException as exc:  # noqa: BLE001
                    self._record_error(exc)

                unflushed = False
                last_flush = time.monotonic()
                while True:
                    timeout = None
                    if unflushed:
                        timeout = max(
                            0.0,
                            last_flush + self._flush_interval - time.monotonic(),
                        )
                    try:
                        item = self._queue.get(timeout=timeout)
                    except queue.Empty:
                        # Scan went idle with rows still buffered.
                        self._flush(output)
                        unflushed = False
                        last_flush = time.monotonic()
                        continue

                    try:
                        if item is self._SENTINEL:
                            break

                        request = item
                        self._record_queue_delay(request.enqueued_at)
                        if self._write_request(output, request):
                            unflushed = True
                        if unflushed and (
                            request.completed is not None
                            or time.monotonic() - last_flush >= self._flush_interval
                        ):
                            self._flush(output, request)
                            unflushed = False
                            last_flush = time.monotonic()

                        if request.completed is not None:
                            request.completed.set()
                    finally:
                        self._queue.task_done()
        except BaseException as exc:  # noqa: BLE001
            self._record_error(exc)

    def _write_request(self, output: Any, request: _WriteRequest) -> bool:
        """Write one row; return whether it was written to the buffer."""
        error = self._get_error()
        if error is not None:
            request.error = error
            return False
        try:
            assert output is not None
            line = self.format_point_line(
                request.point,
                self._format_value,
                timestamp_output_format=self._timestamp_output_format,
            )
            output.write(line + "\n")
        except BaseException as exc:  # noqa: BLE001
            request.error = self._record_error(exc)
            return False
        return True

    def _flush(
        self,
        output: Any,
        request: Optional[_WriteRequest] = None,
    ) -> None:
        if output is None or self._get_error() is not None:
            return
        try:
            output.flush()
        except BaseException as exc:  # noqa: BLE001
            error = self._record_error(exc)
            if request is not None:
                request.error = error

    def _record_queue_delay(self, enqueued_at: float) -> None:
        delay = max(0.0, time.perf_counter() - enqueued_at)
        with self._metrics_lock:
            self._maximum_queue_delay = max(self._maximum_queue_delay, delay)

    def _get_error(self) -> Optional[BaseException]:
        with self._error_lock:
            return self._first_error

    def _record_error(self, error: BaseException) -> BaseException:
        with self._error_lock:
            if self._first_error is None:
                self._first_error = error
            return self._first_error

    def _raise_if_failed(self) -> None:
        error = self._get_error()
        if error is not None:
            raise error
