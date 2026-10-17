#!/usr/bin/env python3
"""Worker classifier: pick the lab and budget for a brief across our labs (Founder, 11 Oct 2026).

Priors are taken from the "Claude 5.5 · which one?" cheat sheet (Daniel Wright), adapted to our labs and Founder rules:
  - Haiku 5.5: sorting, extracting, routing at scale; not long multi-step coding.
  - Sonnet 5.5: everyday work (bugs, docs, slides, spreadsheets), near Opus at half the price, but more output tokens.
  - Opus 5.5: complex, open-ended work that needs judgement (plus marketing/brand: Opus only, Founder rule).
  - Luna 6 (Codex): computer/browser clicking, UI prove, recon (Founder 11 Oct: best clicker); vault fallback.
  - Sol 6.1 (Codex): back ON 15 Oct (Founder "odblokuj sol globálně"); strongest allowed executor for vault/credential
    briefs (POLICY-VAULT-AND-2FA), second choice for clicker and judgement.
  - Multi-lab (Founder 11 Oct: \"we need multilab not anthropic only\"): every lane lists candidates across vendors
    (Anthropic, OpenAI/Codex, Cursor, xAI/Grok); availability comes from labs.json (OUT/NEVER skipped; Astra and Fable
    never chosen; Sol 6.1 back ON 15 Oct: first for vault, second for clicker/judgement); everyday coding is spread across vendors by today's hire counts.
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
    "vault": {"candidates": ["sol", "luna"], "desc": "Credential, token, key, password, vault or keychain work done via ~/bin/nasvault on the Pro (vault briefs)."},
    "clicker": {"candidates": ["luna", "sol", "cursor"], "desc": "Computer or browser use: UI prove, click-through E2E, screenshots, user tests, desktop-app checks, web recon or scouting."},
    "sort": {"candidates": ["haiku", "cursor", "luna-medium"], "desc": "Sorting, classifying, triaging, extracting, labelling, deduplicating or inventorying many items; short mechanical transforms; no multi-step coding."},
    "everyday": {"candidates": ["sonnet", "luna-medium", "cursor", "grok"], "spread": True, "desc": "Everyday engineering: a well-specified bug fix, rebase, tests, lint, small refactor, docs, README, slides, spreadsheets, CI follow-up on an existing PR."},
    "judgement": {"candidates": ["opus", "sol", "luna", "grok"], "desc": "Complex open-ended work needing judgement: architecture, design, specs, security review, audit, root cause, incident, migration, multi-repo change."},
    "marketing": {"candidates": ["opus"], "desc": "Marketing, pricing, brand, positioning or copy (Founder rule: Opus only)."},
}
ART = os.path.expanduser("~/Developer-artifacts/guardian")


def _example(name):
    """Shipped example config: next to this module in a checkout, or under <prefix>/share/estate-classify when the
    estate-classify wheel is installed (0.3.0+)."""
    for d in (HERE, os.path.join(sys.prefix, "share", "estate-classify")):
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return os.path.join(HERE, name)


# the live policy (the guard flips switches in it); a fresh checkout falls back to the shipped example
LABS_FILE = os.environ.get("ESTATE_LABS") or next(
    (p for p in (f"{ART}/labs.json",) if os.path.exists(p)), _example("labs.example.json"))
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
        import classify  # shared Jev client: Keychain key, UA, retries, and every answer retained for replay
        answers, _ = classify.jev_ask(text, {"lane": {"type": "choice",
                                       "instructions": "Which kind of worker should take this engineering brief?",
                                       "criteria": {l: v["desc"] for l, v in LANES.items()}}},
                                      replay_only=bool(os.environ.get("ROUTER_JEV_REPLAY")))
        if not answers:
            return None, 0.0
        a = answers["lane"]
        return a["choice"], float(a.get("confidence", 0))
    except Exception:
        return None, 0.0


VENDOR_WORDS = {"anthropic": r"anthropic|claude|opus|sonnet|haiku", "openai": r"openai|codex|gpt|luna",
                "cursor": r"cursor|composer", "xai": r"xai|grok"}
ADMIN_POLICY = os.environ.get("ESTATE_ADMIN_POLICY", f"{ART}/admin_policy.json")


def parse_preferences(text):
    """One free-text preference prompt per Agent Jack Bot instead of five dropdowns (Founder 13 Oct). Rules turn it into
    constraints the router applies; the admin policy (admin_policy.json) overrides wrong user preferences."""
    t = (text or "").lower()
    p = {"avoid": set(), "prefer": [], "cost": None}
    for v, rx in VENDOR_WORDS.items():
        for m in re.finditer(rf"(?:no|not|avoid|never|without|bez|nechci|nepou[žz]ívat|ne)\s+(?:\w+\s+){{0,2}}?(?:{rx})", t):
            p["avoid"].add(v)
        if re.search(rf"(?:prefer|preferuj\w*|rad[šs]i|use|pou[žz]ij\w*|chci)\s+(?:\w+\s+){{0,2}}?(?:{rx})", t) and v not in p["avoid"]:
            p["prefer"].append(v)
    if re.search(r"cheap|levn|[šs]et[řr]|low cost|budget|n[íi]zk\w* cen", t):
        p["cost"] = "low"
    if re.search(r"best|nejlep|quality|kvalit|maximum|nejsiln|careful|pe[čc]liv", t):
        p["cost"] = "high"
    return p


def admin_policy():
    try:
        return json.load(open(ADMIN_POLICY))
    except Exception:  # default: the Founder rules that preferences may never break
        return {"locked_lanes": {"vault": ["sol", "luna"], "marketing": ["opus"]}, "deny_vendors": [], "max_budget": "high"}


def route(path, title="", preferences=""):
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
    cands = list(LANES[lane]["candidates"])
    prefs, admin, applied, overridden = parse_preferences(preferences), admin_policy(), [], []
    locked = admin.get("locked_lanes", {}).get(lane)
    for v in admin.get("deny_vendors", []):
        usable = {k: x for k, x in usable.items() if policy.get(k, {}).get("vendor") != v}
    if locked:
        if preferences:
            overridden.append(f"lane {lane} is locked by the admin to {locked}; preferences ignored")
        cands = [c for c in cands if c in locked] or cands
    else:
        if prefs["avoid"]:
            left = {k: x for k, x in usable.items() if policy.get(k, {}).get("vendor") not in prefs["avoid"]}
            if any(c in left for c in cands + sum((policy.get(c, {}).get("fallback", []) for c in cands), [])):
                usable = left
                applied.append(f"avoid {sorted(prefs['avoid'])}")
            else:
                overridden.append(f"avoid {sorted(prefs['avoid'])} would leave no usable lab for {lane}; ignored")
        if prefs["prefer"]:
            order = list(cands)
            cands.sort(key=lambda c: (policy.get(c, {}).get("vendor") not in prefs["prefer"], order.index(c)))
            applied.append(f"prefer {prefs['prefer']}")
    pick = None
    if LANES[lane].get("spread") and not prefs["prefer"]:
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
        # vault: only its own locked candidates (Sol, then Luna), never the generic fallbacks (15 Oct: Sol back ON)
        chain = [primary] + (cands[1:] if lane == "vault" else policy.get(primary, {}).get("fallback", []) + cands[1:])
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
    budget = "high" if pick == "luna" or lane == "vault" else "medium"  # vault = sensitive work, strongest setting
    if lane in ("everyday", "judgement") and re.search(HIGH, (title + " " + text[:6000]).lower()) and hire != "cursor":
        budget = "high"
    if prefs["cost"] and not locked:
        want = "medium" if prefs["cost"] == "low" else "high"
        if want != budget:
            applied.append(f"budget {budget}->{want} ({prefs['cost']} cost preference)")
            budget = want
    if budget == "high" and admin.get("max_budget") == "medium":
        overridden.append("budget capped at medium by the admin")
        budget = "medium"
    return {"lab": hire, "model_lab": pick, "vendor": policy[pick]["vendor"], "budget": budget, "lane": lane, "how": how,
            "confidence": round(conf, 2), "host": host, "fallback_from": fell_from, "reason": LANES[lane]["desc"], "scores": score,
            "preferences_applied": applied, "preferences_overridden": overridden}


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


CAPS_FILE = os.environ.get("ESTATE_CAPABILITIES") or next(
    (p for p in (f"{ART}/capabilities.json",) if os.path.exists(p)), _example("capabilities.example.json"))


def capability(name, host=None):
    """Which worker, on which machine, reaches <name> (Founder 13 Oct: Agent Jack Bots only need to know where and through
    which worker to go). Picks the first usable lab from the capability's list (labs.json switch x readiness)."""
    caps = json.load(open(CAPS_FILE)).get("capabilities", {})
    if name not in caps:
        return {"capability": name, "blocked": True, "reason": f"unknown capability; known: {', '.join(sorted(caps))}"}
    c = caps[name]
    host = host or c.get("host", "jupiter")
    policy, usable = lab_state(host)
    pick = next((l for l in c.get("labs", []) if l in usable), None)
    if not pick:
        return {"capability": name, "host": host, "blocked": True,
                "reason": f"no usable lab among {c.get('labs')} on {host}", "how": c.get("how"), "ref": c.get("ref")}
    out = {"capability": name, "lab": policy[pick]["hire"], "model_lab": pick, "vendor": policy[pick]["vendor"],
           "host": host, "how": c.get("how"), "ref": c.get("ref")}
    if c.get("restricted"):  # vault, production, the Founder's Chrome, public posting: never a ready-to-run line
        out["hire"] = None
        out["path"] = "Founder-approved path only: write the brief (plan gate / Linear ticket / #refusal as the 'how' says)"
    else:
        out["hire"] = f"HIRE_HOST={host} ~/Developer-artifacts/bin/hire.sh {policy[pick]['hire']} <BRIEF>.md \"<title>\""
    return out


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--capability":
        print(json.dumps(capability(sys.argv[2]), ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "--capabilities":
        print(json.dumps(sorted(json.load(open(CAPS_FILE)).get("capabilities", {})), ensure_ascii=False))
    else:
        a = sys.argv[1:]
        prefs = a[a.index("--prefs") + 1] if "--prefs" in a else ""
        pos = [x for i, x in enumerate(a) if x != "--prefs" and (i == 0 or a[i - 1] != "--prefs")]
        print(json.dumps(route(pos[0], pos[1] if len(pos) > 1 else "", prefs), ensure_ascii=False))
