# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

""" 
Pluggable detector acquisition strategies for scan engines. 
yaml:
    - detector_reader_strategy: direct
      detector_pvs_monitor: false
    - detector_reader_strategy: direct
      detector_pvs_monitor: true
    - detector_reader_strategy: snapshot
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, List, Optional, Protocol, Sequence, Tuple

logger = logging.getLogger(__name__)


class DetectorPV(Protocol):
    """Detector-PV operations required by the built-in strategies."""

    pvname: str

    def check_pv(self) -> None:
        """Raise if the PV is uninitialized or disconnected."""
        ...

    def get_with_metadata(self, *, use_monitor: bool) -> Optional[Any]:
        """Return the current value and metadata."""
        ...

    def add_callback(
        self,
        callback: Callable[..., None],
        *,
        run_now: bool = False,
    ) -> int:
        """Register a monitor callback and return its identifier."""
        ...

    def remove_callback(self, callback_id: int) -> None:
        """Remove one callback previously returned by ``add_callback``."""
        ...


class DetectorReadStrategy(ABC):
    """Strategy interface used by :class:`DetectorReader`."""

    @abstractmethod
    def start(self) -> None:
        """Prepare acquisition resources."""

    @abstractmethod
    def read(self) -> List[Any]:
        """Return one ordered reading per configured detector."""

    @abstractmethod
    def stop(self) -> None:
        """Release acquisition resources."""


class DirectDetectorReadStrategy(DetectorReadStrategy):
    """Read every detector through the established synchronous wrapper API."""

    def __init__(
        self,
        detector_pvs: Sequence[DetectorPV],
        *,
        use_monitor: bool,
    ) -> None:
        self._detector_pvs = tuple(detector_pvs)
        self._use_monitor = use_monitor

    def start(self) -> None:
        """Direct acquisition does not own additional resources."""
        logger.debug( "Starting direct detector acquisition: detectors=%d use_monitor=%s", len(self._detector_pvs), self._use_monitor)

    def read(self) -> List[Any]:
        readings: List[Any] = []
        for pv in self._detector_pvs:
            try:
                reading = pv.get_with_metadata(use_monitor=self._use_monitor)
                if reading is None:
                    # HOT PATH (blocks scan loop)                    
                    logger.info("Detector read returned no data: pv=%s", pv.pvname)
                readings.append(reading)
            except Exception as exc:  # noqa: BLE001
                # HOT PATH (blocks scan loop)
                logger.error("Detector read failed: pv=%s error=%s", pv.pvname, exc)
                readings.append(None)
        return readings

    def stop(self) -> None:
        """Direct acquisition does not own additional resources."""
        logger.debug("Stopped direct detector acquisition")


class MonitorSnapshotDetectorReadStrategy(DetectorReadStrategy):
    """ Return cached snapshot of values maintained by monitor callbacks. """

    def __init__(
        self,
        detector_pvs: Sequence[DetectorPV],
        *,
        use_monitor: bool = True,
    ) -> None:
        self._detector_pvs = tuple(detector_pvs)
        self._use_monitor = use_monitor
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._latest: List[Any] = [None] * len(self._detector_pvs)
        self._callback_handles: List[Tuple[DetectorPV, int]] = []
        self._started = False
        self._generation = 0
        self._active_generation: Optional[int] = None

    def _warn_connection_status(self, phase: str) -> None:
        """Check connections outside acquisition and aggregate failures."""
        failures: List[str] = []
        for pv in self._detector_pvs:
            try:
                pv.check_pv()
            except Exception as exc:  # noqa: BLE001
                # Diagnostics must not interrupt the scan, warn once for all
                failures.append(f"{pv.pvname} ({exc})")
        if failures:
            logger.warning("Detector connection check %s: failed=%d/%d PVs=%s; snapshot readings retain the last cached values",
                phase, len(failures), len(self._detector_pvs), "; ".join(failures))

    def _build_callback(
        self,
        index: int,
        generation: int,
    ) -> Callable[..., None]:
        def _callback(
            pvname: Optional[str] = None,
            value: Any = None,
            **metadata: Any,
        ) -> None:
            reading = dict(metadata)
            reading["value"] = value
            reading["pvname"] = (
                pvname or self._detector_pvs[index].pvname
            )
            with self._lock:
                if (
                    self._started
                    and self._active_generation == generation
                ):
                    self._latest[index] = reading

        return _callback

    def start(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._started:
                    logger.debug("Detector snapshot acquisition already started")
                    return
                cleanup_required = bool(self._callback_handles)

            if cleanup_required:
                logger.debug("Cleaning up %d stale detector callbacks before start", len(self._callback_handles))
                self.stop()

            with self._lock:
                self._latest = [None] * len(self._detector_pvs)
                self._generation += 1
                generation = self._generation
                self._active_generation = generation
                self._started = True

            try:
                for index, pv in enumerate(self._detector_pvs):
                    callback_id = pv.add_callback(
                        self._build_callback(index, generation),
                        run_now=True,
                    )
                    with self._lock:
                        self._callback_handles.append((pv, callback_id))
            except Exception:
                logger.exception("Detector snapshot startup failed: registered=%d detectors=%d",
                    len(self._callback_handles), len(self._detector_pvs))
                try:
                    self.stop()
                except Exception:
                    logger.exception("Failed to clean up detector callbacks after startup error")
                raise

            logger.debug(
                "Started detector snapshot acquisition: detectors=%d callbacks=%d generation=%d",
                len(self._detector_pvs), len(self._callback_handles), generation)
            if not self._use_monitor:
                logger.warning("detector_pvs_monitor=false is ignored for snapshot detector reader strategy")
            self._warn_connection_status("after reader startup") # only outside of HOT PATH

    def read(self) -> List[Any]:
        with self._lock:
            if not self._started:
                raise RuntimeError("Detector snapshot strategy is not started")
            return list(self._latest)

    def stop(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if not self._started and not self._callback_handles:
                    return
                was_started = self._started

            # Do not hold the snapshot lock while checking Channel Access.
            if was_started:
                self._warn_connection_status("before reader teardown")

            with self._lock:
                self._started = False
                self._active_generation = None
                callback_handles = list(self._callback_handles)
                self._callback_handles = []

            failed_handles: List[Tuple[DetectorPV, int]] = []
            first_error: Optional[Exception] = None
            for pv, callback_id in callback_handles:
                try:
                    pv.remove_callback(callback_id)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to remove detector callback for PV %s: %s", pv.pvname, exc)
                    failed_handles.append((pv, callback_id))
                    if first_error is None:
                        first_error = exc

            with self._lock:
                self._callback_handles = failed_handles

            if first_error is not None:
                raise RuntimeError(
                    "Failed to stop one or more detector callbacks"
                ) from first_error

            logger.debug("Stopped detector snapshot acquisition: callbacks=%d", len(callback_handles))


class DetectorReader:
    """Facade that keeps scan engines independent of acquisition strategy."""

    def __init__(self, strategy: DetectorReadStrategy) -> None:
        self._strategy = strategy

    @property
    def strategy(self) -> DetectorReadStrategy:
        """Return the configured strategy for diagnostics and testing."""
        return self._strategy

    def start(self) -> None:
        self._strategy.start()

    def read(self) -> List[Any]:
        return self._strategy.read()

    def stop(self) -> None:
        self._strategy.stop()


def create_detector_reader(
    strategy_name: str,
    detector_pvs: Sequence[DetectorPV],
    *,
    use_monitor: bool,
) -> DetectorReader:
    """Create a detector reader for one of the built-in strategy names."""
    normalized = str(strategy_name).strip().lower().replace("-", "_")
    if normalized == "monitor_snapshot":
        normalized = "snapshot"

    if normalized == "direct":
        strategy: DetectorReadStrategy = DirectDetectorReadStrategy(
            detector_pvs,
            use_monitor=use_monitor,
        )
    elif normalized == "snapshot":
        strategy = MonitorSnapshotDetectorReadStrategy(
            detector_pvs,
            use_monitor=use_monitor,
        )
    else:
        raise ValueError(f"Unknown detector reader strategy {strategy_name!r}")

    logger.debug("Configured detector reader: strategy=%s detectors=%d use_monitor=%s", normalized, len(detector_pvs), use_monitor)
    return DetectorReader(strategy)


__all__ = [
    "DetectorReadStrategy",
    "DetectorReader",
    "DirectDetectorReadStrategy",
    "MonitorSnapshotDetectorReadStrategy",
    "create_detector_reader",
]
