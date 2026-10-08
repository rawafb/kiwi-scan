# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from queue import Empty, Full, Queue
from typing import Any, Dict, List, Optional

import numpy as np

from kiwi_scan.epics_wrapper import EpicsPV as PV

logger = logging.getLogger(__name__)

class MetadataCAMonitor:
    """
    Event-driven sidecar logger for metadata PVs.

    Append initial PV snapshots and monitor updates to a TSV sidecar.
    Stop drains accepted events; 
    Errors are conted reported outside, in some cases linter warnings BLE001 are suppressed for that reason.
    """

    def __init__(
        self,
        pvs: List[str],
        constants: Dict[str, Any],
        outfile: str,
        queue_maxsize: int = 10000,
    ) -> None:
        self._pvspecs = pvs or []
        self._constants = dict(constants or {})
        self._outfile = outfile
        self._q: Queue[Dict[str, Any]] = Queue(maxsize=queue_maxsize)
        self._stop = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None
        self._pvobjs: List[PV] = []
        self._drop_lock = threading.Lock()
        self._dropped_events = 0
        self._accepting = False
        self._rows_written = 0
        self._format_failures = 0
        self._callback_failures = 0
        self._unwritten_events = 0
        self._writer_error: Optional[Exception] = None
        self._writer_failed = threading.Event()
        self._inflight = False

    def start(self) -> None:
        if self._writer_thread is not None:
            if self._writer_thread.is_alive():
                logger.debug(
                    "MetadataCAMonitor: restart blocked; writer still running file=%s "
                    "queued_events=%d inflight=%s",
                    self._outfile, self._q.qsize(), self._inflight,
                )
                raise RuntimeError("Metadata writer is still running; stop it before restarting")
            if not self._stop.is_set():
                raise RuntimeError("Call stop() to clean up the previous metadata run")
            # A timed-out stop completed in the background. Account for any
            # remaining events before resetting the session state.
            self.stop(join_timeout=0)
        if not self._pvspecs and not self._constants:
            logger.info("MetadataCAMonitor: nothing to start (no PVs/constants).")
            return

        # 1) Write file header (constants + column names)
        logger.debug(
            "MetadataCAMonitor: starting file=%s configured_pvs=%d queue_maxsize=%d",
            self._outfile, len(self._pvspecs), self._q.maxsize,
        )
        self._write_header()

        # 2) Create PVs, install callbacks (events go to queue even before writer starts)
        self._stop.clear()
        self._writer_failed.clear()
        self._writer_error = None
        with self._drop_lock:
            self._accepting = True
        self._pvobjs = []
        for name in self._pvspecs:
            try:
                pv = PV(name, auto_monitor=True)
                pv.add_callback(self._on_event)
                self._pvobjs.append(pv)
            except ConnectionError as exc:
                logger.warning(
                    "MetadataCAMonitor: skipping unavailable PV %s: %s",
                    name,
                    exc,
                )
            except Exception:
                logger.exception(
                    "MetadataCAMonitor: failed to subscribe %s",
                    name,
                )

        # 3) Write one initial snapshot row per PV at the TOP (right after header)
        #    This guarantees an initial value even when CA monitors only fire on change.
        snapshot_rows_before = self._rows_written
        try:
            self._write_initial_snapshot_rows()
        except Exception as exc: # noqa BLE001
            self._record_writer_error(exc)
        logger.debug(
            "MetadataCAMonitor: initial snapshots finished file=%s "
            "rows_written=%d writer_failed=%s queued_events=%d",
            self._outfile, self._rows_written - snapshot_rows_before,
            self._writer_failed.is_set(), self._q.qsize(),
        )

        # 4) Start writer thread for subsequent monitor events
        self._stop.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="scan-meta-writer",
            daemon=True,
        )
        self._writer_thread.start()

        logger.info(
            "MetadataCAMonitor: started with %d PVs → %s",
            len(self._pvobjs),
            self._outfile,
        )

    def stop(self, join_timeout: float = 2.0) -> None:
        disconnected = []
        for pv in self._pvobjs:
            try:
                pv.check_pv()
            except Exception: # noqa BLE001
                disconnected.append(pv.pvname)
            try:
                pv.clear_callbacks()
            except Exception:
                logger.debug("Failed to clear %s", pv.pvname, exc_info=True)
            try:
                pv.disconnect()
            except Exception:
                logger.debug("Failed to disconnect %s", pv.pvname, exc_info=True)
        self._pvobjs.clear()

        # Serialize admission with callbacks already in flight. Once stopped,
        # late callbacks cannot append behind the writer's final empty check.
        with self._drop_lock:
            self._accepting = False
        self._stop.set()
        thread = self._writer_thread
        rows_before_join = self._rows_written
        drain_started = time.monotonic()
        logger.debug(
            "MetadataCAMonitor: callbacks removed; draining file=%s "
            "queued_events=%d inflight=%s join_timeout=%s writer_failed=%s",
            self._outfile, self._q.qsize(), self._inflight, join_timeout,
            self._writer_failed.is_set(),
        )
        if thread is not None:
            thread.join(timeout=join_timeout)
        running = thread is not None and thread.is_alive()
        abandoned_events = 0
        if not running:
            self._writer_thread = None
            # A failed writer cannot drain; account for every remaining event.
            while True:
                try:
                    self._q.get_nowait()
                except Empty:
                    break
                abandoned_events += 1
                with self._drop_lock:
                    self._unwritten_events += 1
        pending = self._q.qsize() + int(self._inflight)
        logger.debug(
            "MetadataCAMonitor: drain finished file=%s elapsed_s=%.3f "
            "rows_written_during_join=%d queued_events_accounted_unwritten=%d "
            "pending_events=%d writer_running=%s",
            self._outfile, time.monotonic() - drain_started,
            self._rows_written - rows_before_join, abandoned_events, pending, running,
        )
        if running:
            logger.debug(
                "MetadataCAMonitor: join timed out; retaining writer reference "
                "and blocking restart file=%s pending_events=%d",
                self._outfile, pending,
            )
        log = logger.warning if (
            running or self._dropped_events or self._unwritten_events
            or self._format_failures or self._callback_failures
            or self._writer_error is not None or disconnected
        ) else logger.info
        log(
            "MetadataCAMonitor: stopped rows_written=%d format_failures=%d "
            "callback_failures=%d dropped_queue_events=%d unwritten_events=%d "
            "pending_events=%d writer_running=%s writer_error=%r disconnected_pvs=%s",
            self._rows_written, self._format_failures, self._callback_failures,
            self.get_drop_count(), self._unwritten_events, pending, running,
            self._writer_error, disconnected,
        )

    def get_drop_count(self) -> int:
        """Return the number of monitor events dropped because the queue was full."""
        with self._drop_lock:
            return self._dropped_events

    @property
    def dropped_events(self) -> int:
        """Number of queue-full drops recorded since this monitor was created."""
        return self.get_drop_count()

    # ---------- internals ----------
    def _write_header(self) -> None:
        cols = [
            "TS-ISO8601",      # wall-clock receive time (UTC)
            "PV",              # pv name
            "VALUE",           # best-effort numeric or str
            "PV-TS-ISO8601",   # PV timestamp if available
            "SEVR",            # severity if available
            "STAT",            # status if available
        ]

        columns = "\t".join(cols) + "\n"
        # Format everything first: a bad constant must not leave a partial header.
        parts = []
        if self._constants:
            parts.append("# metadata_constants\n")
            parts.extend(f"# {k}\t{v}\n" for k, v in self._constants.items())
            parts.append("# --- metadata above; monitor data below ---\n")
        parts.append(columns)
        header = "".join(parts)

        with open(self._outfile, "a+", encoding="utf-8") as f:
            f.seek(0, 2)
            existing_bytes = f.tell()
            if existing_bytes:
                f.seek(0)
                existing_columns = next(
                    (line for line in f if not line.startswith("#")), None,
                )
                if existing_columns != columns:
                    raise ValueError(
                        "Metadata file has an incomplete or invalid header: "
                        f"{self._outfile}"
                    )
                logger.debug(
                    "MetadataCAMonitor: resuming validated file=%s existing_bytes=%d; "
                    "preserving header and rows, appending initial snapshots and updates",
                    self._outfile, existing_bytes,
                )
                return
            f.write(header)
            f.flush()
        logger.debug("MetadataCAMonitor: wrote new header file=%s", self._outfile)

    def _write_initial_snapshot_rows(self) -> None:
        """
        Append exactly one snapshot row per PV right after the header.
        Uses the same column format as the monitor updates.
        """
        if not self._pvobjs:
            return

        opened = False
        try:
            with open(self._outfile, "a", encoding="utf-8") as f:
                opened = True
                for index, pv in enumerate(self._pvobjs):
                    pvname = pv.pvname
                    pvname = pvname or "UNKNOWN"

                    md = None
                    try:
                        md = pv.get_with_metadata()
                    except Exception:
                        logger.debug("Failed to read metadata PV %s", pv.pvname, exc_info=True)
                        md = None

                    value = None
                    ts = None
                    sevr = None
                    stat = None

                    if isinstance(md, dict) and md:
                        value = md.get("value")
                        ts = md.get("timestamp")
                        sevr = md.get("severity")
                        stat = md.get("status")
                    else:
                        value = None

                    try:
                        self._write_event(f, {
                            "recv_ts": time.time(), "pv": pvname, "value": value,
                            "pv_ts": ts, "sevr": sevr, "stat": stat,
                        })
                    except Exception:
                        with self._drop_lock:
                            self._unwritten_events += len(self._pvobjs) - index - 1
                        raise
        except Exception:
            if not opened:
                with self._drop_lock:
                    self._unwritten_events += len(self._pvobjs)
            raise

    def _on_event(self, **kwargs) -> None:
        # No formatting, file I/O or logging on the CA callback thread.
        with self._drop_lock:
            if not self._accepting:
                return
            if self._writer_failed.is_set():
                self._unwritten_events += 1
                return
            try:
                self._q.put_nowait({
                    "recv_ts": time.time(),
                    "pv": kwargs.get("pvname") or kwargs.get("pv") or "UNKNOWN",
                    "value": kwargs.get("value"),
                    "pv_ts": kwargs.get("timestamp"),
                    "sevr": kwargs.get("severity"),
                    "stat": kwargs.get("status"),
                })
            except Full:
                self._dropped_events += 1
            except Exception: # noqa BLE001
                self._callback_failures += 1

    @staticmethod
    def _ts_to_iso(ts: Any) -> str:
        try:
            if ts is not None:
                return datetime.fromtimestamp( float(ts), tz=timezone.utc,).isoformat()
            return ""
        except (TypeError, ValueError, OverflowError, OSError):
            return ""

    def _record_writer_error(self, exc: Exception) -> None:
        first_error = self._writer_error is None
        if first_error:
            self._writer_error = exc
        self._writer_failed.set()
        if first_error:
            logger.debug(
                "MetadataCAMonitor: writer failed file=%s queued_events=%d "
                "unwritten_events=%d; queued rows will be accounted at stop",
                self._outfile, self._q.qsize(), self._unwritten_events,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _write_event(self, f, ev: Dict[str, Any]) -> None:
        try:
            row = [
                self._ts_to_iso(ev.get("recv_ts")),
                self._fmt_plain(ev.get("pv")),
                self._fmt_value(ev.get("value")),
                self._ts_to_iso(ev.get("pv_ts")),
                self._fmt_plain(ev.get("sevr")),
                self._fmt_plain(ev.get("stat")),
            ]
            line = "\t".join(row) + "\n"
        except Exception:
            with self._drop_lock:
                self._format_failures += 1
                first_failure = self._format_failures == 1
            if first_failure:
                logger.debug(
                    "MetadataCAMonitor: skipping unformattable row file=%s "
                    "value_type=%s; writer continues, further failures counted in stop summary",
                    self._outfile, type(ev.get("value")).__name__, exc_info=True,
                )
            return
        try:
            f.write(line)
            f.flush()
        except Exception:
            # This row may have been partially written. Do not retry it.
            with self._drop_lock:
                self._unwritten_events += 1
            raise
        with self._drop_lock:
            self._rows_written += 1

    def _writer_loop(self) -> None:
        try:
            if self._writer_failed.is_set():
                logger.debug(
                    "MetadataCAMonitor: writer not started after snapshot failure file=%s",
                    self._outfile,
                )
                return
            with open(self._outfile, "a", encoding="utf-8") as f:
                logger.debug("MetadataCAMonitor: writer opened for append file=%s", self._outfile)
                while True:
                    try:
                        ev = self._q.get(timeout=0.25)
                    except Empty:
                        if self._stop.is_set():
                            logger.debug(
                                "MetadataCAMonitor: queue drained; writer exiting file=%s",
                                self._outfile,
                            )
                            break
                        continue
                    self._inflight = True
                    try:
                        self._write_event(f, ev)
                    finally:
                        self._inflight = False
        except Exception as exc: # noqa BLE001
            self._record_writer_error(exc)

    @staticmethod
    def _fmt_scalar(value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, (int, float)):
            return f"{float(value):.12e}"

        return str(value)

    @classmethod
    def _fmt_value(cls, value: Any) -> str:
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", errors="replace")

        if isinstance(value, np.ndarray):
            # A zero-dimensional array contains one scalar value.
            if value.ndim == 0:
                return cls._fmt_scalar(value.item())

            values = value.tolist()

        elif isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            values = value

        else:
            return cls._fmt_scalar(value)

        formatted_values = (cls._fmt_scalar(item) for item in values)
        return f"[{' '.join(formatted_values)}]"

    @staticmethod
    def _fmt_plain(v: Any) -> str:
        return "" if v is None else str(v)

