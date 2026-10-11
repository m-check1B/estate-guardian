#!/usr/bin/env python3
"""Worker classifier: pick the lab and budget for a brief across our labs (Founder, 11 Oct 2026).

Priors are taken from the "Claude 5.5 · which one?" cheat sheet (Daniel Wright), adapted to our labs and Founder rules:
  - Haiku 5.5: sorting, extracting, routing at scale; not long multi-step coding.
  - Sonnet 5.5: everyday work (bugs, docs, slides, spreadsheets), near Opus at half the price, but more output tokens.
  - Opus 5.5: complex, open-ended work that needs judgement (plus marketing/brand: Opus only, Founder rule).
  - Luna 6 (Codex): computer/browser clicking, UI prove, recon (Founder 11 Oct), and vault/credential briefs on the Pro.
  - Multi-lab (Founder 11 Oct: \"we need multilab not anthropic only\"): every lane lists candidates across vendors
    (Anthropic, OpenAI/Codex, Cursor, xAI/Grok); availability comes from labs.json (OUT/NEVER skipped; Astra, Fable,
    Sol never chosen); everyday coding is spread across vendors by today's hire counts.
These are priors, not truths: every decision is logged to guardian/worker-routing.jsonl and the autoresearch loop
keeps or changes the rules on our own outcomes (RESULT line 1 per hire).

Tier 1: rules on the brief's file name, title and text. Tier 2: Jev (TypeSafe choice) when rules are unsure.
Default when both are unsure: Opus medium (a weak model failing costs more than Opus).
Usage: worker_route.py <brief.md> [title]   -> prints JSON {lab, budget, lane, how, confidence, reason}
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LANES = {  # candidates in preference order, across vendors; OUT/NEVER labs are skipped (labs.json)
    "vault": {"candidates": ["luna"], "desc": "Credential, token, key, password, vault or keychain work done via ~/bin/nasvault on the Pro (vault briefs)."},
    "clicker": {"candidates": ["luna", "cursor"], "desc": "Computer or browser use: UI prove, click-through E2E, screenshots, user tests, desktop-app checks, web recon or scouting."},
    "sort": {"candidates": ["haiku", "cursor", "luna-medium"], "desc": "Sorting, classifying, triaging, extracting, labelling, deduplicating or inventorying many items; short mechanical transforms; no multi-step coding."},
    "everyday": {"candidates": ["sonnet", "luna-medium", "cursor", "grok"], "spread": True, "desc": "Everyday engineering: a well-specified bug fix, rebase, tests, lint, small refactor, docs, README, slides, spreadsheets, CI follow-up on an existing PR."},
    "judgement": {"candidates": ["opus", "luna", "grok"], "desc": "Complex open-ended work needing judgement: architecture, design, specs, security review, audit, root cause, incident, migration, multi-repo change."},
    "marketing": {"candidates": ["opus"], "desc": "Marketing, pricing, brand, positioning or copy (Founder rule: Opus only)."},
}
ART = os.path.expanduser("~/Developer-artifacts/guardian")
# the live policy (the guard flips switches in it); a fresh checkout falls back to the shipped example
LABS_FILE = os.environ.get("ESTATE_LABS") or next(
    (p for p in (f"{ART}/labs.json",) if os.path.exists(p)), os.path.join(HERE, "labs.example.json"))
ROUTING_LOG = os.environ.get("ESTATE_ROUTING_LOG", f"{ART}/worker-routing.jsonl")
READINESS = os.environ.get("ESTATE_READINESS", f"{ART}/lab-readiness.json")
USAGE = os.environ.get("ESTATE_USAGE", f"{ART}/lab-usage.json")
RULES = [  # (lane, regex) checked on name + title + text; filename hints weigh double
    ("vault", r"vault[-_ ]brief|nasvault|credential|api[ -]?key|token rotat|rotate .*token|password|keychain|notary|secret|"
              r"sign[- ]in to (?:the )?(?:cloudflare|namecheap|tailscale|linear|zitadel|google|apple|github|openrouter)"),
    ("clicker", r"ui prove|click|browser|playwright|e2e|screenshot|user ?test|usertest|desktop[- ]app check|recon|scout|walk ?through|computer use"),
    ("sort", r"\bclassif|\btriage|\bextract|\blabel(l)?ing|\bdedup|\binventory|\bsort(ing)?\b|categori[sz]e|route .* items|tag each"),
    ("everyday", r"\bbug ?fix|\bfix the|\brebase|\btests?\b|\blint|\brefactor|\bdocs?\b|readme|\bslides?\b|spreadsheet|\bci\b|flak|\bbump|\brename|\bhelper\b|call sites"),
    ("judgement", r"architect|\bdesign\b|\bspec\b|security review|\baudit\b|root cause|incident|\bp0\b|migrat|multi-repo|strategy|threat model|blast radius"),
    ("marketing", r"marketing|pricing|\bbrand|positioning|\bcopywriting|landing page copy|campaign|\bad copy"),
]
HIGH = r"\bp0\b|security|incident|race|flaky|production|hotfix|data loss|revok"



def rules(name, title, text):
    hay_name, hay = (name + " " + title).lower(), (title + " " + text[:6000]).lower()
    score = {}
    for lane, rx in RULES:
        s = 2 * len(re.findall(rx, hay_name)) + len(re.findall(rx, hay))
        if s:
            score[lane] = s
    if not score:
        return None, 0.0, score
    best = max(score, key=score.get)
    others = sorted(score.values())[:-1]
    if "vault" in name.lower() and "vault" in score:  # a VAULT brief decides host + safety path (hire.sh routes it to the Pro)
        best = "vault"
    elif best == "vault" and "vault" not in name.lower() and len(score) > 1:
        # credential words inside a code brief do not make it a vault job: take the best non-vault lane
        best = max((l for l in score if l != "vault"), key=score.get)
    margin = score[best] - (others[-1] if others else 0)
    conf = min(0.95, 0.55 + 0.1 * margin)
    return best, conf, score


def jev_lane(text):
    if os.environ.get("ROUTER_NO_JEV"):  # offline / tests: rules + default only
        return None, 0.0
    try:
        import classify  # reuses the Keychain key handling (Founder-authorized), UA and retries
        import urllib.request
        k = ""
        for item in classify.JEV_ITEMS:
            r = classify.subprocess.run(["security", "find-generic-password", "-s", item, "-w"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                k = r.stdout.strip(); break
        if not k:
            return None, 0.0
        body = {"model": "jev-latest", "state": text[:4000], "questions": {"lane": {"type": "choice",
                "instructions": "Which kind of worker should take this engineering brief?",
                "criteria": {l: v["desc"] for l, v in LANES.items()}}}}
        req = urllib.request.Request(classify.JEV_URL, data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {k}", "Content-Type": "application/json", "User-Agent": classify.UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            a = json.load(resp)["answers"]["lane"]
        return a["choice"], float(a.get("confidence", 0))
    except Exception:
        return None, 0.0


def route(path, title=""):
    text = open(path, errors="replace").read() if os.path.exists(path) else ""
    name = os.path.basename(path)
    lane, conf, score = rules(name, title, text)
    how = "rules"
    if not lane or conf < 0.75:
        jl, jc = jev_lane(f"{title}\n{name}\n{text}")
        if jl and jc >= 0.6 and (not lane or jc > conf):
            lane, conf, how = jl, jc, "jev"
    if not lane or conf < 0.6:
        lane, conf, how = "judgement", conf, "default"
    host = "pro" if lane == "vault" else os.environ.get("HIRE_HOST", "jupiter")
    policy, usable = lab_state(host)
    cands = LANES[lane]["candidates"]
    pick = None
    if LANES[lane].get("spread"):
        # spread everyday work across vendors so one quota or outage never stops the estate (11 Oct Grok 402 lesson)
        on = [c for c in cands if c in usable]
        if on:
            used = today_counts()
            usage = vendor_usage()
            # least loaded vendor wins: today's hires + plan usage from CodexBar; the primary keeps a small edge
            pick = min(on, key=lambda c: ((used.get(policy[c]["vendor"], 0) + usage.get(policy[c]["vendor"], 0) / 10)
                                          * (1.0 if c == cands[0] else 1.5), cands.index(c)))
    chain, fell_from = [], None
    if not pick:
        primary = cands[0]
        chain = [primary] + ([] if lane == "vault" else policy.get(primary, {}).get("fallback", []) + cands[1:])
        for c in chain:
            if c in usable:
                pick = c
                break
        if pick and pick != primary:
            fell_from = f"{primary} ({policy.get(primary, {}).get('switch')}/{policy.get(primary, {}).get('reason', usable.get(primary, 'not-ready'))})"
    if not pick:
        return {"lab": None, "lane": lane, "how": how, "confidence": round(conf, 2), "blocked": True,
                "reason": f"no usable lab for lane {lane} on {host}; chain {chain or cands}"}
    hire = policy[pick]["hire"]
    budget = "high" if pick == "luna" else "medium"
    if lane in ("everyday", "judgement") and re.search(HIGH, (title + " " + text[:6000]).lower()) and hire != "cursor":
        budget = "high"
    return {"lab": hire, "model_lab": pick, "vendor": policy[pick]["vendor"], "budget": budget, "lane": lane, "how": how,
            "confidence": round(conf, 2), "host": host, "fallback_from": fell_from, "reason": LANES[lane]["desc"], "scores": score}


def lab_state(host):
    """Policy (labs.json: switch ON/OFF/NEVER, until, fallback) x readiness (tentacles doctor per host, cached by the guard)."""
    import datetime as _dt
    try:
        policy = json.load(open(LABS_FILE))["labs"]
    except Exception:
        policy = {"opus": {"vendor": "anthropic", "hire": "claude", "instance": "claudeAgent", "switch": "ON"}}
    try:
        ready = json.load(open(READINESS)).get(host, {})
    except Exception:
        ready = {}  # no readiness data yet: trust the switch
    today = _dt.date.today().isoformat()
    usable = {}
    for k, v in policy.items():
        sw = v.get("switch")
        if sw == "OFF" and v.get("until"):
            u = str(v["until"])
            if (v.get("reason") == "founder" and today > u[:10]) or (v.get("reason") == "usage" and _dt.datetime.utcnow().isoformat() > u[:19]):
                sw = "ON"  # announced return date or the plan's usage reset has passed
        if sw == "ON" and v.get("hire") and ready.get(v.get("instance"), True):
            usable[k] = "ready"
    return policy, usable


def vendor_usage():
    """Plan usage % per vendor from the guard's CodexBar snapshot (guardian/lab-usage.json)."""
    try:
        snap = json.load(open(USAGE))
    except Exception:
        return {}
    m = {"claude": "anthropic", "codex": "openai", "cursor": "cursor", "grok": "xai"}
    return {m[k]: v["usedPercent"] for k, v in snap.items() if k in m and isinstance(v, dict)}


def today_counts():
    import datetime as _dt
    day, c = _dt.date.today().strftime("%Y-%m-%d"), {}
    try:
        for line in open(ROUTING_LOG):
            d = json.loads(line)
            if d.get("at", "").startswith(day) and not d.get("dry"):
                c[d.get("vendor", "?")] = c.get(d.get("vendor", "?"), 0) + 1
    except FileNotFoundError:
        pass
    return c


if __name__ == "__main__":
    print(json.dumps(route(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""), ensure_ascii=False))
