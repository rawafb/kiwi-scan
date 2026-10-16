# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from kiwi_scan.plugin.base import ScanPlugin
from kiwi_scan.plugin.context import ScanPluginContext


class AsyncScanPlugin(ScanPlugin):
    """
    Base class for non-blocking scan-point plugin processing.
    Interface to be implemented
    - ``get_headers(timestamps)`` - return column header strings
    - ``get_sync_values(idx, pos)``  - per-point values
    - ``get_async_default_values()`` - pending/error fallback values
    - ``build_data_snapshot(idx, pos)`` to copy all data needed by the worker
    - ``process_data_snapshot(data_snapshot)`` for the non blocking processing step
    """

    def __init__(
        self,
        name: str,
        parameters: Optional[Dict[str, Any]] = None,
        scan: Optional[ScanPluginContext] = None,
    ):
        super().__init__(name=name, parameters=parameters, scan=scan)

        # normally one thread but control applications easiyly need more
        # override get_values() for better handling of more threads
        self._max_workers = max(1, int(self.parameters.get("max_workers", 1)))

        self._async_executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix=f"kiwi-plugin-{self.name}",
        )
        self._lock = threading.RLock()  # plugin must be reentrant 
        self._async_job: Optional[Future] = None    # asynchronous task
        self._latest_async_result_values: List[Any] = list(self.get_async_default_values())
        self._latest_async_result_timestamp: Optional[float] = None
        self._latest_async_error: Optional[str] = None
        self._submitted_jobs = 0
        self._completed_jobs = 0
        self._dropped_snapshots = 0
        self._closed = False

        self.logger.debug("Initialized async plugin %s with max_workers=%d", self.name, self._max_workers)
    
    def _update_async_data(self) -> None:
        """ Copy finished async job data into the latest-result cache. """
        with self._lock:
            async_job = self._async_job

        if async_job is None:
            self.logger.debug("Async plugin %s has no pending job", self.name)
            return
        if not async_job.done():
            self.logger.debug("Async plugin %s has no finished result yet", self.name)
            return

        try:
            result_values = async_job.result()
            if result_values is None:
                result_values = []
            result_values = list(result_values)
            error = None
            self.logger.debug("Async plugin %s completed, %d result values", self.name, len(result_values))
        except Exception as exc: # noqa: BLE001  - Framework boundary
            self.logger.error("Async plugin %s processing failed: %s", self.name, exc)
            result_values = list(self.get_async_default_values())
            error = str(exc)

        with self._lock:
            if self._async_job is async_job:
                self._latest_async_result_values = result_values
                self._latest_async_result_timestamp = time.time()
                self._latest_async_error = error
                self._async_job = None
                self._completed_jobs += 1

    def _submit_async_background_job(self, idx: int, pos: Any) -> None:
        """ Submit one background job unless the plugin is already busy. """
        data_snapshot = self.build_data_snapshot(idx, pos)
        if data_snapshot is None:
            self.logger.debug("Async plugin %s skipped submission for idx=%s: data is None", self.name, idx)
            return

        with self._lock:
            if self._closed:
                self.logger.debug("Async plugin %s closed, ignore submission", self.name)
                return

            # until the result  is collected by _update_async_data()
            if self._async_job is not None:
                self._dropped_snapshots += 1
                self.logger.debug("Async plugin %s dropped snapshot: idx=%s - previous job not collected", self.name, idx)
                return

            self._async_job = self._async_executor.submit(self.process_data_snapshot, data_snapshot)
            self._submitted_jobs += 1
            self.logger.debug("Async plugin %s: background job #%d [idx=%s]", self.name, self._submitted_jobs, idx)

    def get_sync_values(self, idx: int, pos: Any) -> List[Any]:
        """ Synchronous values for the current scan point. """
        return []

    def get_async_default_values(self) -> List[Any]:
        """
        Return default async values used before the first result is ready.
        These values are also used after a failed async job. The length must get_headers().
        """
        return []

    def build_data_snapshot(self, idx: int, pos: Any) -> Any:
        """
        Build input data snapshot for background processing. This method runs in the scan thread.         
        Returns ``None`` to skip submitting a background job for this point.
        """
        return {
            "scanIndex": idx,
            "position": pos,
        }

    def process_data_snapshot(self, data_snapshot: Any) -> List[Any]:
        """ 
        -------------------------------------------------------------------------------
        Parallel thread: Process one data snapshot in the background worker.
        Runs outside of the scan loop for asynchronous values added later to scan rows.
        -------------------------------------------------------------------------------
        """
        return []

    def get_values(self, idx: int, pos: Any) -> List[Any]:
        """
        -------------------------------------------------------------------------------
        Return sync values plus the latest completed async result values.
        Non-blocking readout of the async job if done.
        -------------------------------------------------------------------------------
        """
        self._update_async_data()
        self._submit_async_background_job(idx, pos)

        sync_values = self.get_sync_values(idx, pos)

        with self._lock:
            async_values = list(self._latest_async_result_values)

        return list(sync_values) + async_values

    def get_async_status(self) -> Dict[str, Any]:
        """ Debug/status information. """
        with self._lock:
            running = self._async_job is not None and not self._async_job.done()
            return {
                "running": running,
                "submitted_jobs": self._submitted_jobs,
                "completed_jobs": self._completed_jobs,
                "dropped_snapshots": self._dropped_snapshots,
                "latest_result_timestamp": self._latest_async_result_timestamp,
                "latest_error": self._latest_async_error,
                "max_workers": self._max_workers,
            }

    def close(self) -> None:
        """ Stop accepting new work and shut down the async executor. """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            async_job = self._async_job

        if async_job is not None:
            cancelled = async_job.cancel()
            self.logger.debug("Async plugin %s: cancel active job: %s", self.name, cancelled)

        self.logger.debug("Closing async plugin %s: submitted=%d completed=%d dropped=%d",
            self.name,
            self._submitted_jobs,
            self._completed_jobs,
            self._dropped_snapshots,
        )
        # make lint happy
        if sys.version_info >= (3, 9):
            shutdown_kwargs = {"wait": False, "cancel_futures": True}
        else:
            shutdown_kwargs = {"wait": False}
        self._async_executor.shutdown(**shutdown_kwargs)

    def on_end(self) -> None:
        self.close()
