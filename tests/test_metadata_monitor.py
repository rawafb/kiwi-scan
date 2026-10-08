"""Metadata sidecar regressions; stub PVs, no IOC or pyepics required."""
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import kiwi_scan.scan.metadata_monitor as module
from kiwi_scan.test_support import make_fake_metadata_pv_class


class BadValue:
    def __str__(self):
        raise ValueError("cannot format")


class MetadataMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "metadata.tsv"
        # Each test gets an independent role-specific class from shared support.
        pv_patch = patch.object(module, "PV", make_fake_metadata_pv_class())
        pv_patch.start()
        self.addCleanup(pv_patch.stop)
        self.monitor = module.MetadataCAMonitor(["PV:A", "PV:B"], {"test": 3}, str(self.path))
        self.addCleanup(self.monitor.stop)

    def rows(self):
        return [line for line in self.path.read_text().splitlines()
                if not line.startswith("#")][1:]

    def emit(self, value=2):
        self.monitor._on_event(pvname="PV:A", value=value, timestamp=1001)

    def test_header_and_initial_rows(self):
        self.monitor.start()
        self.monitor.stop()
        self.assertEqual(len(self.rows()), 2)
        self.assertIn("# test\t3", self.path.read_text())
        self.assertEqual(self.monitor._rows_written, 2)
        for row in self.rows():
            fields = row.split("\t")
            self.assertEqual(fields[2], "1.000000000000e+00")
            self.assertEqual(fields[3], "1970-01-01T00:16:40+00:00")
            self.assertEqual(fields[4:], ["0", "0"])

    def test_stop_drains_5000_events(self):
        self.monitor.start()
        for value in range(5000):
            self.emit(value)
        self.monitor.stop()
        self.assertEqual(len(self.rows()), 5002)
        self.assertEqual(self.monitor.get_drop_count(), 0)
        self.assertEqual(self.monitor._unwritten_events, 0)
        self.assertTrue(self.monitor._q.empty())

    def test_restart_preserves_file_and_writes_header_once(self):
        self.monitor.start()
        self.emit(42)
        self.monitor.stop()
        before = self.path.read_text()
        self.monitor.start()
        self.emit(43)
        self.monitor.stop()
        self.assertTrue(self.path.read_text().startswith(before))
        self.assertEqual(len(self.rows()), 6)
        self.assertEqual(self.path.read_text().count("TS-ISO8601\tPV\tVALUE"), 1)

    def test_empty_existing_file_gets_header(self):
        self.path.touch()
        self.monitor.start()
        self.monitor.stop()
        self.assertEqual(len(self.rows()), 2)

    def test_bad_constant_leaves_file_untouched_and_allows_retry(self):
        self.monitor._constants = {"test": BadValue()}
        with self.assertRaisesRegex(ValueError, "cannot format"):
            self.monitor.start()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.monitor._accepting)
        self.assertIsNone(self.monitor._writer_thread)

        self.path.write_text("# preserved\n")
        with self.assertRaisesRegex(ValueError, "cannot format"):
            self.monitor.start()
        self.assertEqual(self.path.read_text(), "# preserved\n")

        self.path.unlink()
        self.monitor._constants = {"test": "recovered"}
        self.monitor.start()
        self.monitor.stop()
        self.assertEqual(len(self.rows()), 2)
        self.assertIn("# test\trecovered", self.path.read_text())

    def test_incomplete_or_invalid_header_is_preserved_and_rejected(self):
        for content in (
            "# metadata_constants\n",
            "# constants\nTS-ISO8601\tPV\tVALUE",
            "TS-ISO8601\tPV\tVALUE\n",
        ):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaisesRegex(ValueError, "incomplete or invalid header"):
                    self.monitor.start()
                self.assertEqual(self.path.read_text(), content)
                self.assertFalse(self.monitor._accepting)
                self.assertIsNone(self.monitor._writer_thread)

    def test_interrupted_header_write_cannot_be_resumed_as_valid(self):
        import builtins
        from contextlib import contextmanager

        @contextmanager
        def interrupted_open(*args, **kwargs):
            with builtins.open(*args, **kwargs) as f:
                proxy = unittest.mock.MagicMock(wraps=f)

                def interrupted_write(text):
                    f.write(text[:12])
                    f.flush()
                    raise OSError("header write interrupted")

                proxy.write.side_effect = interrupted_write
                yield proxy

        with patch.object(
            module, "open", side_effect=interrupted_open, create=True
        ), self.assertRaisesRegex(OSError, "header write interrupted"):
            self.monitor.start()
        before = self.path.read_bytes()
        self.assertTrue(before)
        with self.assertRaisesRegex(ValueError, "incomplete or invalid header"):
            self.monitor.start()
        self.assertEqual(self.path.read_bytes(), before)

    def test_format_error_does_not_kill_writer(self):
        self.monitor.start()
        self.emit(BadValue())
        self.emit(3)
        with self.assertLogs(module.logger, logging.WARNING) as logs:
            self.monitor.stop()
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.monitor._format_failures, 1)
        self.assertIsNone(self.monitor._writer_error)
        self.assertIn("format_failures=1", logs.output[0])

    def test_io_error_accounts_failed_and_queued_rows(self):
        self.monitor.start()
        self.monitor.stop()
        for value in range(4):
            self.monitor._q.put({"value": value})
        for method in ("write", "flush"):
            with self.subTest(method=method):
                # Four accepted events: the failing row and three queued rows.
                if self.monitor._q.empty():
                    for value in range(4):
                        self.monitor._q.put({"value": value})
                previous = self.monitor._unwritten_events
                fake = unittest.mock.MagicMock()
                getattr(fake.__enter__.return_value, method).side_effect = OSError("disk error")
                self.monitor._writer_failed.clear()
                self.monitor._writer_error = None
                with patch.object(module, "open", return_value=fake, create=True):
                    self.monitor._writer_loop()
                self.assertIsInstance(self.monitor._writer_error, OSError)
                with self.assertLogs(module.logger, logging.WARNING) as logs:
                    self.monitor.stop()
                self.assertEqual(self.monitor._unwritten_events - previous, 4)
                self.assertEqual(self.monitor.get_drop_count(), 0)
                self.assertIn("disk error", logs.output[0])

    def test_open_error_accounts_entire_queue(self):
        self.monitor._q.put({"value": 1})
        with patch.object(module, "open", side_effect=OSError("open failed"), create=True):
            self.monitor._writer_loop()
        self.monitor.stop()
        self.assertEqual(self.monitor._unwritten_events, 1)
        self.assertIsInstance(self.monitor._writer_error, OSError)

    def test_real_writer_thread_records_error_without_traceback(self):
        import builtins

        def open_file(*args, **kwargs):
            if threading.current_thread().name == "scan-meta-writer":
                raise OSError("writer open failed")
            return builtins.open(*args, **kwargs)

        with patch.object(module, "open", side_effect=open_file, create=True), \
                patch.object(threading, "excepthook") as excepthook:
            self.monitor.start()
            self.assertTrue(self.monitor._writer_failed.wait(2))
            for value in range(4):
                self.emit(value)
            with self.assertLogs(module.logger, logging.WARNING):
                self.monitor.stop()
            excepthook.assert_not_called()
        self.assertEqual(self.monitor._unwritten_events, 4)
        self.assertEqual(self.monitor.get_drop_count(), 0)
        self.assertEqual(len(self.rows()), 2)

    def test_initial_snapshot_open_failure_is_reported(self):
        import builtins
        calls = 0

        def open_file(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("snapshot open failed")
            return builtins.open(*args, **kwargs)

        with patch.object(module, "open", side_effect=open_file, create=True):
            self.monitor.start()
            with self.assertLogs(module.logger, logging.WARNING):
                self.monitor.stop()
        self.assertEqual(self.monitor._unwritten_events, 2)
        self.assertEqual(len(self.rows()), 0)

    def test_events_after_writer_failure_count_as_unwritten(self):
        self.monitor.start()
        self.monitor._writer_failed.set()
        self.emit()
        self.monitor.stop()
        self.assertEqual(self.monitor._unwritten_events, 1)
        self.assertEqual(self.monitor.get_drop_count(), 0)

    def test_timeout_retains_writer_and_blocks_restart(self):
        entered, release = threading.Event(), threading.Event()
        original = self.monitor._write_event

        def blocked_write(f, event):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test barrier timeout")
            original(f, event)

        self.monitor.start()
        with patch.object(self.monitor, "_write_event", side_effect=blocked_write):
            try:
                self.emit()
                self.assertTrue(entered.wait(2))
                self.emit()
                thread = self.monitor._writer_thread
                with self.assertLogs(module.logger, logging.WARNING) as logs:
                    self.monitor.stop(join_timeout=0)
                self.assertIs(self.monitor._writer_thread, thread)
                self.assertIn("writer_running=True", logs.output[0])
                self.assertIn("pending_events=2", logs.output[0])
                with self.assertRaises(RuntimeError):
                    self.monitor.start()
                release.set()
                self.monitor.stop()
            finally:
                release.set()
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.monitor._unwritten_events, 0)

    def test_callbacks_have_no_logging_and_count_full_queue(self):
        self.monitor = module.MetadataCAMonitor([], {}, str(self.path), queue_maxsize=1)
        self.monitor._accepting = True
        with patch.object(module, "logger") as logger:
            self.emit()
            self.emit()
            logger.assert_not_called()
            self.assertEqual(logger.mock_calls, [])
        self.assertEqual(self.monitor.get_drop_count(), 1)
        self.assertIsInstance(self.monitor._q.get_nowait()["recv_ts"], float)

    def test_restart_after_timed_out_writer_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = self.monitor._write_event

        def blocked_write(f, event):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test barrier timeout")
            original(f, event)

        self.monitor.start()
        thread = self.monitor._writer_thread
        with patch.object(self.monitor, "_write_event", side_effect=blocked_write):
            try:
                self.emit(42)
                self.assertTrue(entered.wait(2))
                self.emit(43)
                with self.assertLogs(module.logger, logging.WARNING):
                    self.monitor.stop(join_timeout=0)
                with self.assertRaises(RuntimeError):
                    self.monitor.start()
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        before = self.path.read_text()
        # Match BaseScan: no second stop() before the next start().
        self.monitor.start()
        self.emit(44)
        self.monitor.stop()
        self.assertTrue(self.path.read_text().startswith(before))
        self.assertEqual(len(self.rows()), 7)
        self.assertEqual(self.monitor._unwritten_events, 0)

    def test_restart_accounts_failure_after_stop_timeout(self):
        entered, release = threading.Event(), threading.Event()

        def failed_write(text):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test barrier timeout")
            raise OSError("late disk error")

        self.monitor.start()
        thread = self.monitor._writer_thread
        fake = unittest.mock.MagicMock()
        fake.write.side_effect = failed_write
        with patch.object(self.monitor, "_write_event", side_effect=lambda f, ev:
                          module.MetadataCAMonitor._write_event(self.monitor, fake, ev)):
            try:
                self.emit()
                self.assertTrue(entered.wait(2))
                self.emit()
                with self.assertLogs(module.logger, logging.WARNING):
                    self.monitor.stop(join_timeout=0)
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.monitor._unwritten_events, 1)
        self.assertEqual(self.monitor._q.qsize(), 1)
        with self.assertLogs(module.logger, logging.WARNING) as logs:
            self.monitor.start()
        self.assertIn("unwritten_events=2", logs.output[0])
        self.assertIn("late disk error", logs.output[0])
        self.assertIsNone(self.monitor._writer_error)
        self.assertFalse(self.monitor._writer_failed.is_set())
        self.emit(45)
        self.monitor.stop()
        self.assertEqual(self.monitor._unwritten_events, 2)
        self.assertEqual(len(self.rows()), 5)

    def test_failed_writer_without_stop_still_requires_cleanup(self):
        def failed_writer():
            self.monitor._record_writer_error(OSError("disk error"))

        with patch.object(self.monitor, "_writer_loop", side_effect=failed_writer):
            self.monitor.start()
            self.monitor._writer_thread.join(2)
        self.assertFalse(self.monitor._stop.is_set())
        with self.assertRaisesRegex(RuntimeError, "Call stop"):
            self.monitor.start()

    def test_late_callbacks_cannot_enqueue_after_stop(self):
        self.monitor.start()
        self.monitor.stop()
        self.emit()
        self.assertTrue(self.monitor._q.empty())

    def test_disconnected_pv_in_stop_summary(self):
        self.monitor.start()
        self.monitor._pvobjs[0].connected = False
        with self.assertLogs(module.logger, logging.WARNING) as logs:
            self.monitor.stop()
        self.assertIn("disconnected_pvs=['PV:A']", logs.output[0])

    def test_value_formatting(self):
        self.assertEqual(module.MetadataCAMonitor._fmt_value(None), "")
        self.assertEqual(module.MetadataCAMonitor._fmt_value(b"abc"), "abc")
        self.assertEqual(module.MetadataCAMonitor._fmt_value(np.array([1, 2])),
                         "[1.000000000000e+00 2.000000000000e+00]")
        self.assertEqual(module.MetadataCAMonitor._ts_to_iso(None), "")


if __name__ == "__main__":
    unittest.main()
