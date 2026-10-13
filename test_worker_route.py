#!/usr/bin/env python3
"""Offline tests for the multi-lab worker router: lanes, on/off switches, auto-ON, fallbacks, NEVER labs, vault
pinning, readiness and the cross-vendor spread. No network, no Keychain: run `python3 -m unittest test_worker_route`."""
import datetime as dt, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["ROUTER_NO_JEV"] = "1"


class Router(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.labs = json.load(open(os.path.join(HERE, "labs.example.json")))
        self.ready, self.usage, self.log = {}, {}, []
        import worker_route
        self.wr = worker_route

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name, doc):
        p = os.path.join(self.tmp.name, name)
        if isinstance(doc, list):
            open(p, "w").write("".join(json.dumps(d) + "\n" for d in doc))
        else:
            json.dump(doc, open(p, "w"))
        return p

    def route(self, text, name="BRIEF-x.md", title=""):
        wr = self.wr
        wr.LABS_FILE = self.path("labs.json", self.labs)
        wr.READINESS = self.path("ready.json", self.ready)
        wr.USAGE = self.path("usage.json", self.usage)
        wr.ROUTING_LOG = self.path("routing.jsonl", self.log)
        brief = self.path(name, {})
        open(brief, "w").write(text)
        return wr.route(brief, title)

    def off(self, lab, reason="usage", until=None):
        self.labs["labs"][lab].update({"switch": "OFF", "reason": reason})
        if until:
            self.labs["labs"][lab]["until"] = until

    # lanes
    def test_lanes(self):
        cases = [
            ("UI prove: click through the onboarding and take screenshots.", "BRIEF-ui.md", "clicker", "luna"),
            ("Rotate the Linear token and store it via nasvault.", "VAULT-BRIEF-linear.md", "vault", "luna"),
            ("Triage and label 200 inbox items, dedup duplicates.", "BRIEF-triage.md", "sort", "haiku"),
            ("Root cause the production incident; propose an architecture change.", "BRIEF-inc.md", "judgement", "opus"),
            ("Write landing page copy and pricing positioning.", "BRIEF-mkt.md", "marketing", "opus"),
            ("Fix the failing lint and rebase on master.", "BRIEF-lint.md", "everyday", "sonnet"),
        ]
        for text, name, lane, lab in cases:
            with self.subTest(name=name):
                d = self.route(text, name)
                self.assertEqual((d["lane"], d["model_lab"]), (lane, lab), d)

    def test_unclear_brief_defaults_to_judgement(self):
        d = self.route("Do the thing we talked about.")
        self.assertEqual((d["lane"], d["model_lab"], d["how"]), ("judgement", "opus", "default"))

    # switches and fallbacks
    def test_usage_off_falls_back_in_declared_order(self):
        future = (dt.datetime.utcnow() + dt.timedelta(hours=3)).isoformat()
        self.off("opus", until=future)
        d = self.route("Security review and threat model of the auth design.", "BRIEF-sec.md")
        self.assertEqual(d["model_lab"], "sonnet")  # opus fallback: [sonnet, luna]
        self.assertTrue(d["fallback_from"].startswith("opus (OFF/usage"))
        self.off("sonnet", until=future)
        self.assertEqual(self.route("Security review of the auth design.", "BRIEF-sec.md")["model_lab"], "luna")

    def test_usage_off_auto_on_after_reset(self):
        self.off("opus", until=(dt.datetime.utcnow() - dt.timedelta(minutes=5)).isoformat())
        d = self.route("Root cause the incident.", "BRIEF-inc.md")
        self.assertEqual((d["model_lab"], d["fallback_from"]), ("opus", None))

    def test_founder_off_until_date(self):
        self.off("opus", "founder", until=(dt.date.today() + dt.timedelta(days=2)).isoformat())
        self.assertEqual(self.route("Root cause the incident.", "BRIEF-inc.md")["model_lab"], "sonnet")
        self.off("opus", "founder", until=(dt.date.today() - dt.timedelta(days=1)).isoformat())
        self.assertEqual(self.route("Root cause the incident.", "BRIEF-inc.md")["model_lab"], "opus")

    def test_founder_off_without_date_stays_off(self):
        self.off("haiku", "founder")
        d = self.route("Triage and label the inbox items.", "BRIEF-triage.md")
        self.assertEqual(d["model_lab"], "cursor")  # haiku fallback: [cursor, luna-medium]

    def test_never_labs_are_never_chosen(self):
        for lab in self.labs["labs"]:
            if lab not in ("astra", "fable"):
                self.off(lab, "founder")
        for text, name in [("Root cause the incident.", "BRIEF-inc.md"), ("Fix the lint.", "BRIEF-lint.md")]:
            d = self.route(text, name)
            self.assertTrue(d.get("blocked"), d)
            self.assertNotIn(d.get("model_lab"), ("astra", "fable"))

    def test_vault_never_falls_back(self):
        self.off("luna", "founder")
        d = self.route("Rotate the token via nasvault.", "VAULT-BRIEF-x.md")
        self.assertTrue(d.get("blocked"), d)
        self.assertEqual(d["lane"], "vault")

    def test_not_ready_instance_is_skipped(self):
        self.ready = {"jupiter": {"claudeAgent": False}}
        d = self.route("Root cause the incident.", "BRIEF-inc.md")
        self.assertEqual(d["vendor"], "openai")  # opus -> sonnet (same instance, not ready) -> luna

    def test_vault_runs_on_the_pro(self):
        self.assertEqual(self.route("Rotate the token via nasvault.", "VAULT-BRIEF-x.md")["host"], "pro")

    # spread
    def test_everyday_spreads_across_vendors(self):
        today = dt.date.today().isoformat()
        self.log = [{"at": today + "T08:00", "vendor": "anthropic"}] * 3
        d = self.route("Fix the lint.", "BRIEF-lint.md")
        self.assertNotEqual(d["vendor"], "anthropic", d)

    def test_everyday_avoids_a_vendor_near_its_plan_limit(self):
        self.usage = {"claude": {"usedPercent": 95}}
        self.assertNotEqual(self.route("Fix the lint.", "BRIEF-lint.md")["vendor"], "anthropic")

    def test_capability_routes_to_first_usable_lab(self):
        wr = self.wr
        wr.LABS_FILE = self.path("labs.json", self.labs)
        wr.READINESS = self.path("ready.json", self.ready)
        wr.CAPS_FILE = os.path.join(HERE, "capabilities.example.json")
        d = wr.capability("clickup.advertes")
        self.assertEqual((d["model_lab"], d["host"]), ("opus", "pro"))
        self.assertIn("hire.sh claude", d["hire"])
        self.off("opus", "founder")
        self.assertEqual(wr.capability("clickup.advertes")["model_lab"], "sonnet")
        self.off("luna", "founder")
        self.assertTrue(wr.capability("vault.credentials")["blocked"])  # vault never falls back off Luna
        self.assertTrue(wr.capability("no.such.thing")["blocked"])

    def test_shipped_example_is_valid(self):
        labs = self.labs["labs"]
        for k, v in labs.items():
            self.assertIn(v["switch"], ("ON", "OFF", "NEVER"), k)
            for f in v.get("fallback", []):
                self.assertIn(f, labs, f"{k} falls back to unknown {f}")
        for lane in self.wr.LANES.values():
            for c in lane["candidates"]:
                self.assertIn(c, labs)


if __name__ == "__main__":
    unittest.main()
