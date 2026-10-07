# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from kiwi_scan.scan._point_frame import _DetectorLayout
from kiwi_scan.scan.point_pipeline import PointPipeline


class _ProviderState:
    def __init__(self, headers=None, values=None) -> None:
        self.headers: List[str] = list(headers or [])
        self.values: List[Any] = list(values or [])
        self.reset_count = 0

    def get_headers(self, _include_timestamps: bool) -> List[str]:
        return list(self.headers)

    def get_values(self) -> List[Any]:
        return list(self.values)

    def update_last_point(
        self,
        last: Dict[str, Any],
        _include_timestamps: bool,
    ) -> None:
        for header, value in zip(self.headers, self.values):
            last[header] = value

    def reset_window(self) -> None:
        self.reset_count += 1


class _FlakyProvider(_ProviderState):
    """Provider whose ``get_values()`` can start failing mid-scan."""

    def __init__(self, headers=None, values=None) -> None:
        super().__init__(headers, values)
        self.error: Optional[Exception] = None

    def get_values(self) -> List[Any]:
        if self.error is not None:
            raise self.error
        return super().get_values()


class _Plugin:
    def __init__(self, headers: List[str]) -> None:
        self.headers = list(headers)

    def get_headers(self, timestamps: bool) -> List[str]:
        if not timestamps:
            return [header for header in self.headers if not header.startswith("TS-")]
        return list(self.headers)


def _make_pipeline(
    *,
    include_timestamps: bool = True,
    timestamp_output_format: str = "iso8601",
    detector_headers: Tuple[str, ...] = ("DET",),
    plugins=None,
) -> PointPipeline:
    plugin_list = list(plugins or [])
    return PointPipeline(
        include_timestamps=include_timestamps,
        timestamp_output_format=timestamp_output_format,
        detector_layout=_DetectorLayout.from_headers(detector_headers),
        get_plugins=lambda: plugin_list,
    )


