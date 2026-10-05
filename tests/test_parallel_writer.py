# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, List

from kiwi_scan.scan._parallel_writer import _ParallelPointWriter
from kiwi_scan.scan._point_frame import _PreparedPoint


def _point(*values: Any) -> _PreparedPoint:
    return _PreparedPoint(
        row_values=tuple(values),
        completed_values={},
        line_timestamp=None,
    )


def _read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _wait_for_lines(path: Path, count: int, timeout: float = 2.0) -> List[str]:
    deadline = time.monotonic() + timeout
    lines = _read_lines(path)
    while len(lines) < count and time.monotonic() < deadline:
        time.sleep(0.01)
        lines = _read_lines(path)
    return lines


class ParallelPointWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "scan.txt"

    def _start_writer(
        self,
        format_value=str,
        flush_interval: float = 0.05,
    ) -> _ParallelPointWriter:
        writer = _ParallelPointWriter(format_value, flush_interval=flush_interval)
        writer.start(str(self.path))
        self.addCleanup(self._stop_quietly, writer)
        return writer

    @staticmethod
    def _stop_quietly(writer: _ParallelPointWriter) -> None:
        try:
            writer.stop()
        except Exception:  # noqa: BLE001
            pass

    def test_async_rows_reach_file_before_stop(self) -> None:
        writer = self._start_writer()

        for index in range(3):
            writer.submit(_point(index, index * 10))

        lines = _wait_for_lines(self.path, 3)
        self.assertTrue(writer.is_running)
        self.assertEqual(lines, ["0\t0", "1\t10", "2\t20"])

    def test_async_rows_are_batched_within_flush_interval(self) -> None:
        writer = self._start_writer(flush_interval=60.0)

        for index in range(3):
            writer.submit(_point(index))
        time.sleep(0.2)

        self.assertEqual(_read_lines(self.path), [])
        writer.stop()
        self.assertEqual(_read_lines(self.path), ["0", "1", "2"])

    def test_negative_flush_interval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ParallelPointWriter(str, flush_interval=-1.0)

    def test_submit_and_wait_row_is_on_disk_when_call_returns(self) -> None:
        writer = self._start_writer()

        writer.submit_and_wait(_point(1, 2))

        self.assertEqual(_read_lines(self.path), ["1\t2"])

    def test_unopenable_file_is_reported(self) -> None:
        writer = _ParallelPointWriter(str, flush_interval=0.05)
        writer.start(str(Path(self._tmp.name) / "missing" / "scan.txt"))

        with self.assertRaises(FileNotFoundError):
            try:
                writer.submit(_point(1))
            finally:
                writer.stop()

    def test_format_error_fails_request_and_later_submits(self) -> None:
        def format_value(value: Any) -> str:
            if value == "bad":
                raise ValueError("cannot format")
            return str(value)

        writer = self._start_writer(format_value)

        with self.assertRaisesRegex(ValueError, "cannot format"):
            writer.submit_and_wait(_point("bad"))
        with self.assertRaisesRegex(ValueError, "cannot format"):
            writer.submit(_point(1))
        with self.assertRaisesRegex(ValueError, "cannot format"):
            writer.stop()
        self.assertEqual(_read_lines(self.path), [])


if __name__ == "__main__":
    unittest.main()
