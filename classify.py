#!/usr/bin/env python3
"""Classify a problem into a GUARDIAN failure class and return its solution card.

Tier 1: finding-key prefixes and regex patterns from taxonomy.json (free, deterministic).
Tier 2: Jev (TypeSafe decision model, typed choice + probabilities) for free text the rules miss.
        Key: Keychain (JEV_ITEMS, Founder-authorized 11 Oct; never printed). No key -> tier 2 is skipped.
Tier 3: low confidence -> "unclassified"; the case is appended to guardian/needs-judgment.jsonl so
        GUARDIAN decides it and adds it to the eval set (the classifier learns from its misses).

Usage: classify.py "<text>" [--key finding-key] [--no-jev]   |   import classify; classify.classify(text, key)
"""
import json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TAX = json.load(open(os.path.join(HERE, "taxonomy.json")))["classes"]
# Cloudflare refuses the default Python-urllib UA (403, error 1010; 11 Oct).
UA = "estate-guardian/1"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
# Founder 11 Oct 2026: use the existing TypeSafe key from the Keychain; a dedicated GUARDIAN item wins if it exists.
JEV_ITEMS = (json.load(open(os.path.expanduser(os.environ.get("ESTATE_CONFIG", "~/Developer-artifacts/guardian/estate.local.json"))))
             .get("jev_keychain_items", []) if os.path.exists(os.path.expanduser(os.environ.get("ESTATE_CONFIG", "~/Developer-artifacts/guardian/estate.local.json"))) else [])
JUDGE_Q = os.path.expanduser("~/Developer-artifacts/guardian/needs-judgment.jsonl")
MIN_CONF = 0.6


def rules(text, key=""):
    for cid, c in TAX.items():
        if key and any(key.startswith(p) for p in c.get("key_prefixes", [])):
            return cid, 1.0, "key"
    low = text.lower()
    hits = {cid: sum(bool(re.search(p, low)) for p in c.get("patterns", [])) for cid, c in TAX.items()}
    best = max(hits, key=hits.get)
    if hits[best] and sorted(hits.values())[-2] < hits[best]:  # a unique winner only
        return best, min(0.95, 0.6 + 0.1 * hits[best]), "pattern"
    return None, 0.0, "none"


def jev(text):
    k = ""
    for item in JEV_ITEMS:
        r = subprocess.run(["security", "find-generic-password", "-s", item, "-w"], capture_output=True, text=True, timeout=10)
        k = r.stdout.strip() if r.returncode == 0 else ""
        if k:
            break
    if not k:
        return None, 0.0, "jev-no-key"
    body = {"model": "jev-latest", "state": text[:4000], "questions": {"cls": {
        "type": "choice", "instructions": "Which failure mechanism best explains this infrastructure or delivery problem?",
        "criteria": {cid: c["desc"] for cid, c in TAX.items()}}}}
    req = urllib.request.Request(JEV_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json", "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                a = json.load(resp)["answers"]["cls"]
            return a["choice"], float(a.get("confidence", 0)), "jev"
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return None, 0.0, f"jev-http-{e.code}"
        except Exception as e:
            return None, 0.0, f"jev-{type(e).__name__}"
    return None, 0.0, "jev-retry"


def classify(text, key="", use_jev=True, record=True):
    cid, conf, how = rules(text, key)
    if not cid and use_jev:
        cid, conf, how = jev(text)
    if not cid or conf < MIN_CONF:
        if record:
            try:
                with open(JUDGE_Q, "a") as q:
                    q.write(json.dumps({"t": int(time.time()), "key": key, "text": text[:600], "guess": cid,
                                        "conf": conf, "how": how}) + "\n")
            except OSError:
                pass
        return {"class": "unclassified", "confidence": conf, "how": how, "card": None}
    return {"class": cid, "confidence": round(conf, 2), "how": how, "card": TAX[cid]["card"]}


def card_markdown(res):
    if not res.get("card"):
        return "**Class:** unclassified (queued for GUARDIAN judgment; it becomes an eval example)."
    c = res["card"]
    return (f"**Class:** `{res['class']}` ({res['how']}, confidence {res['confidence']})\n"
            f"- Root-cause question: {c['root_cause_question']}\n- Smallest safe fix: {c['smallest_safe_fix']}\n"
            f"- Guard: {c['guard']}\n- Owner: {c['owner']} · Playbook: {c['playbook']}")


if __name__ == "__main__":
    a = sys.argv[1:]
    key = a[a.index("--key") + 1] if "--key" in a else ""
    text = a[0] if a else sys.stdin.read()
    print(json.dumps(classify(text, key, use_jev="--no-jev" not in a, record=False), indent=1))
