# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from kiwi_scan.actuator.single import PvEvent
from kiwi_scan.datamodels import ActuatorConfig, ScanConfig, ScanDimension
from kiwi_scan.scan.common import BaseScan
from kiwi_scan.scan_concrete.cm import CMScan


class SequenceActuator:
    """Small actuator fake with deterministic RBV and ready-state sequences."""

    def __init__(self, rbv_values, ready_values=(False,), backlash=0.0):
        self._rbv_values = list(rbv_values)
        self._ready_values = list(ready_values)
        self.rbv_reads = 0
        self.backlash = backlash
        self.run_move = Mock()
        self.get_velocity = Mock(return_value=1.0)
        self.set_velocity = Mock()
        self.stop = Mock()

    @staticmethod
    def _next_value(values):
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    @property
    def rbv(self):
        self.rbv_reads += 1
        return self._next_value(self._rbv_values)

    def is_ready(self):
        return self._next_value(self._ready_values)


class CMScanTestCase(unittest.TestCase):
    def make_scan(
        self, rbv_values=(0.0,), ready_values=(False,), scan_class=CMScan,
    ):
        """Create an isolated CMScan without EPICS connections or worker threads."""
        scan = object.__new__(scan_class)
        actuator = SequenceActuator(rbv_values, ready_values)

        scan.cfg = SimpleNamespace(sample_rate_hz=1.0)
        scan.scan_dimensions = [
            ScanDimension("motor", 0.0, 10.0, 0, velocity=2.0)
        ]
        scan.actuators = {"motor": actuator}
        scan.first_actuator = actuator
        scan._start = 0.0
        scan._stop = 10.0
        scan._maxindex = 0
        scan._original_velocities = {}
        scan._position = None
        scan._position_sync_subscription_set = False
        scan._stop_requested = threading.Event()
        scan._daq_is_on = True
        scan.performance = Mock()
        scan.performance.time_block.side_effect = (
            lambda *_args, **_kwargs: nullcontext()
        )
        scan.include_timestamps = False
        scan.busyflag = False
        scan.plugins = []
        scan.sync_controller = Mock()
        scan._initialize_event_handler()

        scan.write_header_to_output_file = Mock()
        scan.get_stop_pv = Mock(return_value=0)
        scan._arm_sync_controller = Mock()
        scan._wait_for_sync = Mock(return_value=True)
        scan._fire_triggers = Mock()
        scan.read_detectors = Mock(return_value=[10.0])
        scan._detector_reader = Mock()
        scan._begin_point_frame = Mock()
        scan._point_pipeline = Mock()
        scan._commit_point_async = Mock(return_value=["saved-row"])

        scan._start_plugins = Mock()
        scan._end_plugins = Mock()
        scan._close_plugins = Mock()
        scan._start_metadata_monitor = Mock()
        scan._stop_metadata_monitor = Mock()
        scan._start_subscriptions = Mock()
        scan._stop_subscriptions = Mock()
        scan._drain_parallel_writer_for_cleanup = Mock(return_value=None)
        scan._propagate_parallel_writer_error = Mock()

        return scan, actuator


class TestCMScanInitialization(CMScanTestCase):
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

    @staticmethod
    def _config(start=0.0, stop=10.0, dimensions=True):
        scan_dimensions = None
        actuators = {}
        if dimensions:
            scan_dimensions = [ScanDimension("motor", start, stop, 5)]
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
            CMScan(self._config(dimensions=False))

    def test_init_rejects_equal_start_and_stop(self):
        with patch.object(BaseScan, "__init__", new=self._base_init), \
                self.assertRaisesRegex(ArithmeticError, "Start equals stop"):
            CMScan(self._config(start=2.0, stop=2.0))

    def test_init_sets_range_actuator_and_subscription_roles(self):
        config = self._config(start=1.0, stop=9.0)

        with patch.object(BaseScan, "__init__", new=self._base_init):
            scan = CMScan(config)

        self.assertEqual(scan._start, 1.0)
        self.assertEqual(scan._stop, 9.0)
        self.assertEqual(scan._maxindex, 5)
        self.assertIs(scan.first_actuator, scan.actuators["motor"])
        self.assertEqual(scan.sample_rate_hz, 1.0)
        self.assertEqual(scan.sampletime, 1.0)
        self.assertEqual(scan._original_velocities, {})
        registered_roles = [
            registered_call.args[0]
            for registered_call in scan.subscription_manager.register_role.call_args_list
        ]
        self.assertEqual(registered_roles, ["heartbeat", "sync", "status", "stop"])


