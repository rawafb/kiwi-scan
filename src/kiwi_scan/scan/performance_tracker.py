# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

""" Performance timing and diagnostic reporting for scan execution.
    TODO: Iterative calculations for stddev, P95, (or memory limit)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PerformanceTracker:
    """Collect optional scan timing samples and print the final summary.

    The tracker deliberately has no dependency on ``BaseScan``. Runtime
    counters that remain owned by other subsystems are supplied through narrow
    callbacks and are evaluated only when ``report()`` is called.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        metadata_queue_drop_count: Optional[Callable[[], int]] = None,
        writer_metrics: Optional[Callable[[], Tuple[int, float]]] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.samples: Dict[str, List[float]] = defaultdict(list)
        self._metadata_queue_drop_count = metadata_queue_drop_count
        self._writer_metrics = writer_metrics

        if self.enabled:
            logger.info("Performance reporting enabled")
        else:
            logger.debug("Performance report disabled")

    def record_sample(
        self,
        name: str,
        started_at: float,
        idx: Optional[int] = None,
    ) -> None:
        """Record elapsed wall time for a block whose start time is known."""
        elapsed = time.perf_counter() - started_at
        self.samples[name].append(elapsed)
        if idx is not None:
            logger.debug("[PERF] idx=%d %-20s %.6f s", idx, name, elapsed)
        else:
            logger.debug("[PERF] %-20s %.6f s", name, elapsed)

    @contextmanager
    def time_block(
        self,
        name: str,
        *,
        idx: Optional[int] = None,
    ) -> Iterator[None]:
        """Measure one block when performance reporting is enabled."""
        if not self.enabled:
            yield
            return

        started_at = time.perf_counter()
        try:
            yield
        finally:
            self.record_sample(name, started_at, idx)

    @staticmethod
    def _p95(values: List[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = int(0.95 * (len(ordered) - 1))
        return ordered[index]

    def _read_metadata_queue_drop_count(self) -> int:
        if self._metadata_queue_drop_count is None:
            return 0
        try:
            return int(self._metadata_queue_drop_count())
        except Exception:
            logger.debug(
                "Failed to read metadata queue drop count for performance report",
                exc_info=True,
            )
            return 0

    def _read_writer_metrics(self) -> Tuple[int, float]:
        if self._writer_metrics is None:
            return 0, 0.0
        try:
            queue_high_water, maximum_queue_delay = self._writer_metrics()
            return int(queue_high_water), float(maximum_queue_delay)
        except Exception:
            logger.debug(
                "Failed to read point-writer metrics for performance report",
                exc_info=True,
            )
            return 0, 0.0

    def report(self) -> None:
        """Print and log a compact summary once at the end of a scan."""
        if not self.enabled:
            return

        metadata_queue_drops = self._read_metadata_queue_drop_count()
        writer_queue_high_water, writer_maximum_queue_delay = (
            self._read_writer_metrics()
        )

        print("========== PERFORMANCE SUMMARY ==========")
        for name, values in sorted(self.samples.items()):
            if not values:
                continue
            count = len(values)
            total = sum(values)
            mean = total / count
            maximum = max(values)
            p95 = self._p95(values)

            print(
                f"[PERF] {name:<20} "
                f"n={count} total={total:.3f}s mean={mean:.6f}s "
                f"p95={p95:.6f}s max={maximum:.6f}s"
            )
            logger.info(
                "[PERF] %-20s n=%d total=%.3fs mean=%.6fs p95=%.6fs max=%.6fs",
                name,
                count,
                total,
                mean,
                p95,
                maximum,
            )

        print(
            f"[PERF] {'metadata_queue_drops':<20} "
            f"count={metadata_queue_drops}"
        )
        logger.info(
            "[PERF] %-20s count=%d",
            "metadata_queue_drops",
            metadata_queue_drops,
        )
        print(
            "[PERF] "
            f"{'point_writer_queue_high_water':<20} "
            f"count={writer_queue_high_water}"
        )
        print(
            "[PERF] "
            f"{'point_writer_max_queue_delay':<20} "
            f"seconds={writer_maximum_queue_delay:.6f}"
        )
        print("==========================================")
