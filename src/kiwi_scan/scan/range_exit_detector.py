# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import logging

logger = logging.getLogger(__name__)

class RangeExitDetector:
    def __init__(self, start, stop, eps=0.0, out_threshold=3):
        self.start = start
        self.stop = stop
        self.eps = eps
        self.out_threshold = out_threshold

        self.entered = False
        self.out_counter = 0

        # determine scan direction
        self.forward = stop > start

    def _in_range(self, pos, start, stop):
        """
        TODO: tolerance in range check. E.g. useful for poll/para scan types for noice, FE etc.
        """
        if pos is None or start is None or stop is None:
            logger.debug(f"Could not detect range: {start}:{pos}:{stop}")
            return False

        return start <= pos <= stop if start <= stop else stop <= pos <= start

    def in_range(self, pos):
        """
        TODO: integrate poll scan type checks for the start == stop case
        Return True when ``pos`` is inside the configured scan range.
        """
        return self._in_range(pos, self.start, self.stop)

    def prime(self, pos):
        """
        Seed the detector with the current position before the DAQ loop.

        This is useful after a backlash/overshoot preparation move where the
        actuator may already have entered the scan range before the first call
        to ``update()``. Returns True when the detector is now in the entered
        state.
        """
        if self.in_range(pos):
            self.entered = True
            self.out_counter = 0
            logger.debug("[RangeExitDetector] Primed inside range at pos=%s", pos)
        else:
            logger.debug("[RangeExitDetector] Prime position outside range: pos=%s", pos)
        return self.entered

    def reset(self):
        """Clear the entered and exit-counter state for detector reuse."""
        self.entered = False
        self.out_counter = 0

    def _past_end(self, pos):
        if pos is None:
            logger.debug("Could not detect end crossing: pos is None")
            return False

        if self.forward:
            return pos > self.stop + self.eps
        else:
            return pos < self.stop - self.eps

    def update(self, pos):
        """
        Returns True if scan should stop.
        """

        # 1. Detect entering the scan range
        if self.in_range(pos):
            self.entered = True
            self.out_counter = 0
            return False

        # 2. Only consider exit AFTER we were inside
        if not self.entered:
            return False

        # 3. Direction-aware exit condition
        if self._past_end(pos):
            self.out_counter += 1
            logger.debug( "[RangeExitDetector] EXIT candidate pos=%s (counter=%d/%d)",
                pos, self.out_counter, self.out_threshold)
            if self.out_counter >= self.out_threshold:
                return True
        else:
            # still near boundary -> reset (handles oscillation)
            self.out_counter = 0

        return False