class TestCMScanSynchronization(CMScanTestCase):
    def test_sync_event_records_position_and_notifies_controller(self):
        scan, _actuator = self.make_scan()
        subscription = SimpleNamespace(name="motor_sync")
        scan._is_position_sync_subscription = Mock(
            side_effect=[True, True, False]
        )

        numeric_event = PvEvent("MOTOR:RBV", "2.5", source="rbv")
        scan.event_handler.on_sync_event(numeric_event, subscription)
        self.assertEqual(scan._position, 2.5)
        self.assertTrue(scan._position_sync_subscription_set)

        raw_event = PvEvent("MOTOR:RBV", "invalid", source="rbv")
        scan.event_handler.on_sync_event(raw_event, subscription)
        self.assertEqual(scan._position, "invalid")

        unrelated_event = PvEvent("OTHER:RBV", 8.0, source="rbv")
        scan.event_handler.on_sync_event(unrelated_event, subscription)
        self.assertEqual(scan._position, "invalid")
        self.assertEqual(
            scan.sync_controller.note_event.call_args_list,
            [call("motor_sync"), call("motor_sync"), call("motor_sync")],
        )

    @patch.object(BaseScan, "stop")
    def test_run_daq_prefers_synchronized_position(self, base_stop):
        scan, actuator = self.make_scan(rbv_values=(99.0,))
        scan._maxindex = 1
        plugin = Mock()
        plugin.on_scan_point.return_value = [20.0]
        plugin.get_headers.return_value = ["PluginValue"]
        scan.plugins = [plugin]
        monitor = Mock()

        subscription = SimpleNamespace(name="motor_sync")
        scan._is_position_sync_subscription = Mock(return_value=True)
        scan._arm_sync_controller.side_effect = (
            lambda: scan.event_handler.on_sync_event(
                PvEvent("MOTOR:RBV", "2.5", source="rbv"),
                subscription,
            )
        )

        scan.run_daq(monitor)

        # The one RBV read is the initial snapshot. The acquired point comes
        # from the sync event and must not be replaced by another RBV read.
        self.assertEqual(actuator.rbv_reads, 1)
        scan._begin_point_frame.assert_called_once_with(
            idx=0,
            pos=2.5,
            values=[10.0],
        )
        plugin.on_scan_point.assert_called_once_with(0, 2.5)
        scan._point_pipeline.append_plugin_point_values.assert_called_once_with(
            ["PluginValue"],
            [20.0],
        )
        scan._commit_point_async.assert_called_once_with(
            2.5,
            [10.0],
        )
        monitor.update.assert_called_once_with(["saved-row"])
        base_stop.assert_called_once_with()

    @patch.object(BaseScan, "stop")
    def test_run_daq_polls_rbv_without_position_sync(self, base_stop):
        scan, actuator = self.make_scan(rbv_values=(-1.0, 3.0))
        scan._maxindex = 1

        scan.run_daq()

        self.assertEqual(actuator.rbv_reads, 2)
        self.assertEqual(scan._position, 3.0)
        scan._commit_point_async.assert_called_once_with(3.0, [10.0])
        base_stop.assert_called_once_with()


class TestCMScanDaq(CMScanTestCase):
    def test_run_daq_honors_stop_requested_before_iteration(self):
        scan, _actuator = self.make_scan()
        scan._stop_requested.set()

        scan.run_daq()

        self.assertTrue(scan._stop_requested.is_set())
        scan.get_stop_pv.assert_not_called()
        scan._commit_point_async.assert_not_called()

    def test_run_daq_skips_until_range_and_stops_after_confirmed_exit(self):
        scan, _actuator = self.make_scan(
            rbv_values=(-2.0, -1.0, 0.0, 11.0, 12.0)
        )

        scan.run_daq()

        saved_positions = [
            args[0]
            for args, _kwargs in scan._commit_point_async.call_args_list
        ]
        self.assertEqual(saved_positions, [0.0, 11.0])
        self.assertEqual(scan.read_detectors.call_count, 2)

    @patch.object(BaseScan, "stop")
    def test_run_daq_stops_when_stop_pv_is_set(self, base_stop):
        scan, _actuator = self.make_scan()
        scan.get_stop_pv.return_value = 1

        scan.run_daq()

        base_stop.assert_called_once_with()
        scan._arm_sync_controller.assert_not_called()
        scan._commit_point_async.assert_not_called()

    def test_run_daq_stops_when_actuator_is_ready(self):
        scan, _actuator = self.make_scan(ready_values=(True,))

        scan.run_daq()

        scan.read_detectors.assert_not_called()
        scan._commit_point_async.assert_not_called()

    def test_run_daq_honors_stop_requested_while_waiting(self):
        scan, _actuator = self.make_scan()
        scan._wait_for_sync.side_effect = (
            lambda **_kwargs: scan._stop_requested.set()
        )

        scan.run_daq()

        scan.read_detectors.assert_not_called()
        scan._commit_point_async.assert_not_called()

    def test_run_daq_honors_stop_requested_while_arming(self):
        scan, _actuator = self.make_scan()
        scan._arm_sync_controller.side_effect = scan._stop_requested.set

        scan.run_daq()

        scan._wait_for_sync.assert_not_called()
        scan.read_detectors.assert_not_called()
        scan._commit_point_async.assert_not_called()


