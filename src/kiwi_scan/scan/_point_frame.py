# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DetectorLayout:
    """
    Immutable detector-column metadata compiled once per detector list.
    Timestamp headers are the internal, representation-neutral cache keys (TS).
    File-specific timestamp headers are produced by ``BaseScan``.
    TODO: Add duplicate header check, also validate internal TS-names.
    """

    headers: Tuple[str, ...]
    timestamp_headers: Tuple[str, ...]
    detector_headers: FrozenSet[str]

    @classmethod
    def from_headers(cls, headers: Iterable[str]) -> _DetectorLayout:
        normalized = tuple(str(header) for header in headers)
        logger.debug("Compiled detector layout with %d detector columns", len(normalized))
        return cls(
            headers=normalized,
            timestamp_headers=tuple("TS-" + header for header in normalized),
            detector_headers=frozenset(normalized),
        )


@dataclass(frozen=True)
class _PreparedPoint:
    """
    ``row_values`` always contains raw POSIX timestamps. 
    ``timestamp_indices`` identifies the columns that the writer may render as ISO-8601 or Unix format
    The scan thread perform no timestamp formatting
    TODO: nested dictionaries, lists and arrays remain shared, add ownership or make a copy, no need to modify this data afterward yet.
    """

    row_values: Tuple[Any, ...]
    completed_values: Mapping[str, Any]
    line_timestamp: Any
    timestamp_indices: FrozenSet[int] = frozenset()

    @classmethod
    def from_values(
        cls,
        row_values: Iterable[Any],
        completed_values: Mapping[str, Any],
        line_timestamp: Any,
        timestamp_indices: Iterable[int] = (),
    ) -> _PreparedPoint:
        return cls(
            row_values=tuple(row_values),
            completed_values=MappingProxyType(dict(completed_values)),
            line_timestamp=line_timestamp,
            timestamp_indices=frozenset(timestamp_indices),
        )

    @classmethod
    def from_owned_values(
        cls,
        row_values: Iterable[Any],
        completed_values: Dict[str, Any],
        line_timestamp: Any,
        timestamp_indices: Iterable[int] = (),
    ) -> _PreparedPoint:
        """Freeze containers already owned exclusively by the scan engine."""
        return cls(
            row_values=tuple(row_values),
            completed_values=MappingProxyType(completed_values),
            line_timestamp=line_timestamp,
            timestamp_indices=frozenset(timestamp_indices),
        )


