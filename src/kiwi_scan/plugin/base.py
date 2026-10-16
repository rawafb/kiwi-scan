# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import logging
import os
import time
import weakref
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from kiwi_scan.actuator.single import PvEvent
from kiwi_scan.data.loader import get_kiwi_data_dir_from_environ
from kiwi_scan.plugin.context import ScanPluginContext


def wrap_values(values: List[Any]) -> List[Dict[str, Any]]:
    """
    Normalize plugin output values into the EPICS PV data structure expected by the scan writer.

    This method takes the raw values produced by the concrete plugin, align them
    with the expanded headers, and return a serializable list that can be
    written directly into the scan row. If `timestamps` is enabled in yaml config, 
    include the corresponding timestamp values in the correct positions.
    """
    result = []
    now = time.time()  # current time in seconds as float
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    for v in values:
        result.append({
            'value': v,
            'timestamp': now,
            'posixseconds': sec,
            'nanoseconds': nsec
        })
    return result

class ScanPlugin(ABC):
    def __init__(self, 
                 name: str,
                 parameters: Optional[Dict[str, Any]] = None,
                 scan: Optional[ScanPluginContext] = None):

        self.name = name
        self.parameters = parameters or {}
        self.logger = logging.getLogger(f"ScanPlugin.{self.name}")
        kiwi_data_dir = get_kiwi_data_dir_from_environ()
        plugin_log_dir = 'plugin_log'
        self.DEFAULT_LOG_FILE = 'plugin.log'
        if kiwi_data_dir is not None:
            self.log_dir = os.path.normpath(os.path.join(kiwi_data_dir, plugin_log_dir))
        else: 
            current_file_dir = os.path.dirname(os.path.abspath(__file__))
            self.log_dir =  os.path.normpath(os.path.join(current_file_dir, '..', '..', '..', plugin_log_dir))
        # Keep the existing weak reference while exposing only the plugin context type
        self.scan: Optional[ScanPluginContext] = (
            weakref.proxy(scan) if scan is not None else None
        )

    def _init_logging(self) -> None:
        """Configure plugin-specific file logging."""
        level = self.parameters.get("log_level", logging.WARNING)
        self.logger.setLevel(level)
        #self.logger.info(f"Set logging level to {level}, {self.parameters}")

        # This BUGFIXed the following: 
        # cannot access local variable 'filepath' where it is not associated with a value, 
        # -> Always compute filepath
        filepath = os.path.join(
            self.log_dir,
            self.parameters.get("log_file", self.DEFAULT_LOG_FILE),
        )

        # Ensure log directory exists
        os.makedirs(self.log_dir, exist_ok=True)

        # Only add handler once
        if not self.logger.handlers:
            handler = logging.FileHandler(filepath)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
            self.logger.addHandler(handler)

        self.logger.debug("Logging initialized at level %s to %s", level, filepath)
    
    def expand_headers(self, hdrs: List[str], timestamps: bool = False) -> List[str]:
        """
        Produce scan-file column headers.
        This should return the headers in the exact order they will appear in the output row.
        When `timestamps` is true, append any matching timestamp columns using the same ordering convention as the rest of the scan output.
        """
        if timestamps:
            expanded = []
            for h in hdrs:
                expanded += [h, f"TS-{h}"]
            return expanded
        return hdrs
    
    def on_start(self) -> None:
        pass

    def pre_move(self, idx: int, pos: Dict[str, Any]) -> None:
        """Step scans: called before the moves of point ``idx`` are issued.

        ``pos`` maps actuator names to their targets. Use it for preparation
        that is valid while the axes travel; keep it short, it runs in the scan thread.
        """

    def post_move(self, idx: int, pos: Dict[str, Any]) -> None:
        """Step scans: called once all axes of point ``idx`` are in position.

        Runs before the integration wait and detector reads, so an acquisition
        started here overlaps them and can be collected in ``get_values``.
        Not called for the backlash overshoot step or after a stop.
        """

    @abstractmethod
    def get_headers(self, timestamps: bool) -> List[str]:
        """
        Subclasses must return a list of column header strings.
        """
    
    @abstractmethod
    def get_values(self, idx: int, pos: Any) -> List[Any]:
        """Return additional data at each scan point. Data must match header."""
    
    def on_scan_point(self, idx: int, pos: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Return column headers wrapped as [{'value': header}, ...].
        Subclasses must implement get_header_names().
        """
        return wrap_values(self.get_values(idx, pos))

    def on_end(self) -> None:
        pass

    def close(self) -> None:
        """Release plugin resources.

        The default implementation is intentionally a no-op to keep existing
        synchronous plugins fully backward compatible. Plugins that own worker
        threads, executors, files, sockets, or other resources can override this
        hook.
        """

    def on_monitor(self, ev: PvEvent) -> None:
        """
        Optional hook: called on monitor updates.
        Default: Used as debug log.
        """
        if not self.logger.isEnabledFor(logging.DEBUG):
            return

        # Keep monitor timestamps in their native POSIX representation.
        ts = ev.timestamp if ev.timestamp is not None else "n/a"

        self.logger.info(
            "[MON] pv=%s value=%r ts=%r sev=%r stat=%r src=%r",
            ev.pvname,
            ev.value,
            ts,
            ev.severity,
            ev.status,
            ev.source,
        )

