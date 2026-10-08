import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from kiwi_scan import test_support

# Install a tiny pyepics stub before importing kiwi_scan modules.
if "epics" not in sys.modules:
    sys.modules["epics"] = test_support.make_fake_epics_module()


import kiwi_scan
from kiwi_scan.actuator_concrete.single_epics import EpicsActuator
from kiwi_scan.datamodels import (
    ActuatorConfig,
    ConfigError,
    PluginConfig,
    ScanTriggers,
)
from kiwi_scan.plugin.base import ScanPlugin
from kiwi_scan.plugin.registry import PLUGIN_REGISTRY, create_plugin, register_plugin
from kiwi_scan.scan.registry import SCAN_REGISTRY, load_all_scan_types
from kiwi_scan.scan.trigger_manager import TriggerManager


class _DemoPlugin(ScanPlugin):
    def get_headers(self, timestamps: bool):
        return ["demo"]

    def get_values(self, idx, pos):
        return [idx]


FakeTriggerPV = test_support.make_fake_trigger_pv_class()
FakeMonitorPV = test_support.make_fake_monitor_pv_class(start_index=100)


class TestPluginRegistry(unittest.TestCase):
    def setUp(self):
        self._saved = dict(PLUGIN_REGISTRY)
        PLUGIN_REGISTRY.clear()

    def tearDown(self):
        PLUGIN_REGISTRY.clear()
        PLUGIN_REGISTRY.update(self._saved)

    def test_plugin_registration_by_explicit_name(self):
        alias = "explicit_demo_plugin"

        @register_plugin(alias)
        class ExplicitPlugin(_DemoPlugin):
            pass

        plugin = create_plugin(
            PluginConfig(
                type=alias,
                name="friendly",
                parameters={"answer": 42},
            )
        )

        self.assertIn(alias, PLUGIN_REGISTRY)
        self.assertIs(PLUGIN_REGISTRY[alias], ExplicitPlugin)
        self.assertIsInstance(plugin, ExplicitPlugin)
        self.assertEqual(plugin.name, "friendly")
        self.assertEqual(plugin.parameters, {"answer": 42})

    def test_external_plugin_loading_from_env_path(self):
        with tempfile.TemporaryDirectory() as td:
            plugin_file = Path(td) / "ext_plugin.py"
            plugin_file.write_text(
                textwrap.dedent(
                    """
                    from kiwi_scan.plugin.base import ScanPlugin
                    from kiwi_scan.plugin.registry import register_plugin

                    @register_plugin("env_plugin")
                    class EnvPlugin(ScanPlugin):
                        def get_headers(self, timestamps: bool):
                            return ["env"]

                        def get_values(self, idx, pos):
                            return [idx]
                    """
                ).strip()
            )

            with patch.dict(os.environ, {"KIWI_SCAN_PLUGIN_PATH": td}, clear=False):
                kiwi_scan.load_all_plugins(raise_on_error=True)

        self.assertIn("env_plugin", PLUGIN_REGISTRY)
        plugin = create_plugin(PluginConfig(type="env_plugin", name="loaded"))
        self.assertEqual(plugin.name, "loaded")
        self.assertEqual(plugin.get_headers(False), ["env"])


class TestScanRegistry(unittest.TestCase):
    def setUp(self):
        self._saved = dict(SCAN_REGISTRY)
        SCAN_REGISTRY.clear()

    def tearDown(self):
        SCAN_REGISTRY.clear()
        SCAN_REGISTRY.update(self._saved)

    def test_external_scan_loading_from_env_path(self):
        with tempfile.TemporaryDirectory() as td:
            scan_file = Path(td) / "ext_scan.py"
            scan_file.write_text(
                textwrap.dedent(
                    """
                    from kiwi_scan.scan.registry import register_scan

                    @register_scan("env_scan")
                    class EnvScan:
                        pass
                    """
                ).strip()
            )

            with patch.dict(os.environ, {"KIWI_SCAN_SCAN_PATH": td}, clear=False):
                load_all_scan_types(raise_on_error=True)

        self.assertIn("env_scan", SCAN_REGISTRY)
        self.assertEqual(SCAN_REGISTRY["env_scan"].__name__, "EnvScan")

    def test_external_scan_with_postponed_annotations_dataclass(self):
        with tempfile.TemporaryDirectory() as td:
            scan_file = Path(td) / "dc_scan.py"
            scan_file.write_text(
                textwrap.dedent(
                    """
                    from __future__ import annotations

                    from dataclasses import dataclass

                    from kiwi_scan.scan.registry import register_scan

                    @dataclass
                    class Params:
                        width: float = 1.0

                    @register_scan("dc_scan")
                    class DcScan:
                        params: Params
                    """
                ).strip()
            )

            with patch.dict(os.environ, {"KIWI_SCAN_SCAN_PATH": td}, clear=False):
                load_all_scan_types(raise_on_error=True)

        self.assertIn("dc_scan", SCAN_REGISTRY)

    def test_failing_external_scan_is_rolled_back_and_tried_once(self):
        with tempfile.TemporaryDirectory() as td:
            scan_file = Path(td) / "half_scan.py"
            scan_file.write_text(
                textwrap.dedent(
                    """
                    from kiwi_scan.scan.registry import register_scan

                    @register_scan("half_scan")
                    class HalfScan:
                        pass

                    raise RuntimeError("boom after registration")
                    """
                ).strip()
            )

            with patch.dict(os.environ, {"KIWI_SCAN_SCAN_PATH": td}, clear=False):
                with self.assertLogs("kiwi_scan.scan.registry", level="ERROR") as logs:
                    load_all_scan_types()
                    load_all_scan_types()
                    load_all_scan_types()

        self.assertNotIn("half_scan", SCAN_REGISTRY)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("RuntimeError", logs.output[0])


