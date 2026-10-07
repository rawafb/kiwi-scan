# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from kiwi_scan.actuator.single import PvEvent
from kiwi_scan.datamodels import ActuatorConfig, ScanConfig, ScanDimension
from kiwi_scan.scan.common import BaseScan
from kiwi_scan.scan.output_manager import OutputManager
from kiwi_scan.scan.range_exit_detector import RangeExitDetector
from kiwi_scan.scan_concrete.para import ParaScan


class SequenceActuator:
    """Actuator fake with deterministic RBV, ready, and moving sequences."""

    def __init__(
        self,
        rbv_values=(0.0,),
        ready_values=(True,),
        moving_values=(False,),
    ):
        self._rbv_values = list(rbv_values)
        self._ready_values = list(ready_values)
        self._moving_values = list(moving_values)

    @staticmethod
    def _next(values):
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def rbv(self):
        return self._next(self._rbv_values)

    def is_ready(self):
        return self._next(self._ready_values)

    def is_moving(self):
        return self._next(self._moving_values)


class ParaScanTestCase(unittest.TestCase):
    def make_scan(
        self,
        *,
        dimensions=None,
        actuators=None,
        maxindex=1,
    ):
        """Create an isolated ParaScan without EPICS connections or workers."""
        scan = object.__new__(ParaScan)
        scan.scan_dimensions = dimensions or [
            ScanDimension("motor", 0.0, 10.0, maxindex)
        ]
        scan.actuators = actuators or {
            dim.actuator: SequenceActuator()
            for dim in scan.scan_dimensions
        }
        scan._maxindex = maxindex
        scan._last_position_snapshot = {}
        scan._position = None
        scan._stop_requested = threading.Event()
        scan._daq_is_on = False
        scan.performance = Mock()
        scan.performance.time_block.side_effect = (
            lambda *_args, **_kwargs: nullcontext()
        )
        scan.integration_time = 0.0
        scan.sampletime = 0.05
        scan.include_timestamps = False
        scan.busyflag = False
        scan.plugins = []
        scan.sync_controller = Mock()
        scan.sync_controller.is_enabled.return_value = False
        scan._initialize_event_handler()

        scan.write_header_to_output_file = Mock()
        scan.get_stop_pv = Mock(return_value=0)
        scan._arm_sync_controller = Mock()
        scan._wait_for_sync = Mock()
        scan._wait_for_tick_or_timeout = Mock()
        scan._fire_triggers = Mock()
        scan._point_pipeline = Mock()
        scan._point_pipeline.reset_data_column_provider_windows = Mock()
        scan._point_pipeline.append_plugin_point_values = Mock()
        scan.read_detectors = Mock(return_value=[10.0])
        scan._detector_reader = Mock()
        scan._begin_point_frame = Mock()
        scan._commit_point_async = Mock()
        scan._start_metadata_monitor = Mock()
        scan._stop_metadata_monitor = Mock()
        scan._start_subscriptions = Mock()
        scan._stop_subscriptions = Mock()
        scan._drain_parallel_writer_for_cleanup = Mock(return_value=None)
        scan._propagate_parallel_writer_error = Mock()

        return scan