class TestPointPipelineColumns(unittest.TestCase):
    def test_output_policy_is_fixed_at_construction(self) -> None:
        pipeline = _make_pipeline(
            include_timestamps=False,
            timestamp_output_format="unix",
        )

        self.assertFalse(pipeline.include_timestamps)
        self.assertEqual(pipeline.timestamp_output_format, "unix")
        self.assertFalse(hasattr(pipeline, "set_output_policy"))
        self.assertFalse(hasattr(pipeline, "set_include_timestamps"))

    def test_format_scan_value_handles_numbers_text_and_none(self) -> None:
        self.assertEqual(PointPipeline.format_scan_value(None), "")
        self.assertEqual(
            PointPipeline.format_scan_value(1.25),
            "1.250000000000e+00",
        )
        self.assertEqual(PointPipeline.format_scan_value("ready"), "ready")

    def test_pipeline_owns_providers_and_returns_defensive_list(self) -> None:
        pipeline = _make_pipeline()
        provider = _ProviderState(["mean"], [3.0])

        pipeline.add_column_provider(provider)
        returned = pipeline.get_data_column_providers()
        returned.clear()

        self.assertEqual(pipeline.get_data_column_providers(), [provider])
        self.assertEqual(pipeline.get_data_column_headers(False), ["mean"])
        self.assertEqual(pipeline.get_data_column_values(), [3.0])

    def test_provider_failures_do_not_hide_other_providers(self) -> None:
        pipeline = _make_pipeline()
        broken = MagicMock()
        broken.get_headers.side_effect = RuntimeError("broken headers")
        broken.get_values.side_effect = RuntimeError("broken values")
        working = _ProviderState(["mean"], [3.0])
        pipeline.add_column_provider(broken)
        pipeline.add_column_provider(working)

        with self.assertLogs("kiwi_scan.scan.point_pipeline", level="ERROR"):
            self.assertEqual(pipeline.get_data_column_headers(False), ["mean"])
            self.assertEqual(pipeline.get_data_column_values(), [3.0])

    def test_provider_window_and_last_point_updates_are_owned_by_pipeline(self) -> None:
        pipeline = _make_pipeline()
        provider = _ProviderState(["mean"], [7.5])
        pipeline.add_column_provider(provider)
        last = {}

        pipeline.reset_data_column_provider_windows()
        pipeline.update_data_column_provider_cache(last, False)

        self.assertEqual(provider.reset_count, 1)
        self.assertEqual(last, {"mean": 7.5})

    def test_output_headers_combine_provider_detector_and_plugin_columns(self) -> None:
        pipeline = _make_pipeline(
            detector_headers=("DET:A", "DET:B"),
            plugins=[_Plugin(["P", "TS-P"])],
        )
        pipeline.add_column_provider(_ProviderState(["mean"], [3.0]))

        self.assertEqual(
            pipeline.build_output_headers(True),
            [
                "Position",
                "mean",
                "TS-ISO8601",
                "DET:A",
                "TS-ISO8601-DET:A",
                "DET:B",
                "TS-ISO8601-DET:B",
                "P",
            ],
        )

    def test_unix_output_policy_keeps_only_row_and_detector_timestamp_headers(self) -> None:
        pipeline = _make_pipeline(
            timestamp_output_format="unix",
            plugins=[_Plugin(["P", "TS-P"])],
        )

        self.assertEqual(
            pipeline.build_output_headers(True),
            ["Position", "TS-UNIX", "DET", "TS-UNIX-DET", "P"],
        )

    def test_build_output_row_values_keeps_raw_timestamps(self) -> None:
        pipeline = _make_pipeline(plugins=[_Plugin(["P", "TS-P"])])
        pipeline.add_column_provider(_ProviderState(["mean"], [9.0]))

        row = pipeline.build_output_row_values(
            10.0,
            [{"value": 1.5, "timestamp": 123.0}, 2.5],
            include_timestamps=True,
            line_timestamp=456.0,
        )

        self.assertEqual(row, [10.0, 9.0, 456.0, 1.5, 123.0, 2.5])

    def test_build_output_row_values_can_omit_detector_timestamps(self) -> None:
        pipeline = _make_pipeline()

        row = pipeline.build_output_row_values(
            10.0,
            [{"value": 1.5, "timestamp": 123.0}, 2.5],
            include_timestamps=False,
            line_timestamp=456.0,
            provider_values=[],
        )

        self.assertEqual(row, [10.0, 456.0, 1.5, 2.5])


