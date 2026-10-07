# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import logging
from typing import Optional

from kiwi_scan.datamodels import ScanConfig
from kiwi_scan.monitor.base import BaseMonitor
from kiwi_scan.scan.common import BaseScan
from kiwi_scan.scan.range_exit_detector import RangeExitDetector

logger = logging.getLogger(__name__)

# TODO: refactor with poll,monocm, ...
# TODO: offsets for backlash and end of range
class CMScan(BaseScan):
    def __init__(self, config: ScanConfig, data_dir=None):
        super().__init__(config, data_dir)
        
        scan_dimensions = self.scan_dimensions
        if not scan_dimensions:
            raise ValueError("CMScan requires at least one ScanDimension")
        logger.info("Creating samplerate from scan dimensions: %s", scan_dimensions)
        dim = scan_dimensions[0]
        if dim.start == dim.stop:
            raise ArithmeticError(f"Start equals stop == {dim.start!r}")
        self._start = dim.start
        self._stop = dim.stop
        self._maxindex = dim.steps
        self.set_samplerate()
        self.first_actuator = self.actuators[self.scan_dimensions[0].actuator]

        self.register_subscription_role("heartbeat", self.event_handler.on_heartbeat_event)
        self.register_subscription_role("sync", self.event_handler.on_sync_event)
        self.register_subscription_role("status", self.event_handler.on_status_event)
        self.register_subscription_role("stop", self.event_handler.on_stop_event)

        self._original_velocities = {}

    def init_scan(self) -> None:
        """Initialize sweep configuration before preparation, once per scan."""

    def _prepare_sweep(self) -> None:
        """Prepare motion; external scan types may replace this entire step."""
        self._move_to_start_positions()
        self._store_original_velocities()

    def _start_sweep(self) -> None:
        """Start motion before subscriptions and continuous DAQ are started."""
        self._start_continuous_motion()

    def _restore_sweep_state(self) -> None:
        """Restore motion state during cleanup, including failed scans."""
        self._restore_original_velocities()

    def _restore_original_velocities(self) -> None:
        """Restore actuator velocities saved before the continuous move."""
        for name, orig_vel in self._original_velocities.items():
            if orig_vel is None:
                logger.debug("Skipping velocity restore for actuator '%s': original velocity is None", name)
                continue

            actuator = self.actuators.get(name)
            if actuator is None:
                logger.warning("Cannot restore velocity for unknown actuator '%s'", name)
                continue

            try:
                actuator.set_velocity(orig_vel)
                logger.info("Restored velocity for actuator %s to %s", name, orig_vel)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to restore velocity for actuator %s: %s", name, exc)

    def _read_daq_position(self):
        """Return the synchronized position, polling RBV as a fallback."""
        if self._position_sync_subscription_set:
            return self._position

        position = self.first_actuator.rbv
        self._position = position
        return position

    def run_daq(self, monitor: Optional[BaseMonitor] = None):
        """ DAQ loop driven by sync-controller """
        with self.performance.time_block("write:header"):
            self.write_header_to_output_file()
        index = 0

        # initial snapshot; may quickly be overwritten by sync subscription indicated by flag
        self._position_sync_subscription_set = False
        self._position = self.first_actuator.rbv
        range_exit = RangeExitDetector(
            self._start,
            self._stop,
            eps=0.001,   # TODO: parameter to overwrite default 
            out_threshold=2,
        )
        while True:
            logger.debug("run_daq: Entered scan loop")
            if self._daq_stop_requested():
                break

            if not self._wait_for_scan_cycle(index):
                break
            
            position = self._read_daq_position()
            scan_finished = range_exit.update(position)
            actuator_ready = self.first_actuator.is_ready()
            
            if actuator_ready:
                logger.info("run_daq: First actuator is ready.")
                break
            if scan_finished:
                logger.info("Scan termination detected at position=%s", position)
                break
            if not range_exit.entered:
                continue
            if position is None:
                continue

            self._acquire_daq_point_continuous(index, position, monitor)
            index += 1
            if self._maxindex > 0 and index >= self._maxindex:
                super().stop()
                break

    def _move_to_start_positions(self) -> None:
        """Move each scan actuator to its backlash-adjusted start position."""
        with self.performance.time_block("move:to_start"):
            for dim in self.scan_dimensions:
                name = dim.actuator
                actuator = self.actuators[name]
                backlash = (
                    -actuator.backlash
                    if dim.stop > dim.start
                    else actuator.backlash
                )
                overshoot = dim.start + backlash
                logger.info("overshoot=%s, bdist=%s, backlash=%s", overshoot, backlash, actuator.backlash)
                try:
                    actuator.run_move(overshoot, sync=True)
                    logger.info("Started actuator '%s' moving to %s", name, dim.start)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to move actuator '%s': %s", name, exc)

    def _store_original_velocities(self) -> None:
        """Read actuator velocities for best-effort restoration after the scan."""
        with self.performance.time_block("velocity:read"):
            for name, actuator in self.actuators.items():
                try:
                    velocity = actuator.get_velocity()
                    if velocity is None:
                        logger.warning( "Could not read original velocity for actuator '%s'; velocity restore will be skipped", name)
                        continue
                    self._original_velocities[name] = velocity
                    logger.info("Stored velocity for actuator '%s': %s", name, velocity)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not read velocity for actuator '%s': %s", name, exc)

    def _start_continuous_motion(self) -> None:
        """Apply configured velocities and start all continuous moves."""
        with self.performance.time_block("move:start"):
            for dim in self.scan_dimensions:
                name = dim.actuator
                actuator = self.actuators[name]
                try:
                    actuator.set_velocity(dim.velocity)
                    logger.info("Set velocity of actuator '%s' to %s", name, dim.velocity)
                    actuator.run_move(dim.stop, sync=False, wait_startup=True)
                    logger.info("Started actuator '%s' moving to %s", name, dim.stop)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to configure/startup actuator '%s': %s", name, exc)

    def _cleanup_scan(
        self,
        monitor: Optional[BaseMonitor] = None,
        *,
        scan_failed: bool = False,
    ) -> None:
        """Release scan resources without allowing one failure to block others."""
        writer_error = self._drain_parallel_writer_for_cleanup()
        self._run_cleanup_step(
            "detectors:stop",
            self._stop_detector_reader,
        )
        self._run_cleanup_step("velocity:restore", self._restore_sweep_state)
        self._run_cleanup_step("plugins:stop", self._end_plugins)
        self._run_cleanup_step("plugins:close", self._close_plugins)
        self._run_cleanup_step("metadata:stop", self._stop_metadata_monitor)
        self._run_cleanup_step("subscriptions:stop", self._stop_subscriptions)
        if monitor is not None:
            self._run_cleanup_step("monitor:close", monitor.close)
        self._run_cleanup_step("triggers:after", lambda: self._fire_triggers("after"))
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

    # ---------------- cm scan logic --------------------
    def scan(self, positions, monitor: Optional[BaseMonitor] = None):
        """
        1) Move to start position
        2) Store current velocities
        3) Apply configured velocities and start moves
        4) Run DAQ while 1st actuator is within range
        5) Restore original velocities
        """
        del positions

        self.busyflag = True
        self._stop_requested.clear()
        scan_failed = False
        try:
            with self.performance.time_block("detectors:start"):
                self._start_detector_reader()
            with self.performance.time_block("plugins:start"):
                self._start_plugins()
            self.init_scan()
            self._prepare_sweep()

            with self.performance.time_block("metadata:start"):
                self._start_metadata_monitor()
            with self.performance.time_block("triggers:before"):
                self._fire_triggers("before")
            self._start_sweep()

            with self.performance.time_block("subscriptions:start"):
                self._start_subscriptions()
            with self.performance.time_block("daq:run"):
                self.run_daq(monitor)
        except BaseException:
            scan_failed = True
            raise
        finally:
            self._cleanup_scan(monitor, scan_failed=scan_failed)

    def execute(self):
        self._execute_standard(None)