class TestParaScanInitialization(ParaScanTestCase):
    @staticmethod
    def _base_init(scan, config, data_dir=None):
        del data_dir
        scan.cfg = config
        scan.scan_dimensions = config.scan_dimensions or []
        scan.actuators = {
            dim.actuator: Mock()
            for dim in scan.scan_dimensions
        }
        scan.subscription_manager = Mock()
        scan.sync_controller = Mock()
        scan._initialize_event_handler()
        scan.detector_pvs = []
        scan.plugins = []
        scan.include_timestamps = bool(getattr(config, "include_timestamps", False))
        scan.timestamp_output_format = getattr(config, "timestamp_output_format", "iso8601")
        scan.performance = Mock()
        scan.performance.time_block.side_effect = (
            lambda *_args, **_kwargs: nullcontext()
        )
        scan.output_manager = OutputManager(
            data_dir=".",
            requested_output_file="unused.txt",
            data_writing_enabled=False,
        )
        scan._initialize_point_pipeline()

    @staticmethod
    def _config(dimensions=True):
        scan_dimensions = None
        actuators = {}
        if dimensions:
            scan_dimensions = [ScanDimension("motor", 0.0, 10.0, 7)]
            actuators = {"motor": ActuatorConfig(pv="MOTOR")}
        return ScanConfig(
            actuators=actuators,
            detector_pvs=[],
            scan_dimensions=scan_dimensions,
            data_writing_enabled=False,
        )

    def test_init_rejects_missing_dimension(self):
        with patch.object(BaseScan, "__init__", new=self._base_init), \
                self.assertRaisesRegex(
                    ValueError,
                    "requires at least one ScanDimension",
                ):
            ParaScan(self._config(dimensions=False))

    @patch("kiwi_scan.scan_concrete.para.StatsCollector")
    def test_init_sets_rate_roles_limit_and_stats_provider(self, collector_cls):
        config = self._config()

        with patch.object(BaseScan, "__init__", new=self._base_init):
            scan = ParaScan(config)

        self.assertEqual(scan.sample_rate_hz, 20.0)
        self.assertEqual(scan.sampletime, 0.05)
        self.assertEqual(scan._maxindex, 7)
        self.assertEqual(scan._last_position_snapshot, {})
        scan.sync_controller.set_timer_period.assert_called_once_with(0.05)
        collector_cls.assert_called_once_with([], role="stat")
        self.assertEqual(
            scan._point_pipeline.get_data_column_providers(),
            [collector_cls.return_value],
        )
        registered_roles = [
            registered_call.args[0]
            for registered_call in scan.subscription_manager.register_role.call_args_list
        ]
        self.assertEqual(
            registered_roles,
            ["heartbeat", "stat", "status", "stop", "trigger", "plugin"],
        )

    def test_stat_event_only_collects_during_daq(self):
        scan = self.make_scan()
        scan.stats_collector = Mock()
        event = PvEvent("STAT:PV", 4.5)
        subscription = SimpleNamespace(name="statistic")

        scan._on_stat_event(event, subscription)
        scan._daq_is_on = True
        scan._on_stat_event(event, subscription)

        self.assertEqual(
            scan.stats_collector.update.call_args_list,
            [
                call(event, subscription, collect=False),
                call(event, subscription, collect=True),
            ],
        )