@dataclass
class _PointFrame:
    """Mutable, scan-thread-owned representation of one in-progress point.

    Timestamps remain raw POSIX values for the complete acquisition and plugin
    pipeline. ISO-8601 conversion is deliberately a persistence concern.
    """

    index: int
    position: Any
    include_timestamps: bool
    current_values: Dict[str, Any]
    output_values: List[Any] = field(default_factory=list)
    completed_values: Dict[str, Any] = field(default_factory=dict)
    raw_values: List[Any] = field(default_factory=list)
    _timestamp_output_indices: List[int] = field(default_factory=list)
    _value_count: int = 0
    _state: str = "open"
    _owner_thread_id: int = field(default_factory=threading.get_ident)

    def _require_open(self) -> None:
        if self._state == "finished":
            raise RuntimeError("Cannot modify a completed point frame")
        if self._state == "aborted":
            raise RuntimeError("Cannot modify an aborted point frame")
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("Point frame may only be modified by its scan thread")

    @staticmethod
    def _metadata_header(item: Any) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        return item.get("pvname") or item.get("name")

    def _append_output_timestamp(self, timestamp: Any) -> None:
        self._timestamp_output_indices.append(len(self.output_values))
        self.output_values.append(timestamp)

    def append_values(
        self,
        headers: List[str],
        values: List[Any],
        *,
        output_timestamps: bool = True,
    ) -> None:
        """
        Append values and optional timestamp columns from output.  Cached metadata (timestamp) is stored for debugging
        For maximum robustness a failed plugin reading should not stop the scan, detector point and scan continues.
        TODO: plugins must ensure consistant data sets but must not fail scan, add rete limited logging
        """
        self._require_open()

        for offset, item in enumerate(values or []):
            header = str(headers[offset]) if offset < len(headers) else None
            if isinstance(item, dict):
                scalar = item.get("value")
                timestamp = item.get("timestamp")
            else:
                scalar = item
                timestamp = None

            self.output_values.append(scalar)
            if self.include_timestamps and output_timestamps:
                self._append_output_timestamp(timestamp)

            if header is not None:
                self.current_values[header] = scalar
                if timestamp is not None:
                    self.current_values["TS-" + header] = timestamp

            completed_header = header or self._metadata_header(item)
            if completed_header is None:
                self.completed_values[f"col{self._value_count}"] = item
            else:
                self.completed_values[completed_header] = item
                if self.include_timestamps and isinstance(item, dict):
                    self.completed_values["TS-" + completed_header] = timestamp

            self.raw_values.append(item)
            self._value_count += 1

    def _append_detector_values_with_timestamps(
        self,
        headers: Tuple[str, ...],
        timestamp_headers: Tuple[str, ...],
        detector_values: List[Any],
    ) -> None:
        """Append aligned detector values when timestamp columns are enabled."""
        output_values = self.output_values
        current_values = self.current_values
        completed_values = self.completed_values
        raw_values = self.raw_values
        timestamp_output_indices = self._timestamp_output_indices

        for header, timestamp_header, item in zip(
            headers, timestamp_headers, detector_values
        ):
            is_metadata = isinstance(item, dict)
            if is_metadata:
                scalar = item.get("value")
                timestamp = item.get("timestamp")
            else:
                scalar = item
                timestamp = None

            output_values.append(scalar)
            timestamp_output_indices.append(len(output_values))
            output_values.append(timestamp)

            current_values[header] = scalar
            if timestamp is not None:
                current_values[timestamp_header] = timestamp

            completed_values[header] = item
            if is_metadata:
                completed_values[timestamp_header] = timestamp

            raw_values.append(item)

    def _append_detector_values_without_timestamps(
        self,
        headers: Tuple[str, ...],
        timestamp_headers: Tuple[str, ...],
        detector_values: List[Any],
    ) -> None:
        """ 
        Append aligned detector values when timestamp columns are disabled. 
        Timestamp data must be available in detector values!
        """
        output_values = self.output_values
        current_values = self.current_values
        completed_values = self.completed_values
        raw_values = self.raw_values

        for header, timestamp_header, item in zip(
            headers, timestamp_headers, detector_values
        ):
            is_metadata = isinstance(item, dict)
            if is_metadata:
                scalar = item.get("value")
                timestamp = item.get("timestamp")
            else:
                scalar = item
                timestamp = None

            output_values.append(scalar)

            current_values[header] = scalar
            if timestamp is not None:
                current_values[timestamp_header] = timestamp

            completed_values[header] = item
            raw_values.append(item)

    def _append_detector_values_fast(
        self,
        layout: _DetectorLayout,
        values: List[Any],
    ) -> None:
        """Single-pass detector append used by the production hot path."""
        detector_values = values or []
        headers = layout.headers
        timestamp_headers = layout.timestamp_headers

        if len(detector_values) != len(headers):
            message = (
                "Detector value count mismatch: "
                f"received {len(detector_values)} values for "
                f"{len(headers)} configured detector headers"
            )
            logger.error(message)
            raise ValueError(message)

        value_count = self._value_count
        if self.include_timestamps:
            self._append_detector_values_with_timestamps(
                headers, timestamp_headers, detector_values
            )
        else:
            self._append_detector_values_without_timestamps(
                headers, timestamp_headers, detector_values
            )
        self._value_count = value_count + len(detector_values)

    def append_detector_values(
        self,
        layout: _DetectorLayout,
        values: List[Any],
    ) -> None:
        """Append detector values using the production fast path."""
        self._require_open()
        self._append_detector_values_fast(layout, values)

    def matches(
        self,
        position: Any,
        values: List[Any],
        include_timestamps: bool,
    ) -> bool:
        """Return whether a legacy save call contains this frame's values."""
        if self._state != "open" or self.include_timestamps != include_timestamps:
            return False
        if len(self.raw_values) != len(values or []):
            return False
        if self.position is not position:
            try:
                if not bool(self.position == position):
                    return False
            except (TypeError, ValueError):
                return False
        return all(left is right for left, right in zip(self.raw_values, values or []))

    def build_output_row(
        self,
        provider_values: List[Any],
        line_timestamp: Any,
    ) -> Tuple[Any, ...]:
        self._require_open()
        return (
            self.position,
            *(provider_values or []),
            line_timestamp,
            *self.output_values,
        )

    def build_timestamp_indices(self, provider_count: int) -> FrozenSet[int]:
        """Return absolute row indices containing timestamps.

        The point timestamp follows Position plus provider columns. The other
        indices were recorded relative to ``output_values`` while the frame was
        assembled.
        """
        line_timestamp_index = 1 + int(provider_count)
        output_offset = line_timestamp_index + 1
        return frozenset(
            [line_timestamp_index]
            + [output_offset + index for index in self._timestamp_output_indices]
        )

    def current_snapshot(self) -> Dict[str, Any]:
        return dict(self.current_values)

    @property
    def is_open(self) -> bool:
        return self._state == "open"

    def finish(self) -> None:
        self._require_open()
        self._state = "finished"

    def abort(self) -> None:
        if self._state == "open":
            self._require_open()
            self._state = "aborted"
            logger.debug("Aborted point frame %d with %d values and %d output columns",
                self.index, self._value_count, len(self.output_values))
