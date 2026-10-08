# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiwi_scan.scan.output_manager import OutputManager


class _CountingLock:
    def __init__(self) -> None:
        self.enter_count = 0

    def __enter__(self):
        self.enter_count += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback
        return False


class TestOutputManager(unittest.TestCase):
    def test_point_write_uses_one_lock_without_nested_accessors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            header_factory = MagicMock(return_value=["Position", "DET:A"])
            manager = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                data_writing_enabled=True,
                output_timestamp="20260904180000",
                header_factory=header_factory,
            )
            counting_lock = _CountingLock()
            manager._lock = counting_lock

            with manager.point_write() as path:
                self.assertIsNotNone(path)
                self.assertEqual(counting_lock.enter_count, 1)

            with manager.point_write() as path:
                self.assertIsNotNone(path)
                self.assertEqual(counting_lock.enter_count, 2)

            header_factory.assert_called_once_with()
            self.assertTrue(manager._header_written)

    def test_runtime_writing_state_reports_changes(self) -> None:
        manager = OutputManager(
            data_dir=".",
            requested_output_file="scan.txt",
            data_writing_enabled=True,
            output_timestamp="20260904180000",
        )

        self.assertTrue(manager.get_data_writing_enabled())
        self.assertFalse(manager.set_data_writing_enabled(True))
        self.assertTrue(manager.set_data_writing_enabled(False))
        self.assertFalse(manager.get_data_writing_enabled())

    def test_generate_and_create_file_uses_suffix_after_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                output_timestamp="20260825160000",
            )
            first = Path(tmp) / "scan-20260825160000.txt"
            first.touch()

            with patch(
                "kiwi_scan.scan.output_manager.random.choices",
                return_value=list("ABC123"),
            ):
                result = manager.generate_and_create_file()

            self.assertEqual(
                result,
                str(Path(tmp) / "scan-20260825160000_ABC123.txt"),
            )
            self.assertTrue(Path(result).is_file())

    def test_generate_and_create_file_never_reuses_file_created_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                output_timestamp="20260825160000",
            )
            other = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                output_timestamp="20260825160000",
            )
            taken = Path(tmp) / "scan-20260825160000.txt"
            real_open = open
            raced = []

            def racing_open(path, mode="r", *args, **kwargs):
                # Another scan creates and fills the file just before we create it.
                if not raced and str(path) == str(taken):
                    raced.append(True)
                    self.assertEqual(other.generate_and_create_file(), str(taken))
                    with real_open(taken, "w", encoding="utf-8") as file:
                        file.write("other scan\n")
                return real_open(path, mode, *args, **kwargs)

            with patch("builtins.open", side_effect=racing_open):
                result = manager.generate_and_create_file()

            self.assertNotEqual(result, str(taken))
            self.assertTrue(Path(result).is_file())
            self.assertEqual(taken.read_text(encoding="utf-8"), "other scan\n")

    def test_ensure_output_file_exists_is_lazy_idempotent_and_respects_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                data_writing_enabled=False,
                output_timestamp="20260904180000",
            )

            self.assertIsNone(manager.ensure_output_file_exists())
            self.assertIsNone(manager.output_file)

            manager.set_data_writing_enabled(True)
            first = manager.ensure_output_file_exists()
            second = manager.ensure_output_file_exists()

            self.assertEqual(first, second)
            self.assertIsNotNone(first)
            self.assertTrue(Path(first or "").is_file())

            manager.set_data_writing_enabled(False)
            self.assertIsNone(manager.ensure_output_file_exists())
            self.assertEqual(manager.output_file, first)

    def test_write_header_writes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                data_writing_enabled=True,
                output_timestamp="20260904180000",
            )

            path = manager.write_header(["Position", "DET:A"])
            manager.write_header(["replacement"])

            self.assertIsNotNone(path)
            self.assertTrue(manager.header_written)
            self.assertEqual(
                Path(path or "").read_text(encoding="utf-8"),
                "Position\tDET:A\n",
            )

    def test_changing_output_file_resets_header_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = OutputManager(
                data_dir=tmp,
                requested_output_file="scan.txt",
                data_writing_enabled=True,
                output_timestamp="20260904180000",
            )
            first = Path(tmp) / "first.txt"
            second = Path(tmp) / "second.txt"

            manager.output_file = str(first)
            manager.write_header(["A"])
            self.assertTrue(manager.header_written)

            manager.output_file = str(second)
            self.assertFalse(manager.header_written)
            manager.write_header(["B"])

            self.assertEqual(second.read_text(encoding="utf-8"), "B\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
