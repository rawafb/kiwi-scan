import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from kiwi_scan.datamodels import SubscriptionConfig


class SyncSource:
    """Small interface for one source that can satisfy a sync cycle."""

    def __init__(self, name: str) -> None:
        self.name = name

    def arm(self, now: float) -> None:
        raise NotImplementedError

    def is_ready(self, now: float) -> bool:
        raise NotImplementedError

    def next_deadline(self) -> Optional[float]:
        return None


class EventSyncSource(SyncSource):
    """External event source with an optional per-cycle relative timeout."""

    def __init__(self, name: str, timeout: Optional[float] = None) -> None:
        super().__init__(name)
        self.timeout = None if timeout is None else max(0.0, float(timeout))
        self._count = 0
        self._baseline = 0
        self._deadline: Optional[float] = None

    def arm(self, now: float) -> None:
        self._baseline = self._count
        self._deadline = None if self.timeout is None else now + self.timeout

    def note_event(self) -> None:
        self._count += 1

    def event_received(self) -> bool:
        return self._count > self._baseline

    def timed_out(self, now: float) -> bool:
        return (
            not self.event_received()
            and self._deadline is not None
            and now >= self._deadline
        )

    def is_ready(self, now: float) -> bool:
        return self.event_received() or self.timed_out(now)

    def next_deadline(self) -> Optional[float]:
        if self.event_received():
            return None
        return self._deadline


class AbsoluteTimerSource(SyncSource):
    """Fixed-period monotonic timer whose deadlines never accumulate drift."""

    def __init__(self, period: float, name: str = "__timer__") -> None:
        super().__init__(name)
        self.period = float(period)
        if self.period <= 0.0:
            raise ValueError("timer period must be > 0")
        self._epoch: Optional[float] = None
        self._deadline: Optional[float] = None

    def arm(self, now: float) -> None:
        if self._epoch is None:
            self._epoch = now

        # Select the first fixed slot strictly after now. If scan work overruns
        # one or more slots, they are skipped instead of shifting the schedule.
        slot = int(max(0.0, now - self._epoch) / self.period) + 1
        self._deadline = self._epoch + slot * self.period

    def is_ready(self, now: float) -> bool:
        return self._deadline is not None and now >= self._deadline

    def next_deadline(self) -> Optional[float]:
        return self._deadline


class SyncController:
    """Coordinate external sync sources or an internal absolute timer.

    When role="sync" subscriptions exist they define the scan rhythm and form
    one AND group. Each of those sources may have its own relative timeout to
    bridge an occasional missed monitor event.

    When no external sync source is configured, the internal absolute timer is
    used. This replaces repeated relative sleeps with fixed monotonic deadlines.
    """

    def __init__(
        self,
        subscriptions: Optional[List[SubscriptionConfig]] = None,
        *,
        timer_period: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logging.getLogger(__name__)
        self._clock = clock
        self._cond = threading.Condition()

        sync_subs = [
            sub for sub in (subscriptions or [])
            if getattr(sub, "role", None) == "sync"
        ]
        self._event_sources: Dict[str, EventSyncSource] = {
            sub.name: EventSyncSource(sub.name, getattr(sub, "timeout", None))
            for sub in sync_subs
        }
        self._required_names: Tuple[str, ...] = tuple(self._event_sources)
        self._timer = (
            AbsoluteTimerSource(timer_period)
            if timer_period is not None
            else None
        )

        self._logger.info(
            "SyncController initialized sync_sources=%s timer_period=%s",
            self._required_names,
            timer_period,
        )

    @property
    def required_names(self) -> Tuple[str, ...]:
        return self._required_names

    def is_enabled(self) -> bool:
        """Return True when external role="sync" sources are configured."""
        return bool(self._event_sources)

    def set_timer_period(self, period: Optional[float]) -> None:
        """Set or disable the internal timer and restart its absolute epoch."""
        with self._cond:
            self._timer = AbsoluteTimerSource(period) if period is not None else None
            self._cond.notify_all()

    def arm(self) -> None:
        """Start a new synchronization cycle."""
        now = self._clock()
        with self._cond:
            if self._event_sources:
                for source in self._event_sources.values():
                    source.arm(now)
            elif self._timer is not None:
                self._timer.arm(now)

    def note_event(self, subscription_name: Optional[str]) -> None:
        """Record one event from a configured sync subscription."""
        if not subscription_name:
            return

        with self._cond:
            source = self._event_sources.get(subscription_name)
            if source is None:
                self._logger.debug(
                    "Ignoring event from unknown sync source '%s'",
                    subscription_name,
                )
                return
            source.note_event()
            self._cond.notify_all()

    def _is_ready_at(self, now: float) -> bool:
        if self._event_sources:
            return all(
                source.is_ready(now)
                for source in self._event_sources.values()
            )
        if self._timer is not None:
            return self._timer.is_ready(now)
        return True

    def is_ready(self) -> bool:
        return self._is_ready_at(self._clock())

    def _next_wait_time(self, now: float) -> Optional[float]:
        if self._event_sources:
            deadlines = [
                source.next_deadline()
                for source in self._event_sources.values()
            ]
        else:
            deadlines = [
                self._timer.next_deadline() if self._timer is not None else None
            ]

        # A deadline that has already passed belongs to a source that is ready
        # through its timeout. Waiting on it would return at once and spin while
        # the other sources are still pending.
        deadlines = [
            deadline for deadline in deadlines
            if deadline is not None and deadline > now
        ]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def wait(self, stop_event: Optional[threading.Event] = None) -> bool:
        """Wait until all active sync sources are satisfied.

        Returns False only when a stop was requested. A per-source timeout is a
        normal successful fallback for that source.
        """
        with self._cond:
            while True:
                if stop_event is not None and stop_event.is_set():
                    self._logger.info("SyncController wait aborted by stop_event")
                    return False

                now = self._clock()
                if self._is_ready_at(now):
                    timed_out = [
                        source.name
                        for source in self._event_sources.values()
                        if source.timed_out(now)
                    ]
                    if timed_out:
                        self._logger.debug("Sync source timeout fallback: %s", timed_out)
                    return True

                wait_time = self._next_wait_time(now)
                if wait_time is None and stop_event is not None:
                    wait_time = 0.1
                self._cond.wait(timeout=wait_time)

    def wake(self) -> None:
        """Wake waiters so they can observe a stop request immediately."""
        with self._cond:
            self._cond.notify_all()
