#!/usr/bin/env python3
"""Pain → solution classifier: a product-agnostic core driven by preset files.

A preset (presets/<name>.json, or any path) declares:
  taxonomy     classes {id: {desc, key_prefixes, patterns, card}}; inline, or a path relative to the preset
  question     the Jev instruction for the class choice
  min_conf     below this the case is "unclassified" and goes to the judge queue
  paths        judge_queue, answers (retained Jev answers, for replay), eval (labelled cases)
  key          where the TypeSafe key comes from: {"source": "keychain", "items": [...] | "items_from_config": "<cfg key>"},
               {"source": "file", "path": "/run/secrets/typesafe"} or {"source": "env", "name": "TYPESAFE_API_KEY"}
  questions    optional extra typed questions (choice / yes-no / score) a product can ask with Classifier.ask()
  user_agent   HTTP User-Agent (Cloudflare refuses Python's default; 11 Oct)

Tier 1: finding-key prefixes and regex patterns (free, deterministic).
Tier 2: Jev (TypeSafe System One: typed choice + calibrated confidence) for what the rules miss. Every answer is kept
        and replayed (AgentRun lesson, 12 Oct): a threshold or code change re-scores past cases with no model call.
Tier 3: low confidence → "unclassified", appended to the preset's judge queue so a human/owner decides it and it
        becomes an eval example.

Library:  from classify import Classifier; c = Classifier("trznice"); c.classify(text); c.ask(state, ["lane"])
          (module-level classify()/jev_ask()/card_markdown() keep using the "guardian" preset, unchanged)
CLI:      classify.py "<text>" [--preset NAME|PATH] [--key finding-key] [--no-jev] [--replay]
"""
import hashlib, json, os, re, subprocess, sys, time, urllib.error, urllib.request

__version__ = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
JEV_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai") + "/v1/systemone"
ESTATE_CONFIG = os.path.expanduser(os.environ.get("ESTATE_CONFIG", "~/Developer-artifacts/guardian/estate.local.json"))


def _expand(p, base):
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def load_preset(preset="guardian"):
    """A preset name (presets/<name>.json next to this file) or a path to a preset JSON."""
    path = preset if (os.sep in preset or preset.endswith(".json")) else os.path.join(HERE, "presets", f"{preset}.json")
    path = os.path.expanduser(path)
    p = json.load(open(path))
    base = os.path.dirname(os.path.abspath(path))
    tax = p.get("taxonomy", {})
    if isinstance(tax, str):
        tax = json.load(open(_expand(tax, base)))
    p["classes"] = tax.get("classes", tax) if isinstance(tax, dict) else {}
    p["paths"] = {k: _expand(v, base) for k, v in (p.get("paths") or {}).items()}
    p["_path"] = path
    return p