class TestPointPipelineProviderFailures(unittest.TestCase):
    def test_failing_provider_keeps_row_aligned_with_header(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)
        stats = _FlakyProvider(["aMean", "aStd"], [1.0, 2.0])
        pipeline.add_column_provider(stats)
        pipeline.add_column_provider(_ProviderState(["bMean"], [9.0]))
        headers = pipeline.build_output_headers()
        self.assertEqual(
            headers,
            ["Position", "aMean", "aStd", "bMean", "TS-ISO8601", "DET"],
        )

        stats.error = RuntimeError("broken values")
        with patch("kiwi_scan.scan.point_pipeline.logger") as log:
            frame = pipeline.begin_point_frame(idx=0, pos=5.0, values=[10.0])
            self.assertIsNone(pipeline.get_current_row_value("aMean", "missing"))
            self.assertIsNone(pipeline.get_current_row_value("aStd", "missing"))
            self.assertEqual(pipeline.get_current_row_value("bMean"), 9.0)
            with patch("kiwi_scan.scan.point_pipeline.time.time", return_value=100.0):
                point = pipeline.freeze_point_frame(frame)

        self.assertEqual(
            list(point.row_values),
            [5.0, None, None, 9.0, 100.0, 10.0],
        )
        self.assertEqual(point.timestamp_indices, frozenset({4}))
        self.assertEqual(log.method_calls, [])

    def test_wrong_value_count_blanks_only_that_provider(self) -> None:
        pipeline = _make_pipeline()
        pipeline.add_column_provider(_ProviderState(["aMean", "aStd"], [1.0]))
        pipeline.add_column_provider(_ProviderState(["bMean"], [2.0, 3.0]))
        pipeline.add_column_provider(_ProviderState(["cMean"], [4.0]))
        pipeline.build_output_headers()

        self.assertEqual(pipeline.get_data_column_values(), [None, None, None, 4.0])

        with self.assertLogs("kiwi_scan.scan.point_pipeline", level="WARNING") as logs:
            pipeline.report_data_column_failures()
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(len(messages), 2)
        self.assertIn("(aMean, aStd)", messages[0])
        self.assertIn("returned 1 value(s) for 2 column(s)", messages[0])
        self.assertIn("(bMean)", messages[1])
        self.assertIn("returned 2 value(s) for 1 column(s)", messages[1])

    def test_failures_are_reported_once_per_scan(self) -> None:
        pipeline = _make_pipeline()
        stats = _FlakyProvider(["aMean", "aStd"], [1.0, 2.0])
        pipeline.add_column_provider(stats)
        pipeline.build_output_headers()
        stats.error = RuntimeError("broken values")
        for _ in range(3):
            pipeline.get_data_column_values()

        with self.assertLogs("kiwi_scan.scan.point_pipeline", level="WARNING") as logs:
            pipeline.report_data_column_failures()
        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn("_FlakyProvider (aMean, aStd)", message)
        self.assertIn("3 value read(s) failed", message)
        self.assertIn("get_values() raised RuntimeError: broken values", message)

        with patch("kiwi_scan.scan.point_pipeline.logger") as log:
            pipeline.report_data_column_failures()
        self.assertEqual(log.method_calls, [])

    def test_rows_use_provider_headers_captured_with_output_header(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)
        no_headers = MagicMock()
        no_headers.get_headers.side_effect = RuntimeError("broken headers")
        no_headers.get_values.return_value = [7.0]
        provider = MagicMock()
        provider.get_headers.return_value = ["mean"]
        provider.get_values.return_value = [3.0]
        pipeline.add_column_provider(no_headers)
        pipeline.add_column_provider(provider)
        with self.assertLogs("kiwi_scan.scan.point_pipeline", level="ERROR"):
            headers = pipeline.build_output_headers()

        provider.get_headers.side_effect = RuntimeError("headers changed")
        frame = pipeline.begin_point_frame(idx=0, pos=1.0, values=[2.0])
        point = pipeline.freeze_point_frame(frame)

        self.assertEqual(headers, ["Position", "mean", "TS-ISO8601", "DET"])
        self.assertEqual(len(point.row_values), len(headers))
        self.assertEqual(point.row_values[1], 3.0)
        self.assertEqual(pipeline.get_current_row_value("mean"), 3.0)
        provider.get_headers.assert_called_once_with(False)
        no_headers.get_values.assert_not_called()


