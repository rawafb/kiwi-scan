# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from kiwi_scan.actuator.single import AbstractActuator
from kiwi_scan.datamodels import ScanConfig
from kiwi_scan.monitor.base import BaseMonitor


class ScanABC(ABC):
    """
    Abstract base class for all scan implementations.
    Defines the interface/contract for scan behavior.
    """
    cfg: ScanConfig
    data_dir: str
    output_file: Optional[str]
    include_timestamps: bool
    busyflag: bool

    @abstractmethod
    def __init__(self, config: ScanConfig, data_dir=None):
        """Initialize scan with configuration."""

    @abstractmethod
    def scan(self, positions: Dict[str, List[Any]], monitor: Optional[BaseMonitor] = None) -> None:
        """Perform the scan for the given positions."""

    @abstractmethod
    def load_data(self):
        """Load and return the scan data from output file."""

    @abstractmethod
    def get_output_file(self) -> Optional[str]:
        """Return the current output file path, or None until a file is created."""

    @abstractmethod
    def set_data_writing_enabled(self, enabled: bool) -> None:
        """Enable or disable scan data and metadata writing at runtime."""

    @abstractmethod
    def get_data_writing_enabled(self) -> bool:
        """Checks if scan data and metadata writing is currently enabled."""

    @property
    @abstractmethod
    def busy(self) -> bool:
        """True while scan."""

    @property
    @abstractmethod
    def position(self) -> Optional[Any]:
        """Current position of first actuator."""

    @abstractmethod
    def stop(self) -> None:
        """
        Stop.
        """
    @abstractmethod
    def execute(self) -> None:
        """
        Execute scan
        """

    @abstractmethod
    def get_value(
        self,
        name: str,
        *,
        default: Any = None,
        with_metadata: bool = False,
    ) -> Any:
        """
        Return the last-acquired datapoint by column name.

        name can be:
          - 'Position' or other base headers
          - detector PV name (e.g. 'BL:DET:SIG')
          - plugin header (e.g. 'ControllerSetpoint')
          - column from column provider
          - timestamp columns (e.g. 'TS-ISO8601', 'TS-ISO8601-<PV>', 'TS-<PluginHeader>')
        If with_metadata=True, return the full stored structure if available.
        """

    @abstractmethod
    def get_current_row_cache(self) -> Dict[str, Any]:
        """Return the current in-progress scan row before it is written."""

    @abstractmethod
    def get_current_row_value(self, key: str, default: Any = None) -> Any:
        """Return one value from the current in-progress scan row."""

    @abstractmethod
    def get_actuator(self, name: str) -> AbstractActuator:
        """Return the actuator object by name (for sharing)."""

    @abstractmethod
    def get_actuators(self) -> Dict[str, AbstractActuator]:
        """Return the full actuator mapping (for sharing / composition)."""

