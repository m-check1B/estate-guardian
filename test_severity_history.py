#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import guardian


class SeverityHistory(unittest.TestCase):
    def setUp(self):
        self.finding = SimpleNamespace(key="risk-disk-test", p0=True, owner="senior")
        self.state = {"first": 100, "opened": 100, "class": "infra", "history_first": 100}
        self.events = []
        self.hist = patch.object(guardian, "hist", side_effect=self.events.append)
        self.hist.start()
        self.addCleanup(self.hist.stop)

    def test_first_open_records_p0_in_history_and_state(self):
        self.state.pop("opened")
        with patch.object(guardian, "NOW", 200):
            guardian.sync_severity_history(self.finding, self.state, {"class": "infra"})

        self.assertTrue(self.state["p0"])
        self.assertEqual(self.state["history_first"], 100)
        self.assertEqual(self.events, [{
            "ev": "open", "key": "risk-disk-test", "class": "infra",
            "p0": True, "owner": "senior", "first": 100,
        }])

    def test_demotion_closes_p0_interval_then_opens_non_p0(self):
        self.finding.p0 = False
        with patch.object(guardian, "NOW", 300):
            guardian.sync_severity_history(self.finding, self.state, {"class": "infra"})

        self.assertEqual([event["ev"] for event in self.events], ["close", "open"])
        self.assertEqual(self.events[0], {
            "ev": "close", "key": "risk-disk-test", "class": "infra",
            "first": 100, "last": 300,
        })
        self.assertEqual(self.events[1], {
            "ev": "open", "key": "risk-disk-test", "class": "infra",
            "p0": False, "owner": "senior", "first": 300,
        })
        self.assertFalse(self.state["p0"])
        self.assertEqual(self.state["history_first"], 300)

    def test_promotion_is_symmetric_and_repeated_state_does_not_split(self):
        self.state["p0"] = False
        with patch.object(guardian, "NOW", 400):
            guardian.sync_severity_history(self.finding, self.state, {"class": "infra"})
        self.assertEqual([event["ev"] for event in self.events], ["close", "open"])
        self.assertTrue(self.events[1]["p0"])
        self.assertEqual(self.events[1]["first"], 400)
        self.events.clear()
        with patch.object(guardian, "NOW", 500):
            guardian.sync_severity_history(self.finding, self.state, {"class": "infra"})
        self.assertEqual(self.events, [])

    def test_dry_run_does_not_write_history(self):
        with tempfile.TemporaryDirectory() as temp:
            history = os.path.join(temp, "history.jsonl")
            with patch.object(guardian, "HISTORY", history), patch.object(guardian, "NOW", 600), \
                    patch.object(sys, "argv", ["guardian.py", "--dry-run"]):
                guardian.hist({"ev": "open", "key": self.finding.key})
            self.assertFalse(os.path.exists(history))


if __name__ == "__main__":
    unittest.main()
