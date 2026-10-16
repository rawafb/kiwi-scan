# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

import epics

logger = logging.getLogger(__name__)
_CA_LOCK = threading.Lock()


def ensure_ca_context() -> None:
    """ Attach the current thread to the initial EPICS CA context if possible.  """
    try:
        epics.ca.use_initial_context()
    except epics.ca.ChannelAccessException:
        logger.debug("Could not attach initial EPICS CA context", exc_info=True)

def _safe_poll() -> None:
    """ 
    try: Yield to EPICS CA
    Best-effort; varies across pyepics versions.
    """
    try:
        time.sleep(0.01)
        epics.ca.poll()
    except Exception:
        logger.debug("EPICS CA poll failed", exc_info=True)

class EpicsPV:
    """
    Safe EPICS PV wrapper.
    Some CA-touching calls are serialized with _CA_LOCK to avoid segfaults from EPICS base.
    """
    def __init__(
        self,
        pvname: str,
        timeout: float = 1.0,
        queueing_delay: float = 0.01,
        auto_monitor: bool = False,
        callback: Optional[Callable[..., None]] = None,
        connection_timeout: Optional[float] = None,
    ):
        if queueing_delay < 0:
            raise ValueError(f"queueing_delay must be non-negative, got {queueing_delay}")

        self.pvname = pvname
        self.timeout = float(timeout)
        self.queueing_delay = float(queueing_delay)
        self.connection_timeout = float(connection_timeout) if connection_timeout is not None else max(10.0, self.timeout)

        self._pv: Optional[epics.PV] = None
        self._callback_refs: Dict[int, Callable[..., None]] = {}
        self.last_written: Any = None

        logger.debug("Creating PV %s", pvname)

        # Create + connect (serialized)
        def _create_and_connect() -> bool:
            try:
                pv = epics.PV(pvname, auto_monitor=False, connection_timeout=self.connection_timeout)
            except TypeError:
                pv = epics.PV(pvname, auto_monitor=False)

            self._pv = pv
            logger.debug("Wait for connection to %s", pvname)
            ok = pv.wait_for_connection(timeout=self.connection_timeout)
            _safe_poll()
            return ok

        try:
            ok = self._ca(_create_and_connect)
            if not ok:
                raise ConnectionError(f"EPICS PV '{pvname}' did not connect within {self.connection_timeout:.2f}s")
        except BaseException:
            try:
                self.close()
            except Exception:
                logger.debug("Failed to close PV %s after connection failed", pvname, exc_info=True)
            raise

        # Enable monitoring AFTER connection (serialized)
        if auto_monitor:
            self._ca(self._enable_monitoring)

        # Optional callback
        if callback and auto_monitor:
            self.add_callback(callback, run_now=False)

    # ----------------- internal helpers -----------------

    def _ca(self, fn: Callable[[], Any]) -> Any:
        """Run a function while holding the global CA lock."""
        with _CA_LOCK:
            return fn()

    def _require_pv(self) -> epics.PV:
        if self._pv is None:
            raise RuntimeError(f"PV '{self.pvname}' not initialized")
        return self._pv

    def _enable_monitoring(self) -> None:
        pv = self._require_pv()
        try:
            pv.auto_monitor = True
        except Exception:
            logger.debug("Failed to enable EPICS auto_monitor", exc_info=True)
        _safe_poll()

    @property
    def nelm(self) -> Optional[int]:
        """Return the configured number of PV elements when available."""
        pv = self._require_pv()
        raw_nelm = getattr(pv, "nelm", None)
        if raw_nelm is None:
            return None
        try:
            return int(raw_nelm)
        except (TypeError, ValueError):
            logger.debug(
                "Invalid NELM value for PV %s: %r",
                self.pvname,
                raw_nelm,
            )
            return None

    def _wrap_callback(self, user_cb: Callable[..., None]) -> Callable[..., None]:
        def _cb(pvname=None, value=None, **kwargs):
            try:
                user_cb(pvname=pvname, value=value, **kwargs)
            except TypeError:
                # logger.error("Callback mismatch: %s %s", user_cb, e)
                user_cb(pvname, value)
            except Exception:
                logger.exception("Error in PV callback for '%s'", self.pvname)
        return _cb

    # ----------------- public API -----------------

    def get(self, *, use_monitor: bool = False, timeout: Optional[float] = None) -> Any:
        """ Get PV and floor timeout """
        pv = self._require_pv()
        # TODO: change to simply self.timeout if timeout is None?
        t = min(self.timeout, 1.0) if timeout is None else float(timeout)
        return pv.get(timeout=t, use_monitor=use_monitor)
    
    def get_fast(self) -> Optional[Dict[str, Any]]:
        if self._pv is None:
            return None
        return self._pv.get_with_metadata(use_monitor=True)

    def get_with_metadata(self, *, use_monitor: bool = False, timeout: Optional[float] = None, full: Optional[bool] = False) -> Optional[Dict[str, Any]]:
        """ Return dict with value + timestamp-ish metadata. TODO: how about full meta data? """
        pv = self._require_pv()
        t = min(self.timeout, 0.2) if timeout is None else float(timeout)

        def _do() -> Optional[Dict[str, Any]]:
            if full:
                meta = pv.get_with_metadata(use_monitor=use_monitor)
                return meta
            else:
                """ Thread safe usage: self._ca(_do) """
                val = pv.get(timeout=t, use_monitor=use_monitor)
                if val is None:
                    return None
                # pyepics has pv.timestamp, pv.severity, pv.status sometimes
                timestamp = pv.timestamp
                meta: Dict[str, Any] = {"value": val}
                meta["pvname"] = self.pvname
                if timestamp is None:
                    logger.debug("No timestamp available for PV %s; using local time", self.pvname)
                    meta["timestamp"] = time.time()
                else:
                    try:
                        meta["timestamp"] = float(timestamp)
                    except (TypeError, ValueError, OverflowError):
                        logger.debug("Invalid timestamp for PV %s; using local time", self.pvname, exc_info=True)
                        meta["timestamp"] = time.time()
                return meta

        return _do()

    def put(self, value: Any) -> bool:
        """ Put a value to the PV.
        Supports scalar writes and waveform (array) writes.
        """
        pv = self._require_pv()

        # Normalization (e.g. numpy arrays)
        v = value
        tolist = getattr(v, "tolist", None)
        if callable(tolist):
            v = tolist()
        try:
            pv.put(v, timeout=self.timeout)
            if self.queueing_delay > 0:
                time.sleep(self.queueing_delay)
            logger.debug("Set PV %s = %r", self.pvname, v)
            self.last_written = v
            return True
        except Exception as e: # noqa BLE001
            logger.error("Failed to set PV %s to %r: %s", self.pvname, v, e)
            return False
    
    def add_callback(self, callback: Callable[..., None], **kwargs: Any) -> int:
        pv = self._require_pv()
        wrapped = self._wrap_callback(callback)

        def _do_add() -> int:
            # Ensure monitoring (best effort)
            try:
                pv.auto_monitor = True
            except Exception:
                logger.debug("Failed to enable auto_monitor for PV %s", self.pvname, exc_info=True)
            _safe_poll()
            try:
                cb_index = pv.add_callback(wrapped, **kwargs)
            except TypeError:
                # Some versions don’t accept run_now, etc.
                kwargs.pop("run_now", None)
                kwargs.pop("with_ctrlvars", None)
                cb_index = pv.add_callback(wrapped, **kwargs)
            _safe_poll()

            if cb_index is None:
                raise RuntimeError(
                    f"Adding callback to PV {self.pvname!r} returned no callback index"
                )
            self._callback_refs[cb_index] = wrapped
            return cb_index

        return self._ca(_do_add)

    def remove_callback(self, index: int) -> None:
        """Remove one callback by the index returned from add_callback()."""
        pv = self._require_pv()

        def _do_remove() -> None:
            pv.remove_callback(index)
            self._callback_refs.pop(index, None)

        self._ca(_do_remove)

    def disconnect(self) -> None:
        """Disconnect this PV from Channel Access."""
        pv = self._require_pv()
        self._ca(pv.disconnect)

    # TODO: FIXME: Call close() from scan cleanups including subscriptions after stopping workers.
    def close(self) -> None:
        """ Release PV resources. Can be called repeatedly from users after workes have stopped.  """
        with _CA_LOCK:
            pv = self._pv
            if pv is None:
                return
            try:
                pv.clear_callbacks()
            except Exception:
                logger.debug("Failed to clear callbacks while closing PV %s", self.pvname, exc_info=True)
            # Keep PV if disconnect fails
            pv.disconnect()
            self._callback_refs.clear()
            self._pv = None

    def clear_callbacks(self) -> None:
        pv = self._require_pv()

        def _do_clear() -> None:
            try:
                pv.clear_callbacks()
            except Exception:
                logger.debug("Failed to clear callbacks for PV %s", self.pvname, exc_info=True)
            self._callback_refs.clear()
            _safe_poll()

        self._ca(_do_clear)

    def check_pv(self) -> None:
        if not self._pv:
            raise RuntimeError(f"PV '{self.pvname}' not initialized")
        ok = self._ca(lambda: bool(getattr(self._pv, "connected", False)))
        if not ok:
            raise ConnectionError(f"PV '{self.pvname}' not connected")
    
    # Ignore strict connection handling above
    @classmethod
    def create_monitor(cls, pvname: str, **kwargs) -> EpicsPV:
        """
        Create a PV for monitoring without blocking on connection.
        """
        obj = cls.__new__(cls)  # bypass __init__

        obj.pvname = pvname
        obj.timeout = float(kwargs.get("timeout", 1.0))
        obj.queueing_delay = float(kwargs.get("queueing_delay", 0.01))
        obj.connection_timeout = float(kwargs.get("connection_timeout", 10.0))
        obj._callback_refs = {}
        obj.last_written = None

        # create PV WITHOUT waiting
        obj._pv = epics.PV(pvname, auto_monitor=True)

        return obj

