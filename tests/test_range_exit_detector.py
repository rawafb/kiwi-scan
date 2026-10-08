# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import unittest

from kiwi_scan.scan.range_exit_detector import RangeExitDetector


class TestRangeExitDetector(unittest.TestCase):
    """ TODO: implement the start==stop special case handled by scan classes individually """
    def test_stops_after_three_consecutive_samples_past_end(self):
        # start < stop
        detector = RangeExitDetector(0.0, 10.0, out_threshold=3)
        
        self.assertFalse(detector.update(-1.0))
        self.assertFalse(detector.update(5.0))
        self.assertFalse(detector.update(10.1))
        self.assertFalse(detector.update(10.2))
        self.assertTrue(detector.update(10.3))
        # start > stop
        detector = RangeExitDetector(10.0, 0.0, out_threshold=0)
        
        self.assertFalse(detector.update(10.1))
        self.assertFalse(detector.update(5.0))
        self.assertTrue(detector.update(-1.0))
        # TODO: start == stop

if __name__ == "__main__":
    unittest.main(verbosity=2)