class TestCMScanVelocityHandling(CMScanTestCase):
    def test_restore_original_velocities_skips_invalid_entries(self):
        scan, _actuator = self.make_scan()
        restored = Mock()
        failing = Mock()
        failing.set_velocity.side_effect = RuntimeError("restore failed")
        scan.actuators = {
            "restored": restored,
            "failing": failing,
        }
        scan._original_velocities = {
            "restored": 4.0,
            "none": None,
            "missing": 3.0,
            "failing": 5.0,
        }

        with self.assertLogs(
            "kiwi_scan.scan_concrete.cm",
            level="DEBUG",
        ):
            scan._restore_original_velocities()

        restored.set_velocity.assert_called_once_with(4.0)
        failing.set_velocity.assert_called_once_with(5.0)

class TestCMScanExecution(CMScanTestCase):
    def test_scan_runs_forward_and_reverse_moves_and_cleans_up(self):
        scan, _unused_actuator = self.make_scan()
        forward = Mock()
        forward.backlash = 2.0
        forward.get_velocity.return_value = 4.0
        reverse = Mock()
        reverse.backlash = 3.0
        reverse.get_velocity.return_value = 5.0
        scan.scan_dimensions = [
            ScanDimension("forward", 0.0, 10.0, 3, velocity=1.5),
            ScanDimension("reverse", 10.0, 0.0, 3, velocity=2.5),
        ]
        scan.actuators = {"forward": forward, "reverse": reverse}
        scan.first_actuator = forward
        scan.run_daq = Mock()
        monitor = Mock()

        scan.scan({}, monitor)

        self.assertEqual(
            forward.run_move.call_args_list,
            [
                call(-2.0, sync=True),
                call(10.0, sync=False, wait_startup=True),
            ],
        )
        self.assertEqual(
            reverse.run_move.call_args_list,
            [
                call(13.0, sync=True),
                call(0.0, sync=False, wait_startup=True),
            ],
        )
        self.assertEqual(
            forward.set_velocity.call_args_list,
            [call(1.5), call(4.0)],
        )
        self.assertEqual(
            reverse.set_velocity.call_args_list,
            [call(2.5), call(5.0)],
        )
        scan._start_plugins.assert_called_once_with()
        scan._start_metadata_monitor.assert_called_once_with()
        scan._start_subscriptions.assert_called_once_with()
        scan._end_plugins.assert_called_once_with()
        scan._close_plugins.assert_called_once_with()
        scan._stop_metadata_monitor.assert_called_once_with()
        scan._stop_subscriptions.assert_called_once_with()
        monitor.close.assert_called_once_with()
        self.assertEqual(
            scan._fire_triggers.call_args_list,
            [call("before"), call("after")],
        )
        self.assertFalse(scan.busyflag)
        scan._point_pipeline.report_data_column_failures.assert_called_once_with()
        scan.performance.report.assert_called_once_with()

    def test_scan_logs_actuator_failures_and_reaches_daq(self):
        scan, _unused_actuator = self.make_scan()
        actuator = Mock()
        actuator.backlash = 1.0
        actuator.run_move.side_effect = RuntimeError("move failed")
        actuator.get_velocity.side_effect = RuntimeError("read failed")
        actuator.set_velocity.side_effect = RuntimeError("write failed")
        no_velocity = Mock()
        no_velocity.get_velocity.return_value = None
        scan.actuators = {
            "motor": actuator,
            "no_velocity": no_velocity,
        }
        scan.run_daq = Mock()

        with self.assertLogs(
            "kiwi_scan.scan_concrete.cm",
            level="WARNING",
        ):
            scan.scan({})

        scan._start_subscriptions.assert_called_once_with()
        scan.run_daq.assert_called_once_with(None)
        scan._stop_subscriptions.assert_called_once_with()
        self.assertFalse(scan.busyflag)

    def test_cleanup_continues_and_preserves_original_scan_error(self):
        scan, _actuator = self.make_scan()
        monitor = Mock()
        scan._start_plugins.side_effect = RuntimeError("scan failed")
        scan._end_plugins.side_effect = RuntimeError("plugin stop failed")
        scan._stop_metadata_monitor.side_effect = RuntimeError(
            "metadata stop failed"
        )
        scan._stop_subscriptions.side_effect = RuntimeError(
            "subscription stop failed"
        )
        monitor.close.side_effect = RuntimeError("monitor close failed")

        with self.assertLogs(
            "kiwi_scan.scan.common",
            level="ERROR",
        ), self.assertRaisesRegex(RuntimeError, "scan failed"):
            scan.scan({}, monitor)

        scan._end_plugins.assert_called_once_with()
        scan._close_plugins.assert_called_once_with()
        scan._stop_metadata_monitor.assert_called_once_with()
        scan._stop_subscriptions.assert_called_once_with()
        monitor.close.assert_called_once_with()
        scan._fire_triggers.assert_called_once_with("after")
        self.assertFalse(scan.busyflag)
        scan.performance.report.assert_called_once_with()

    def test_execute_uses_standard_execution(self):
        scan, _actuator = self.make_scan()
        scan._execute_standard = Mock()

        scan.execute()

        scan._execute_standard.assert_called_once_with(None)