class TestPointPipelineState(unittest.TestCase):
    def test_current_cache_is_owned_and_returned_defensively(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)

        row = pipeline.update_current_row_cache(
            idx=3,
            pos=4.5,
            values=[{"value": 11.0, "timestamp": 10.0}],
        )
        row["DET"] = 99.0

        self.assertEqual(pipeline.get_current_row_value("idx"), 3)
        self.assertEqual(pipeline.get_current_row_value("Position"), 4.5)
        self.assertEqual(pipeline.get_current_row_value("DET"), 11.0)


    def test_update_current_row_cache_flattens_explicit_headers(self) -> None:
        pipeline = _make_pipeline(detector_headers=("DET:A", "DET:B"))

        row = pipeline.update_current_row_cache(
            idx=3,
            pos="4.5",
            values=[{"value": 11.0, "timestamp": 0}, 12.0],
            headers=["DET:A", "DET:B"],
            provider_values=[],
            line_timestamp=200.0,
        )

        self.assertEqual(row["idx"], 3)
        self.assertEqual(row["pos"], "4.5")
        self.assertEqual(row["Position"], 4.5)
        self.assertEqual(row["TS"], 200.0)
        self.assertEqual(row["DET:A"], 11.0)
        self.assertEqual(row["DET:B"], 12.0)
        self.assertEqual(row["TS-DET:A"], 0)

    def test_update_current_row_cache_rejects_detector_count_mismatch(self) -> None:
        pipeline = _make_pipeline(detector_headers=("DET:A", "DET:B"))

        with self.assertLogs(
            "kiwi_scan.scan._point_frame", level="ERROR"
        ) as logs, self.assertRaisesRegex(
            ValueError, "Detector value count mismatch"
        ):
            pipeline.update_current_row_cache(
                idx=3,
                pos=4.5,
                values=[{"value": 11.0, "timestamp": 0}],
                provider_values=[],
            )

        self.assertIn(
            "received 1 values for 2 configured detector headers",
            "\n".join(logs.output),
        )

    def test_update_current_row_cache_can_keep_existing_values(self) -> None:
        pipeline = _make_pipeline(
            include_timestamps=False,
            detector_headers=(),
        )
        pipeline.replace_current_row_cache({"old": 1})

        row = pipeline.update_current_row_cache(
            idx=2,
            pos=None,
            values=[],
            provider_values=[],
            clear=False,
        )

        self.assertEqual(row["old"], 1)
        self.assertIsNone(row["Position"])

    def test_starting_next_point_aborts_previous_frame(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)
        first = pipeline.begin_point_frame(
            idx=0,
            pos=1.0,
            values=[{"value": 10.0, "timestamp": 10.0}],
        )

        second = pipeline.begin_point_frame(
            idx=1,
            pos=2.0,
            values=[{"value": 20.0, "timestamp": 20.0}],
        )

        self.assertIs(pipeline.active_point_frame, second)
        with self.assertRaisesRegex(RuntimeError, "aborted point frame"):
            first.append_values(["late"], [99.0])

    def test_plugin_values_extend_the_same_in_progress_cache(self) -> None:
        pipeline = _make_pipeline(include_timestamps=True)
        pipeline.begin_point_frame(
            idx=0,
            pos=1.0,
            values=[{"value": 10.0, "timestamp": 11.0}],
        )

        pipeline.append_plugin_point_values(
            ["P1"],
            [{"value": 12.0, "timestamp": 13.0}],
        )

        self.assertEqual(pipeline.get_current_row_value("DET"), 10.0)
        self.assertEqual(pipeline.get_current_row_value("P1"), 12.0)
        self.assertEqual(pipeline.get_current_row_value("TS-P1"), 13.0)


