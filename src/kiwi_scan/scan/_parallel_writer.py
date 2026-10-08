# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import io
import logging
import os
import queue
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar, Optional

from kiwi_scan.scan._point_frame import _PreparedPoint

logger = logging.getLogger(__name__)

@dataclass
class _WriteRequest:
    point: _PreparedPoint
    completed: Optional[threading.Event] = None
    error: Optional[BaseException] = None
    enqueued_at: float = field(default_factory=time.perf_counter)


class _ParallelPointWriter:
    """
    Backgound scan FIFO writer.
    Raw POSIX timestamps of the hot scan path is performed here.
    ISO-8601 conversion cannot consume scan loop CPU time.
    TODO: tune queue size for given applications, changing the flushing strategy will affact the optimum size.
    """

    _SENTINEL = object()   # unique ID for stop recording
    _TIMESTAMP_FORMATS: ClassVar[frozenset[str]] = frozenset({"iso8601", "unix"})

    def __init__(
        self,
        format_value: Callable[[Any], str],
        *,
        timestamp_output_format: str = "iso8601",
        queue_size: int = 1024,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be greater than zero")

        timestamp_output_format = str(timestamp_output_format).strip().lower()
        if timestamp_output_format not in self._TIMESTAMP_FORMATS:
            raise ValueError(
                "timestamp_output_format must be one of: iso8601, unix "
                f"(got {timestamp_output_format!r})"
            )

        self._format_value = format_value
        self._timestamp_output_format = timestamp_output_format
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
            raise ValueError(f"timestamp_output_format must be one of: iso8601, unix (got {timestamp_output_format!r})")

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
        """Start writing the ``output_file``."""
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
        """Queue one point, blocking only when writer queue is full. TODO: warning on blocking queue"""
        self._enqueue(_WriteRequest(point=point))

    def submit_and_wait(self, point: _PreparedPoint) -> None:
        """Queue one point and synchronously reports the result. Nothing is ever dropped """
        completed = threading.Event()
        request = _WriteRequest(point=point, completed=completed)
        self._enqueue(request)
        completed.wait()
        if request.error is not None:
            raise request.error

    def stop(self) -> None:
        """ End of scan: stop accepting points, drain the queue, flush, and join the worker."""
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
        """
        TODO: Optimize for extreme cases. In a step scan with a small number of detectors flushing the output
        could be done more frequent. For scans with very high data rates and nfs mounts the data could be bigger.
        """
        try:
            with ExitStack() as stack:
                output = None

                try:
                    # TODO: set `buffering` manually or not? For now defaults: local disc ~4kB, NFS ~1MB
                    output = stack.enter_context(open(str(self._output_file), "a", encoding="utf-8"))
                    blksize = os.fstat(output.fileno()).st_blksize
                    logger.debug( "FIFO writer %s: buffer %d bytes", self._output_file, blksize if blksize > 1 else io.DEFAULT_BUFFER_SIZE)
                except BaseException as exc:
                    logger.exception("Point writer thread for %s crashed", self._output_file)
                    self._record_error(exc)

                while True:
                    item = self._queue.get()
                    try:
                        if item is self._SENTINEL:
                            break

                        request = item
                        self._record_queue_delay(request.enqueued_at)

                        error = self._get_error()
                        if error is not None:
                            request.error = error
                        else:
                            try:
                                assert output is not None
                                line = self.format_point_line(
                                    request.point,
                                    self._format_value,
                                    timestamp_output_format=self._timestamp_output_format,
                                )
                                output.write(line + "\n")

                                if request.completed is not None:
                                    output.flush()
                            except BaseException as exc: 
                                logger.debug("Point writer failed on %s (%s request, %d still queued, row=%.200r): %r",
                                    self._output_file, "sync" if request.completed is not None else "async",
                                    self._queue.qsize(), request.point.row_values, exc, exc_info=True)
                                request.error = self._record_error(exc)

                        if request.completed is not None:
                            request.completed.set()
                    finally:
                        self._queue.task_done()
        except BaseException as exc:
            logger.exception( "Point writer for %s failed while writing or closing the file; this exception should never happen", self._output_file)
            self._record_error(exc)

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
