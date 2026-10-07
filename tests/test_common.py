# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiwi_scan import test_support

# common.py imports the EPICS wrapper during module loading. These tests exercise only BaseScan helpers, 
# so provide the project fake unless another test has already loaded an EPICS module.
if "epics" not in sys.modules:
    sys.modules["epics"] = test_support.make_fake_epics_module()

from kiwi_scan.actuator.single import PvEvent
from kiwi_scan.datamodels import (
    ActuatorConfig,
    JogConfig,
    ScanConfig,
    ScanDimension,
    SubscriptionConfig,
)
from kiwi_scan.scan._point_frame import _DetectorLayout
from kiwi_scan.scan.common import BaseScan
from kiwi_scan.scan.metadata_monitor import MetadataCAMonitor
from kiwi_scan.scan.output_manager import OutputManager
from kiwi_scan.scan.performance_tracker import PerformanceTracker


class DummyScan(BaseScan):
    """Concrete BaseScan without the hardware-heavy BaseScan constructor."""

    def __init__(
        self,
        *,
        include_timestamps: bool = True,
        timestamp_output_format: str = "iso8601",
    ):
        # BaseScan.__init__ is intentionally bypassed in these unit tests.
        # Keep the runtime invariants used by the optimized point hot path
        # explicit here rather than making production code defensive again.
        self.detector_pvs = []
        self.plugins = []
        self.include_timestamps = include_timestamps
        self.timestamp_output_format = timestamp_output_format
        self.performance = PerformanceTracker(enabled=False)
        self.output_manager = OutputManager(
            data_dir=".",
            requested_output_file="scan.txt",
            data_writing_enabled=False,
            output_timestamp="20260901120000",
        )
        self._initialize_point_pipeline()
        self._initialize_event_handler()

    def execute(self):
        pass



class TestScanConfigActuatorValidation(unittest.TestCase):
    @staticmethod
    def _config(actuators):
        return ScanConfig(
            actuators=actuators,
            detector_pvs=[],
            scan_dimensions=[ScanDimension("motor", 0.0, 1.0, 2)],
        )

    def test_validate_normalizes_actuator_dicts(self):
        raw_actuator = {
            "pv": "MOTOR",
            "rb_pv": "MOTOR.RBV",
            "jog": {"velocity_pv": "MOTOR:JOGVELO"},
        }
        config = self._config({"motor": raw_actuator})

        config.validate()

        actuator = config.actuators["motor"]
        self.assertIsInstance(actuator, ActuatorConfig)
        self.assertEqual(actuator.pv, "MOTOR")
        self.assertEqual(actuator.rb_pv, "MOTOR.RBV")
        self.assertIsInstance(actuator.jog, JogConfig)
        self.assertEqual(actuator.jog.velocity_pv, "MOTOR:JOGVELO")
        self.assertIsInstance(raw_actuator["jog"], dict)

    def test_validate_keeps_actuator_config_instances(self):
        actuator = ActuatorConfig(pv="MOTOR", rb_pv="MOTOR.RBV")
        config = self._config({"motor": actuator})

        config.validate()

        self.assertIs(config.actuators["motor"], actuator)

    def test_validate_rejects_invalid_actuator_config_type(self):
        config = self._config({"motor": object()})

        with self.assertRaisesRegex(
            TypeError,
            "Actuator config for 'motor'.*ActuatorConfig",
        ):
            config.validate()