class TestPointPipelinePersistence(unittest.TestCase):
    def test_pipeline_owns_writer_state_and_metrics(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)

        self.assertIsNone(pipeline.get_parallel_writer())
        self.assertEqual(pipeline.point_writer_queue_size, 1024)
        self.assertEqual(pipeline.point_writer_queue_high_water, 0)
        self.assertEqual(pipeline.point_writer_maximum_queue_delay, 0.0)

        pipeline.set_point_writer_queue_size(8)
        self.assertEqual(pipeline.point_writer_queue_size, 8)

    def test_invalid_writer_queue_size_is_rejected(self) -> None:
        pipeline = _make_pipeline()

        with self.assertRaisesRegex(ValueError, "greater than zero"):
            pipeline.set_point_writer_queue_size(0)

    def test_synchronous_persistence_does_not_start_writer(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)
        point = pipeline.prepare_legacy_point(
            2.0,
            [{"value": 8.0, "timestamp": 10.0}],
            False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scan.txt")
            pipeline.persist_prepared_point_sync(path, point)
            contents = Path(path).read_text(encoding="utf-8")

        columns = contents.rstrip("\n").split("\t")
        self.assertEqual(columns[0], "2.000000000000e+00")
        self.assertTrue(columns[1])
        self.assertEqual(columns[2], "8.000000000000e+00")
        self.assertIsNone(pipeline.get_parallel_writer())

    def test_async_persistence_writer_is_owned_and_drained_by_pipeline(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)
        first = pipeline.prepare_legacy_point(
            1.0,
            [{"value": 10.0, "timestamp": 11.0}],
            False,
        )
        second = pipeline.prepare_legacy_point(
            2.0,
            [{"value": 20.0, "timestamp": 12.0}],
            False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scan.txt")
            pipeline.persist_prepared_point_async(path, first)
            writer = pipeline.get_parallel_writer()
            self.assertIsNotNone(writer)
            pipeline.persist_prepared_point_async(path, second)
            pipeline.stop_parallel_writer()
            contents = Path(path).read_text(encoding="utf-8").splitlines()

        self.assertIsNone(pipeline.get_parallel_writer())
        self.assertEqual(len(contents), 2)
        self.assertTrue(contents[0].startswith("1.000000000000e+00\t"))
        self.assertTrue(contents[1].startswith("2.000000000000e+00\t"))
        self.assertGreaterEqual(pipeline.point_writer_queue_high_water, 1)
        self.assertGreaterEqual(pipeline.point_writer_maximum_queue_delay, 0.0)

    def test_sync_persistence_uses_running_writer_fifo(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)
        first = pipeline.prepare_legacy_point(
            1.0,
            [{"value": 10.0, "timestamp": 11.0}],
            False,
        )
        second = pipeline.prepare_legacy_point(
            2.0,
            [{"value": 20.0, "timestamp": 12.0}],
            False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scan.txt")
            pipeline.persist_prepared_point_async(path, first)
            pipeline.persist_prepared_point_sync(path, second)
            pipeline.stop_parallel_writer()
            contents = Path(path).read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(contents), 2)
        self.assertTrue(contents[0].startswith("1.000000000000e+00\t"))
        self.assertTrue(contents[1].startswith("2.000000000000e+00\t"))

    def test_stop_detaches_writer_even_when_writer_reports_error(self) -> None:
        pipeline = _make_pipeline(include_timestamps=False)
        writer = MagicMock()
        writer.stop.side_effect = OSError("disk full")
        writer.queue_high_water = 7
        writer.maximum_queue_delay = 0.25
        pipeline._parallel_point_writer = writer

        with self.assertRaisesRegex(OSError, "disk full"):
            pipeline.stop_parallel_writer()

        self.assertIsNone(pipeline.get_parallel_writer())
        self.assertEqual(pipeline.point_writer_queue_high_water, 7)
        self.assertEqual(pipeline.point_writer_maximum_queue_delay, 0.25)


class TestPointPipelineCompletedPoint(unittest.TestCase):
    def test_freeze_publishes_last_point_and_detaches_frame(self) -> None:
        plugin = _Plugin(["P1", "TS-P1"])
        pipeline = _make_pipeline(include_timestamps=True, plugins=[plugin])
        frame = pipeline.begin_point_frame(
            idx=0,
            pos=5.0,
            values=[{"value": 10.0, "timestamp": 101.25}],
        )
        pipeline.append_plugin_point_values(
            ["P1"],
            [{"value": 11.0, "timestamp": 102.25}],
        )

        with patch("kiwi_scan.scan.point_pipeline.time.time", return_value=100.25):
            point = pipeline.freeze_point_frame(frame)

        self.assertEqual(
            list(point.row_values),
            [5.0, 100.25, 10.0, 101.25, 11.0],
        )
        self.assertEqual(point.timestamp_indices, frozenset({1, 3}))
        self.assertEqual(len(point.row_values), len(pipeline.build_output_headers()))
        self.assertEqual(pipeline.get_value("TS-P1"), 102.25)
        self.assertEqual(pipeline.get_value("DET"), 10.0)
        self.assertEqual(pipeline.get_value("P1"), 11.0)
        self.assertEqual(pipeline.get_value("TS-DET"), 101.25)
        self.assertIsNone(pipeline.active_point_frame)

    def test_get_value_preserves_metadata_option(self) -> None:
        pipeline = _make_pipeline()
        metadata = {"value": 12.5, "timestamp": 100.0}
        pipeline.replace_last_point({"DET": metadata, "state": "ready"})

        self.assertEqual(pipeline.get_value("DET"), 12.5)
        self.assertIs(pipeline.get_value("DET", with_metadata=True), metadata)
        self.assertEqual(pipeline.get_value("state"), "ready")
        self.assertEqual(pipeline.get_last_point_keys(), ["DET", "state"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
