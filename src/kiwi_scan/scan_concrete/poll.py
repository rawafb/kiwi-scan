# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import logging
import time
from typing import Optional

from kiwi_scan.datamodels import ScanConfig
from kiwi_scan.monitor.base import BaseMonitor
from kiwi_scan.scan.common import BaseScan
from kiwi_scan.scan.range_exit_detector import RangeExitDetector

logger = logging.getLogger(__name__)

class PollScan(BaseScan):
    """
    Poll detector values while the primary actuator is within [start, stop].
    TODO: range check for all actuators.

    Event Roles::
      - TODO: this heardbeat can be fully replaced by sync subscripts 
        If a heartbeat subscription (role="heartbeat") is configured, each heartbeat
        wakes the loop immediately (event-driven scan).
      - The configured poll interval (self.sampletime) becomes a timeout, so the loop
        still runs periodically even if heartbeat stalls or isn't configured.
    """

    def __init__(self, config: ScanConfig, data_dir=None):
        super().__init__(config, data_dir)

        if not self.scan_dimensions:
            raise ValueError("PollScan requires at least one ScanDimension")

        logger.info("Creating samplerate from scan dimensions: %s", self.scan_dimensions)
        self.set_samplerate()
        self._start = self.scan_dimensions[0].start
        self._stop = self.scan_dimensions[0].stop
        self.first_actuator = self.actuators[self.scan_dimensions[0].actuator]

        self.register_subscription_role("heartbeat", self.event_handler.on_heartbeat_event)
        self.register_subscription_role("sync", self.event_handler.on_sync_event)
        self.register_subscription_role("status", self.event_handler.on_status_event)
        self.register_subscription_role("stop", self.event_handler.on_stop_event)
        
        self._maxindex = 0
        if self.scan_dimensions:
            self._maxindex = self.scan_dimensions[0].steps

    @staticmethod
    def _wait_for_motion_start(actuator) -> None:
        """Wait until the externally commanded actuator begins moving."""
        while not actuator.is_moving():
            logger.debug("Wait for actuator to start")
            time.sleep(0.05)

    def _read_daq_position(self, actuator):
        """Return the synchronized position, polling RBV as a fallback."""
        if self._position_sync_subscription_set:
            return self._position

        position = actuator.rbv
        self._position = position
        return position

    def _cleanup_scan(
        self,
        monitor: Optional[BaseMonitor] = None,
        *,
        scan_failed: bool = False,
    ) -> None:
        """Release polling-scan resources using its established semantics."""
        writer_error = self._drain_parallel_writer_for_cleanup()
        self._run_cleanup_step("detectors:stop", self._stop_detector_reader)
        self._stop_metadata_monitor()
        if monitor is not None:
            monitor.close()
        try:
            self._clear_subscriptions()
        except Exception:
            logger.exception("Error clearing scan subscriptions")
        self._fire_triggers("after")
        self.busyflag = False
        self._run_cleanup_step(
            "columns:report",
            self._point_pipeline.report_data_column_failures,
        )
        self._run_cleanup_step("performance:report", self.performance.report)
        self._propagate_parallel_writer_error(
            writer_error,
            scan_failed=scan_failed,
        )

    def run_daq(self, monitor: Optional[BaseMonitor] = None):
        """ DAQ loop driven by sync-controller """
        with self.performance.time_block("write:header"):
            self.write_header_to_output_file()
        index = 0
        # initial snapshot; may quickly be overwritten by sync subscription indicated by flag
        self._position_sync_subscription_set = False
        range_exit = RangeExitDetector(
            self._start,
            self._stop,
            eps=0.001,   # TODO: parameter to overwrite default 
            out_threshold=6,
        )
        while True:
            logger.debug("run_daq: Entered scan loop")
            if self._daq_stop_requested():
                break

            if not self._wait_for_scan_cycle(index):
                break

            position = self._read_daq_position(self.first_actuator)
            if position is None:
                continue
            
            range_exit_detected = range_exit.update(position)
            first_actuator_ready = self.first_actuator.is_ready() 
            if range_exit_detected and first_actuator_ready and self._start != self._stop:
                logger.info("Scan termination detected at pos=%s", position)
                break
            if first_actuator_ready:
                continue

            self._acquire_daq_point_continuous(index, position, monitor)
            index += 1

            if self._maxindex > 0 and index >= self._maxindex:
                super().stop()
                break

    
    def scan(self, positions, monitor: Optional[BaseMonitor] = None):
        """
        Poll detector values.
        Now synchronized by heartbeat events when available, with poll timeout fallback.
        """

        del positions

        self._start_metadata_monitor()
        self._fire_triggers("before")
        self._wait_for_motion_start(self.first_actuator)

        self.busyflag = True
        self._stop_requested.clear()
        scan_failed = False
        try:
            self._start_detector_reader()
            self._start_subscriptions()
            with self.performance.time_block("daq:run"):
                self.run_daq(monitor)
        
        except BaseException:
            scan_failed = True
            raise
        finally:
            self._cleanup_scan(monitor, scan_failed=scan_failed)
    
    def execute(self) -> None:
        self._execute_standard(None) 