class TestParaScanPositionState(ParaScanTestCase):
    def test_dim_in_range_supports_both_directions_and_rejects_bad_values(self):
        forward = ScanDimension("motor", 0.0, 10.0, 2)
        reverse = ScanDimension("motor", 10.0, 0.0, 2)

        for dim in (forward, reverse):
            with self.subTest(dim=dim):
                self.assertTrue(ParaScan._dim_in_range(dim, "0"))
                self.assertTrue(ParaScan._dim_in_range(dim, 10.0))
                self.assertFalse(ParaScan._dim_in_range(dim, -0.1))
                self.assertFalse(ParaScan._dim_in_range(dim, None))
                self.assertFalse(ParaScan._dim_in_range(dim, "invalid"))

    def test_position_changed_handles_tolerance_none_and_strings(self):
        tolerance = {"motor": 0.1}

        self.assertTrue(ParaScan._position_changed({"motor": 1.0}, {}, tolerance))
        self.assertFalse(
            ParaScan._position_changed(
                {"motor": 1.05},
                {"motor": 1.0},
                tolerance,
            )
        )
        self.assertTrue(
            ParaScan._position_changed(
                {"motor": 1.2},
                {"motor": 1.0},
                tolerance,
            )
        )
        self.assertFalse(
            ParaScan._position_changed(
                {"motor": None},
                {"motor": None},
                tolerance,
            )
        )
        self.assertTrue(
            ParaScan._position_changed(
                {"motor": None},
                {"motor": 1.0},
                tolerance,
            )
        )
        self.assertFalse(
            ParaScan._position_changed(
                {"motor": "parked"},
                {"motor": "parked"},
                tolerance,
            )
        )
        self.assertTrue(
            ParaScan._position_changed(
                {"motor": "moving"},
                {"motor": "parked"},
                tolerance,
            )
        )

    def test_position_tolerances_scale_with_span_and_handle_zero_span(self):
        scan = self.make_scan(
            dimensions=[
                ScanDimension("wide", -10.0, 10.0, 2),
                ScanDimension("fixed", 3.0, 3.0, 2),
            ]
        )

        self.assertEqual(
            scan._position_tolerances(),
            {"wide": 2e-8, "fixed": 1e-12},
        )

    def test_read_snapshot_and_range_check_multiple_actuators(self):
        dimensions = [
            ScanDimension("x", 0.0, 10.0, 2),
            ScanDimension("y", 5.0, -5.0, 2),
        ]
        scan = self.make_scan(
            dimensions=dimensions,
            actuators={
                "x": SequenceActuator(rbv_values=(4.0,)),
                "y": SequenceActuator(rbv_values=(RuntimeError("read failed"),)),
            },
        )

        with self.assertLogs(
            "kiwi_scan.scan_concrete.para",
            level="ERROR",
        ):
            snapshot = scan._read_position_snapshot()

        self.assertEqual(snapshot, {"x": 4.0, "y": None})
        self.assertFalse(scan._all_positions_in_range(snapshot))
        self.assertTrue(scan._all_positions_in_range({"x": 4.0, "y": -2.0}))

    def test_ready_and_moving_state_failures_are_tolerated(self):
        dimensions = [
            ScanDimension("x", 0.0, 10.0, 2),
            ScanDimension("y", 0.0, 10.0, 2),
        ]
        scan = self.make_scan(
            dimensions=dimensions,
            actuators={
                "x": SequenceActuator(ready_values=(True,), moving_values=(False,)),
                "y": SequenceActuator(
                    ready_values=(RuntimeError("ready failed"),),
                    moving_values=(RuntimeError("moving failed"),),
                ),
            },
        )

        with self.assertLogs(
            "kiwi_scan.scan_concrete.para",
            level="ERROR",
        ):
            self.assertFalse(scan._all_actuators_ready())
            self.assertFalse(scan._any_actuator_moving())

        scan.actuators["y"] = SequenceActuator(
            ready_values=(True,),
            moving_values=(True,),
        )
        self.assertTrue(scan._all_actuators_ready())
        self.assertTrue(scan._any_actuator_moving())

        scan.actuators["y"] = SequenceActuator(ready_values=(False,))
        self.assertFalse(scan._all_actuators_ready())

    def test_termination_detector_skips_invalid_values_and_detects_exit(self):
        dimensions = [
            ScanDimension("x", 0.0, 10.0, 2),
            ScanDimension("y", 0.0, 5.0, 2),
        ]
        scan = self.make_scan(dimensions=dimensions)
        exits = {
            "x": RangeExitDetector(0.0, 10.0, out_threshold=1),
            "y": RangeExitDetector(0.0, 5.0, out_threshold=1),
        }

        self.assertFalse(scan._termination_detected({"x": "bad", "y": 2.0}, exits))
        self.assertTrue(scan._termination_detected({"x": 4.0, "y": 6.0}, exits))


class TestParaScanWaiting(ParaScanTestCase):
    def test_wait_cycle_uses_sync_controller_when_enabled(self):
        scan = self.make_scan()
        scan.sync_controller.is_enabled.return_value = True

        scan._wait_cycle()

        scan._arm_sync_controller.assert_called_once_with()
        scan._wait_for_sync.assert_called_once_with(
            stop_event=scan._stop_requested,
        )
        scan._wait_for_tick_or_timeout.assert_not_called()

    def test_wait_cycle_uses_sampletime_without_sync(self):
        scan = self.make_scan()

        scan._wait_cycle()

        scan._wait_for_tick_or_timeout.assert_called_once_with(0.05)
        scan._wait_for_sync.assert_not_called()

    def test_wait_cycle_returns_when_arming_requests_stop(self):
        scan = self.make_scan()
        scan._arm_sync_controller.side_effect = scan._stop_requested.set

        scan._wait_cycle()

        scan.sync_controller.is_enabled.assert_not_called()
        scan._wait_for_sync.assert_not_called()
        scan._wait_for_tick_or_timeout.assert_not_called()


