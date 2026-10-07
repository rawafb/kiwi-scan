# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from ._parallel_writer import _ParallelPointWriter
from ._point_frame import _DetectorLayout, _PointFrame, _PreparedPoint
from .column_provider import DataColumnProvider

logger = logging.getLogger(__name__)

# Each provider with the headers it had when the output header was built.
_ProviderLayout = Tuple[Tuple[DataColumnProvider, Tuple[str, ...]], ...]


@dataclass
class _ProviderFailures:
    """Failed value reads of one data column provider since the last report."""

    provider: Any
    headers: Tuple[str, ...]
    first_failure: str
    count: int = 1


class PointPipeline:
    """
    Own mutable point/frame/cache state for one scan instance.
    - owns point/cache state, data-column providers, and output column construction
    - never holds a reference to the scan object
    """
    def __init__(
        self,
        *,
        include_timestamps: bool,
        timestamp_output_format: str,
        detector_layout: _DetectorLayout,
        get_plugins: Callable[[], Sequence[Any]],
        performance_enabled: Optional[Callable[[], bool]] = None,
        record_perf_sample: Optional[
            Callable[[str, float, Optional[int]], None]
        ] = None,
        writer_queue_size: int = 1024,
    ) -> None:
        self.include_timestamps = bool(include_timestamps)
        self.timestamp_output_format = self._normalize_timestamp_output_format(
            timestamp_output_format
        )
        self._detector_layout = detector_layout # fixed
        self._get_plugins = get_plugins
        self._data_column_providers: List[DataColumnProvider] = []
        self._data_column_layout: Optional[_ProviderLayout] = None
        self._data_column_failures: Dict[int, _ProviderFailures] = {}

        self._performance_enabled = performance_enabled or (lambda: False)
        self._record_perf_sample = record_perf_sample

        self._last_point: Dict[str, Any] = {}
        self._current_row_cache: Dict[str, Any] = {}
        self._active_point_frame: Optional[_PointFrame] = None

        self._writer_lock = threading.RLock()
        self._parallel_point_writer: Optional[_ParallelPointWriter] = None
        self._point_writer_queue_size = self._normalize_writer_queue_size(writer_queue_size)
        self._point_writer_queue_high_water = 0
        self._point_writer_maximum_queue_delay = 0.0

    # -------------------- state / diagnostics --------------------

    @staticmethod
    def _normalize_timestamp_output_format(value: str) -> str:
        normalized = str(value or "iso8601").strip().lower()
        if normalized not in {"iso8601", "unix"}:
            raise ValueError(f"timestamp_output_format must be iso8601 or unix (got {value!r})")
        return normalized

    @staticmethod
    def _normalize_writer_queue_size(value: int) -> int:
        size = int(value)
        if size <= 0:
            raise ValueError("writer_queue_size must be greater than zero")
        return size

    @property
    def parallel_point_writer(self) -> Optional[_ParallelPointWriter]:
        """Return the owned writer for diagnostics and compatibility tests."""
        with self._writer_lock:
            return self._parallel_point_writer

    @property
    def point_writer_queue_size(self) -> int:
        with self._writer_lock:
            return self._point_writer_queue_size

    def set_point_writer_queue_size(self, queue_size: int) -> None:
        """Set the queue size used when the next writer is created."""
        normalized = self._normalize_writer_queue_size(queue_size)
        with self._writer_lock:
            self._point_writer_queue_size = normalized

    @property
    def point_writer_queue_high_water(self) -> int:
        with self._writer_lock:
            return self._point_writer_queue_high_water

    @property
    def point_writer_maximum_queue_delay(self) -> float:
        with self._writer_lock:
            return self._point_writer_maximum_queue_delay

    @property
    def detector_layout(self) -> _DetectorLayout:
        return self._detector_layout

    def set_detector_layout(self, layout: _DetectorLayout) -> None:
        """
        Replace compiled detector metadata.
        Production scans compile this once during initialization.
        """
        self._detector_layout = layout

    @property
    def active_point_frame(self) -> Optional[_PointFrame]:
        """Return the currently owned frame for diagnostics and tests."""
        return self._active_point_frame

    def replace_last_point(self, values: Dict[str, Any]) -> None:
        """Replace completed-point state while preserving nested metadata objects."""
        self._last_point = dict(values or {})

    def replace_current_row_cache(self, values: Dict[str, Any]) -> None:
        """Replace in-progress row state and detach any unfinished frame."""
        self.abort_active_point_frame()
        self._current_row_cache = dict(values or {})

    # -------------------- providers / column construction --------------------

    def add_column_provider(self, provider: DataColumnProvider) -> None:
        """Register an object that contributes dynamic scan-file columns.

        Register providers before the scan starts: the output header fixes
        which provider columns every row contains.
        """
        if provider is not None:
            self._data_column_providers.append(provider)
            self._data_column_layout = None

    def get_data_column_providers(self) -> List[DataColumnProvider]:
        """Return a defensive copy of registered providers."""
        return list(self._data_column_providers)

    def _capture_data_column_layout(self, include_timestamps: bool) -> _ProviderLayout:
        layout = []
        for provider in self._data_column_providers:
            try:
                headers = tuple(provider.get_headers(include_timestamps))
            except Exception:
                logger.exception("Failed to read data column provider headers from %s", provider)
                headers = ()
            layout.append((provider, headers))
        captured = tuple(layout)
        self._data_column_layout = captured
        return captured

    def _get_data_column_layout(self) -> _ProviderLayout:
        layout = self._data_column_layout
        if layout is None:
            layout = self._capture_data_column_layout(self.include_timestamps)
        return layout

    def get_data_column_headers(self, include_timestamps: bool) -> List[str]:
        """Return provider headers and fix the provider columns of later rows.

        Called when an output header is built. Every row built afterwards
        holds exactly one value per header returned here.
        """
        layout = self._capture_data_column_layout(include_timestamps)
        return [header for _provider, headers in layout for header in headers]

    def get_data_column_values(self) -> List[Any]:
        """Return one value per provider header, in header order.

        HOT PATH: no logging. A provider that raises, or returns more or fewer
        values than it has headers, gets ``None`` in all of its columns so
        later columns keep their position. Failures are counted and logged
        once by ``report_data_column_failures()`` at scan cleanup.
        """
        values: List[Any] = []
        for index, (provider, headers) in enumerate(self._get_data_column_layout()):
            if not headers:
                continue
            try:
                provider_values = list(provider.get_values())
            except Exception as exc:  # noqa: BLE001 - counted, reported at cleanup
                self._count_data_column_failure(index, provider, headers, exc)
                values.extend([None] * len(headers))
                continue
            if len(provider_values) != len(headers):
                self._count_data_column_failure(index, provider, headers, len(provider_values))
                provider_values = [None] * len(headers)
            values.extend(provider_values)
        return values

    def _count_data_column_failure(
        self,
        index: int,
        provider: DataColumnProvider,
        headers: Tuple[str, ...],
        problem: Union[Exception, int],
    ) -> None:
        """Count one failed provider read; only the first one is described."""
        failures = self._data_column_failures.get(index)
        if failures is not None:
            failures.count += 1
            return
        if isinstance(problem, Exception):
            first_failure = f"get_values() raised {type(problem).__name__}: {problem}"
        else:
            first_failure = (
                f"get_values() returned {problem} value(s) for {len(headers)} column(s)"
            )
        self._data_column_failures[index] = _ProviderFailures(provider, headers, first_failure)

    def report_data_column_failures(self) -> None:
        """Log one warning per provider whose values failed since the last report."""
        failures, self._data_column_failures = self._data_column_failures, {}
        for failure in failures.values():
            logger.warning(
                "Data column provider %s (%s): %d value read(s) failed during the scan "
                "and left its columns empty. First failure: %s",
                type(failure.provider).__name__,
                ", ".join(str(header) for header in failure.headers),
                failure.count,
                failure.first_failure,
            )

    def update_data_column_provider_cache(
        self,
        last: Dict[str, Any],
        include_timestamps: bool,
    ) -> None:
        for provider in self._data_column_providers:
            try:
                provider.update_last_point(last, include_timestamps)
            except Exception:
                logger.exception("Failed to update last-point cache from %s", provider)

    def reset_data_column_provider_windows(self) -> None:
        """Start a new provider data window for the next scan point."""
        for provider in self._data_column_providers:
            reset = getattr(provider, "reset_window", None)
            if not callable(reset):
                continue
            try:
                reset()
            except Exception:
                logger.exception("Failed to reset data column provider %s", provider)

    def _plugins(self) -> Sequence[Any]:
        plugins = self._get_plugins()
        return tuple(plugins or ())

    def build_detector_headers(self, include_timestamps: bool) -> List[str]:
        """Return detector file-column headers in configured timestamp format."""
        detector_headers = list(self._detector_layout.headers)
        if not include_timestamps:
            return detector_headers

        timestamp_prefix = (
            "TS-ISO8601-"
            if self.timestamp_output_format == "iso8601"
            else "TS-UNIX-"
        )
        return [
            item
            for header in detector_headers
            for item in (header, timestamp_prefix + header)
        ]

    def build_plugin_headers(self, include_timestamps: bool) -> List[str]:
        """Return plugin value headers; plugins share the row timestamp."""
        del include_timestamps  # retained for compatibility with existing callers
        plugin_headers: List[str] = []
        for plugin in self._plugins():
            plugin_headers.extend(plugin.get_headers(False))
        return plugin_headers

    def build_output_headers(
        self,
        include_timestamps: Optional[bool] = None,
    ) -> List[str]:
        """Build the complete data-file header list in write order."""
        if include_timestamps is None:
            include_timestamps = self.include_timestamps

        headers: List[str] = ["Position"]
        headers += self.get_data_column_headers(include_timestamps)
        headers.append(
            "TS-ISO8601"
            if self.timestamp_output_format == "iso8601"
            else "TS-UNIX"
        )
        headers += self.build_detector_headers(include_timestamps)
        headers += self.build_plugin_headers(include_timestamps)
        logger.debug("Built output headers: %s", headers)
        return headers

    def build_output_row_values(
        self,
        position: Any,
        detector_values: List[Any],
        include_timestamps: Optional[bool] = None,
        *,
        line_timestamp: Optional[float] = None,
        provider_values: Optional[List[Any]] = None,
    ) -> List[Any]:
        """Build a raw row from detector values followed by plugin values.

        Only the configured detector prefix receives per-value timestamps.
        """
        if include_timestamps is None:
            include_timestamps = self.include_timestamps
        if line_timestamp is None:
            line_timestamp = time.time()
        if provider_values is None:
            provider_values = self.get_data_column_values()

        row: List[Any] = [position]
        row += list(provider_values or [])
        row.append(line_timestamp)
        detector_count = len(self._detector_layout.headers)
        for index, item in enumerate(detector_values or []):
            if isinstance(item, dict):
                value = item.get("value")
                timestamp = item.get("timestamp")
            else:
                value = item
                timestamp = None
            row.append(value)
            if include_timestamps and index < detector_count:
                row.append(timestamp)
        return row

    def last_point_data_headers(self) -> Tuple[List[str], Set[str]]:
        """Return logical detector/plugin value headers for completed cache."""
        data_headers = list(self._detector_layout.headers)
        detector_names = set(data_headers)
        for plugin in self._plugins():
            try:
                data_headers.extend(plugin.get_headers(False))
            except Exception:  # noqa: BLE001
                logger.error( "Failed to get headers from plugin %s", getattr(plugin, "name", plugin))
        return data_headers, detector_names

    # -------------------- current-point cache --------------------

    @staticmethod
    def plain_scan_value(item: Any) -> Any:
        """Return the scalar value stored in one detector/plugin value object."""
        if isinstance(item, dict):
            return item.get("value")
        return item

    def detector_layout_for(
        self,
        headers: Optional[List[str]],
    ) -> _DetectorLayout:
        """Return the compiled detector layout, or compile an explicit override."""
        if headers is not None:
            return _DetectorLayout.from_headers(headers)
        return self._detector_layout

    def populate_provider_row(
        self,
        row: Dict[str, Any],
        provider_values: Optional[List[Any]],
    ) -> None:
        values = (
            self.get_data_column_values()
            if provider_values is None
            else provider_values
        )
        names = (
            name
            for _provider, headers in self._get_data_column_layout()
            for name in headers
        )
        for name, item in zip(names, values):
            row[str(name)] = self.plain_scan_value(item)

    def _perf_is_enabled(self) -> bool:
        try:
            return bool(self._performance_enabled())
        except Exception:
            logger.debug("Failed to get point-pipeline performance state", exc_info=True)
            return False

    def _record_perf(
        self,
        name: str,
        started_at: float,
        idx: Optional[int],
    ) -> None:
        if self._record_perf_sample is None:
            return
        self._record_perf_sample(name, started_at, idx)

    def begin_point_frame(
        self,
        *,
        idx: int,
        pos: Any,
        values: List[Any],
        headers: Optional[List[str]] = None,
        provider_values: Optional[List[Any]] = None,
        line_timestamp: Optional[float] = None,
        clear: bool = True,
    ) -> _PointFrame:
        """Create and own the mutable frame for one in-progress scan point."""
        self.abort_active_point_frame()
        perf_enabled = self._perf_is_enabled()
        perf_started = time.perf_counter() if perf_enabled else 0.0

        row: Dict[str, Any] = (
            {} if clear else dict(self._current_row_cache)
        )
        row["idx"] = idx
        row["pos"] = pos
        row["Position"] = float(pos) if pos is not None else pos
        if line_timestamp is not None:
            row["TS"] = line_timestamp

        if perf_enabled:
            self._record_perf("row_cache:base", perf_started, idx)
            perf_started = time.perf_counter()

        self.populate_provider_row(row, provider_values)

        if perf_enabled:
            self._record_perf("row_cache:providers", perf_started, idx)
            perf_started = time.perf_counter()

        frame = _PointFrame(
            index=idx,
            position=pos,
            include_timestamps=self.include_timestamps,
            current_values=row,
        )
        frame.append_detector_values(
            self.detector_layout_for(headers),
            values,
        )

        if perf_enabled:
            self._record_perf("row_cache:detectors", perf_started, idx)

        self._active_point_frame = frame
        self._current_row_cache = frame.current_values
        return frame

    def update_current_row_cache(
        self,
        *,
        idx: int,
        pos: Any,
        values: List[Any],
        headers: Optional[List[str]] = None,
        provider_values: Optional[List[Any]] = None,
        line_timestamp: Optional[float] = None,
        clear: bool = True,
    ) -> Dict[str, Any]:
        frame = self.begin_point_frame(
            idx=idx,
            pos=pos,
            values=values,
            headers=headers,
            provider_values=provider_values,
            line_timestamp=line_timestamp,
            clear=clear,
        )
        return frame.current_snapshot()

    def abort_active_point_frame(self) -> None:
        """Abort and detach an incomplete point, if one exists."""
        frame = self._active_point_frame
        if frame is not None:
            frame.abort()
        self._active_point_frame = None

    def append_plugin_point_values(
        self,
        headers: List[str],
        values: List[Any],
    ) -> Optional[_PointFrame]:
        """Append plugin columns to the active point without a cache copy."""
        frame = self._active_point_frame
        if frame is not None and frame.is_open:
            frame.append_values(
                [str(name) for name in headers or []],
                values,
                output_timestamps=False,
            )
            self._current_row_cache = frame.current_values
            return frame
        return None

    def get_current_row_cache(self) -> Dict[str, Any]:
        return dict(self._current_row_cache)

    def get_current_row_value(self, key: str, default: Any = None) -> Any:
        return self._current_row_cache.get(key, default)

    # -------------------- frame preparation / completed cache --------------------

    def standalone_point_frame(
        self,
        position: Any,
        values: List[Any],
        include_timestamps: bool,
    ) -> _PointFrame:
        """Build a frame for compatibility callers that only save a point."""
        data_headers, _detector_headers = self.last_point_data_headers()
        frame = _PointFrame(
            index=-1,
            position=position,
            include_timestamps=include_timestamps,
            current_values={},
        )
        detector_count = len(self._detector_layout.headers)
        values = values or []
        frame.append_values(data_headers[:detector_count], values[:detector_count])
        frame.append_values(
            data_headers[detector_count:],
            values[detector_count:],
            output_timestamps=False,
        )
        return frame

    def point_frame_for_save(
        self,
        position: Any,
        values: List[Any],
        include_timestamps: bool,
    ) -> _PointFrame:
        frame = self._active_point_frame
        if frame is not None and frame.matches(
            position,
            values,
            include_timestamps,
        ):
            return frame

        if frame is not None:
            self.abort_active_point_frame()
        return self.standalone_point_frame(
            position,
            values,
            include_timestamps,
        )

    def publish_point_frame(
        self,
        frame: _PointFrame,
        line_timestamp: float,
    ) -> Dict[str, Any]:
        """Publish a fully assembled frame using raw POSIX timestamps."""
        last: Dict[str, Any] = {
            "Position": (
                float(frame.position)
                if frame.position is not None
                else frame.position
            )
        }

        if any(headers for _provider, headers in self._get_data_column_layout()):
            self.update_data_column_provider_cache(last, frame.include_timestamps)
        last["TS"] = line_timestamp
        last.update(frame.completed_values)
        self._last_point = last
        return last

    def seal_point_frame(self, frame: _PointFrame) -> None:
        frame.finish()
        if self._active_point_frame is frame:
            self._active_point_frame = None

    def freeze_point_frame(self, frame: _PointFrame) -> _PreparedPoint:
        """Publish, seal, and freeze one fully assembled raw-timestamp point."""
        line_timestamp = time.time()
        provider_values = self.get_data_column_values()
        row_values = frame.build_output_row(provider_values, line_timestamp)
        timestamp_indices = frame.build_timestamp_indices(len(provider_values))

        try:
            completed_values = self.publish_point_frame(frame, line_timestamp)
        except Exception:
            logger.debug("Failed to update last-point cache", exc_info=True)
            completed_values = dict(self._last_point)
        finally:
            self.seal_point_frame(frame)

        return _PreparedPoint.from_owned_values(
            row_values,
            completed_values,
            line_timestamp,
            timestamp_indices,
        )

    @staticmethod
    def positions_match(left: Any, right: Any) -> bool:
        if left is right:
            return True
        try:
            return bool(left == right)
        except (TypeError, ValueError):
            return False

    def point_frame_for_internal_commit(
        self,
        position: Any,
        values: List[Any],
        include_timestamps: bool,
    ) -> _PointFrame:
        """Use the owned active frame without retraversing its raw readings."""
        frame = self._active_point_frame
        if (
            frame is not None
            and frame.is_open
            and frame.include_timestamps == include_timestamps
            and self.positions_match(frame.position, position)
        ):
            return frame

        if frame is not None:
            self.abort_active_point_frame()
        return self.standalone_point_frame(
            position,
            values,
            include_timestamps,
        )

    def prepare_legacy_point(
        self,
        position: Any,
        values: List[Any],
        include_timestamps: bool,
    ) -> _PreparedPoint:
        frame = self.point_frame_for_save(
            position,
            values,
            include_timestamps,
        )
        return self.freeze_point_frame(frame)

    def prepare_internal_point(
        self,
        position: Any,
        values: List[Any],
        include_timestamps: bool,
    ) -> _PreparedPoint:
        frame = self.point_frame_for_internal_commit(
            position,
            values,
            include_timestamps,
        )
        return self.freeze_point_frame(frame)

    # -------------------- point persistence / writer lifecycle --------------------

    @staticmethod
    def format_scan_value(value: Any) -> str:
        """Format one non-timestamp value for text-file persistence."""
        if value is None:
            return ""
        try:
            return f"{float(value):.12e}"
        except (ValueError, TypeError):
            return str(value)

    def get_parallel_writer(self) -> Optional[_ParallelPointWriter]:
        """Return the current owned writer, if one has been started."""
        return self.parallel_point_writer

    def _ensure_parallel_writer_locked(
        self,
        output_file: str,
    ) -> _ParallelPointWriter:
        """Return/create the writer while ``_writer_lock`` is already held."""
        writer = self._parallel_point_writer
        if writer is not None and writer.is_running:
            return writer
        if writer is not None:
            self._record_stopped_writer_metrics(writer)

        writer = _ParallelPointWriter(
            self.format_scan_value,
            timestamp_output_format=self.timestamp_output_format,
            queue_size=self._point_writer_queue_size,
        )
        writer.start(output_file)
        self._parallel_point_writer = writer
        return writer

    def _record_stopped_writer_metrics(
        self,
        writer: _ParallelPointWriter,
    ) -> None:
        self._point_writer_queue_high_water = max(
            self._point_writer_queue_high_water,
            writer.queue_high_water,
        )
        self._point_writer_maximum_queue_delay = max(
            self._point_writer_maximum_queue_delay,
            writer.maximum_queue_delay,
        )

    def stop_parallel_writer(self) -> None:
        """Drain and detach the owned per-scan writer.

        The writer is detached before ``stop()`` reports an error so a failed
        writer cannot remain reachable as an apparently reusable scan writer.
        """
        with self._writer_lock:
            writer = self._parallel_point_writer
            self._parallel_point_writer = None
            if writer is None:
                return
            try:
                writer.stop()
            finally:
                self._record_stopped_writer_metrics(writer)

    def write_prepared_point_sync(
        self,
        output_file: str,
        point: _PreparedPoint,
    ) -> None:
        """Synchronously append one immutable point without starting a worker."""
        line = _ParallelPointWriter.format_point_line(
            point,
            self.format_scan_value,
            timestamp_output_format=self.timestamp_output_format,
        ) + "\n"
        logger.debug("Save line to file: %s", line)
        with open(output_file, "a", encoding="utf-8") as file:
            file.write(line)

    def persist_prepared_point_async(
        self,
        output_file: str,
        point: _PreparedPoint,
    ) -> None:
        """Enqueue one prepared point on the owned persistent writer."""
        with self._writer_lock:
            writer = self._ensure_parallel_writer_locked(output_file)
            writer.submit(point)

    def persist_prepared_point_sync(
        self,
        output_file: str,
        point: _PreparedPoint,
    ) -> None:
        """Persist one prepared point with legacy synchronous semantics.

        If the asynchronous writer is already active, the point enters the same
        FIFO and this call waits for that request. Otherwise the row is written
        directly without creating a background writer.
        """
        with self._writer_lock:
            writer = self._parallel_point_writer
            if writer is not None and writer.is_running:
                writer.submit_and_wait(point)
                return
            self.write_prepared_point_sync(output_file, point)

    def get_value(
        self,
        name: str,
        *,
        default: Any = None,
        with_metadata: bool = False,
    ) -> Any:
        if not self._last_point or name not in self._last_point:
            return default
        value = self._last_point.get(name, default)
        if with_metadata:
            return value
        if isinstance(value, dict) and "value" in value:
            return value.get("value", default)
        return value

    def get_last_point_keys(self) -> List[str]:
        if not self._last_point:
            return []
        return list(self._last_point.keys())