NO_ALARM = 0
MINOR_ALARM = 1
MAJOR_ALARM = 2
INVALID_ALARM = 3

STATUS_NAMES = {
    0: "NO_ALARM",
    1: "READ",
    2: "WRITE",
    3: "HIHI",
    4: "HIGH",
    5: "LOLO",
    6: "LOW",
    7: "STATE",
    8: "COS",
    9: "COMM",
    10: "TIMEOUT",
    11: "HWLIMIT",
    12: "CALC",
    13: "SCAN",
    14: "LINK",
    15: "SOFT",
    16: "BAD_SUB",
    17: "UDF",
    18: "DISABLE",
    19: "SIMM",
    20: "READ_ACCESS",
    21: "WRITE_ACCESS",
}

SEVERITY_NAMES = {
    NO_ALARM: "NO_ALARM",
    MINOR_ALARM: "MINOR",
    MAJOR_ALARM: "MAJOR",
    INVALID_ALARM: "INVALID",
}

SEVERITY_RANK = {
    "NO_ALARM": 0,
    "MINOR": 1,
    "MAJOR": 2,
    "INVALID": 3,
    "ERROR": 4,
}



def severity_name(severity: Optional[int]) -> str:
    """
    Return EPICS severity name.

    Examples
    --------
    >>> severity_name(3)
    'INVALID'

    >>> severity_name(None)
    'UNKNOWN'
    """
    if severity is None:
        return "UNKNOWN"

    return SEVERITY_NAMES.get(
        severity,
        f"UNKNOWN_SEVERITY({severity})",
    )

def severity_rank(severity: Optional[int]) -> int:
    """
    Convert EPICS severity to sortable rank.

    Examples
    --------
    >>> severity_rank(0)
    0

    >>> severity_rank(2)
    2

    >>> severity_rank(None)
    4
    """
    return SEVERITY_RANK.get(severity_name(severity), SEVERITY_RANK["ERROR"])

def status_name(status: Optional[int]) -> str:
    """
    Return EPICS alarm status name.

    Examples
    --------
    >>> status_name(17)
    'UDF'

    >>> status_name(None)
    'UNKNOWN'
    """
    if status is None:
        return "UNKNOWN"

    return STATUS_NAMES.get( status, f"UNKNOWN_STATUS({status})")

def alarm_info(
    status: Optional[int],
    severity: Optional[int],
) -> str:
    """
    Return formatted EPICS alarm information.
    """
    return (
        f"status={status_name(status)} "
        f"severity={severity_name(severity)}"
    )

def has_alarm( severity: Optional[int]) -> bool:
    """
    True if PV is in alarm state or invalid.
    """
    if severity is None:
        return True

    return severity != NO_ALARM
