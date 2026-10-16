# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

"""Read configurable scalar EMC getters in one serial background job."""

from __future__ import annotations

import math
import socket
from time import monotonic, time
from typing import Any, Callable, Dict, List, Optional, Tuple

from kiwi_scan.plugin.async_base import AsyncScanPlugin
from kiwi_scan.plugin.context import ScanPluginContext
from kiwi_scan.plugin.registry import register_plugin

_REPLY_TERMINATORS: Dict[str, Optional[bytes]] = {
    "none": None,
    "lf": b"\n",
    "crlf": b"\r\n",
    "cr": b"\r",
}
_CONVERTERS: Dict[str, Callable[[bytes], Any]] = {"float": float, "int": int}


@register_plugin("hzb.emc_getter")
class EmcGetterPlugin(AsyncScanPlugin):
    """Publish the latest complete group of EMC values without waiting in the scan.

    One plugin owns one persistent socket and exactly one worker. A reply must
    finish before the next command is sent. Configure getter commands only;
    command names are deliberately not hard-coded, so new getters need no code.
    """

    def __init__(
        self,
        name: str,
        parameters: Optional[Dict[str, Any]] = None,
        scan: Optional[ScanPluginContext] = None,
    ) -> None:
        params = dict(parameters or {})
        if params.get("max_workers", 1) != 1:
            raise ValueError("EMC getters require max_workers=1 (serial communication)")
        params["max_workers"] = 1
        self._address = self._parse_address(params)
        self._timeout = float(params.get("timeout_s", 0.5))
        self._min_interval = float(params.get("min_interval_s", 0.0))
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            raise ValueError("timeout_s must be finite and positive")
        if not math.isfinite(self._min_interval) or self._min_interval < 0:
            raise ValueError("min_interval_s must be finite and non-negative")
        record_times = params.get("record_times", False)
        if not isinstance(record_times, bool):
            raise TypeError("record_times must be a boolean")
        self._reply_terminator = self._parse_terminator(params.get("reply_terminator", "none"))
        prepared, self._headers = self._parse_getters(params.get("getters"), record_times)

        # These exist before super(): the async base asks for default values.
        self._getters = tuple(prepared)
        self._defaults = [float("nan")] * len(self._headers)
        self._socket: Optional[socket.socket] = None
        self._next_request_at = 0.0
        # Select the loop once; the untimed loop never calls the wall clock.
        self._read_group = self._read_timed if record_times else self._read_values
        super().__init__(name, params, scan)

    @staticmethod
    def _parse_address(params: Dict[str, Any]) -> Tuple[str, int]:
        host = params.get("host", "localhost")
        port = params.get("port", 5001)
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        return host, port

    @staticmethod
    def _parse_terminator(reply_terminator: Any) -> Optional[bytes]:
        if not isinstance(reply_terminator, str):
            raise TypeError("reply_terminator must be one of: none, lf, crlf, cr")
        terminator_key = reply_terminator.lower()
        if terminator_key not in _REPLY_TERMINATORS:
            raise ValueError("reply_terminator must be one of: none, lf, crlf, cr")
        return _REPLY_TERMINATORS[terminator_key]

    @staticmethod
    def _parse_getters(
        getters: Any, record_times: bool
    ) -> Tuple[List[Tuple[bytes, Callable[[bytes], Any]]], List[str]]:
        """Validate getter mappings; return (encoded command, converter) pairs and headers."""
        if not isinstance(getters, list) or not getters:
            raise ValueError("getters must be a non-empty list")
        prepared: List[Tuple[bytes, Callable[[bytes], Any]]] = []
        headers: List[str] = []
        for getter in getters:
            if not isinstance(getter, dict):
                raise TypeError("each getter must be a mapping")
            command = getter.get("command")
            column = getter.get("column")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("each getter needs a non-empty command")
            if any(ord(char) < 32 or ord(char) > 126 for char in command):
                raise ValueError("commands must contain one printable ASCII line")
            if not isinstance(column, str) or not column or any(c.isspace() for c in column):
                raise ValueError("each getter needs a non-empty column without whitespace")
            value_type = getter.get("value_type", "float")
            if value_type not in _CONVERTERS:
                raise ValueError("value_type must be 'float' or 'int'")
            prepared.append(((command.strip() + "\n").encode("ascii"), _CONVERTERS[value_type]))
            headers.append(column)
            if record_times:
                headers.extend((column + "_before", column + "_after"))
        if len(set(headers)) != len(headers):
            raise ValueError("getter columns and generated timing columns must be unique")
        return prepared, headers

    def get_headers(self, timestamps: bool = False) -> List[str]:
        """Return cached value headers; communication times are explicit columns.

        Treat this cached list as read-only. The pipeline requests these headers
        at every point. Its generic timestamp flag does not change our columns.
        """
        return self._headers

    def get_async_default_values(self) -> List[Any]:
        return list(self._defaults)

    def build_data_snapshot(self, idx: int, pos: Any) -> Optional[int]:
        """Scan thread: no position copy, network operation, or wait."""
        if self._min_interval and monotonic() < self._next_request_at:
            return None
        return idx

    def _connect(self) -> socket.socket:
        """Worker only: connect lazily, then reuse the same socket."""
        if self._socket is None:
            sock = socket.create_connection(self._address, timeout=self._timeout)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                sock.close()
                raise
            self._socket = sock
        return self._socket

    def _disconnect(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def _exchange(self, sock: socket.socket, command: bytes) -> bytes:
        """Send ONE command and receive its complete reply before returning.

        ``reply_terminator=none`` preserves the legacy EMC behavior and treats
        one TCP receive as one small scalar reply. Framed modes read until the
        configured LF, CRLF or CR terminator, including fragmented replies.
        """
        deadline = monotonic() + self._timeout
        sock.settimeout(self._timeout)
        try:
            sock.sendall(command)
            terminator = self._reply_terminator
            if terminator is None:
                reply = sock.recv(4096)
                if not reply:
                    raise ConnectionError("EMC closed the connection before a reply")
                if len(reply) >= 4096:
                    raise ValueError("EMC reply exceeds 4096 bytes")
                return reply

            reply = bytearray()
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise socket.timeout("EMC exchange deadline exceeded")
                sock.settimeout(remaining)
                chunk = sock.recv(4096 - len(reply))
                if not chunk:
                    raise ConnectionError("EMC closed the connection before a complete reply")
                reply.extend(chunk)
                end = reply.find(terminator)
                if end >= 0:
                    if end != len(reply) - len(terminator):
                        raise ValueError("unexpected data after EMC reply")
                    return bytes(reply)
                if len(reply) >= 4096:
                    raise ValueError("EMC reply exceeds 4096 bytes")
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"EMC command {command.strip()!r}: {exc}") from exc

    @staticmethod
    def _parse(reply: bytes, convert: Callable[[bytes], Any], command: bytes) -> Any:
        fields = reply.split()
        if len(fields) != 2 or fields[0] != b"t":
            raise ValueError(f"EMC command {command.strip()!r}: invalid/failed reply {reply!r}")
        try:
            value = convert(fields[1])
        except ValueError as exc:
            raise ValueError(f"EMC command {command.strip()!r}: invalid value {fields[1]!r}") from exc
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"EMC command {command.strip()!r}: non-finite value")
        return value

    def _read_values(self, sock: socket.socket) -> List[Any]:
        values = []
        for command, convert in self._getters:
            if self._closed:
                return self.get_async_default_values()
            reply = self._exchange(sock, command)
            values.append(self._parse(reply, convert, command))
        return values

    def _read_timed(self, sock: socket.socket) -> List[Any]:
        values = []
        for command, convert in self._getters:
            if self._closed:
                return self.get_async_default_values()
            before = time()
            reply = self._exchange(sock, command)
            after = time()
            values.extend((self._parse(reply, convert, command), before, after))
        return values

    def process_data_snapshot(self, data_snapshot: Any) -> List[Any]:
        """Worker: connect and read one entire group in configuration order."""
        if self._closed:
            return self.get_async_default_values()
        if self._min_interval:
            # Rate limiting starts only for jobs actually run, not busy points.
            self._next_request_at = monotonic() + self._min_interval
        try:
            values = self._read_group(self._connect())
        except (OSError, RuntimeError, ValueError):
            # Never reuse a connection after an incomplete or invalid exchange.
            self._disconnect()
            raise  # The async base publishes defaults and records the error.
        # Background hot path: at most one additional DEBUG message per group.
        self.logger.debug("EMC group complete: idx=%s getters=%d", data_snapshot, len(self._getters))
        return values

    def close(self) -> None:
        """Stop submissions immediately; let active I/O finish before closing.

        No join or network wait runs in the scan thread. The active exchange
        has a deadline, and the worker checks _closed before the next command.
        """
        super().close()
        with self._lock:
            job = self._async_job
        if job is None:
            self._disconnect()
        else:
            # Runs immediately for a finished/cancelled job, otherwise when
            # the worker finishes. No worker can still be using the socket then.
            job.add_done_callback(lambda _job: self._disconnect())
