#!/usr/bin/env python3
"""guard_budget levels and the pulse_throttle gate (16 Oct 2026 weekly-cap incident)."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import guardian
import pulse_throttle

WEEK = 604800


def iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Levels(unittest.TestCase):
    def level(self, weekly, five=0, left=0.02):
        now = 1_800_000_000
        with patch.object(guardian, "NOW", now):
            i, _ = guardian._throttle_level(weekly, five, iso(now + left * WEEK))
        return guardian.THROTTLE_LEVELS[i][1]

    def test_thresholds(self):
        self.assertEqual(self.level(10), "normal")
        self.assertEqual(self.level(72), "warn")
        self.assertEqual(self.level(81), "throttle")
        self.assertEqual(self.level(90), "minimal")
        self.assertEqual(self.level(96), "stop")

    def test_pace_throttles_before_80(self):
        # 60% used with 4 of 7 days left -> projected 140% -> throttle now, not at 80%
        # 50% used with 4 of 7 days left -> projected 117% -> throttle; 45% -> 105% -> warn; 20% -> normal
        self.assertEqual(self.level(50, left=4 / 7), "throttle")
        self.assertEqual(self.level(45, left=4 / 7), "warn")
        self.assertEqual(self.level(20, left=4 / 7), "normal")

    def test_five_hour_window(self):
        self.assertEqual(self.level(20, five=92), "throttle")


class Gate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(f"{self.d}/estate")
        json.dump({"lines": [{"id": "senior", "heartbeat": f"{self.d}/HB"}, {"id": "guardian", "heartbeat": f"{self.d}/GHB"}]},
                  open(f"{self.d}/estate/lines.json", "w"))
        p = patch.object(pulse_throttle, "ART", self.d)
        p.start()
        self.addCleanup(p.stop)

    def throttle(self, level, cadence=60, keep=None):
        json.dump({"level": level, "cadence_min": cadence, "keep": keep, "weekly_pct": 85, "reading_epoch": time.time(),
                   "resets_epoch": time.time() + 86400}, open(f"{self.d}/estate/PULSE-THROTTLE.json", "w"))

    def hb(self, minutes_ago, name="HB"):
        open(f"{self.d}/{name}", "w").write(str(int(time.time() - minutes_ago * 60)))

    def test_fails_open(self):
        self.assertEqual(pulse_throttle.decide("senior"), "RUN")

    def test_throttle_by_heartbeat_age(self):
        self.throttle("throttle")
        self.hb(20)
        self.assertTrue(pulse_throttle.decide("senior").startswith("SKIP"))
        self.hb(58)
        self.assertEqual(pulse_throttle.decide("senior"), "RUN")

    def test_minimal_and_stop_keep_guardian(self):
        self.throttle("minimal", 120, ["guardian", "syfon"])
        self.hb(200)
        self.hb(200, "GHB")
        self.assertTrue(pulse_throttle.decide("senior").startswith("SKIP minimal"))
        self.assertEqual(pulse_throttle.decide("guardian"), "RUN")
        self.throttle("stop", 120, ["guardian", "syfon"])
        self.assertTrue(pulse_throttle.decide("syfon").startswith("SKIP stop"))
        self.assertEqual(pulse_throttle.decide("guardian"), "RUN")

    def test_stale_reading_or_past_reset_runs(self):
        self.hb(5)
        json.dump({"level": "stop", "cadence_min": 120, "reading_epoch": time.time() - 4 * 3600, "resets_epoch": time.time() + 86400},
                  open(f"{self.d}/estate/PULSE-THROTTLE.json", "w"))
        self.assertEqual(pulse_throttle.decide("senior"), "RUN")
        json.dump({"level": "stop", "cadence_min": 120, "reading_epoch": time.time(), "resets_epoch": time.time() - 60},
                  open(f"{self.d}/estate/PULSE-THROTTLE.json", "w"))
        self.assertEqual(pulse_throttle.decide("senior"), "RUN")

    def test_future_heartbeat_runs(self):
        self.throttle("throttle")
        self.hb(-600)
        self.assertEqual(pulse_throttle.decide("senior"), "RUN")

    def test_stale_file_runs(self):
        self.throttle("stop")
        old = time.time() - 4 * 3600
        os.utime(f"{self.d}/estate/PULSE-THROTTLE.json", (old, old))
        self.assertEqual(pulse_throttle.decide("senior"), "RUN")


if __name__ == "__main__":
    unittest.main()
