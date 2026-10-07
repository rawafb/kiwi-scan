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
from kiwi_scan.scan_concrete.poll import PollScan


class SequenceActuator:
    """Actuator fake with deterministic movement, readiness, and RBV values."""

    def __init__(
        self,
        *,
        rbv_values=(0.0,),
        moving_values=(True,),
        ready_values=(False,),
    ):
        self._rbv_values = list(rbv_values)
        self._moving_values = list(moving_values)
        self._ready_values = list(ready_values)
        self.rbv_reads = 0
        self.ready_reads = 0

    @staticmethod
    def _next(values):
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def rbv(self):
        self.rbv_reads += 1
        return self._next(self._rbv_values)

    def is_moving(self):
        return self._next(self._moving_values)

    def is_ready(self):
        self.ready_reads += 1
        return self._next(self._ready_values)


class PollScanTestCase(unittest.TestCase):
    def make_scan(
        self,
        *,
        actuator=None,
        start=0.0,
        stop=10.0,
        maxindex=1,
    ):
        """Create an isolated PollScan without EPICS connections or workers."""
        scan = object.__new__(PollScan)
        actuator = actuator or SequenceActuator()
        scan.first_actuator = actuator
        scan.scan_dimensions = [
            ScanDimension("motor", start, stop, maxindex)
        ]
        scan.actuators = {"motor": actuator}
        scan._start = start
        scan._stop = stop
        scan._maxindex = maxindex
        scan._position = None
        scan._position_sync_subscription_set = False
        scan._stop_requested = threading.Event()
        scan.performance = Mock()
        scan.performance.time_block.side_effect = (
            lambda *_args, **_kwargs: nullcontext()
        )
        scan.include_timestamps = False
        scan.busyflag = False
        scan.plugins = []
        scan._point_pipeline = Mock()
        scan._point_pipeline.append_plugin_point_values = Mock()
        scan.sync_controller = Mock()
        scan._initialize_event_handler()

        scan.write_header_to_output_file = Mock()
        scan._start_metadata_monitor = Mock()
        scan._stop_metadata_monitor = Mock()
        scan._fire_triggers = Mock()
        scan._start_subscriptions = Mock()
        scan._clear_subscriptions = Mock()
        scan.get_stop_pv = Mock(return_value=0)
        scan._arm_sync_controller = Mock()
        scan._wait_for_sync = Mock()
        scan._is_position_sync_subscription = Mock(return_value=False)
        scan.read_detectors = Mock(return_value=[10.0])
        scan._detector_reader = Mock()
        scan._begin_point_frame = Mock()
        scan._commit_point_async = Mock()
        scan._drain_parallel_writer_for_cleanup = Mock(return_value=None)
        scan._propagate_parallel_writer_error = Mock()

        return scan, actuator


class TestPollScanInitialization(PollScanTestCase):
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
    def _config(dimensions=True):
        scan_dimensions = None
        actuators = {}
        if dimensions:
            scan_dimensions = [ScanDimension("motor", 1.0, 9.0, 7)]
            actuators = {"motor": ActuatorConfig(pv="MOTOR")}
        return ScanConfig(
            actuators=actuators,
            detector_pvs=[],
            scan_dimensions=scan_dimensions,
            sample_rate_hz=4.0,
            data_writing_enabled=False,
        )

    def test_init_rejects_missing_dimension(self):
        with patch.object(BaseScan, "__init__", new=self._base_init), \
                self.assertRaisesRegex(
                    ValueError,
                    "requires at least one ScanDimension",
                ):
            PollScan(self._config(dimensions=False))

    def test_init_sets_range_rate_limit_and_subscription_roles(self):
        config = self._config()

        with patch.object(BaseScan, "__init__", new=self._base_init):
            scan = PollScan(config)

        self.assertEqual(scan._start, 1.0)
        self.assertEqual(scan._stop, 9.0)
        self.assertEqual(scan._maxindex, 7)
        self.assertEqual(scan.sample_rate_hz, 4.0)
        self.assertEqual(scan.sampletime, 0.25)
        scan.sync_controller.set_timer_period.assert_called_once_with(0.25)
        registered_roles = [
            registered_call.args[0]
            for registered_call in scan.subscription_manager.register_role.call_args_list
        ]
        self.assertEqual(
            registered_roles,
            ["heartbeat", "sync", "status", "stop"],
        )