class TestBaseScanConfigurationHelpers(unittest.TestCase):
    def test_connect_actuators_uses_validated_actuator_configs(self):
        scan = DummyScan()
        actuator_config = ActuatorConfig(pv="MOTOR", rb_pv="MOTOR.RBV")
        scan.cfg = SimpleNamespace(actuators={"motor": actuator_config})
        actuator = object()

        with patch(
            "kiwi_scan.scan.common.create_actuator",
            return_value=actuator,
        ) as create:
            scan._connect_actuators()

        create.assert_called_once_with(actuator_config)
        self.assertEqual(scan.actuators, {"motor": actuator})

    def test_validate_and_filter_actuators_removes_unused_actuators(self):
        scan = DummyScan()
        scan.cfg = SimpleNamespace(
            scan_dimensions=[SimpleNamespace(actuator="energy")],
            actuators={"energy": object(), "unused": object()},
        )

        with self.assertLogs("kiwi_scan.scan.common", level="WARNING"):
            scan._validate_and_filter_actuators()

        self.assertEqual(list(scan.cfg.actuators), ["energy"])

    def test_validate_and_filter_actuators_requires_a_dimension(self):
        scan = DummyScan()
        scan.cfg = SimpleNamespace(scan_dimensions=[], actuators={})

        with self.assertRaisesRegex(ValueError, "at least one ScanDimension"):
            scan._validate_and_filter_actuators()

    def test_validate_and_filter_actuators_rejects_unknown_actuator(self):
        scan = DummyScan()
        scan.cfg = SimpleNamespace(
            scan_dimensions=[SimpleNamespace(actuator="missing")],
            actuators={"energy": object()},
        )

        with self.assertLogs(
            "kiwi_scan.scan.common", level="WARNING"
        ), self.assertRaisesRegex(ValueError, "unknown actuator: 'missing'"):
            scan._validate_and_filter_actuators()

    def test_position_sync_uses_primary_actuator_readback(self):
        scan = DummyScan()
        scan.scan_dimensions = [SimpleNamespace(actuator="energy")]

        self.assertTrue(
            scan._is_position_sync_subscription(
                SubscriptionConfig(
                    name="energy_sync",
                    role="sync",
                    actuator="energy",
                    source="RBV",
                )
            )
        )
        self.assertFalse(
            scan._is_position_sync_subscription(
                SubscriptionConfig(
                    name="energy_setpoint",
                    role="sync",
                    actuator="energy",
                    source="setpoint",
                )
            )
        )
        self.assertFalse(
            scan._is_position_sync_subscription(
                SubscriptionConfig(
                    name="other_sync",
                    role="sync",
                    actuator="other",
                    source="rbv",
                )
            )
        )

    def test_apply_sample_rate_sets_rate_and_period(self):
        scan = DummyScan()

        scan._apply_sample_rate("4")

        self.assertEqual(scan.sample_rate_hz, 4.0)
        self.assertEqual(scan.sampletime, 0.25)

    def test_apply_sample_rate_uses_default_and_normalizes_negative_rate(self):
        scan = DummyScan()

        scan._apply_sample_rate(None)
        self.assertEqual((scan.sample_rate_hz, scan.sampletime), (1.0, 1.0))

        with self.assertLogs("kiwi_scan.scan.common", level="ERROR"):
            scan._apply_sample_rate(-5)
        self.assertEqual((scan.sample_rate_hz, scan.sampletime), (5.0, 0.2))

    def test_set_samplerate_updates_sync_timer_period(self):
        scan = DummyScan()
        scan.cfg = SimpleNamespace(sample_rate_hz=2.0)
        scan.sync_controller = MagicMock()

        scan.set_samplerate()

        scan.sync_controller.set_timer_period.assert_called_once_with(0.5)


