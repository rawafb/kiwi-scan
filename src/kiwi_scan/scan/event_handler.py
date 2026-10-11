# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

"""
TODO: Stop handler values and exception handling per actuator.
Default subscription event handlers for scan runtime events."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Protocol

from kiwi_scan.actuator.single import PvEvent
from kiwi_scan.datamodels import SubscriptionConfig

logger = logging.getLogger(__name__)


class _ScanEventContext(Protocol):
    """Narrow BaseScan runtime surface required by ``ScanEventHandler``."""

    busyflag: bool
    actuators: Dict[str, Any]
    sync_controller: Any

    _tick_cond: threading.Condition
    _tick_seq: int
    _stop_requested: threading.Event
    _last_heartbeat: Optional[PvEvent]
    _last_status: Optional[PvEvent]
    _position: Any
    _position_sync_subscription_set: bool
    _trigger_q: Any
    _plugin_q: Any

    def _is_position_sync_subscription(
        self,
        subscription: SubscriptionConfig,
    ) -> bool:
        ...


class ScanEventHandler:
    """
    TODO: non-numeric sync values
    Handle default subscription roles without owning scan orchestration.
    """

    def __init__(self, context: _ScanEventContext) -> None:
        self._context = context

    def on_status_event(
        self,
        ev: PvEvent,
        _subscription: SubscriptionConfig,
    ) -> None:
        context = self._context
        context._last_status = ev
        logger.debug("[status] %s=%r", ev.pvname, ev.value)

    def on_heartbeat_event(
        self,
        ev: PvEvent,
        _subscription: SubscriptionConfig,
    ) -> None:
        context = self._context
        context._last_heartbeat = ev
        with context._tick_cond:
            context._tick_seq += 1
            context._tick_cond.notify_all()
        # logger.debug("[heartbeat] %s=%r (seq=%d)", ev.pvname, ev.value, context._tick_seq)

    def on_stop_event(
        self,
        ev: PvEvent,
        _subscription: SubscriptionConfig,
    ) -> None:
        """Immediately stop an active scan and wake blocked scan waits."""
        context = self._context
        logger.info("[stop] %s=%r -> stopping scan", ev.pvname, ev.value)
        if not context.busyflag:
            return

        context._stop_requested.set()
        context.sync_controller.wake()
        with context._tick_cond:
            context._tick_cond.notify_all()
        try:
            for actuator in context.actuators.values():
                actuator.stop()
        except Exception:
            logger.exception("Error while stopping actuators on stop event")

    def on_trigger_event(
        self,
        ev: PvEvent,
        _subscription: SubscriptionConfig,
    ) -> None:
        """Queue monitor-trigger work and return from callback context."""
        self._context._trigger_q.put(ev)

    def on_plugin_event(
        self,
        ev: PvEvent,
        _subscription: SubscriptionConfig,
    ) -> None:
        """Queue a monitor event for plugin processing outside the callback."""
        self._context._plugin_q.put(ev)

    def on_sync_event(
        self,
        ev: PvEvent,
        subscription: SubscriptionConfig,
    ) -> None:
        """Record synchronization state and update the scan position."""
        context = self._context
        context.sync_controller.note_event(subscription.name)

        if context._is_position_sync_subscription(subscription):
            try:
                context._position = float(ev.value)
            except (TypeError, ValueError):
                context._position = ev.value
            context._position_sync_subscription_set = True
        # logger.debug("[sync] %s=%r -> _position=%r (source=%r, sub=%s)", ev.pvname, ev.value, context._position, ev.source, subscription.name)