class TestPollScanSynchronization(PollScanTestCase):
    def test_sync_event_records_primary_position_and_notifies_controller(self):
        scan, _actuator = self.make_scan()
        subscription = SimpleNamespace(name="motor_sync")
        scan._is_position_sync_subscription.side_effect = [True, True, False]

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


class TestPollScanLoop(PollScanTestCase):
    @patch("kiwi_scan.scan_concrete.poll.time.sleep")
    def test_scan_waits_until_actuator_starts_moving(self, sleep):
        actuator = SequenceActuator(moving_values=(False, True))
        scan, _actuator = self.make_scan(actuator=actuator)
        scan.get_stop_pv.return_value = 1

        scan.scan(None)

        sleep.assert_called_once_with(0.05)
        scan._start_subscriptions.assert_called_once_with()
        scan._clear_subscriptions.assert_called_once_with()

    @patch.object(BaseScan, "stop")
    def test_scan_polls_position_and_runs_full_point_pipeline(self, base_stop):
        actuator = SequenceActuator(rbv_values=(2.0,))
        scan, _actuator = self.make_scan(actuator=actuator, maxindex=1)
        plugin = Mock()
        plugin.on_scan_point.return_value = [20.0]
        scan._commit_point_async.return_value = [10.0, 20.0]
        plugin.get_headers.return_value = ["PluginValue"]
        scan.plugins = [plugin]
        monitor = Mock()

        scan.scan(None, monitor)

        self.assertEqual(actuator.rbv_reads, 1)
        self.assertEqual(scan._position, 2.0)
        scan._begin_point_frame.assert_called_once_with(
            idx=0,
            pos=2.0,
            values=[10.0],
        )
        plugin.on_scan_point.assert_called_once_with(0, 2.0)
        scan._point_pipeline.append_plugin_point_values.assert_called_once_with(
            ["PluginValue"],
            [20.0],
        )
        scan._commit_point_async.assert_called_once_with(
            2.0,
            [10.0],
        )
        monitor.update.assert_called_once_with([10.0, 20.0])
        monitor.close.assert_called_once_with()
        self.assertEqual(
            scan._fire_triggers.call_args_list,
            [call("before"), call("on_point"), call("after_point"), call("after")],
        )
        base_stop.assert_called_once_with()
        self.assertFalse(scan.busyflag)
        scan._point_pipeline.report_data_column_failures.assert_called_once_with()

    @patch.object(BaseScan, "stop")
    def test_scan_prefers_synchronized_position(self, base_stop):
        actuator = SequenceActuator(
            rbv_values=(AssertionError("RBV must not be read"),)
        )
        scan, _actuator = self.make_scan(actuator=actuator, maxindex=1)
        scan._is_position_sync_subscription.return_value = True
        subscription = SimpleNamespace(name="motor_sync")
        scan._arm_sync_controller.side_effect = (
            lambda: scan.event_handler.on_sync_event(
                PvEvent("MOTOR:RBV", "2.5", source="rbv"),
                subscription,
            )
        )

        scan.scan(None)

        self.assertEqual(actuator.rbv_reads, 0)
        scan._begin_point_frame.assert_called_once_with(
            idx=0,
            pos=2.5,
            values=[10.0],
        )
        scan._commit_point_async.assert_called_once_with(2.5, [10.0])
        base_stop.assert_called_once_with()

    def test_scan_skips_missing_position(self):
        actuator = SequenceActuator(rbv_values=(None,))
        scan, _actuator = self.make_scan(actuator=actuator, maxindex=0)
        wait_count = 0

        def wait_for_sync(**_kwargs):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                scan._stop_requested.set()

        scan._wait_for_sync.side_effect = wait_for_sync

        scan.scan(None)

        self.assertEqual(actuator.rbv_reads, 1)
        self.assertEqual(actuator.ready_reads, 0)
        scan.read_detectors.assert_not_called()

    @patch("kiwi_scan.scan_concrete.poll.RangeExitDetector")
    def test_scan_stops_after_range_exit_when_actuator_is_ready(
        self,
        range_exit_cls,
    ):
        actuator = SequenceActuator(rbv_values=(11.0,), ready_values=(True,))
        scan, _actuator = self.make_scan(actuator=actuator)
        range_exit_cls.return_value.update.return_value = True

        scan.scan(None)

        range_exit_cls.assert_called_once_with(
            0.0,
            10.0,
            eps=0.001,
            out_threshold=6,
        )
        range_exit_cls.return_value.update.assert_called_once_with(11.0)
        scan.read_detectors.assert_not_called()

    @patch("kiwi_scan.scan_concrete.poll.RangeExitDetector")
    def test_scan_skips_points_while_actuator_is_ready(self, range_exit_cls):
        actuator = SequenceActuator(rbv_values=(5.0,), ready_values=(True,))
        scan, _actuator = self.make_scan(actuator=actuator, maxindex=0)
        range_exit_cls.return_value.update.return_value = False
        wait_count = 0

        def wait_for_sync(**_kwargs):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                scan._stop_requested.set()

        scan._wait_for_sync.side_effect = wait_for_sync

        scan.scan(None)

        self.assertEqual(actuator.rbv_reads, 1)
        scan.read_detectors.assert_not_called()

    @patch("kiwi_scan.scan_concrete.poll.RangeExitDetector")
    def test_equal_range_does_not_terminate_scan(self, range_exit_cls):
        actuator = SequenceActuator(rbv_values=(2.0,), ready_values=(True,))
        scan, _actuator = self.make_scan(
            actuator=actuator,
            start=2.0,
            stop=2.0,
            maxindex=0,
        )
        range_exit_cls.return_value.update.return_value = True
        wait_count = 0

        def wait_for_sync(**_kwargs):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                scan._stop_requested.set()

        scan._wait_for_sync.side_effect = wait_for_sync

        scan.scan(None)

        self.assertEqual(range_exit_cls.return_value.update.call_count, 1)
        scan.read_detectors.assert_not_called()

    def test_scan_honors_stop_requested_before_iteration(self):
        scan, _actuator = self.make_scan()
        scan._start_subscriptions.side_effect = scan._stop_requested.set

        scan.scan(None)

        scan.get_stop_pv.assert_not_called()
        scan._arm_sync_controller.assert_not_called()

    def test_scan_honors_stop_pv_before_arming(self):
        scan, _actuator = self.make_scan()
        scan.get_stop_pv.return_value = 1

        scan.scan(None)

        scan._arm_sync_controller.assert_not_called()
        scan.read_detectors.assert_not_called()

    def test_scan_honors_stop_requested_while_arming(self):
        scan, _actuator = self.make_scan()
        scan._arm_sync_controller.side_effect = scan._stop_requested.set

        scan.scan(None)

        scan._wait_for_sync.assert_not_called()
        scan.read_detectors.assert_not_called()

    def test_scan_honors_stop_requested_while_waiting_for_sync(self):
        scan, _actuator = self.make_scan()
        scan._wait_for_sync.side_effect = (
            lambda **_kwargs: scan._stop_requested.set()
        )

        scan.scan(None)

        scan.read_detectors.assert_not_called()

    def test_scan_logs_subscription_cleanup_failure(self):
        scan, _actuator = self.make_scan()
        scan.get_stop_pv.return_value = 1
        scan._clear_subscriptions.side_effect = RuntimeError("clear failed")

        with self.assertLogs(
            "kiwi_scan.scan_concrete.poll",
            level="ERROR",
        ):
            scan.scan(None)

        scan._fire_triggers.assert_has_calls([call("before"), call("after")])
        self.assertFalse(scan.busyflag)

    def test_execute_uses_standard_execution(self):
        scan, _actuator = self.make_scan()
        scan._execute_standard = Mock()

        scan.execute()

        scan._execute_standard.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