class TestParaScanAcquisition(ParaScanTestCase):
    @staticmethod
    def _no_timing(*_args, **_kwargs):
        return nullcontext()

    @patch("kiwi_scan.scan_concrete.para.time.sleep")
    def test_acquire_point_runs_full_pipeline(self, sleep):
        scan = self.make_scan()
        scan.integration_time = 0.25
        scan.performance.time_block = self._no_timing
        scan._read_position_snapshot = Mock(return_value={"motor": 2.5})
        plugin = Mock()
        plugin.on_scan_point.return_value = [20.0]
        plugin.get_headers.return_value = ["PluginValue"]
        scan.plugins = [plugin]
        monitor = Mock()
        completed_row = [2.5, 123.0, 10.0, 20.0]
        scan._commit_point_async.return_value = completed_row

        scan._acquire_point(3, {"motor": 2.0}, monitor)

        sleep.assert_called_once_with(0.25)
        self.assertEqual(scan._position, 2.5)
        self.assertEqual(scan._last_position_snapshot, {"motor": 2.5})
        scan._point_pipeline.reset_data_column_provider_windows.assert_called_once_with()
        self.assertEqual(
            scan._fire_triggers.call_args_list,
            [call("on_point"), call("after_point")],
        )
        scan._begin_point_frame.assert_called_once_with(
            idx=3,
            pos=2.5,
            values=[10.0],
        )
        plugin.on_scan_point.assert_called_once_with(3, 2.5)
        scan._point_pipeline.append_plugin_point_values.assert_called_once_with(
            ["PluginValue"],
            [20.0],
        )
        scan._commit_point_async.assert_called_once_with(
            2.5,
            [10.0],
        )
        monitor.update.assert_called_once_with(completed_row)
        self.assertFalse(scan._daq_is_on)

    def test_acquire_point_clears_daq_flag_when_detector_read_fails(self):
        scan = self.make_scan()
        scan.performance.time_block = self._no_timing
        scan._read_position_snapshot = Mock(return_value={"motor": 1.0})
        scan.read_detectors.side_effect = RuntimeError("detector failed")

        with self.assertRaisesRegex(RuntimeError, "detector failed"):
            scan._acquire_point(0, {"motor": 1.0})

        self.assertFalse(scan._daq_is_on)
        scan._commit_point_async.assert_not_called()


