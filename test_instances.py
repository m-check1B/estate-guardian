#!/usr/bin/env python3
"""guard_instances: deployed instance health, latency, version drift and PROD-STATUS.md (offline, fake HTTP)."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import guardian


class Instances(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.reg = f"{self.d}/INSTANCES.json"
        self.md = f"{self.d}/estate/PROD-STATUS.md"
        self.http = {}  # url -> (status, body dict|bytes, ms) or an Exception
        for p in (patch.object(guardian, "INSTANCES_FILE", self.reg), patch.object(guardian, "PROD_STATUS_MD", self.md),
                  patch.object(guardian, "_inst_fetch", self.fake_fetch)):
            p.start()
            self.addCleanup(p.stop)

    def fake_fetch(self, url, timeout=20):
        self.assertEqual(timeout, 20)
        r = self.http[url]
        if isinstance(r, Exception):
            raise r
        status, body, ms = r
        return status, body if isinstance(body, bytes) else json.dumps(body).encode(), ms

    def registry(self, *insts):
        json.dump(list(insts), open(self.reg, "w"))

    def inst(self, name, kind="prod", customer=None, expect=None, field="product_version"):
        return {"name": name, "url_health": f"https://{guardian._inst_slug(name)}.example.com/health", "owner": "senior",
                "customer": customer, "kind": kind, "expect_version_from": expect, "version_field": field}

    def expected(self, version, name="expected"):
        p = f"{self.d}/{name}.json"
        json.dump({"version": version}, open(p, "w"))
        return p

    def run_guard(self):
        out = guardian.guard_instances()
        return {f.key: f for f in out}

    def table_rows(self):
        return [l for l in open(self.md).read().splitlines() if l.startswith("| ") and not l.startswith("| name")]

    def test_up(self):
        exp = self.expected("3.20.1")
        self.registry(self.inst("Jack", expect=exp))
        self.http["https://jack.example.com/health"] = (200, {"status": "ok", "product_version": "3.20.1"}, 120)
        self.assertEqual(self.run_guard(), {})
        rows = self.table_rows()
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(cells[:7], ["Jack", "prod", "—", "200", "120", "3.20.1", "3.20.1"])
        self.assertTrue(cells[7])  # checked at
        self.assertTrue(open(self.md).read().splitlines()[2].startswith("GREEN: 1/1"))

    def test_down_prod_customer_p0_staging_amber(self):
        self.registry(self.inst("Prod"), self.inst("Cust", kind="customer", customer="Acme"), self.inst("Stage", kind="staging"))
        self.http["https://prod.example.com/health"] = (503, b"", 40)
        self.http["https://cust.example.com/health"] = OSError("connection refused")
        self.http["https://stage.example.com/health"] = (500, b"", 40)
        got = self.run_guard()
        self.assertEqual(set(got), {"inst-down-prod", "inst-down-cust", "inst-down-stage"})
        self.assertTrue(got["inst-down-prod"].p0 and not got["inst-down-prod"].amber)
        self.assertEqual(got["inst-down-prod"].grace, 9)
        self.assertIn("HTTP 503", got["inst-down-prod"].detail)
        self.assertTrue(got["inst-down-cust"].p0)
        self.assertIn("OSError", got["inst-down-cust"].detail)
        self.assertIn("Acme", got["inst-down-cust"].title)
        self.assertTrue(got["inst-down-stage"].amber and not got["inst-down-stage"].p0)
        md = open(self.md).read()
        self.assertIn("RED: 0/3", md)
        self.assertIn("| Cust | customer | Acme | ERR |", md)

    def test_redirect_counts_as_up(self):
        self.registry(self.inst("Web"))
        self.http["https://web.example.com/health"] = (302, b"", 30)
        self.assertEqual(self.run_guard(), {})

    def test_slow(self):
        self.registry(self.inst("Slowpoke"))
        self.http["https://slowpoke.example.com/health"] = (200, b"<html>ok</html>", 4200)
        got = self.run_guard()
        self.assertEqual(set(got), {"inst-slow-slowpoke"})
        self.assertTrue(got["inst-slow-slowpoke"].amber and not got["inst-slow-slowpoke"].p0)
        self.assertIn("| Slowpoke | prod | — | 200 | 4200 | — |", open(self.md).read())

    def test_drift(self):
        exp = self.expected("3.20.1")
        self.registry(self.inst("Acme Jack", kind="customer", customer="Acme", expect=exp),
                      self.inst("Sha", expect=self.expected("abc1234", "sha"), field="missing_field"))
        self.http["https://acme-jack.example.com/health"] = (200, {"product_version": "3.19.0"}, 100)
        # version_field absent -> release_source_sha; a full sha matches the expected short sha
        self.http["https://sha.example.com/health"] = (200, {"release_source_sha": "abc1234def5678"}, 100)
        got = self.run_guard()
        self.assertEqual(set(got), {"inst-drift-acme-jack"})
        f = got["inst-drift-acme-jack"]
        self.assertTrue(f.amber and not f.p0)
        self.assertEqual(f.grace, 60)
        self.assertIn("3.19.0", f.title)
        self.assertIn("3.20.1", f.title)

    def test_missing_registry(self):
        got = self.run_guard()
        self.assertEqual(set(got), {"inst-registry"})
        self.assertTrue(got["inst-registry"].amber)
        self.assertIn("does not exist", got["inst-registry"].detail)
        self.assertIn("Registry problems:", open(self.md).read())

    def test_bad_registry_never_crashes(self):
        open(self.reg, "w").write("{not json")
        self.assertEqual(set(self.run_guard()), {"inst-registry"})
        self.registry({"name": "x"}, self.inst("Ok"), self.inst("Bad", kind="prod-ish"))
        self.http["https://ok.example.com/health"] = (200, b"", 10)
        got = self.run_guard()
        self.assertEqual(set(got), {"inst-registry"})
        self.assertIn("entry 0", got["inst-registry"].detail)
        self.assertIn("entry 2", got["inst-registry"].detail)
        self.assertEqual(len(self.table_rows()), 1)  # the valid instance is still checked

    def test_unreadable_expected_file(self):
        self.registry(self.inst("Jack", expect=f"{self.d}/nope.json"))
        self.http["https://jack.example.com/health"] = (200, {"product_version": "1"}, 10)
        got = self.run_guard()
        self.assertEqual(set(got), {"inst-registry"})
        self.assertIn("expect_version_from", got["inst-registry"].detail)

    def test_registered(self):
        self.assertIs(guardian.GUARDS["instances"], guardian.guard_instances)

    def test_example_registry_is_valid(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with patch.object(guardian, "INSTANCES_FILE", f"{here}/estate.instances.example.json"):
            good, bad = guardian._inst_load()
        self.assertEqual(bad, [])
        self.assertEqual({i["kind"] for i in good}, {"prod", "customer", "staging"})


if __name__ == "__main__":
    unittest.main()
