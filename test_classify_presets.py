#!/usr/bin/env python3
"""Offline tests for the preset-driven classifier core. No network: Jev is replay-only or absent."""
import json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import classify  # noqa: E402


class Presets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def preset(self, **over):
        p = json.load(open(os.path.join(HERE, "presets", "example.json")))
        p["paths"] = {"judge_queue": os.path.join(self.tmp.name, "q.jsonl"), "answers": os.path.join(self.tmp.name, "a.jsonl")}
        p.update(over)
        path = os.path.join(self.tmp.name, "p.json")
        json.dump(p, open(path, "w"))
        return path

    def test_guardian_preset_loads_the_estate_taxonomy(self):
        c = classify.Classifier("guardian")
        self.assertGreaterEqual(len(c.classes), 10)
        self.assertEqual(c.rules("", "runner-offline-x")[0], c.rules("", "runner-offline-x")[0])
        self.assertEqual(c.ua, "estate-guardian/1")

    def test_module_api_is_the_guardian_preset(self):
        self.assertEqual(classify.default().name, "guardian")
        self.assertEqual(classify.rules("", "tls-x"), classify.Classifier("guardian").rules("", "tls-x"))

    def test_preset_by_path_rules_and_unclassified_queue(self):
        c = classify.Classifier(self.preset())
        self.assertEqual(c.classify("the app shows an error", use_jev=False)["class"], "bug")
        self.assertEqual(c.classify("bug-123 something", key="bug-123", use_jev=False)["how"], "key")
        r = c.classify("nothing matches here", use_jev=False)
        self.assertEqual(r["class"], "unclassified")
        q = [json.loads(l) for l in open(c.judge_q)]
        self.assertEqual(q[-1]["preset"], "example")

    def test_empty_jack_slot_is_safe(self):
        c = classify.Classifier("jack")
        self.assertEqual(c.classes, {})
        self.assertEqual(c.classify("anything", use_jev=False, record=False)["class"], "unclassified")
        self.assertEqual(c.jev("anything")[2], "no-classes")

    def test_key_sources(self):
        c = classify.Classifier(self.preset(key={"source": "env", "name": "XKEY_TEST"}))
        os.environ["XKEY_TEST"] = "k-env"
        self.assertEqual(c._key(), "k-env")
        f = os.path.join(self.tmp.name, "secret")
        open(f, "w").write("k-file\n")
        self.assertEqual(classify.Classifier(self.preset(key={"source": "file", "path": f}))._key(), "k-file")
        self.assertEqual(classify.Classifier(self.preset(key={"source": "file", "path": f + ".missing"}))._key(), "")

    def test_replay_uses_retained_answers_without_network(self):
        c = classify.Classifier(self.preset(key={"source": "env", "name": "NO_SUCH_KEY_VAR"}))
        qs = {"cls": {"type": "choice", "instructions": c.p["question"],
                      "criteria": {cid: x["desc"] for cid, x in c.classes.items()}}}
        body = {"model": "jev-latest", "state": "odd text", "questions": qs}
        fp = classify.hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
        open(c.answers_file, "w").write(json.dumps({"fp": fp, "answers": {"cls": {"choice": "request", "confidence": 0.9}}}) + "\n")
        r = c.classify("odd text", record=False, replay_only=True)
        self.assertEqual((r["class"], r["how"]), ("request", "jev-replay"))
        self.assertEqual(c.classify("other odd text", record=False, replay_only=True)["how"], "jev-not-in-replay")

    def test_named_questions(self):
        c = classify.Classifier(self.preset(key={"source": "env", "name": "NO_SUCH_KEY_VAR"}))
        self.assertEqual(c.ask("state", ["urgent"], replay_only=True), (None, "jev-not-in-replay"))


if __name__ == "__main__":
    unittest.main()