class TestParaScanLoop(ParaScanTestCase):
    def test_scan_records_one_point_and_cleans_up(self):
        scan = self.make_scan(maxindex=1)
        scan._wait_cycle = Mock()
        scan._read_position_snapshot = Mock(return_value={"motor": 2.0})
        scan._acquire_point = Mock()
        monitor = Mock()

        scan.scan({"ignored": [1.0]}, monitor)

        scan.write_header_to_output_file.assert_called_once_with()
        scan._start_subscriptions.assert_called_once_with()
        scan._start_metadata_monitor.assert_called_once_with()
        scan._acquire_point.assert_called_once_with(
            0,
            {"motor": 2.0},
            monitor,
        )
        self.assertEqual(
            scan._fire_triggers.call_args_list,
            [call("before"), call("after")],
        )
        scan._stop_metadata_monitor.assert_called_once_with()
        scan._stop_subscriptions.assert_called_once_with()
        monitor.close.assert_called_once_with()
        self.assertFalse(scan.busyflag)
        scan._point_pipeline.report_data_column_failures.assert_called_once_with()
        scan.performance.report.assert_called_once_with()

    def test_scan_waits_for_range_ready_and_changed_position(self):
        scan = self.make_scan(maxindex=1)
        scan._wait_cycle = Mock()
        snapshots = [
            {"motor": -1.0},
            {"motor": 1.0},
            {"motor": 1.0},
            {"motor": 2.0},
        ]
        scan._read_position_snapshot = Mock(side_effect=snapshots)
        scan._all_positions_in_range = Mock(
            side_effect=[False, True, True, True]
        )
        scan._all_actuators_ready = Mock(side_effect=[False, True, True])
        scan._position_changed = Mock(side_effect=[False, True])
        scan._termination_detected = Mock(return_value=False)
        scan._acquire_point = Mock()

        scan.scan(None)

        scan._acquire_point.assert_called_once_with(
            0,
            {"motor": 2.0},
            None,
        )
        self.assertEqual(scan._wait_cycle.call_count, 4)
        self.assertEqual(scan._all_actuators_ready.call_count, 3)
        self.assertEqual(scan._position_changed.call_count, 2)

    def test_scan_requires_moving_state_before_second_point(self):
        scan = self.make_scan(maxindex=2)
        scan._wait_cycle = Mock()
        scan._read_position_snapshot = Mock(
            side_effect=[{"motor": 1.0}, {"motor": 2.0}]
        )
        scan._any_actuator_moving = Mock(side_effect=[False, True])
        scan._acquire_point = Mock()

        scan.scan(None)

        self.assertEqual(
            scan._acquire_point.call_args_list,
            [
                call(0, {"motor": 1.0}, None),
                call(1, {"motor": 2.0}, None),
            ],
        )
        self.assertEqual(scan._any_actuator_moving.call_count, 2)
        self.assertEqual(scan._wait_cycle.call_count, 3)

    def test_scan_stops_when_stop_pv_is_set_before_wait(self):
        scan = self.make_scan()
        scan.get_stop_pv.return_value = 1
        scan._wait_cycle = Mock()
        scan._acquire_point = Mock()

        scan.scan(None)

        scan._wait_cycle.assert_not_called()
        scan._acquire_point.assert_not_called()
        self.assertFalse(scan.busyflag)

    def test_scan_honors_stop_requested_during_wait(self):
        scan = self.make_scan()
        scan._wait_cycle = Mock(side_effect=scan._stop_requested.set)
        scan._read_position_snapshot = Mock()

        scan.scan(None)

        scan._read_position_snapshot.assert_not_called()
        self.assertFalse(scan.busyflag)

    def test_scan_honors_stop_requested_before_first_iteration(self):
        scan = self.make_scan()
        scan._start_subscriptions.side_effect = scan._stop_requested.set
        scan._wait_cycle = Mock()

        scan.scan(None)

        scan._wait_cycle.assert_not_called()
        scan.get_stop_pv.assert_not_called()

    def test_scan_honors_stop_pv_while_waiting_for_external_move(self):
        scan = self.make_scan(maxindex=2)
        scan._wait_cycle = Mock()
        scan._read_position_snapshot = Mock(return_value={"motor": 1.0})
        scan._acquire_point = Mock()
        scan.get_stop_pv.side_effect = [0, 1]

        scan.scan(None)

        scan._acquire_point.assert_called_once_with(
            0,
            {"motor": 1.0},
            None,
        )
        self.assertTrue(scan._stop_requested.is_set())

    def test_scan_stops_after_confirmed_range_exit(self):
        scan = self.make_scan(maxindex=2)
        scan._wait_cycle = Mock()
        scan._read_position_snapshot = Mock(
            side_effect=[{"motor": 1.0}, {"motor": 11.0}]
        )
        scan._any_actuator_moving = Mock(return_value=True)
        scan._termination_detected = Mock(return_value=True)
        scan._acquire_point = Mock()

        scan.scan(None)

        scan._acquire_point.assert_called_once_with(
            0,
            {"motor": 1.0},
            None,
        )
        scan._termination_detected.assert_called_once()

    def test_scan_handles_subscription_cleanup_failure(self):
        scan = self.make_scan()
        scan.get_stop_pv.return_value = 1
        scan._stop_subscriptions.side_effect = RuntimeError("stop failed")

        with self.assertLogs(
            "kiwi_scan.scan_concrete.para",
            level="ERROR",
        ):
            scan.scan(None)

        scan._fire_triggers.assert_has_calls([call("before"), call("after")])
        self.assertFalse(scan.busyflag)
        scan.performance.report.assert_called_once_with()

    def test_after_trigger_failure_still_clears_busy_and_reports_performance(self):
        scan = self.make_scan()
        scan.get_stop_pv.return_value = 1
        scan._fire_triggers.side_effect = [None, RuntimeError("after failed")]

        with self.assertRaisesRegex(RuntimeError, "after failed"):
            scan.scan(None)

        self.assertFalse(scan.busyflag)
        scan.performance.report.assert_called_once_with()

    def test_execute_uses_standard_execution(self):
        scan = self.make_scan()
        scan._execute_standard = Mock()

        scan.execute()

        scan._execute_standard.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
