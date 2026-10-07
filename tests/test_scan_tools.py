# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin fuer Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

import sys
import tempfile
import unittest
from pathlib import Path

from kiwi_scan import test_support

# tools.py imports BaseScan, which loads the EPICS wrapper.
if "epics" not in sys.modules:
    sys.modules["epics"] = test_support.make_fake_epics_module()

from kiwi_scan.scan.tools import load_scan_configs

GOOD_YAML = """\
actuators: {}
detector_pvs: ["SIM:DET"]
"""


class TestLoadScanConfigs(unittest.TestCase):
    def test_bad_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "good.yaml").write_text(GOOD_YAML)
            (d / "empty.yaml").write_text("")
            (d / "broken.yaml").write_text("a: [1\n")
            (d / "list.yaml").write_text("- 1\n- 2\n")

            with self.assertLogs("kiwi_scan.scan.tools", level="WARNING") as logs:
                configs = load_scan_configs(tmp, None)

        self.assertEqual(sorted(configs), ["good"])
        self.assertEqual(configs["good"].detector_pvs, ["SIM:DET"])
        self.assertEqual(len(logs.records), 3)

    def test_missing_dir_still_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_scan_configs("/nonexistent/kiwi-scan-config", None)


if __name__ == "__main__":
    unittest.main()