class TestTriggerParsing(unittest.TestCase):
    def test_trigger_action_reports_missing_value_with_config_path(self):
        with self.assertLogs("kiwi_scan.datamodels", level="DEBUG") as logs, \
                self.assertRaisesRegex(
                    ConfigError,
                    r"Invalid triggers\.before\[0\]: missing required key\(s\): "
                    r"value.*same mapping",
                ):
            ScanTriggers.from_dict(
                {
                    "before": [
                        {"pv": "PV:TRIGGER"},
                        {"value": 9},
                    ]
                }
            )

        self.assertTrue(
            any(
                "Invalid trigger action at triggers.before[0]" in message
                for message in logs.output
            )
        )

    def test_trigger_action_must_be_mapping(self):
        with self.assertRaisesRegex(
            ConfigError,
            r"Invalid triggers\.after\[0\]: expected a mapping",
        ):
            ScanTriggers.from_dict({"after": ["PV:TRIGGER=5"]})

    @patch("kiwi_scan.scan.trigger_manager.EpicsPV", FakeTriggerPV)
    def test_trigger_parsing_keeps_monitor_and_custom_phases(self):
        triggers = ScanTriggers.from_dict(
            {
                "before": [{"pv": "PV:BEFORE", "value": 1}],
                "monitor": [{"pv": "PV:MON", "value": "[1, 2, 3]", "delay": 0.25}],
                "custom": [{"pv": "PV:CUSTOM", "value": 7}],
            }
        )

        self.assertEqual(len(triggers.before), 1)
        self.assertEqual(len(triggers.monitor), 1)
        self.assertTrue(hasattr(triggers, "custom"))
        self.assertEqual(len(triggers.custom), 1)

        manager = TriggerManager.from_config(triggers)

        self.assertIn("monitor", manager.phases)
        self.assertIn("custom", manager.phases)
        self.assertTrue(manager.has_actions("monitor"))
        self.assertTrue(manager.has_actions("custom"))

        monitor_action = manager._actions_by_phase["monitor"][0]
        custom_action = manager._actions_by_phase["custom"][0]

        self.assertEqual(monitor_action.pv.pvname, "PV:MON")
        self.assertEqual(monitor_action.value, [1.0, 2.0, 3.0])
        self.assertAlmostEqual(monitor_action.delay, 0.25)
        self.assertEqual(custom_action.value, 7)


class TestEpicsActuatorMonitorLifecycle(unittest.TestCase):
    @patch("kiwi_scan.actuator_concrete.single_epics.EpicsPV", FakeMonitorPV)
    def test_callback_add_remove_lifecycle(self):
        FakeMonitorPV.next_index = 100
        actuator = EpicsActuator(
            ActuatorConfig(
                pv="SET:PV",
                rb_pv="READ:PV",
                status_pv="STAT:PV",
                queueing_delay=0.0,
            )
        )

        received = []
        handle = actuator.add_monitor("MON:PV", user_callback=received.append)

        self.assertIs(handle, actuator._monitors["MON:PV"])
        self.assertEqual(actuator._epics_cb_indices["MON:PV"], [100])

        # Keep the callback object so we can later simulate an event that was
        # already queued/in flight when the monitor was removed.
        stale_callback = handle.callbacks[100]

        handle.trigger(100, value=12.5, timestamp=3.0, severity=1, status=0)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].pvname, "MON:PV")
        self.assertEqual(received[0].value, 12.5)
        self.assertEqual(received[0].timestamp, 3.0)
        self.assertEqual(received[0].severity, 1)
        self.assertEqual(received[0].status, 0)
        self.assertEqual(actuator.get_last_event("MON:PV").value, 12.5)

        actuator.remove_monitor("MON:PV")

        self.assertEqual(handle._pv.removed, [100])
        self.assertTrue(handle._pv.disconnected)
        self.assertNotIn(100, handle.callbacks)
        self.assertNotIn("MON:PV", actuator._monitors)
        self.assertIsNone(actuator.get_last_event("MON:PV"))

        # Simulate an already queued/in-flight callback after monitor removal.
        # The actuator dispatcher must ignore it because the monitor and its
        # listeners have already been removed.
        stale_callback(pvname="MON:PV", value=99.0)
        self.assertEqual(len(received), 1)
        self.assertIsNone(actuator.get_last_event("MON:PV"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