class TestBaseScanOutputHelpers(unittest.TestCase):
    def test_obsolete_point_pipeline_delegations_are_removed(self):
        scan = DummyScan()
        removed = (
            "_get_data_column_providers",
            "_get_data_column_headers",
            "_get_data_column_values",
            "_abort_active_point_frame",
            "_append_plugin_point_values",
            "_update_data_column_provider_cache",
            "_reset_data_column_provider_windows",
            "_format_scan_value",
            "_standalone_point_frame",
            "_point_frame_for_save",
            "_publish_point_frame",
            "_seal_point_frame",
            "_freeze_point_frame",
            "_positions_match",
            "_point_frame_for_internal_commit",
            "_get_parallel_writer",
            "_ensure_parallel_writer",
            "_write_prepared_point_sync",
            "_prepare_legacy_point",
            "_last_point_data_headers",
            "_resolve_last_point_header",
            "_last_point_timestamp_header",
            "_cache_last_point_item",
            "_cache_last_point_timestamp",
            "_update_last_point_cache",
            "build_output_headers",
            "build_output_row_values",
            "update_current_row_cache",
            "extend_current_row_cache",
            "_stop_parallel_writer",
            "_initialize_performance_state",
            "_record_perf_sample",
            "_record_detector_profile",
            "_time_block",
            "_perf_report",
        )
        for name in removed:
            with self.subTest(name=name):
                self.assertFalse(hasattr(scan, name))

    def test_point_cache_state_is_owned_by_point_pipeline(self):
        scan = DummyScan()

        self.assertIn("_point_pipeline", scan.__dict__)
        self.assertNotIn("_last_point", scan.__dict__)
        self.assertNotIn("_current_row_cache", scan.__dict__)
        self.assertNotIn("_active_point_frame", scan.__dict__)
        self.assertNotIn("_detector_layout", scan.__dict__)
        self.assertNotIn("_data_column_providers", scan.__dict__)
        self.assertNotIn("_parallel_point_writer", scan.__dict__)
        self.assertNotIn("_point_writer_queue_size", scan.__dict__)
        self.assertNotIn("_point_writer_queue_high_water", scan.__dict__)
        self.assertNotIn("_point_writer_maximum_queue_delay", scan.__dict__)
        self.assertIsNone(scan._point_pipeline.get_parallel_writer())
        self.assertEqual(scan.get_current_row_cache(), {})
        self.assertEqual(scan.get_last_point_keys(), [])

    def setUp(self):
        self.scan = DummyScan()
        self.scan.detector_pvs = [
            SimpleNamespace(pvname="DET:A"),
            SimpleNamespace(pvname="DET:B"),
        ]
        self.scan._point_pipeline.set_detector_layout(
            _DetectorLayout.from_headers(
                pv.pvname for pv in self.scan.detector_pvs
            )
        )
        self.scan.plugins = []
        self.scan.include_timestamps = True
        self.scan._point_pipeline.replace_current_row_cache({})
        self.scan._point_pipeline.replace_last_point({})

    def test_begin_point_frame_profiles_hot_path_sections(self):
        self.scan.performance.enabled = True
        provider = MagicMock()
        provider.get_values.return_value = [9.0]
        provider.get_headers.return_value = ["mean"]
        self.scan.add_column_provider(provider)

        self.scan._begin_point_frame(
            idx=4,
            pos=2.5,
            values=[
                {"value": 1.0, "timestamp": 100.0},
                {"value": 2.0, "timestamp": 101.0},
            ],
        )

        for name in (
            "row_cache:base",
            "row_cache:providers",
            "row_cache:detectors",
        ):
            self.assertEqual(len(self.scan.performance.samples[name]), 1)
            self.assertGreaterEqual(self.scan.performance.samples[name][0], 0.0)

        for name in (
            "detectors:normalize",
            "detectors:timestamps",
            "detectors:cache_store",
        ):
            self.assertNotIn(name, self.scan.performance.samples)

    def test_get_value_supports_scalar_metadata_and_defaults(self):
        metadata = {"value": 12.5, "timestamp": 100.0}
        self.scan._point_pipeline.replace_last_point(
            {"DET:A": metadata, "state": "ready"}
        )

        self.assertEqual(self.scan.get_value("DET:A"), 12.5)
        self.assertIs(self.scan.get_value("DET:A", with_metadata=True), metadata)
        self.assertEqual(self.scan.get_value("state"), "ready")
        self.assertEqual(self.scan.get_value("missing", default=-1), -1)
        self.assertEqual(self.scan.get_last_point_keys(), ["DET:A", "state"])

    def test_column_provider_failures_do_not_hide_other_providers(self):
        broken = MagicMock()
        broken.get_headers.side_effect = RuntimeError("broken headers")
        broken.get_values.side_effect = RuntimeError("broken values")
        working = MagicMock()
        working.get_headers.return_value = ["mean"]
        working.get_values.return_value = [3.0]
        self.scan.add_column_provider(broken)
        self.scan.add_column_provider(working)

        with self.assertLogs("kiwi_scan.scan.point_pipeline", level="ERROR"):
            self.assertEqual(
                self.scan._point_pipeline.get_data_column_headers(False),
                ["mean"],
            )
            self.assertEqual(
                self.scan._point_pipeline.get_data_column_values(),
                [3.0],
            )


