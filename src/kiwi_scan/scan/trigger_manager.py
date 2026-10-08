# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from kiwi_scan.datamodels import ScanTriggers, TriggerAction
from kiwi_scan.epics_wrapper import EpicsPV

logger = logging.getLogger(__name__)

@dataclass
class PreparedTriggerAction:
    pv: EpicsPV
    value: Any
    delay: float = 0.0


class TriggerManager:
    """
    * Trigger parsing, 
    * Create PVs
    * value format
    * Interface to fire triggers."""

    DEFAULT_PHASES = ("before", "on_point", "after_point", "after", "monitor")

    def __init__(
        self,
        actions_by_phase: Optional[Dict[str, List[PreparedTriggerAction]]] = None,
        phases: Optional[Sequence[str]] = None,
    ):
        self._phases = tuple(phases or self.DEFAULT_PHASES)
        self._actions_by_phase: Dict[str, List[PreparedTriggerAction]] = {
            phase: [] for phase in self._phases
        }
        if actions_by_phase:
            for phase in self._phases:
                self._actions_by_phase[phase] = list(actions_by_phase.get(phase, []))

    @classmethod
    def from_config(cls, triggers: Optional[ScanTriggers]) -> "TriggerManager":
        phases = cls._detect_phases(triggers)
        manager = cls(phases=phases)
        if triggers is None:
            logger.info("No triggers configured.")
            return manager

        for phase in phases:
            actions = getattr(triggers, phase, []) or []
            manager._actions_by_phase[phase] = manager._prepare_actions(phase, actions)

        return manager

    @classmethod
    def _detect_phases(cls, triggers: Optional[ScanTriggers]) -> Sequence[str]:
        phases: List[str] = list(cls.DEFAULT_PHASES)
        if triggers is None:
            return phases

        for phase in vars(triggers):
            if phase not in phases:
                phases.append(phase)
        return tuple(phases)

    @staticmethod
    def _prepare_actions(phase: str, actions: Iterable[TriggerAction]) -> List[PreparedTriggerAction]: 
        """ prepare actions from trigger config. """
        prepared: List[PreparedTriggerAction] = []
        for action in actions:
            pvname = action.pv
            if not pvname:
                logger.warning("Trigger action missing 'pv' in phase %s: %r", phase, action)
                continue

            value = TriggerManager._normalize_value(action.value)
            delay = float(action.delay or 0.0)
            if delay < 0:
                logger.warning(f"Invalid trigger delay {action.delay!r} for {pvname} (phase {phase})")
                delay = 0.0
            try:
                pv = EpicsPV(pvname, timeout=1.0, queueing_delay=0.0)
            except Exception:  # noqa: BLE001
                logger.error(f"Failed to init trigger PV {pvname}, phase {phase}")
                continue

            prepared.append(PreparedTriggerAction( pv=pv, value=value, delay=delay))
            logger.debug("Initialized trigger PV %s (phase=%s, value=%r, delay=%.3f)", pvname, phase, value, delay)

        return prepared

    @staticmethod
    def _normalize_value(value: Any) -> Any: # pylint: disable=too-many-return-statements
        if isinstance(value, (list, tuple)):
            return value

        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            try:
                return tolist()
            except Exception: # noqa BLE001   - fails arbitrary 
                return value

        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                inner = stripped[1:-1].strip()
                if not inner:
                    return []
                parts = inner.replace(",", " ").split()
                try:
                    return [float(part) for part in parts]
                except ValueError:
                    return value
        return value

    def fire(self, phase: str) -> None:
        if phase not in self._actions_by_phase:
            logger.warning("Unknown trigger phase: %s", phase)
            return

        logger.debug("************ FIRE TRIGGERS (%s) ************", phase)
        for action in self._actions_by_phase.get(phase, []):
            ok = action.pv.put(action.value)
            if not ok:
                logger.error( "Failed to write trigger PV %s value %r", action.pv.pvname, action.value)
            else:
                logger.debug( "Wrote trigger PV %s value %r", action.pv.pvname, action.value)
            if action.delay > 0:
                time.sleep(action.delay)

    def has_actions(self, phase: Optional[str] = None) -> bool:
        if phase is None:
            return any(self._actions_by_phase[p] for p in self._phases)
        return bool(self._actions_by_phase.get(phase, []))

    @property
    def phases(self) -> Sequence[str]:
        return self._phases
