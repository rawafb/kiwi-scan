# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import math
import socket
import threading
import time
import unittest

from kiwi_scan.plugin.registry import PLUGIN_REGISTRY
from kiwi_scan.plugin_concrete.emc_getter import EmcGetterPlugin


class FakeEmcServer:
    """Line based TCP server answering each command from a reply table."""

    def __init__(self, replies, split_replies=False):
        self.replies = dict(replies)
        self.split_replies = split_replies
        self.commands = []
        self.connections = 0
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            with conn:
                self._handle(conn)

    def _handle(self, conn):
        conn.settimeout(0.1)
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                command = line.decode("ascii")
                self.commands.append(command)
                reply = self.replies.get(command, b"f 0\n")
                if reply is None:
                    return  # simulate a dropped connection
                if self.split_replies and len(reply) > 1:
                    conn.sendall(reply[:2])
                    time.sleep(0.02)
                    conn.sendall(reply[2:])
                else:
                    conn.sendall(reply)

    def close(self):
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2.0)


def make_plugin(port=5001, **overrides):
    parameters = {
        "host": "127.0.0.1",
        "port": port,
        "timeout_s": 1.0,
        "getters": [
            {"command": "getCurrent", "column": "emc_current"},
            {"command": "getRange", "column": "emc_range", "value_type": "int"},
        ],
    }
    parameters.update(overrides)
    return EmcGetterPlugin("emc", parameters)


class TestEmcGetterConfig(unittest.TestCase):
    def test_registered_name(self):
        self.assertIs(PLUGIN_REGISTRY["hzb.emc_getter"], EmcGetterPlugin)

    def test_headers_and_defaults(self):
        plugin = make_plugin(5001)
        try:
            self.assertEqual(plugin.get_headers(), ["emc_current", "emc_range"])
            defaults = plugin.get_async_default_values()
            self.assertEqual(len(defaults), 2)
            self.assertTrue(all(math.isnan(v) for v in defaults))
        finally:
            plugin.close()

    def test_record_times_adds_timing_columns(self):
        plugin = make_plugin(5001, record_times=True)
        try:
            self.assertEqual(
                plugin.get_headers(),
                [
                    "emc_current", "emc_current_before", "emc_current_after",
                    "emc_range", "emc_range_before", "emc_range_after",
                ],
            )
            self.assertEqual(len(plugin.get_async_default_values()), 6)
        finally:
            plugin.close()

    def test_invalid_parameters(self):
        cases = [
            ({"max_workers": 2}, ValueError),
            ({"host": ""}, ValueError),
            ({"port": 0}, ValueError),
            ({"port": True}, ValueError),
            ({"timeout_s": 0}, ValueError),
            ({"timeout_s": float("inf")}, ValueError),
            ({"min_interval_s": -1}, ValueError),
            ({"record_times": "yes"}, TypeError),
            ({"reply_terminator": "tab"}, ValueError),
            ({"reply_terminator": 1}, TypeError),
            ({"getters": []}, ValueError),
            ({"getters": ["getCurrent"]}, TypeError),
            ({"getters": [{"command": "", "column": "a"}]}, ValueError),
            ({"getters": [{"command": "get\tX", "column": "a"}]}, ValueError),
            ({"getters": [{"command": "getX", "column": "a b"}]}, ValueError),
            ({"getters": [{"command": "getX", "column": "a", "value_type": "str"}]}, ValueError),
            ({"getters": [{"command": "getX", "column": "a"}, {"command": "getY", "column": "a"}]}, ValueError),
            (
                {
                    "record_times": True,
                    "getters": [
                        {"command": "getX", "column": "a"},
                        {"command": "getY", "column": "a_before"},
                    ],
                },
                ValueError,
            ),
        ]
        for overrides, error in cases:
            with self.subTest(overrides=overrides), self.assertRaises(error):
                make_plugin(**overrides)