class TestBaseScanRuntimeHelpers(unittest.TestCase):
    def test_manager_helpers_delegate_to_their_managers(self):
        scan = DummyScan()
        scan.subscription_manager = MagicMock()
        scan.trigger_manager = MagicMock()
        scan.sync_controller = MagicMock()
        scan.sync_controller.wait.return_value = True
        handler = object()
        stop_event = threading.Event()

        scan.register_subscription_role("sync", handler)
        scan._start_subscriptions()
        scan._clear_subscriptions()
        scan._fire_triggers("before")
        scan._arm_sync_controller()

        self.assertTrue(scan._wait_for_sync(stop_event))
        scan.subscription_manager.register_role.assert_called_once_with("sync", handler)
        scan.subscription_manager.start.assert_called_once_with()
        scan.subscription_manager.stop.assert_called_once_with()
        scan.trigger_manager.fire.assert_called_once_with("before")
        scan.sync_controller.arm.assert_called_once_with()
        scan.sync_controller.wait.assert_called_once_with(stop_event=stop_event)

    def test_read_detectors_delegates_to_detector_reader(self):
        scan = DummyScan()
        scan._detector_reader = MagicMock()
        scan._detector_reader.read.return_value = [{"value": 5.0}]

        readings = scan.read_detectors()

        self.assertEqual(readings, [{"value": 5.0}])
        scan._detector_reader.read.assert_called_once_with()

    def test_move_scan_step_moves_configured_actuators(self):
        scan = DummyScan()
        scan._stop_requested = threading.Event()
        scan._daq_is_on = True
        energy = MagicMock()
        gap = MagicMock()
        unused = MagicMock()
        scan.actuators = {
            "energy": energy,
            "gap": gap,
            "unused": unused,
        }

        completed = scan._move_scan_step(
            {
                "energy": [1.0, 2.0],
                "gap": [10.0, 20.0],
            },
            1,
        )

        self.assertTrue(completed)
        self.assertFalse(scan._daq_is_on)
        energy.move.assert_called_once_with(2.0)
        gap.move.assert_called_once_with(20.0)
        unused.move.assert_not_called()

    def test_move_scan_step_stops_before_remaining_moves(self):
        scan = DummyScan()
        scan._stop_requested = threading.Event()
        scan._daq_is_on = True
        first = MagicMock()
        second = MagicMock()
        first.move.side_effect = lambda _target: scan._stop_requested.set()
        scan.actuators = {"first": first, "second": second}

        completed = scan._move_scan_step(
            {"first": [1.0], "second": [2.0]},
            0,
        )

        self.assertFalse(completed)
        first.move.assert_called_once_with(1.0)
        second.move.assert_not_called()

    def test_acquire_scan_point_processes_and_publishes_values(self):
        scan = DummyScan(include_timestamps=False)
        scan.performance.enabled = False
        scan._stop_requested = threading.Event()
        scan._point_pipeline.reset_data_column_provider_windows = MagicMock()
        scan._fire_triggers = MagicMock()
        scan.integration_time = 0.0
        detector_values = [{"value": 3.0, "timestamp": 101.0}]
        scan.read_detectors = MagicMock(return_value=detector_values)
        scan.detector_pvs = [SimpleNamespace(pvname="DetectorValue")]
        scan._point_pipeline.set_detector_layout(
            _DetectorLayout.from_headers(
                pv.pvname for pv in scan.detector_pvs
            )
        )
        scan._point_pipeline.replace_current_row_cache({})
        scan._point_pipeline.replace_last_point({})
        scan._point_pipeline.abort_active_point_frame()
        plugin = MagicMock()
        plugin.on_scan_point.return_value = [
            {"value": 4.0, "timestamp": 102.0}
        ]
        plugin.get_headers.return_value = ["PluginValue"]
        scan.plugins = [plugin]
        monitor = MagicMock()

        completed = scan._acquire_daq_point_step(5, 2.0, monitor)

        self.assertTrue(completed)
        self.assertTrue(scan._daq_is_on)
        self.assertEqual(scan._position, 2.0)
        scan._point_pipeline.reset_data_column_provider_windows.assert_called_once_with()
        scan._fire_triggers.assert_called_once_with("on_point")
        plugin.on_scan_point.assert_called_once_with(5, 2.0)
        monitor_values = monitor.update.call_args.args[0]
        self.assertEqual(monitor_values[0], 2.0)
        self.assertEqual(monitor_values[2:], [3.0, 4.0])
        self.assertEqual(scan.get_current_row_value("DetectorValue"), 3.0)
        self.assertEqual(scan.get_current_row_value("PluginValue"), 4.0)
        self.assertEqual(scan.get_value("DetectorValue"), 3.0)
        self.assertEqual(scan.get_value("PluginValue"), 4.0)
        self.assertIsNone(scan._point_pipeline.active_point_frame)

    def test_acquire_scan_point_stops_during_integration(self):
        scan = DummyScan()
        scan.performance.enabled = False
        scan._stop_requested = threading.Event()
        scan._stop_requested.set()
        scan._point_pipeline.reset_data_column_provider_windows = MagicMock()
        scan._fire_triggers = MagicMock()
        scan.integration_time = 1.0
        scan.read_detectors = MagicMock()

        completed = scan._acquire_daq_point_step(0, 1.0, None)

        self.assertFalse(completed)
        scan.read_detectors.assert_not_called()

    def test_parallel_writer_cleanup_delegates_to_pipeline(self):
        scan = DummyScan()

        with patch.object(
            scan._point_pipeline,
            "stop_parallel_writer",
        ) as stop_writer:
            error = scan._drain_parallel_writer_for_cleanup()

        self.assertIsNone(error)
        stop_writer.assert_called_once_with()

    def test_parallel_writer_cleanup_captures_error_for_later_propagation(self):
        scan = DummyScan()
        writer_error = OSError("disk full")

        with patch.object(
            scan._point_pipeline,
            "stop_parallel_writer",
            side_effect=writer_error,
        ):
            captured = scan._drain_parallel_writer_for_cleanup()

        self.assertIs(captured, writer_error)
        with self.assertRaisesRegex(OSError, "disk full"):
            scan._propagate_parallel_writer_error(
                captured,
                scan_failed=False,
            )

        with self.assertLogs("kiwi_scan.scan.common", level="ERROR"):
            scan._propagate_parallel_writer_error(
                captured,
                scan_failed=True,
            )

    def test_scan_cleanup_continues_and_preserves_original_error(self):
        scan = DummyScan()
        scan.performance.enabled = False
        scan._stop_requested = threading.Event()
        scan.write_header_to_output_file = MagicMock(
            side_effect=RuntimeError("scan failed")
        )
        scan._end_plugins = MagicMock(
            side_effect=RuntimeError("plugin stop failed")
        )
        scan._close_plugins = MagicMock()
        scan._stop_metadata_monitor = MagicMock(
            side_effect=RuntimeError("metadata stop failed")
        )
        scan._stop_subscriptions = MagicMock(
            side_effect=RuntimeError("subscription stop failed")
        )
        scan.performance.report = MagicMock()
        scan._point_pipeline.report_data_column_failures = MagicMock()
        monitor = MagicMock()
        monitor.close.side_effect = RuntimeError("monitor close failed")

        with patch(
            "kiwi_scan.scan.common.ensure_ca_context"
        ), self.assertLogs(
            "kiwi_scan.scan.common",
            level="ERROR",
        ), self.assertRaisesRegex(RuntimeError, "scan failed"):
            scan.scan({}, monitor)

        scan._end_plugins.assert_called_once_with()
        scan._close_plugins.assert_called_once_with()
        scan._stop_metadata_monitor.assert_called_once_with()
        scan._stop_subscriptions.assert_called_once_with()
        monitor.close.assert_called_once_with()
        scan._point_pipeline.report_data_column_failures.assert_called_once_with()
        scan.performance.report.assert_called_once_with()
        self.assertFalse(scan.busyflag)

    def test_metadata_drop_count_returns_zero_without_monitor(self):
        scan = DummyScan()

        self.assertEqual(scan.get_metadata_queue_drop_count(), 0)

    def test_metadata_drop_count_uses_monitor_method(self):
        scan = DummyScan()
        monitor = MagicMock(spec=MetadataCAMonitor)
        monitor.get_drop_count.return_value = 7
        scan._meta_mon = monitor

        self.assertEqual(scan.get_metadata_queue_drop_count(), 7)
        monitor.get_drop_count.assert_called_once_with()

    def test_metadata_drop_count_returns_zero_when_monitor_fails(self):
        scan = DummyScan()
        monitor = MagicMock(spec=MetadataCAMonitor)
        monitor.get_drop_count.side_effect = RuntimeError("monitor failure")
        scan._meta_mon = monitor

        with self.assertLogs(
            "kiwi_scan.scan.common",
            level="DEBUG",
        ):
            result = scan.get_metadata_queue_drop_count()

        self.assertEqual(result, 0)

    def test_metadata_monitor_start_and_stop_are_idempotent(self):
        scan = DummyScan()
        scan.output_manager.set_data_writing_enabled(True)
        scan._meta_mon_started = False
        scan._meta_mon = MagicMock()

        scan._start_metadata_monitor()
        scan._start_metadata_monitor()
        self.assertTrue(scan._meta_mon_started)
        scan._meta_mon.start.assert_called_once_with()

        scan._stop_metadata_monitor()
        scan._stop_metadata_monitor()
        self.assertFalse(scan._meta_mon_started)
        scan._meta_mon.stop.assert_called_once_with()

    def test_metadata_monitor_failures_leave_consistent_started_state(self):
        scan = DummyScan()
        scan.output_manager.set_data_writing_enabled(True)
        scan._meta_mon_started = False
        scan._meta_mon = MagicMock()
        scan._meta_mon.start.side_effect = RuntimeError("start failed")

        with self.assertLogs("kiwi_scan.scan.common", level="ERROR"):
            scan._start_metadata_monitor()
        self.assertFalse(scan._meta_mon_started)

        scan._meta_mon_started = True
        scan._meta_mon.stop.side_effect = RuntimeError("stop failed")
        with self.assertLogs("kiwi_scan.scan.common", level="ERROR"):
            scan._stop_metadata_monitor()
        self.assertFalse(scan._meta_mon_started)

    def test_prepare_positions_filters_pads_and_does_not_modify_input(self):
        scan = DummyScan()
        scan.actuators = {
            "energy": SimpleNamespace(backlash=0.0),
            "gap": SimpleNamespace(backlash=0.0),
        }
        positions = {"energy": [1.0, 2.0, 3.0], "gap": [10.0], "empty": []}

        prepared, overshoot = scan._prepare_positions(positions)

        self.assertEqual(
            prepared,
            {"energy": [1.0, 2.0, 3.0], "gap": [10.0, 10.0, 10.0]},
        )
        self.assertFalse(overshoot)
        self.assertEqual(positions["gap"], [10.0])

    def test_prepare_positions_adds_direction_dependent_backlash_point(self):
        scan = DummyScan()
        scan.actuators = {
            "up": SimpleNamespace(backlash=0.5),
            "down": SimpleNamespace(backlash=0.25),
            "plain": SimpleNamespace(backlash=0.0),
        }

        prepared, overshoot = scan._prepare_positions(
            {
                "up": [10.0, 20.0],
                "down": [20.0, 10.0],
                "plain": [3.0, 4.0],
            }
        )

        self.assertTrue(overshoot)
        self.assertEqual(prepared["up"], [9.5, 10.0, 20.0])
        self.assertEqual(prepared["down"], [20.25, 20.0, 10.0])
        self.assertEqual(prepared["plain"], [3.0, 3.0, 4.0])

    def test_data_writing_toggle_starts_and_stops_metadata_monitor(self):
        scan = DummyScan()
        scan.output_manager.set_data_writing_enabled(True)
        scan.busyflag = True
        scan._start_metadata_monitor = MagicMock()
        scan._stop_metadata_monitor = MagicMock()

        scan.set_data_writing_enabled(False)
        self.assertFalse(scan.get_data_writing_enabled())
        scan._stop_metadata_monitor.assert_called_once_with()

        scan.set_data_writing_enabled(True)
        self.assertTrue(scan.get_data_writing_enabled())
        scan._start_metadata_monitor.assert_called_once_with()

    def test_output_file_api_is_backed_by_output_manager(self):
        scan = DummyScan()

        scan.output_file = "scan-data.txt"

        self.assertEqual(scan.output_file, "scan-data.txt")
        self.assertEqual(scan.get_output_file(), "scan-data.txt")
        self.assertEqual(scan.output_manager.output_file, "scan-data.txt")

    def test_write_header_to_output_file_writes_header_once(self):
        scan = DummyScan(include_timestamps=False)
        scan._point_pipeline.build_output_headers = MagicMock(
            return_value=["Position", "TS-ISO8601"]
        )

        with tempfile.TemporaryDirectory() as tmp:
            scan.output_manager = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                data_writing_enabled=True,
                output_timestamp="20260901120000",
            )
            scan.output_file = str(Path(tmp) / "scan.txt")

            scan.write_header_to_output_file()
            scan.write_header_to_output_file()

            content = Path(scan.output_file).read_text(encoding="utf-8")

        self.assertEqual(content, "Position\tTS-ISO8601\n")
        self.assertTrue(scan.output_manager.header_written)
        scan._point_pipeline.build_output_headers.assert_called_once_with()

    def test_save_to_file_updates_cache_when_file_writing_is_disabled(self):
        scan = DummyScan(include_timestamps=False)
        scan.detector_pvs = [SimpleNamespace(pvname="DET:A")]
        scan.plugins = []
        scan._point_pipeline.set_detector_layout(
            _DetectorLayout.from_headers(["DET:A"])
        )
        scan._point_pipeline.replace_last_point({})

        row = scan.save_to_file(
            2.0,
            [{"value": 8.0, "timestamp": 0}],
            include_timestamps=False,
        )

        self.assertEqual(row[0], 2.0)
        self.assertEqual(row[-1], 8.0)
        self.assertEqual(scan.get_value("Position"), 2.0)
        self.assertEqual(scan.get_value("DET:A"), 8.0)

    def test_column_provider_window_helpers_are_best_effort(self):
        scan = DummyScan()
        working = MagicMock()
        broken = MagicMock()
        broken.update_last_point.side_effect = RuntimeError("update failed")
        broken.reset_window.side_effect = RuntimeError("reset failed")
        without_reset = SimpleNamespace(update_last_point=MagicMock())
        scan.add_column_provider(working)
        scan.add_column_provider(broken)
        scan.add_column_provider(without_reset)
        last = {"Position": 1.0}

        with self.assertLogs("kiwi_scan.scan.point_pipeline", level="ERROR"):
            scan._point_pipeline.update_data_column_provider_cache(last, True)
            scan._point_pipeline.reset_data_column_provider_windows()

        working.update_last_point.assert_called_once_with(last, True)
        broken.update_last_point.assert_called_once_with(last, True)
        without_reset.update_last_point.assert_called_once_with(last, True)
        working.reset_window.assert_called_once_with()
        broken.reset_window.assert_called_once_with()

    def test_get_actuator_reports_errors_and_get_actuators_returns_copy(self):
        scan = DummyScan()
        energy = object()
        scan.actuators = {"energy": energy, "gap": object()}

        self.assertIs(scan.get_actuator("energy"), energy)
        copied = scan.get_actuators()
        copied.clear()
        self.assertEqual(set(scan.actuators), {"energy", "gap"})

        with self.assertRaisesRegex(KeyError, "Available actuators: energy, gap"):
            scan.get_actuator("missing")

        scan.actuators["none"] = None
        with self.assertRaisesRegex(KeyError, "exists but is None"):
            scan.get_actuator("none")

    def test_stop_is_best_effort_and_wakes_waiters(self):
        scan = DummyScan()
        scan._stop_requested = threading.Event()
        scan._daq_is_on = True
        scan._tick_cond = threading.Condition()
        scan._tick_seq = 2
        working = MagicMock()
        broken = MagicMock()
        broken.stop.side_effect = RuntimeError("cannot stop")
        scan.actuators = {"working": working, "broken": broken}
        scan.sync_controller = MagicMock()

        with self.assertLogs("kiwi_scan.scan.common", level="ERROR"):
            scan.stop()

        self.assertTrue(scan._stop_requested.is_set())
        self.assertFalse(scan._daq_is_on)
        self.assertEqual(scan._tick_seq, 3)
        working.stop.assert_called_once_with()
        broken.stop.assert_called_once_with()
        scan.sync_controller.wake.assert_called_once_with()

    def test_parallel_wait_supports_current_and_legacy_actuator_signatures(self):
        scan = DummyScan()
        scan._stop_requested = threading.Event()
        calls = []

        class CurrentActuator:
            def wait_until_done(self, target, stop_event):
                calls.append(("current", target, stop_event))

        class LegacyActuator:
            def wait_until_done(self, target):
                calls.append(("legacy", target))

        scan._parallel_wait(
            {"current": CurrentActuator(), "legacy": LegacyActuator()},
            {"current": 1.0, "legacy": 2.0},
        )

        self.assertIn(("current", 1.0, scan._stop_requested), calls)
        self.assertIn(("legacy", 2.0), calls)

    def test_plugin_lifecycle_is_best_effort(self):
        scan = DummyScan()
        working = MagicMock(name="working")
        broken = MagicMock(name="broken")
        broken.on_start.side_effect = RuntimeError("start failed")
        broken.on_end.side_effect = RuntimeError("end failed")
        broken.close.side_effect = RuntimeError("close failed")
        no_close = SimpleNamespace(
            name="no-close",
            on_start=MagicMock(),
            on_end=MagicMock(),
        )
        scan.plugins = [broken, working, no_close]

        with self.assertLogs("kiwi_scan.scan.common", level="ERROR"):
            scan._start_plugins()
            scan._end_plugins()
            scan._close_plugins()

        working.on_start.assert_called_once_with()
        working.on_end.assert_called_once_with()
        working.close.assert_called_once_with()
        no_close.on_start.assert_called_once_with()
        no_close.on_end.assert_called_once_with()

    def test_stop_pv_is_read_and_reset(self):
        scan = DummyScan()
        scan.stop_pv = MagicMock()
        scan.stop_pv.get.return_value = 1

        self.assertEqual(scan.get_stop_pv(), 1)
        scan.stop_pv.put.assert_called_once_with(0)

        scan.stop_pv = None
        self.assertIsNone(scan.get_stop_pv())

    def test_event_handlers_store_and_queue_events(self):
        scan = DummyScan()
        scan._last_status = None
        scan._last_heartbeat = None
        scan._tick_cond = threading.Condition()
        scan._tick_seq = 0
        scan._trigger_q = MagicMock()
        scan._plugin_q = MagicMock()
        event = PvEvent("TEST:PV", 5.0)
        subscription = SubscriptionConfig(
            name="test_subscription",
            role="status",
            pv="TEST:PV",
        )

        scan.event_handler.on_status_event(event, subscription)
        scan.event_handler.on_heartbeat_event(event, subscription)
        scan.event_handler.on_trigger_event(event, subscription)
        scan.event_handler.on_plugin_event(event, subscription)

        self.assertIs(scan._last_status, event)
        self.assertIs(scan._last_heartbeat, event)
        self.assertEqual(scan._tick_seq, 1)
        scan._trigger_q.put.assert_called_once_with(event)
        scan._plugin_q.put.assert_called_once_with(event)

    def test_sync_event_updates_controller_and_position(self):
        scan = DummyScan()
        scan.scan_dimensions = [SimpleNamespace(actuator="energy")]
        scan.sync_controller = MagicMock()
        scan._position = None
        scan._position_sync_subscription_set = False

        event = PvEvent("ENERGY:RBV", "12.5")
        subscription = SubscriptionConfig(
            name="energy_sync",
            role="sync",
            actuator="energy",
            source="rbv",
        )

        scan.event_handler.on_sync_event(event, subscription)

        scan.sync_controller.note_event.assert_called_once_with("energy_sync")
        self.assertEqual(scan._position, 12.5)
        self.assertTrue(scan._position_sync_subscription_set)

    def test_trigger_worker_fires_monitor_triggers(self):
        scan = DummyScan()
        scan._trigger_worker_stop = threading.Event()
        event = PvEvent("TEST:TRIGGER", 1)

        def get_event():
            scan._trigger_worker_stop.set()
            return event

        scan._trigger_q = SimpleNamespace(get=get_event)
        scan._fire_triggers = MagicMock()

        scan._trigger_worker_loop()

        scan._fire_triggers.assert_called_once_with("monitor")

    def test_plugin_worker_isolates_plugin_failures(self):
        scan = DummyScan()
        scan._plugin_worker_stop = threading.Event()
        event = PvEvent("TEST:PLUGIN", 2)

        def get_event():
            scan._plugin_worker_stop.set()
            return event

        broken_hook = MagicMock(side_effect=RuntimeError("plugin failed"))
        working_hook = MagicMock()
        scan._plugin_q = SimpleNamespace(get=get_event)
        scan.plugins = [
            SimpleNamespace(name="broken", on_monitor=broken_hook),
            SimpleNamespace(name="working", on_monitor=working_hook),
        ]

        with self.assertLogs("kiwi_scan.scan.common", level="ERROR") as logs:
            scan._plugin_worker_loop()

        broken_hook.assert_called_once_with(event)
        working_hook.assert_called_once_with(event)
        self.assertIn("Plugin 'broken' failed", "\n".join(logs.output))

    def test_stop_event_only_stops_an_active_scan(self):
        scan = DummyScan()
        scan._stop_requested = threading.Event()
        scan._tick_cond = threading.Condition()
        scan.sync_controller = MagicMock()
        actuator = MagicMock()
        scan.actuators = {"energy": actuator}
        event = PvEvent("TEST:STOP", 1)
        subscription = SubscriptionConfig(
            name="stop_subscription",
            role="stop",
            pv="TEST:STOP",
        )

        scan.busyflag = False
        scan.event_handler.on_stop_event(event, subscription)
        self.assertFalse(scan._stop_requested.is_set())
        actuator.stop.assert_not_called()

        scan.busyflag = True
        scan.event_handler.on_stop_event(event, subscription)
        self.assertTrue(scan._stop_requested.is_set())
        scan.sync_controller.wake.assert_called_once_with()
        actuator.stop.assert_called_once_with()

    def test_wait_for_tick_returns_false_when_stopped_or_timed_out(self):
        scan = DummyScan()
        scan._tick_cond = threading.Condition()
        scan._tick_seq = 0
        scan._stop_requested = threading.Event()

        self.assertFalse(scan._wait_for_tick_or_timeout(0))

        scan._stop_requested.set()
        self.assertFalse(scan._wait_for_tick_or_timeout(1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