class TestCMScanLifecycleHooks(CMScanTestCase):
    def test_scan_calls_extension_hooks_in_lifecycle_order(self):
        scan, actuator = self.make_scan()
        events = Mock()
        names = (
            "_start_detector_reader", "_start_plugins", "init_scan",
            "_prepare_sweep", "_start_metadata_monitor", "_fire_triggers",
            "_start_sweep", "_start_subscriptions", "run_daq",
            "_drain_parallel_writer_for_cleanup", "_stop_detector_reader",
            "_restore_sweep_state",
        )
        for name in names:
            method = Mock(return_value=None)
            setattr(scan, name, method)
            events.attach_mock(method, name)

        scan.scan(None)

        self.assertEqual(events.mock_calls, [
            call._start_detector_reader(), call._start_plugins(),
            call.init_scan(), call._prepare_sweep(),
            call._start_metadata_monitor(), call._fire_triggers("before"),
            call._start_sweep(), call._start_subscriptions(), call.run_daq(None),
            call._drain_parallel_writer_for_cleanup(),
            call._stop_detector_reader(), call._restore_sweep_state(),
            call._fire_triggers("after"),
        ])
        actuator.run_move.assert_not_called()
        actuator.get_velocity.assert_not_called()
        actuator.set_velocity.assert_not_called()

    def test_stop_reset_precedes_init_and_preserves_later_stop(self):
        scan, _actuator = self.make_scan()
        scan._stop_requested.set()

        def initialize():
            self.assertFalse(scan._stop_requested.is_set())
            scan._stop_requested.set()

        scan.init_scan = Mock(side_effect=initialize)
        scan._prepare_sweep = Mock()
        scan._start_sweep = Mock()
        scan.scan(None)

        scan.init_scan.assert_called_once_with()
        self.assertTrue(scan._stop_requested.is_set())
        scan._arm_sync_controller.assert_not_called()
        scan._commit_point_async.assert_not_called()

    def test_initialization_failure_runs_cleanup_and_preserves_error(self):
        scan, _actuator = self.make_scan()
        scan.init_scan = Mock(side_effect=RuntimeError("init failed"))
        scan._prepare_sweep = Mock()
        scan._start_sweep = Mock()
        scan._restore_sweep_state = Mock()
        scan.run_daq = Mock()

        with self.assertRaisesRegex(RuntimeError, "init failed"):
            scan.scan(None)

        scan._prepare_sweep.assert_not_called()
        scan._start_sweep.assert_not_called()
        scan.run_daq.assert_not_called()
        scan._drain_parallel_writer_for_cleanup.assert_called_once_with()
        scan._restore_sweep_state.assert_called_once_with()
        scan._propagate_parallel_writer_error.assert_called_once_with(
            None, scan_failed=True,
        )
        self.assertFalse(scan.busyflag)


if __name__ == "__main__":
    unittest.main(verbosity=2)
