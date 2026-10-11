# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, TextIO


class MonitorValueFormatter:
    """Normalize and format monitor values.

    The monitor API receives scan values as a list where each item is usually
    either a ``{"value": ..., "timestamp": ...}`` mapping or a raw scalar.
    This helper contains the common extraction and scalar formatting logic used
    by stdout and plotting monitors.
    """

    DEFAULT_FLOAT_FORMAT = ".12e"

    def __init__(
        self,
        *,
        float_format: str = DEFAULT_FLOAT_FORMAT,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.float_format = str(float_format or self.DEFAULT_FLOAT_FORMAT)
        self.logger = logger or logging.getLogger(__name__)

    def extract_value(self, item: Any) -> Any:
        if item is None:
            self.logger.debug("Encountered None monitor item; writing empty value")
            return None

        if isinstance(item, dict):
            if "value" not in item:
                self.logger.debug( "Monitor item dict has no 'value' key; item keys=%r", list(item.keys()))
            return item.get("value")

        # self.logger.debug("Monitor item is not a dict; using raw item value: %r", item)
        return item

    def extract_timestamp(self, item: Any) -> Any:
        if not isinstance(item, dict):
            return None
        return item.get("timestamp")

    def format_value(self, value: Any) -> str:
        if value is None:
            return ""

        try:
            return format(float(value), self.float_format)
        except (TypeError, ValueError):
            self.logger.debug(
                "Value is not float-convertible; writing as string: %r",
                value,
            )
            return str(value)

    def format_timestamp(self, ts: Any) -> str:
        if ts is None:
            return ""

        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            self.logger.debug(
                "Timestamp is not POSIX-float-convertible; writing as string: %r",
                ts,
            )
            return str(ts)

class RowWriter(Protocol):
    def writerow(self, row: Iterable[Any]) -> Any:
        """Write one row."""

class MonitorRowFormatter:
    """ 
    Row writer for monitor stdout streams.

    Text monitor and queue plotter monitor produce exactly the same stdout stream.
    """

    SUPPORTED_FORMATS = frozenset({"tsv", "csv", "json"})
    DEFAULT_FORMAT = "tsv"

    def __init__(
        self,
        parameters: Optional[Dict[str, Any]] = None,
        *,
        out: Optional[TextIO] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.parameters = parameters or {}
        self.logger = logger or logging.getLogger(__name__)
        self.format = str(self.parameters.get("format", self.DEFAULT_FORMAT)).lower()
        self.include_timestamps = bool(self.parameters.get("include_timestamps", False))
        self.include_header = bool(self.parameters.get("include_header", True))
        self.value_formatter = MonitorValueFormatter(
            float_format=str(
                self.parameters.get(
                    "float_format",
                    MonitorValueFormatter.DEFAULT_FLOAT_FORMAT,
                )
            ),
            logger=self.logger,
        )
        self.signal_names: List[str] = []
        self._writer: Optional[RowWriter] = None
        self._out = out or sys.stdout
        self._rows_written = 0

    @property
    def rows_written(self) -> int:
        return self._rows_written

    def start(self, signal_names: Iterable[str]) -> None:
        self.signal_names = list(signal_names)
        self._rows_written = 0

        if self.format not in self.SUPPORTED_FORMATS:
            self.logger.warning(
                "Unsupported monitor format requested: %r. Falling back to default format %r. Supported formats are: %s",
                self.format,
                self.DEFAULT_FORMAT,
                ", ".join(sorted(self.SUPPORTED_FORMATS)),
            )
            self.format = self.DEFAULT_FORMAT

        if self.format in ("tsv", "csv"):
            delimiter = "\t" if self.format == "tsv" else ","
            writer = csv.writer(self._out, delimiter=delimiter, lineterminator="\n")
            self._writer = writer
            if self.include_header:
                writer.writerow(self.headers())
                self._out.flush()
        else:
            self._writer = None

    def write(self, vals: List[Any]) -> None:
        if len(vals) != len(self.signal_names):
            self.logger.debug(
                "Value/header count mismatch: values=%d signals=%d. Using available order and fallback names for extra values.",
                len(vals),
                len(self.signal_names),
            )

        if self.format in ("tsv", "csv"):
            if self._writer is None:
                self.logger.error("Monitor row writer was not initialized before update")
                return
            self._writer.writerow(self.row(vals))
            self._out.flush()
            self._rows_written += 1
            return

        if self.format == "json":
            print(
                json.dumps(self.json_obj(vals), ensure_ascii=False, default=str),
                file=self._out,
                flush=True,
            )
            self._rows_written += 1
            return

        self.logger.error("Unsupported monitor format at update time: %s", self.format)

    def close(self) -> None:
        try:
            self._out.flush()
        except Exception:
            self.logger.info("Failed to flush monitor output stream", exc_info=True)

    def headers(self) -> List[str]:
        headers: List[str] = []
        for name in self.signal_names:
            headers.append(name)
            if self.include_timestamps:
                headers.append(f"TS-ISO8601-{name}")
        return headers

    def row(self, vals: List[Any]) -> List[str]:
        row: List[str] = []
        for item in vals:
            row.append(
                self.value_formatter.format_value(
                    self.value_formatter.extract_value(item)
                )
            )
            if self.include_timestamps:
                row.append(
                    self.value_formatter.format_timestamp(
                        self.value_formatter.extract_timestamp(item)
                    )
                )
        return row

    def json_obj(self, vals: List[Any]) -> Dict[str, Any]:
        obj: Dict[str, Any] = {}
        for idx, item in enumerate(vals):
            name = self.signal_names[idx] if idx < len(self.signal_names) else f"col{idx}"
            obj[name] = self.value_formatter.extract_value(item)
            if self.include_timestamps:
                obj[f"TS-ISO8601-{name}"] = self.value_formatter.format_timestamp(
                    self.value_formatter.extract_timestamp(item)
                )
        return obj