class Classifier:
    def __init__(self, preset="guardian"):
        self.p = preset if isinstance(preset, dict) else load_preset(preset)
        self.name = self.p.get("name", "custom")
        self.classes = self.p["classes"]
        self.min_conf = float(self.p.get("min_conf", 0.6))
        self.ua = self.p.get("user_agent", "estate-classify/1")
        self.judge_q = self.p["paths"].get("judge_queue")
        self.answers_file = self.p["paths"].get("answers")

    # ---- key ----
    def _key(self):
        k = self.p.get("key") or {}
        src = k.get("source", "keychain")
        if src == "env":
            return os.environ.get(k.get("name", "TYPESAFE_API_KEY"), "").strip()
        if src == "file":
            try:
                return open(os.path.expanduser(k["path"])).read().strip()
            except (OSError, KeyError):
                return ""
        items = list(k.get("items", []))
        if k.get("items_from_config") and os.path.exists(ESTATE_CONFIG):
            items += json.load(open(ESTATE_CONFIG)).get(k["items_from_config"], [])
        for item in items:
            try:
                r = subprocess.run(["security", "find-generic-password", "-s", item, "-w"], capture_output=True,
                                   text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        return ""

    # ---- Jev with retained answers ----
    def jev_ask(self, state, questions, replay_only=False):
        """One typed Jev request; every answer is retained. The fingerprint covers model + state + full question
        text, so changing a question's criteria asks again, while changing the code that reads answers replays."""
        body = {"model": "jev-latest", "state": state[:4000], "questions": questions}
        fp = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
        if self.answers_file:
            try:
                for line in open(self.answers_file):
                    d = json.loads(line)
                    if d.get("fp") == fp:
                        return d["answers"], "jev-replay"
            except FileNotFoundError:
                pass
        if replay_only:
            return None, "jev-not-in-replay"
        k = self._key()
        if not k:
            return None, "jev-no-key"
        req = urllib.request.Request(JEV_URL, data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json",
                                              "User-Agent": self.ua})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    answers = json.load(resp)["answers"]
                if self.answers_file:
                    os.makedirs(os.path.dirname(self.answers_file), exist_ok=True)
                    with open(self.answers_file, "a") as f:
                        f.write(json.dumps({"fp": fp, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "preset": self.name,
                                            "state": state[:4000], "questions": list(questions),
                                            "answers": answers}) + "\n")
                return answers, "jev"
            except urllib.error.HTTPError as e:
                if e.code in (429, 529) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return None, f"jev-http-{e.code}"
            except Exception as e:
                return None, f"jev-{type(e).__name__}"
        return None, "jev-retry"

    def ask(self, state, names, replay_only=False):
        """Ask the preset's named typed questions (choice / yes-no / score) about one state."""
        qs = {n: self.p["questions"][n] for n in names}
        return self.jev_ask(state, qs, replay_only=replay_only)

    # ---- the class choice ----
    def rules(self, text, key=""):
        for cid, c in self.classes.items():
            if key and any(key.startswith(p) for p in c.get("key_prefixes", [])):
                return cid, 1.0, "key"
        if not self.classes:
            return None, 0.0, "none"
        low = text.lower()
        hits = {cid: sum(bool(re.search(p, low)) for p in c.get("patterns", [])) for cid, c in self.classes.items()}
        best = max(hits, key=hits.get)
        second = sorted(hits.values())[-2] if len(hits) > 1 else 0
        if hits[best] and second < hits[best]:  # a unique winner only
            return best, min(0.95, 0.6 + 0.1 * hits[best]), "pattern"
        return None, 0.0, "none"

    def jev(self, text, replay_only=False):
        if not self.classes:
            return None, 0.0, "no-classes"
        answers, how = self.jev_ask(text, {"cls": {
            "type": "choice", "instructions": self.p.get("question", "Which class best explains this case?"),
            "criteria": {cid: c["desc"] for cid, c in self.classes.items()}}}, replay_only=replay_only)
        if not answers:
            return None, 0.0, how
        a = answers["cls"]
        return a["choice"], float(a.get("confidence", 0)), how

    def classify(self, text, key="", use_jev=True, record=True, replay_only=False):
        cid, conf, how = self.rules(text, key)
        if not cid and use_jev:
            cid, conf, how = self.jev(text, replay_only=replay_only)
        if not cid or conf < self.min_conf or cid not in self.classes:
            if record and self.judge_q:
                try:
                    os.makedirs(os.path.dirname(self.judge_q), exist_ok=True)
                    with open(self.judge_q, "a") as q:
                        q.write(json.dumps({"t": int(time.time()), "preset": self.name, "key": key, "text": text[:600],
                                            "guess": cid, "conf": conf, "how": how}) + "\n")
                except OSError:
                    pass
            return {"class": "unclassified", "confidence": conf, "how": how, "card": None}
        return {"class": cid, "confidence": round(conf, 2), "how": how, "card": self.classes[cid].get("card")}


def card_markdown(res):
    if not res.get("card"):
        return "**Class:** unclassified (queued for GUARDIAN judgment; it becomes an eval example)."
    c = res["card"]
    return (f"**Class:** `{res['class']}` ({res['how']}, confidence {res['confidence']})\n"
            f"- Root-cause question: {c['root_cause_question']}\n- Smallest safe fix: {c['smallest_safe_fix']}\n"
            f"- Guard: {c['guard']}\n- Owner: {c['owner']} · Playbook: {c['playbook']}")


# ---- module-level API (the "guardian" preset; behaviour unchanged for guardian.py, worker_route.py, measure.py) ----
_DEFAULT = None


def default():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Classifier(os.environ.get("ESTATE_PRESET", "guardian"))
    return _DEFAULT


def classify(text, key="", use_jev=True, record=True, replay_only=False):
    return default().classify(text, key, use_jev=use_jev, record=record, replay_only=replay_only)


def jev_ask(state, questions, replay_only=False):
    return default().jev_ask(state, questions, replay_only=replay_only)


def jev(text, replay_only=False):
    return default().jev(text, replay_only=replay_only)


def rules(text, key=""):
    return default().rules(text, key)


if __name__ == "__main__":
    a = sys.argv[1:]
    preset = a[a.index("--preset") + 1] if "--preset" in a else os.environ.get("ESTATE_PRESET", "guardian")
    key = a[a.index("--key") + 1] if "--key" in a else ""
    pos = [x for i, x in enumerate(a) if not x.startswith("--") and (i == 0 or a[i - 1] not in ("--preset", "--key"))]
    text = pos[0] if pos else sys.stdin.read()
    print(json.dumps(Classifier(preset).classify(text, key, use_jev="--no-jev" not in a, record=False,
                                                 replay_only="--replay" in a), indent=1, ensure_ascii=False))