class TestEmcGetterCommunication(unittest.TestCase):
    def setUp(self):
        self.server = FakeEmcServer({
            "getCurrent": b"t 1.25e-9\n",
            "getRange": b"t 3\n",
        })
        self.addCleanup(self.server.close)

    def test_reads_group_in_configuration_order(self):
        plugin = make_plugin(self.server.port)
        try:
            self.assertEqual(plugin.process_data_snapshot(0), [1.25e-9, 3])
            self.assertEqual(plugin.process_data_snapshot(1), [1.25e-9, 3])
            self.assertEqual(self.server.commands, ["getCurrent", "getRange"] * 2)
            self.assertEqual(self.server.connections, 1)  # socket is reused
        finally:
            plugin.close()

    def test_record_times(self):
        plugin = make_plugin(self.server.port, record_times=True)
        try:
            before = time.time()
            values = plugin.process_data_snapshot(0)
            after = time.time()
            self.assertEqual(values[0], 1.25e-9)
            self.assertEqual(values[3], 3)
            for t in (values[1], values[2], values[4], values[5]):
                self.assertTrue(before <= t <= after)
            self.assertLessEqual(values[1], values[2])
        finally:
            plugin.close()

    def test_framed_reply_reassembles_fragments(self):
        self.server.split_replies = True
        plugin = make_plugin(self.server.port, reply_terminator="lf")
        try:
            self.assertEqual(plugin.process_data_snapshot(0), [1.25e-9, 3])
        finally:
            plugin.close()

    def test_failed_reply_disconnects_and_reconnects(self):
        self.server.replies["getRange"] = b"f 0\n"
        plugin = make_plugin(self.server.port)
        try:
            with self.assertRaises(ValueError):
                plugin.process_data_snapshot(0)
            self.assertIsNone(plugin._socket)
            self.server.replies["getRange"] = b"t 4\n"
            self.assertEqual(plugin.process_data_snapshot(1), [1.25e-9, 4])
            self.assertEqual(self.server.connections, 2)
        finally:
            plugin.close()

    def test_closed_connection_raises(self):
        self.server.replies["getCurrent"] = None
        plugin = make_plugin(self.server.port, reply_terminator="lf")
        try:
            with self.assertRaises(RuntimeError):
                plugin.process_data_snapshot(0)
            self.assertIsNone(plugin._socket)
        finally:
            plugin.close()

    def test_non_finite_value_rejected(self):
        self.server.replies["getCurrent"] = b"t nan\n"
        plugin = make_plugin(self.server.port)
        try:
            with self.assertRaises(ValueError):
                plugin.process_data_snapshot(0)
        finally:
            plugin.close()

    def test_get_values_publishes_async_result(self):
        plugin = make_plugin(self.server.port)
        try:
            first = plugin.get_values(0, None)
            self.assertTrue(all(math.isnan(v) for v in first))
            deadline = time.monotonic() + 2.0
            values = first
            while time.monotonic() < deadline:
                time.sleep(0.01)
                values = plugin.get_values(1, None)
                if not math.isnan(values[0]):
                    break
            self.assertEqual(values, [1.25e-9, 3])
            self.assertIsNone(plugin.get_async_status()["latest_error"])
        finally:
            plugin.close()

    def test_error_publishes_defaults_and_records_error(self):
        self.server.replies["getCurrent"] = b"f 0\n"
        plugin = make_plugin(self.server.port)
        try:
            plugin.get_values(0, None)
            deadline = time.monotonic() + 2.0
            while plugin.get_async_status()["completed_jobs"] == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
                values = plugin.get_values(1, None)
            self.assertTrue(all(math.isnan(v) for v in values))
            self.assertIn("getCurrent", plugin.get_async_status()["latest_error"])
        finally:
            plugin.close()

    def test_min_interval_skips_submissions(self):
        plugin = make_plugin(self.server.port, min_interval_s=60.0)
        try:
            self.assertEqual(plugin.build_data_snapshot(0, None), 0)
            plugin.process_data_snapshot(0)
            self.assertIsNone(plugin.build_data_snapshot(1, None))
        finally:
            plugin.close()

    def test_close_disconnects_idle_socket(self):
        plugin = make_plugin(self.server.port)
        plugin.process_data_snapshot(0)
        self.assertIsNotNone(plugin._socket)
        plugin.close()
        self.assertIsNone(plugin._socket)
        self.assertEqual(plugin.process_data_snapshot(1), plugin.get_async_default_values())


if __name__ == "__main__":
    unittest.main()
