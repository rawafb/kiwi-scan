# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

from __future__ import annotations

from typing import Any, Dict, List, Protocol


class DataColumnProvider(Protocol):
    """
    TODO: Check index and fill missing data cols from buggy column providers with "None/nan"
          This could be integrated in this common module
    Protocol for objects that add columns to scan output rows.

    Collect values continuously, e.g. from subscription callbacks. 
    Return the latest snapshot with get_values().
    """

    def get_headers(self, include_timestamps: bool = False) -> List[str]:
        """Return column headers in the order produced by ``get_values``."""
        ...

    def get_values(self) -> List[Any]:
        """Return the current column values in header order."""
        ...

    def update_last_point(
        self,
        last_point: Dict[str, Any],
        include_timestamps: bool = False,
    ) -> None:
        """Add provider values to the in-memory ``get_value`` cache."""
        ...

    def reset_window(self) -> None:
        """Start a new data-acquisition window, if the provider is windowed."""
        ...
