#!/usr/bin/env python3
"""GUARDIAN: no-AI estate watch. Runs every 5 min from launchd on the Mac Pro.

Five guards, each aimed at a failure class that hurt us at night (playbook 10-lessons, 4-11 Oct 2026):
  ci        Forgejo runners online, runner daemons/containers + docker0 in each CI VM, disk fsync
            latency, stuck/starved jobs, orphan job containers.
  handoffs  B3-approved PRs left open, release captain WAIT_SENIOR, "-> Senior" / INFRA notes.
  lines     every line's pulse heartbeat, Senior cron loss (pulse-watch wakes), errored/stuck seats,
            provider outages (one error signature across many seats, e.g. Grok 402).
  hosts     Jupiter RAM/load/disk/OOM kills/guest state + reboots, dev-workers RAM, NAS, prod, Pro, mini disks.
  prod      public health of every product, Sphere DB pool saturation, production SSH reachable.

A finding that persists past its grace period files ONE Linear issue "GUARDIAN: <key>" (or comments on
the open one, at most hourly or when the evidence changes). When it clears for 2 runs in a row the issue
gets a RECOVERED comment and moves to Done. P0 findings also wake the standby Pulse Runner seat (at most
every 30 min), so nights are covered even when every Claude session's crons are dead.

Read-only everywhere except Linear and the local state files. Secrets: the Linear key is read from the
Keychain item named in the estate config (linear.key_item) and never printed.
Usage: guardian.py [--dry-run] [--only ci,hosts,...]
"""
import concurrent.futures as cf, datetime as dt, hashlib, json, os, re, socket, sqlite3, ssl, subprocess, sys, time
import urllib.request
import glob as _glob_mod
import urllib.parse

# Cloudflare refuses Python's default 'Python-urllib/3.x' UA (403, error 1010; 11 Oct). Every request sets one.
UA = "estate-guardian/1"
GH = "/opt/homebrew/bin/gh" if os.path.exists("/opt/homebrew/bin/gh") else "gh"
HOME = os.path.expanduser("~")
ART = f"{HOME}/Developer-artifacts"
STATE_DIR = f"{ART}/guardian"
STATE_PATH = f"{STATE_DIR}/state.json"
STATE = {}  # loaded in main(); guards may keep small memories here
STATUS_MD = f"{ART}/GUARDIAN-STATUS.md"
HEARTBEAT = f"{ART}/GUARDIAN-HEARTBEAT"
LOG = f"{STATE_DIR}/guardian.log"
HISTORY = f"{STATE_DIR}/history.jsonl"  # open/close events per finding: the autoresearch evaluator's ground truth
NUDGE_Q = f"{STATE_DIR}/senior-nudges.jsonl"  # GUARDIAN's session sends these to Senior by SendMessage
# Estate-specific values (hosts, IPs, users, IDs, repos, endpoints) live OUTSIDE the repo, in
# ~/Developer-artifacts/guardian/estate.local.json (override with ESTATE_CONFIG). See estate.example.json.
CFG = json.load(open(os.path.expanduser(os.environ.get("ESTATE_CONFIG", "~/Developer-artifacts/guardian/estate.local.json"))))
LINEAR_KEY_ITEM = CFG["linear"]["key_item"]
TEAM_KEY = CFG["linear"]["team_key"]
LABELS = CFG["linear"]["labels"]
OWNER_LABELS = CFG["linear"]["owner_labels"]  # owner line -> label keys; "guardian" = shared infra GUARDIAN fixes itself
REPO_OWNER = CFG["repo_owner"]  # who merges which repo (only the repo's owning line merges)
JUPITER, DEV_WORKERS, PRODUCTION = CFG["hosts"]["jupiter"], CFG["hosts"]["dev_workers"], CFG["hosts"]["production"]
NAS, IOS_MINI = CFG["hosts"]["nas"], CFG["hosts"]["ios_mini"]
FORGEJO, FJ_OWNER = CFG["forgejo_api"], CFG["forgejo_owner"]
EXPECTED_RUNNERS = {int(k): v for k, v in CFG["expected_runners"].items()}  # registrations that must be online
RUNNER_SCOPES = ["/user/actions/runners"] + [f"/repos/{FJ_OWNER}/{r}/actions/runners" for r in CFG["runner_scope_repos"]]
CI_REPOS = CFG["ci_repos"]
CI_VMS = {k: (v["daemons"], v["containers"], v["bridges"]) for k, v in CFG["ci_vms"].items()}
ALWAYS_RUNNING_GUESTS = CFG["always_running_guests"]
HEARTBEATS = [tuple(h) for h in CFG["heartbeats"]]  # file, owner, alarm minutes[, optional]
ENDPOINTS = [tuple(e) for e in CFG["endpoints"]]  # name, url, owner, body must contain

NOW = time.time()


def stamp(ts=None):
    return dt.datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d %H:%M %Z").strip() or "?"


def sh(args, inp=None, t=40):
    try:
        r = subprocess.run(args, input=inp, capture_output=True, text=True, timeout=t)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def ssh(host, script, t=45):
    return sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, script], t=t)


def kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def fj(path):
    tok_file = f"{HOME}/.config/forgejo/token"
    tok = open(tok_file).read().strip() if os.path.exists(tok_file) else sh(
        ["security", "find-generic-password", "-s", "com.tbaone.forgejo.api-token", "-w"])[1].strip()
    req = urllib.request.Request(FORGEJO + path, headers={"Authorization": f"token {tok}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def iso_age_min(s):
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if t.year < 2000:
            return None
        return (NOW - t.timestamp()) / 60
    except Exception:
        return None


class F:
    """A finding. grace = minutes it must persist before filing; p0 wakes the Pulse Runner."""
    def __init__(self, key, owner, title, detail, grace=10, p0=False, repeat=True, hold=0, nudge=False, amber=False, file=True):
        self.key, self.owner, self.title, self.detail = key, owner, title, detail
        self.amber = amber  # prevention-matrix early warning: Linear priority Low, never wakes anyone
        self.file = file  # False: status file only (the guard acts itself), no Linear issue
        self.grace, self.p0, self.repeat = grace, p0, repeat
        self.nudge = nudge  # Senior watch (Founder 11 Oct): wake Senior by message first, escalate after 15 min
        self.hold = hold  # hours it must stay clean before the issue auto-recovers (night-only failures)


# ---------------------------------------------------------------- guard 1: CI
CI_PROBE = r'''
echo uptime_s=$(cut -d. -f1 /proc/uptime)
echo runner_daemons=$(pgrep -c -f "forgejo-runner daemon" || true)
for b in docker0 sphere0; do ip link show $b >/dev/null 2>&1 && echo bridge_$b=1 || echo bridge_$b=0; done
for c in $EXPECT; do echo "ctr_$c=$(docker inspect -f '{{.State.Status}}' $c 2>/dev/null || echo missing)"; done
echo orphan_starts=$(docker ps -q --filter name=FORGEJO-ACTIONS-TASK 2>/dev/null | xargs -r docker inspect -f '{{.State.StartedAt}}' 2>/dev/null | tr '\n' ' ')
python3 - <<'PY'
import os, time
p = "/var/tmp/.guardian-fsync"; fd = os.open(p, os.O_WRONLY | os.O_CREAT); t = time.time()
for i in range(40): os.write(fd, b"x" * 512); os.fsync(fd)
os.close(fd); os.unlink(p); print("fsync_ms=%.2f" % ((time.time() - t) / 40 * 1000))
PY
'''


def guard_ci():
    out = []
    # Forgejo's own view of every runner registration.
    status = {}
    try:
        for scope in RUNNER_SCOPES:
            for r in fj(scope) or []:
                status[r["id"]] = r["status"]
    except Exception as e:
        return [F("forgejo-api", "guardian", "Forgejo API unreachable from the Pro",
                  f"{type(e).__name__}: {e}"[:300], grace=10, p0=True)]
    rc, guests, _ = ssh(JUPITER, "lxc list -f csv -c ns")
    gstate = dict(l.split(",", 1) for l in guests.split() if "," in l)
    expected = dict(EXPECTED_RUNNERS)
    wr = CFG.get("windows_runner")
    if wr and gstate.get(wr["guest"]) == "RUNNING":
        expected[wr["id"]] = wr["name"]
    for rid, name in expected.items():
        st = status.get(rid, "missing")
        if st in ("offline", "missing"):
            out.append(F(f"runner-offline-{rid}", "guardian", f"CI runner {rid} {name} is {st}",
                         f"Forgejo reports runner {rid} ({name}) as {st}. Jobs for its labels queue forever.",
                         grace=10, p0=True))
    # Inside each CI VM: daemons, containers, bridges, disk latency, orphans.
    for vm, (daemons, ctrs, bridges) in CI_VMS.items():
        if gstate.get(vm) != "RUNNING":
            out.append(F(f"guest-down-{vm}", "guardian", f"CI guest {vm} is {gstate.get(vm, 'missing')}",
                         f"lxc list on Jupiter: {vm} = {gstate.get(vm, 'missing')}", grace=5, p0=True))
            continue
        rc, txt, err = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", JUPITER,
                           f"lxc exec {vm} --env EXPECT='{' '.join(ctrs)}' -- sh -s"], inp=CI_PROBE, t=60)
        d = kv(txt)
        if rc != 0 and not d:
            out.append(F(f"ci-probe-{vm}", "guardian", f"Cannot probe CI guest {vm}", (err or "no output")[:300], grace=15))
            continue
        if int(d.get("runner_daemons", "0") or 0) < daemons:
            out.append(F(f"runner-daemon-{vm}", "guardian", f"{vm}: {d.get('runner_daemons')} of {daemons} runner daemons",
                         "forgejo-runner daemon processes missing", grace=10, p0=True))
        for c in ctrs:
            if d.get(f"ctr_{c}") != "running":
                out.append(F(f"runner-ctr-{c}", "guardian", f"Runner container {c} is {d.get(f'ctr_{c}')}",
                             f"in {vm}; lesson 10 Oct: check docker0 and why it stopped before restarting", grace=10, p0=True))
        for b in bridges:
            if d.get(f"bridge_{b}") != "1":
                out.append(F(f"bridge-{vm}-{b}", "guardian", f"{vm}: network bridge {b} missing",
                             "10 Oct: a second dockerd with --bridge=none deleted docker0 and killed Sphere CI for 17 h",
                             grace=5, p0=True))
        try:
            ms = float(d.get("fsync_ms", "0"))
            if ms > 8:
                out.append(F(f"fsync-slow-{vm}", "guardian", f"{vm}: disk fsync {ms:.1f} ms (limit 8)",
                             "Sphere's SQLite DDL tests time out above ~10 ms/fsync (11 Oct). Check mount "
                             "options (nobarrier, commit=30) and host I/O.", grace=15))
        except ValueError:
            pass
        old = [s for s in d.get("orphan_starts", "").split() if (iso_age_min(s) or 0) > 360]
        if old:
            out.append(F(f"orphan-jobs-{vm}", "guardian", f"{vm}: {len(old)} CI job containers older than 6 h",
                         "Orphaned job containers hold RAM and docker.sock (10 Oct infra red).", grace=30))
        up = int(d.get("uptime_s", "99999") or 99999)
        if up < 1200:
            out.append(F(f"reboot-{vm}", "guardian", f"{vm} rebooted {up // 60} min ago",
                         "Unplanned reboot? Check last -x and who restarted it.", grace=0, repeat=False))
    # Jobs stuck running or starved in the queue.
    for repo in CI_REPOS:
        try:
            runs = fj(f"/repos/{FJ_OWNER}/{repo}/actions/runs?page=1&limit=20").get("workflow_runs", [])
        except Exception:
            continue
        stuck = [r for r in runs if r["status"] == "running" and (iso_age_min(r.get("started", "")) or 0) > 75]
        starved = [r for r in runs if r["status"] == "waiting" and (iso_age_min(r.get("created", "")) or 0) > 60]
        if stuck:
            out.append(F(f"ci-stuck-{repo}", "guardian", f"{repo}: {len(stuck)} CI runs running > 75 min",
                         ", ".join(f"run {r['index_in_repo']} {r['title'][:50]}" for r in stuck[:5]), grace=0))
        if starved:
            out.append(F(f"ci-starved-{repo}", "guardian", f"{repo}: {len(starved)} CI runs waiting > 60 min for a runner",
                         ", ".join(f"run {r['index_in_repo']}" for r in starved[:8]), grace=0))
    return out


# ---------------------------------------------------------------- guard 2: handoffs
def guard_handoffs():
    out = []
    b3 = f"{ART}/RESULT-REVIEWER-B3.md"
    try:
        text = open(b3).read()
    except Exception:
        text = ""
    seen = set()
    for repo, num, sha in re.findall(r"APPROVE (" + "|".join(REPO_OWNER) + r") #(\d+) exact head ([0-9a-f]{40})", text):
        if (repo, num) in seen:
            continue
        seen.add((repo, num))
        try:
            pr = fj(f"/repos/{FJ_OWNER}/{repo}/pulls/{num}")
        except Exception:
            continue
        if pr.get("state") == "open" and pr["head"]["sha"] == sha:
            owner = REPO_OWNER.get(repo, "senior")
            prod_fix = bool(re.search(r"hotfix|\bP0\b|production|prod fix|revert|rollback", pr.get("title", ""), re.I))
            out.append(F(f"approved-open-{repo}-{num}", owner, f"B3-approved {repo}#{num} still open (owner: {owner})",
                         f"B3 approved exact head {sha[:8]}; the PR is open with that head. The {owner} line merges it "
                         f"(only the repo's owner merges; an internal ticket) or says why not. 10 Oct: an approved cut waited 7 h.",
                         grace=30, p0=prod_fix, nudge=owner == "senior"))
    for path in sorted(os.listdir(ART)):
        if path.startswith("RESULT-RELEASE-") and path.endswith(".md"):
            p = f"{ART}/{path}"
            if NOW - os.path.getmtime(p) > 86400:
                continue
            first = open(p).readline().strip()
            if first.startswith("WAIT_SENIOR"):
                out.append(F(f"captain-wait-{path}", "captain", f"Release captain waiting on Senior ({path})",
                             first[:400], grace=15, p0=True, nudge=True))
    notes = []
    for path in os.listdir(ART):
        if not (path.startswith("RESULT-") and path.endswith(".md")):
            continue
        p = f"{ART}/{path}"
        if NOW - os.path.getmtime(p) > 6 * 3600:
            continue
        for line in open(p, errors="replace"):
            # an address to Senior ("→ Senior:", "→ Senior (", "→ Senior." or at line end), not prose like "B3 → Senior merges"
            if re.search(r"(→|->) ?Senior(\s*[:(.,;]|\s*$)|INFRA BLOCKER|WAIT_SENIOR", line):
                notes.append(f"{path}: {line.strip()[:300]}")
    seen = STATE.setdefault("notes_seen", None)
    hashes = {hashlib.sha1(n.encode()).hexdigest()[:8]: n for n in notes}
    if seen is None:  # first run: everything already in the files counts as known (0 = handled)
        STATE["notes_seen"] = {h: 0 for h in hashes}
    else:
        for h, n in hashes.items():
            first = seen.setdefault(h, NOW)
            snippet = n.split(": ", 1)[-1][:40]
            if NOW - first < 6 * 3600 and not senior_seen_since(first, snippet):
                out.append(F(f"senior-note-{h}", "senior", f"Note to Senior unanswered: {n[:70]}", n, grace=30,
                             repeat=False, nudge=True))
        for h in [h for h in seen if h not in hashes]:  # forget notes that left the files
            del seen[h]
    out += senior_session_checks()
    return out


@__import__("functools").lru_cache(maxsize=1)
def senior_transcript():
    """Senior's Claude session transcript: the newest one that holds GUARDIAN's messages to Senior."""
    d = os.path.expanduser(CFG["claude_projects_dir"])
    files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")), key=os.path.getmtime, reverse=True)
    for f in files[:25]:
        if NOW - os.path.getmtime(f) > 86400:
            break
        for line in open(f, errors="replace"):  # a peer message from GUARDIAN to Senior, received in that session
            if "GUARDIAN → Senior" in line and '"kind":"peer"' in line:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "user" and (r.get("origin") or {}).get("kind") == "peer":
                    return f
    return None


def senior_seen_since(since, snippet):
    """True if Senior's session saw the snippet (e.g. its pulse printed the note) after `since`."""
    f = senior_transcript()
    if not f or not snippet:
        return False
    for line in open(f, errors="replace"):
        if snippet in line:
            m = re.search(r'"timestamp":"([^"]+)"', line)
            if m and dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp() >= since:
                return True
    return False


def senior_session_checks():
    out, f = [], senior_transcript()
    if not f:
        return out
    last_asst, inbound, ahead = 0.0, [], []
    for line in open(f, errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = r.get("timestamp")
        if not ts:
            continue
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        if NOW - t > 6 * 3600:
            continue
        if r.get("type") == "assistant":
            last_asst = max(last_asst, t)
            for c in r.get("message", {}).get("content", []) or []:
                if c.get("type") != "tool_use":
                    continue
                inp = c.get("input", {})
                body = str(inp.get("body") or inp.get("message") or inp.get("description") or "")
                m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", body[:60])
                if m:
                    local = dt.datetime.fromtimestamp(t)
                    stamp_t = local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0).timestamp()
                    if 180 < stamp_t - t < 5400 and NOW - t < 7200 and body[:60] not in STATE.get("clock_acked", []):
                        ahead.append(f"{body[:60]!r} written at {local:%H:%M:%S} ({(stamp_t - t) / 60:.0f} min ahead)")
        elif r.get("type") == "user" and (r.get("origin") or {}).get("kind") == "peer":
            frm = re.search(r"from-name=\"([^\"]{1,60})", str(r.get("message", {}).get("content", "")))
            inbound.append((t, frm.group(1) if frm else "?"))
    unread = [(t, n) for t, n in inbound if t > last_asst and NOW - t > 1800]
    if unread:
        out.append(F("senior-inbox", "senior", f"{len(unread)} cross-session messages to Senior unread > 30 min",
                     "\n".join(f"- from {n} at {dt.datetime.fromtimestamp(t):%H:%M}" for t, n in unread) +
                     f"\nSenior's last turn: {dt.datetime.fromtimestamp(last_asst):%H:%M}" if last_asst else "",
                     grace=0, p0=True, nudge=True))
    if ahead:
        out.append(F("senior-clock", "senior", "Senior wrote timestamps ahead of the real clock",
                     "\n".join(f"- {a}" for a in ahead[-5:]) + "\nRule: times come from `date` in the same command.",
                     grace=0, repeat=False, nudge=True))
    return out


# ---------------------------------------------------------------- guard 3: lines and seats
SEAT_SQL = """
select substr(t.thread_id,1,8), replace(substr(t.title,1,50),'|','/'), s.status, coalesce(s.provider_name,''),
  replace(replace(substr(coalesce(s.last_error,''),1,220),char(10),' '),'|','/'), s.updated_at,
  coalesce((select max(created_at) from projection_thread_messages m where m.thread_id=t.thread_id),''),
  coalesce(json_extract(t.model_selection_json,'$.model'),'')
from projection_threads t join projection_thread_sessions s on s.thread_id=t.thread_id
where t.archived_at is null and t.title not like 'GUARDIAN%' and (s.status in ('error','running')) and s.updated_at > strftime('%Y-%m-%dT%H:%M:%fZ','now','-12 hours');
"""


def seat_rows():
    rows = []
    try:
        con = sqlite3.connect(f"file:{HOME}/.t3/userdata/state.sqlite?mode=ro", uri=True, timeout=5)
        for r in con.execute(SEAT_SQL):
            rows.append(("pro",) + tuple(str(x) for x in r))
    except Exception:
        pass
    for host, label, bin_ in ((DEV_WORKERS, "jup", "sqlite3"), (IOS_MINI, "mini", "/usr/bin/sqlite3")):
        rc, txt, _ = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                         f"{bin_} -readonly ~/.t3/userdata/state.sqlite"], inp=SEAT_SQL, t=30)
        for line in txt.splitlines():
            parts = line.split("|")
            if len(parts) == 8:
                rows.append((label,) + tuple(parts))
    return rows


def signature(err):
    m = re.search(r'"status":\s*(\d{3})', err)
    msg = re.sub(r"[0-9a-f]{8,}|\d{4,}", "#", re.sub(r'.*"message":\s*"', "", err))[:100]
    return (m.group(1) if m else "") + " " + msg


# Labs the Founder matrix marks OUT and GUARDIAN probes on purpose (GUARDIAN.md §6): their errors are expected,
# not seat failures. 11 Oct 10:2x false positive: the Sol daily probe + a Sol modelcheck counted as "2 seats errored".
EXPECTED_OUT = [r"gpt-6\.1-sol"]


def expected_out(err):
    return any(re.search(p, err or "") for p in EXPECTED_OUT)


LABS_FILE = f"{ART}/guardian/labs.json"
LAB_CHANGES = f"{ART}/guardian/lab-changes.jsonl"
USAGE_RE = re.compile(r"\b402\b|payment required|usage balance|quota|rate.?limit|\b429\b|insufficient|exceeded|"
                      r"not supported when using|usage limit", re.I)


def lab_usage_switch(errs):
    """Founder 11 Oct: labs switch OFF by themselves when their usage runs out; fallbacks take over (worker_route.py).
    >=2 seats of one model failing with a usage-type error in 3 h -> every ON lab on that model goes OFF (reason usage)."""
    if "--dry-run" in sys.argv:
        return
    by_model = {}
    for r in errs:
        if USAGE_RE.search(r[5] or "") and len(r) > 8 and r[8]:
            by_model.setdefault(r[8], []).append(r)
    hit = {m: rs for m, rs in by_model.items() if len(rs) >= 2}
    if not hit:
        return
    try:
        doc = json.load(open(LABS_FILE))
    except Exception:
        return
    changed = []
    for k, v in doc["labs"].items():
        if v.get("model") in hit and v.get("switch") == "ON":
            v.update(switch="OFF", reason="usage", since=stamp(),
                     evidence=f"{len(hit[v['model']])} seats: {signature(hit[v['model']][0][5])[:80]}")
            changed.append(k)
    if changed:
        json.dump(doc, open(LABS_FILE + ".new", "w"), indent=1)
        os.replace(LABS_FILE + ".new", LABS_FILE)
        with open(LAB_CHANGES, "a") as f:
            f.write(json.dumps({"at": stamp(), "labs": changed, "switch": "OFF", "reason": "usage"}) + "\n")


def refresh_lab_readiness():
    """Per-host lab readiness from Tentacles doctor (T3 + Tentacles are the live truth), every 30 min."""
    if "--dry-run" in sys.argv or NOW - STATE.get("readiness_at", 0) < 1800:
        return
    STATE["readiness_at"] = NOW
    out = {}
    for host, cmd in (("pro", [f"{HOME}/.local/bin/tentacles", "doctor", "--json", "--models", "summary"]),
                      ("jupiter", [f"{HOME}/.local/bin/tentacles-computer", "doctor", "--computer", "jupiter", "--json", "--models", "summary"])):
        rc, txt, _ = sh(cmd, t=90)
        try:
            out[host] = {l["instanceId"]: bool(l.get("ready")) for l in json.loads(txt)["labs"]}
        except Exception:
            continue
    if out:
        p = f"{ART}/guardian/lab-readiness.json"
        json.dump(dict(out, at=stamp()), open(p + ".new", "w"), indent=1)
        os.replace(p + ".new", p)


USAGE_SNAPSHOT = f"{ART}/guardian/lab-usage.json"
CODEXBAR = "/opt/homebrew/bin/codexbar"  # open-source CodexBar (steipete) CLI: plan usage per provider
USAGE_PROVIDERS = {"claude": ["opus", "sonnet", "haiku"], "codex": ["luna", "luna-medium"], "cursor": ["cursor"], "grok": ["grok"]}


def refresh_lab_usage():
    """Founder 11 Oct: switch labs OFF before their usage runs out and back ON at the reset, using CodexBar's plan data.
    OFF when a window is >=98% used, or >=90% and CodexBar's pace says it will not last to the reset; 'until' = resetsAt.
    Kill switch: guardian/codexbar.disabled (set automatically if CodexBar ever reports a keychain/cookie problem)."""
    if NOW - STATE.get("usage_at", 0) < 900 or os.path.exists(f"{ART}/guardian/codexbar.disabled") or not os.path.exists(CODEXBAR):
        return
    STATE["usage_at"] = NOW
    snap = {}
    for prov in USAGE_PROVIDERS:
        rc, txt, _ = sh([CODEXBAR, "usage", "--json-only", "--no-color", "--provider", prov], t=60)
        try:
            d = json.loads(txt)
            d = d[0] if isinstance(d, list) else d
        except Exception:
            continue
        err = (d.get("error") or {}).get("message", "")
        if re.search(r"keychain|safe storage", err, re.I) and "--dry-run" not in sys.argv:
            open(f"{ART}/guardian/codexbar.disabled", "w").write(f"{stamp()} {prov}: {err[:200]}\n")
            return
        u, pace = d.get("usage") or {}, d.get("pace") or {}
        wins = [(k, u[k]) for k in ("primary", "secondary", "tertiary") if u.get(k) and u[k].get("usedPercent") is not None]
        if not wins:
            continue
        k, w = max(wins, key=lambda kw: kw[1]["usedPercent"])
        lasts = (pace.get(k) or {}).get("willLastToReset", True)
        snap[prov] = {"usedPercent": w["usedPercent"], "window": k, "resetsAt": w.get("resetsAt"), "lastsToReset": lasts}
    if not snap:
        return
    snap["at"] = stamp()
    if "--dry-run" in sys.argv:
        STATE["usage_preview"] = snap
        return
    json.dump(snap, open(USAGE_SNAPSHOT + ".new", "w"), indent=1)
    os.replace(USAGE_SNAPSHOT + ".new", USAGE_SNAPSHOT)
    try:
        doc = json.load(open(LABS_FILE))
    except Exception:
        return
    changes = []
    for prov, labs in USAGE_PROVIDERS.items():
        x = snap.get(prov)
        if not x:
            continue
        exhausted = x["usedPercent"] >= 98 or (x["usedPercent"] >= 90 and not x["lastsToReset"])
        for lab in labs:
            v = doc["labs"].get(lab)
            if not v:
                continue
            if exhausted and v.get("switch") == "ON":
                v.update(switch="OFF", reason="usage", since=stamp(), until=x["resetsAt"],
                         evidence=f"CodexBar {prov} {x['window']} {x['usedPercent']}% used, resets {x['resetsAt']}")
                changes.append((lab, "OFF"))
            elif not exhausted and v.get("switch") == "OFF" and v.get("reason") == "usage" and x["usedPercent"] < 80:
                v.update(switch="ON", reason=None, since=stamp(), until=None, evidence=f"CodexBar {prov} {x['usedPercent']}% used")
                changes.append((lab, "ON"))
    if changes:
        json.dump(doc, open(LABS_FILE + ".new", "w"), indent=1)
        os.replace(LABS_FILE + ".new", LABS_FILE)
        with open(LAB_CHANGES, "a") as f:
            for lab, sw in changes:
                f.write(json.dumps({"at": stamp(), "labs": [lab], "switch": sw, "reason": "usage (CodexBar)"}) + "\n")


FORBIDDEN_MODEL_SQL = """select t.thread_id, replace(substr(t.title,1,50),'|','/'), s.status,
  coalesce(json_extract(t.model_selection_json,'$.model'),'')
from projection_threads t join projection_thread_sessions s on s.thread_id=t.thread_id
where t.archived_at is null and (t.model_selection_json like '%astra%' or t.model_selection_json like '%fable%');
"""


def forbidden_model_seats():
    """Founder rule: Astra and Fable never run. Tentacles' default models for codex/claudeAgent are exactly those, so a raw
    originate without --model starts one (Senior 11 Oct: guard/tentacles blocks it; raw ~/.local/bin/tentacles bypasses).
    Any live seat on them -> P0; a running one is stopped (session stop, not archived, so it stays reviewable)."""
    out, hits = [], []
    try:
        con = sqlite3.connect(f"file:{HOME}/.t3/userdata/state.sqlite?mode=ro", uri=True, timeout=5)
        hits += [("pro",) + tuple(r) for r in con.execute(FORBIDDEN_MODEL_SQL)]
    except Exception:
        pass
    for host, label, bin_ in ((DEV_WORKERS, "jupiter", "sqlite3"), (IOS_MINI, "mini", "/usr/bin/sqlite3")):
        rc, txt, _ = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"{bin_} -readonly ~/.t3/userdata/state.sqlite"],
                        inp=FORBIDDEN_MODEL_SQL, t=30)
        hits += [(label,) + tuple(l.split("|")) for l in txt.splitlines() if l.count("|") == 3]
    for host, tid, title, status, model in hits:
        stopped = ""
        if status in ("running", "starting") and "--dry-run" not in sys.argv and host in ("pro", "jupiter"):
            intent = json.dumps({"action": "thread.session.stop", "threadId": tid})
            p = f"{STATE_DIR}/stop-{tid[:8]}.json"
            open(p, "w").write(intent)
            cmd = [f"{HOME}/.local/bin/tentacles-computer", "act", "--computer", "jupiter", "--intent-file", p] if host == "jupiter" \
                else [f"{HOME}/.local/bin/tentacles", "act", "--intent-file", p]
            rc, _, _ = sh(cmd, t=60)
            os.remove(p)
            stopped = " — session STOPPED by GUARDIAN" if rc == 0 else " — stop FAILED"
        out.append(F(f"forbidden-model-{tid[:8]}", "senior", f"Seat on a forbidden model: {model} ({host} {tid[:8]})",
                     f"{host} seat {tid[:8]} '{title}' runs {model} (status {status}){stopped}. Founder rule: Astra and Fable never "
                     "run. Archive it and re-hire the work through bin/hire-auto.sh (always passes --model).",
                     grace=0, p0=True, repeat=False, nudge=True))
    return out


def guard_lines():
    refresh_lab_readiness()
    refresh_lab_usage()
    out = []
    paused = CFG.get("paused_lines") or {}  # Founder-paused pulses (e.g. Syfon 12 Oct) are not stale
    for name, owner, limit, *optional in HEARTBEATS:
        p = f"{ART}/{name}"
        if owner in paused or (optional and not os.path.exists(p)):
            continue
        try:
            v = int(re.sub(r"\D", "", open(p).read())[:10])
        except Exception:
            v = 0
        age = (NOW - v) / 60
        if age > limit:
            # A line without a durable watchdog task flaps: one heartbeat refresh closed an internal ticket, the next stall
            # filed an internal ticket. Hold its issue open 24 h after recovery until the watchdog task exists.
            task = "jack" if owner == "senior" else owner
            durable = os.path.exists(os.path.expanduser(f"~/.claude/scheduled-tasks/{task}-pulse-watchdog/SKILL.md"))
            out.append(F(f"heartbeat-{name}", owner, f"{name} is {int(age)} min old (limit {limit})",
                         f"The {owner} line's pulse has not run. Its session crons may be dead; re-create them. "
                         "A durable task fires nothing while one of its runs is still 'running' (12 Oct: the GUARDIAN "
                         "pulse run grew into a 539-message session, stuck in Bash for 21 h): stop that run in the task's Runs pane." +
                         ("" if durable else f" No durable watchdog (~/.claude/scheduled-tasks/{task}-pulse-watchdog): "
                          "session crons starve while busy; add one."),
                         grace=0, p0=owner == "senior", nudge=owner == "senior", hold=0 if durable else 24))
    try:
        wakes = [l for l in open(f"{ART}/PULSE-WATCH.log") if l[:16] >=
                 dt.datetime.fromtimestamp(NOW - 7200).strftime("%Y-%m-%d %H:%M")]
        if len(wakes) >= 3:
            out.append(F("senior-crons-dead", "senior", f"Senior pulse crons dead: {len(wakes)} watchdog wakes in 2 h",
                         "pulse-watch keeps waking the Pulse Runner; the Senior session's own :09/:39 and :24/:54 "
                         "crons are not firing. The runner has no Linear access, so the Linear inbox goes unread.",
                         grace=0, hold=48))  # Senior 11 Oct: close only after 2 clean nights
    except FileNotFoundError:
        pass
    rows = seat_rows()
    lab_usage_switch([r for r in rows if r[3] == "error" and (iso_age_min(r[6]) or 0) < 180])
    errs = [r for r in rows if r[3] == "error" and (iso_age_min(r[6]) or 0) < 180 and not expected_out(r[5])]
    sigs = {}
    for r in errs:
        sigs.setdefault((r[4], signature(r[5])), []).append(r)
    for (prov, sig), rs in sorted(sigs.items(), key=lambda x: -len(x[1])):
        if len(rs) >= 3 and f"provider-{prov or 'unknown'}" not in {f.key for f in out}:
            out.append(F(f"provider-{prov or 'unknown'}", "senior",
                         f"Provider outage: {len(rs)} {prov or 'unknown'} seats failing with the same error",
                         f"Signature: `{sig}`\nSeats: " + ", ".join(f"{r[0]}:{r[1]} {r[2][:30]}" for r in rs[:12]) +
                         "\nRe-route these lanes to a working lab (hire.sh) instead of retrying.", grace=0, p0=True))
    lone = [r for r in errs if len(sigs[(r[4], signature(r[5]))]) < 3 and (iso_age_min(r[6]) or 0) > 20]
    if lone:
        out.append(F("seats-errored", "senior", f"{len(lone)} seats errored in the last 3 h",
                     "\n".join(f"- {r[0]}:{r[1]} {r[2]} ({r[4]}): {r[5][:140]}" for r in lone[:15]), grace=15))
    stuck = [r for r in rows if r[3] == "running" and (iso_age_min(r[7]) or 0) > 60 and (iso_age_min(r[6]) or 0) > 60]
    if stuck:
        out.append(F("seats-stuck", "senior", f"{len(stuck)} seats 'running' with no output for > 60 min",
                     "\n".join(f"- {r[0]}:{r[1]} {r[2]} last message {r[7]}" for r in stuck[:15]), grace=10))
    out += forbidden_model_seats()
    for host, label in ((DEV_WORKERS, "dev-workers"), (IOS_MINI, "iOS mini")):
        rc, txt, _ = ssh(host, "pgrep -f 't3 serve|T3 Code' >/dev/null && echo up || echo down", t=20)
        if txt.strip() == "down":
            out.append(F(f"t3-down-{label}", "guardian", f"T3 is not running on {label}",
                         "Seats on this host cannot run.", grace=10, p0=True))
    return out


# ---------------------------------------------------------------- guard 4: hosts
JUPITER_PROBE = r'''
echo mem_avail_kb=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
echo load15=$(cut -d' ' -f3 /proc/loadavg)
echo nproc=$(nproc)
echo disk_pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
echo oom=$(journalctl -k --since -10min --no-pager 2>/dev/null | grep -i 'killed process' | grep -c -v -i 'memory cgroup')
echo oom_cg=$(journalctl -k --since -10min --no-pager 2>/dev/null | grep -i 'killed process' | grep -c -i 'memory cgroup')
echo oom_who=$(journalctl -k --since -10min --no-pager 2>/dev/null | grep -i 'killed process' | sed -n 's/.*Killed process [0-9]* (\([^)]*\)).*/\1/p' | sort -u | tr '\n' ',')
for g in $(lxc list -f csv -c n); do echo "guest_$g=$(lxc list $g -f csv -c s)"; done
'''


def guard_hosts():
    out = []
    rc, txt, err = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", JUPITER, "sh -s"], inp=JUPITER_PROBE, t=45)
    d = kv(txt)
    if not d:
        out.append(F("jupiter-unreachable", "guardian", f"Jupiter unreachable over SSH ({JUPITER.split('@')[-1]})",
                     (err or "no output")[:300], grace=10, p0=True))
    else:
        avail = int(d.get("mem_avail_kb", "0") or 0) / 1048576
        if avail < 10:
            out.append(F("jupiter-ram", "guardian", f"Jupiter MemAvailable {avail:.1f} GiB (floor 10)",
                         "Three host OOMs 9-10 Oct. Stop seat-run suites, drain CI overflow (playbook 16).",
                         grace=0 if avail < 6 else 10, p0=avail < 6))
        load, cpus = float(d.get("load15", "0")), int(d.get("nproc", "32"))
        if load > 1.5 * cpus:
            out.append(F("jupiter-load", "guardian", f"Jupiter load15 {load:.0f} on {cpus} CPUs",
                         "Over the 1.5x budget; step CI back to two general slots (playbook 16).", grace=10))
        if int(d.get("disk_pct", "0") or 0) > 85:
            out.append(F("jupiter-disk", "guardian", f"Jupiter / at {d['disk_pct']}%", "limit 85%", grace=0))
        if int(d.get("oom", "0") or 0) > 0:
            out.append(F("jupiter-oom", "guardian", f"Jupiter host OOM kills: {d['oom']} in 10 min ({d.get('oom_who', '')})",
                         "Find which guest/process; a killed runner or seat loses its work.", grace=0, p0=True))
        elif int(d.get("oom_cg", "0") or 0) > 0:
            # A kill inside a capped cgroup (MemoryMax) is contained: the host is not short (11 Oct wibes-comfy).
            out.append(F("jupiter-oom-cgroup", "guardian",
                         f"Jupiter cgroup-capped OOM kills: {d['oom_cg']} in 10 min ({d.get('oom_who', '')})",
                         "Contained by the unit's MemoryMax; owner lowers the job's footprint or the cap.", grace=10))
        for g in ALWAYS_RUNNING_GUESTS:
            if g not in CI_VMS and d.get(f"guest_{g}") != "RUNNING":
                out.append(F(f"guest-down-{g}", "guardian", f"Jupiter guest {g} is {d.get(f'guest_{g}', 'missing')}",
                             "lxc list", grace=5, p0=g in ("dev-workers", "staging")))
    rc, txt, _ = ssh(DEV_WORKERS, "awk '/MemAvailable/{print $2}' /proc/meminfo", t=20)
    if txt.strip().isdigit() and int(txt) / 1048576 < 1.5:
        out.append(F("devworkers-ram", "guardian", f"dev-workers MemAvailable {int(txt) / 1048576:.1f} GiB",
                     "Seats get OOM-killed below this (9 Oct 23:03).", grace=5, p0=True))
    rc, txt, _ = ssh(NAS, "df -P /volume1 | tail -1", t=25)
    m = re.search(r"(\d+)%", txt)
    if m and int(m.group(1)) >= 90:
        out.append(F("nas-disk", "guardian", f"NAS /volume1 at {m.group(1)}%",
                     "Forgejo, backups and mirrors live here. Growth was +77 GiB/day on 8 Oct.", grace=0, p0=int(m.group(1)) >= 95))
    rc, txt, _ = ssh(PRODUCTION, "df -P / | tail -1", t=25)
    m = re.search(r"(\d+)%", txt)
    if m and int(m.group(1)) >= 85:
        out.append(F("prod-disk", "senior", f"Production / at {m.group(1)}%", "3 Oct ENOSPC outage.",
                     grace=0, p0=int(m.group(1)) >= 92))
    rc, txt, _ = sh(["df", "-g", "/System/Volumes/Data"])
    try:
        free = int(txt.splitlines()[-1].split()[3])
        if free < 20:
            out.append(F("pro-disk", "guardian", f"Mac Pro free disk {free} GiB (floor 20)", "", grace=0))
    except Exception:
        pass
    rc, txt, _ = ssh(IOS_MINI, "df -g / | tail -1", t=20)
    try:
        free = int(txt.split()[3])
        if free < 15:
            out.append(F("mini-disk", "guardian", f"iOS mini free disk {free} GiB (floor 15)", "", grace=0))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- guard 5: production
def guard_prod():
    out = []
    for name, url, owner, must in ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read(4000).decode("utf-8", "replace").replace(" ", "")
                ok, why = (r.status == 200 and must.replace(" ", "") in body), f"HTTP {r.status}"
                if name == "Sphere":
                    try:
                        pool = json.loads(body).get("database_pool", {})
                        if pool.get("utilization", 0) >= 0.8:
                            out.append(F("sphere-pool", "senior", f"Sphere DB pool {pool['utilization']:.0%} used",
                                         "Lock convoy signal (10 Oct: FOR UPDATE on read checks turned DB timeouts "
                                         f"into 'revoked'). Pool: {pool}", grace=5, p0=True))
                    except Exception:
                        pass
        except Exception as e:
            ok, why = False, f"{type(e).__name__}: {str(e)[:120]}"
        if not ok:
            out.append(F(f"prod-down-{name.lower().replace(' ', '-')}", owner, f"{name} production health failing",
                         f"{url} -> {why}", grace=9, p0=True))
    # Served TLS certificate per host: 4 Oct a stale static cert in traefik-dynamic overrode ACME (sign-in 500 for 20 h).
    for host in sorted({u.split("/")[2] for _, u, _, _ in ENDPOINTS} ):
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
                days = (ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"]) - NOW) / 86400
            if days < 14:
                out.append(F(f"tls-expiry-{host}", "senior", f"TLS certificate for {host} expires in {days:.0f} days",
                             "Check ACME renewal and stale static certs in traefik-dynamic.", grace=0, p0=days < 3))
        except ssl.SSLCertVerificationError as e:
            out.append(F(f"tls-invalid-{host}", "senior", f"TLS certificate for {host} is invalid",
                         f"{e.verify_message if hasattr(e, 'verify_message') else e}"[:200], grace=10, p0=True))
        except Exception:
            pass  # reachability is the endpoint check's job
    # traefik-dynamic must stay 700; a CN deploy re-moded it to 755 on 11 Oct.
    rc, txt, _ = ssh(PRODUCTION, f"stat -c %a {CFG['production']['edge_config_dir']}", t=20)
    if txt.strip() and txt.strip() != "700":
        out.append(F("traefik-dynamic-mode", "senior", f"Production traefik-dynamic is mode {txt.strip()} (must be 700)",
                     "holds TLS and routing config. 11 Oct the CN deploy re-moded it to 755.", grace=0))
    rc, _, _ = sh(["nc", "-z", "-G", "5", PRODUCTION.split("@")[1], "22"], t=15)
    if rc != 0:
        out.append(F("prod-ssh", "senior", "Production SSH (Tailscale :22) refused",
                     "10 Oct: fail2ban locked out the Pro and Jupiter for 16 min (legacy pairer SSH every 20 s).", grace=10))
    return out


# ---------------------------------------------------------------- guard 6: prevention matrix (predict)
RISK_MD = f"{ART}/RISK-MATRIX.md"

PROD_PM2_PROBE = r'''
python3 - <<'PY'
import json, os, re, subprocess, time, glob, datetime
now = time.time()
BTIME = int(next(l for l in open("/proc/stat") if l.startswith("btime")).split()[1])
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout
god = [p for p in sh("pgrep -u " + __import__("getpass").getuser() + " -f '^PM2 v[0-9.]+: God Daemon'").split() if p]
live = []
for g in god[:1]:
    for c in sh(f"pgrep -P {g}").split():
        try:
            st = open(f"/proc/{c}/stat").read().rsplit(")", 1)[1].split()  # exact start: btime + starttime ticks (etimes jitters 1 s -> duplicate keys)
            et = now - (BTIME + int(st[19]) / os.sysconf("SC_CLK_TCK")); cwd = os.readlink(f"/proc/{c}/cwd")
            args = open(f"/proc/{c}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        live.append({"cwd": cwd, "start": int(now - et), "sphere_api": "/releases/sphere/" in cwd and "services/sphere-api" in cwd and "uvicorn" in args})
try:
    dump_cwds = [a.get("pm_cwd", "") for a in json.load(open(os.path.expanduser("~/.pm2/dump.pm2")))]  # cwd only, never env
except Exception:
    dump_cwds = []
locked = []
# restore-YYYYMMDD.log (old) and reviewed-start.log (script since 11 Oct 11:19, no date in name: day from mtime)
for f in glob.glob(os.path.expanduser("__CUSTODY_DIR__/*/restore-*.log")) + glob.glob(os.path.expanduser("__CUSTODY_DIR__/*/reviewed-start.log")):
    m = re.search(r"restore-(\d{8})\.log$", f)
    mt = datetime.datetime.fromtimestamp(os.path.getmtime(f))
    if not m and not f.endswith("/reviewed-start.log"): continue
    for line in open(f, errors="replace"):
        mm = re.match(r"(\d\d:\d\d:\d\d) LOCKED", line)
        if mm:
            if m:
                ts = datetime.datetime.strptime(m.group(1) + mm.group(1), "%Y%m%d%H:%M:%S")
            else:
                ts = datetime.datetime.combine(mt.date(), datetime.datetime.strptime(mm.group(1), "%H:%M:%S").time())
                if ts > mt + datetime.timedelta(seconds=60): ts -= datetime.timedelta(days=1)
            locked.append(int(ts.timestamp()))
listeners = []
for line in sh("ss -ltnpH").splitlines():
    m = re.search(r":(\d+)\s.*pid=(\d+)", line)
    if not m: continue
    port, pid = int(m.group(1)), m.group(2)
    try:
        ppid = int(sh(f"ps -o ppid= -p {pid}").strip() or 0)
        cg = open(f"/proc/{pid}/cgroup").read()
        comm = sh(f"ps -o comm= -p {pid}").strip()[:20]
    except Exception:
        continue
    supervised = (god and str(ppid) == god[0]) or ".service" in cg or "docker" in cg or "containerd" in cg
    listeners.append({"port": port, "pid": int(pid), "ppid": ppid, "comm": comm, "supervised": bool(supervised)})
print(json.dumps({"live": live, "dump_cwds": dump_cwds, "reviewed_locked": sorted(locked), "listeners": listeners}))
PY
'''
PROD_PM2_PROBE = PROD_PM2_PROBE.replace("__CUSTODY_DIR__", CFG["production"]["custody_dir"])


def cached(name, ttl, fn):
    c = STATE.setdefault("cache", {}).get(name)
    if c and NOW - c["t"] < ttl:
        return c["v"]
    v = fn()
    STATE["cache"][name] = {"t": NOW, "v": v}
    return v


def disk_pct(host):
    cmd = {"nas": (NAS, "df -P /volume1 | tail -1"), "production": (PRODUCTION, "df -P / | tail -1"),
           "jupiter": (JUPITER, "df -P / | tail -1")}[host]
    # Used/size in blocks, not the rounded Use% column: on a 1.8 TB disk one integer tick is 18 GB,
    # and a single tick inside the trend window projected "95% in 8.9 days".
    m = re.search(r"\s(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%", ssh(*cmd, t=25)[1])
    return round(100 * int(m.group(2)) / int(m.group(1)), 2) if m and int(m.group(1)) else None


def workflow_labels(repo):
    labels = set()
    for d in (".forgejo/workflows", ".github/workflows"):
        try:
            files = fj(f"/repos/{FJ_OWNER}/{repo}/contents/{d}")
        except Exception:
            continue
        for f in files if isinstance(files, list) else []:
            try:
                tok_file = f"{HOME}/.config/forgejo/token"
                req = urllib.request.Request(f["download_url"], headers={"Authorization": "token " + open(tok_file).read().strip(), "User-Agent": UA})
                txt = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
            except Exception:
                continue
            for lab in re.findall(r"runs-on:\s*\[?\s*([A-Za-z0-9_.-]+)", txt):
                labels.add(lab)
        if labels:
            break  # Forgejo uses .forgejo/workflows when it exists
    return sorted(labels)


def label_coverage():
    online = {"*": set()}
    for scope in ["/admin/actions/runners", "/user/actions/runners"]:
        try:
            for r in fj(scope) or []:
                if r["status"] != "offline":
                    online["*"].update(r["labels"])
        except Exception:
            pass
    gaps = []
    for repo in CI_REPOS:
        mine = set(online["*"])
        try:
            for r in fj(f"/repos/{FJ_OWNER}/{repo}/actions/runners") or []:
                if r["status"] != "offline":
                    mine.update(r["labels"])
        except Exception:
            pass
        exempt = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk_matrix.json"))).get("label_exemptions", {})
        for lab in workflow_labels(repo):
            if lab not in mine and not lab.startswith("${") and lab not in exempt:
                gaps.append(f"{repo}: runs-on {lab}")
    return gaps


IP_PIN_PROBE = CFG["probes"]["ip_pin"]  # estate-specific shell (config): live runner configs naming the dead IP

APT_PROBE = CFG["probes"]["apt_pending"]  # estate-specific shell (config): pending docker/containerd/lxd upgrades


def guard_predict():
    out, rows = [], []
    # 1. disk days-to-full
    samples = STATE.setdefault("samples", {})
    for host in ("nas", "production", "jupiter"):
        pct = disk_pct(host)
        if pct is None:
            continue
        ss = [x for x in samples.get(host, []) if NOW - x[0] < 48 * 3600] + [[NOW, pct]]
        samples[host] = ss[-600:]
        # A trend needs 24 h of samples: a one-off install (e.g. an 18 GB venv) is a step, not a growth rate.
        old = [x for x in ss if NOW - x[0] >= 24 * 3600]
        days = None
        if old:
            slope = (pct - old[0][1]) / ((NOW - old[0][0]) / 86400)
            days = (95 - pct) / slope if slope > 0 else None
        # 12 Oct: model downloads (~110 GB in 1 h, then ~35 GB) are steps, not a steady rate; extrapolating them gave a
        # false P0 at 67 % with 522 GB free. RED only when it is close in time AND space; otherwise warn early.
        st = ("RED" if days is not None and (days < 2 or (days < 5 and pct >= 85)) else
              "AMBER" if days is not None and days < 14 else "GREEN")
        rows.append(("disk-days-to-full", host, f"{pct:.0f}%, " + (f"{days:.1f} days to 95%" if days else "no growth trend yet"), st))
        if st != "GREEN":
            out.append(F(f"risk-disk-{host}", "guardian", f"{host} disk reaches 95% in {days:.1f} days (now {pct:.0f}%)",
                         "Prevention: find the grower, archive to the NAS then prune regenerable data.", grace=0,
                         p0=st == "RED", amber=st == "AMBER"))
    # 2. Jupiter memory headroom
    rc, txt, _ = ssh(JUPITER, "awk '/MemAvailable/{print $2}' /proc/meminfo", t=20)
    if txt.strip().isdigit():
        gib = int(txt) / 1048576
        st = "AMBER" if 10 <= gib < 14 else ("RED" if gib < 10 else "GREEN")
        rows.append(("jupiter-mem-headroom", "jupiter", f"{gib:.1f} GiB available", st))
        if st == "AMBER":
            out.append(F("risk-jupiter-mem", "guardian", f"Jupiter headroom {gib:.1f} GiB (amber < 14, alarm < 10)",
                         "Prevention: no seat-run suites, no new CI capacity; find the guest that grew.", grace=15, amber=True))
    # 3. runner label coverage (hourly)
    gaps = cached("label_cov", 3600, label_coverage)
    rows.append(("runner-label-coverage", "forgejo", "; ".join(gaps) or "every runs-on label has an online runner", "RED" if gaps else "GREEN"))
    if gaps:
        out.append(F("risk-label-coverage", "guardian", f"{len(gaps)} workflow labels have no online runner",
                     "\n".join(f"- {g}" for g in gaps) + "\nJobs with these labels will queue forever. Re-point or restore before they run.", grace=0))
    # 4. IP pins on the dead .20 interface (hourly)
    pins = cached("ip_pins", 3600, lambda: [l for l in sh(["ssh", "-o", "BatchMode=yes", JUPITER, "sh -s"], inp=IP_PIN_PROBE, t=60)[1].splitlines() if l.strip()])
    rows.append(("ip-pin", "CI runner configs", ", ".join(pins) or f"no {CFG['dead_ip']} references", "RED" if pins else "GREEN"))
    if pins:
        out.append(F("risk-ip-pin", "guardian", f"{len(pins)} runner configs still name {CFG['dead_ip']}",
                     "\n".join(f"- {p}" for p in pins), grace=0))
    # 5. pending night upgrades of docker/containerd/lxd (hourly)
    pend = cached("apt_pending", 3600, lambda: [l for l in sh(["ssh", "-o", "BatchMode=yes", JUPITER, "sh -s"], inp=APT_PROBE, t=90)[1].splitlines() if l.strip()])
    rows.append(("night-upgrades", "jupiter + CI guests", ", ".join(pend) or "none pending", "AMBER" if pend else "GREEN"))
    if pend:
        out.append(F("risk-night-upgrades", "guardian", f"{len(pend)} daemon upgrades pending (unattended at night)",
                     "\n".join(f"- {p}" for p in pend) + "\nAn unattended dockerd/containerd/lxd restart kills running CI. Hold and upgrade in a drained window.",
                     grace=0, amber=True))
    # 6. TLS early warning (hourly)
    def tls_all():
        res = {}
        for host in sorted({u.split("/")[2] for _, u, _, _ in ENDPOINTS}):
            try:
                ctx = ssl.create_default_context()
                with socket.create_connection((host, 443), timeout=10) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
                    res[host] = round((ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"]) - time.time()) / 86400)
            except Exception:
                pass
        return res
    tls = cached("tls_days", 3600, tls_all)
    low = {h: d for h, d in tls.items() if d < 30}
    rows.append(("tls-early", "product hosts", ", ".join(f"{h} {d}d" for h, d in sorted(tls.items())), "AMBER" if low else "GREEN"))
    for h, d in low.items():
        if d >= 14:
            out.append(F(f"risk-tls-{h}", "senior", f"TLS for {h} expires in {d} days", "Check ACME renewal now.", grace=0, amber=True))
    # 7. Senior heartbeat early warning
    try:
        age = (NOW - int(re.sub(r"\D", "", open(f"{ART}/PULSE-HEARTBEAT").read())[:10])) / 60
        st = "AMBER" if 20 < age <= 30 else ("RED" if age > 30 else "GREEN")
        rows.append(("senior-heartbeat-early", "Senior", f"{age:.0f} min", st))
    except Exception:
        pass
    # 8. backups
    rc, txt, _ = ssh(JUPITER, "cat ~/.local/state/backup-status/status.json", t=25)
    try:
        d = json.loads(txt)
        bad = []
        for w, r in sorted(d.get("workloads", {}).items()):
            t = r.get("last_ok_at")
            age_h = (NOW - dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()) / 3600 if t else None
            if age_h is None or age_h >= r.get("fail_h", 24):
                bad.append(("RED", w, age_h, r.get("last_result")))
            elif age_h >= r.get("warn_h", 20) or r.get("last_result") not in ("OK", None):
                bad.append(("AMBER", w, age_h, r.get("last_result")))
        st = "RED" if any(b[0] == "RED" for b in bad) else ("AMBER" if bad else "GREEN")
        rows.append(("backup-age", "an internal ticket workloads", "; ".join(f"{w} {('%.0fh' % a) if a else 'never'} {lr}" for _, w, a, lr in bad) or "all within limits", st))
        if bad:
            out.append(F("backup-age", "guardian", f"Backups: {sum(b[0] == 'RED' for b in bad)} failing, {sum(b[0] == 'AMBER' for b in bad)} late",
                         "\n".join(f"- {s_} {w}: age {('%.1fh' % a) if a else 'never OK'}, last {lr}" for s_, w, a, lr in bad),
                         grace=30, amber=st == "AMBER"))
    except Exception:
        rows.append(("backup-age", "an internal ticket", "status.json unreadable", "AMBER"))
    # 9. provider early warning
    try:
        errs = [r for r in seat_rows() if r[3] == "error" and (iso_age_min(r[6]) or 0) < 180 and not expected_out(r[5])]
        by = {}
        for r in errs:
            by.setdefault(r[4], []).append(r)
        early = {p: rs for p, rs in by.items() if 1 <= len(rs) < 3}
        rows.append(("provider-early", "labs", ", ".join(f"{p or '?'} {len(rs)}" for p, rs in by.items()) or "no errored seats", "AMBER" if early else "GREEN"))
    except Exception:
        pass
    # 10/11. production PM2 custody (read-only probe via stdin: no pattern in any process args, no env values printed)
    rc, txt, _ = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", PRODUCTION, "sh -s"], inp=PROD_PM2_PROBE, t=30)
    try:
        d = json.loads(txt.strip().splitlines()[-1])
    except Exception:
        d = None
    if not d:  # a broken probe must never look like a clean matrix (11 Oct: a quoting bug hid these rows for 2.5 h)
        why = (_.strip().splitlines() or [f"ssh rc {rc}, no JSON"])[-1][:160]
        for row in ("pm2-dump-stale", "listeners-supervised", "sphere-restart-reviewed"):
            rows.append((row, "production", f"probe failed: {why}", "AMBER"))
        out.append(F("prod-probe-failed", "guardian", "Production PM2/listener probe returns no data",
                     f"The read-only production probe failed (rc {rc}): {why}. Until fixed, PM2 custody, unsupervised "
                     "listeners and reviewed sphere restarts are unchecked.", grace=20, amber=True))
    if d:
        live = d["live"]  # [{"cwd":..., "start": epoch, "sphere_api": bool}]
        dump = [c for c in d["dump_cwds"] if "/releases/" in c]
        unsaved = [x["cwd"] for x in live if "/releases/" in x["cwd"] and not any(x["cwd"].startswith(c) for c in dump)]
        missing = [c for c in dump if not any(x["cwd"].startswith(c) for x in live)]
        st = "RED" if unsaved or missing else "GREEN"
        rows.append(("pm2-dump-stale", "production",
                     f"{len(live)} PM2 apps live, dump {len(d['dump_cwds'])} apps"
                     + (f"; not in dump: {', '.join(u.split('/releases/')[-1][:40] for u in unsaved)}" if unsaved else "")
                     + (f"; dump points at dead release: {', '.join(m.split('/releases/')[-1][:40] for m in missing)}" if missing else ""), st))
        if st == "RED":
            out.append(F("risk-pm2-dump", "senior", "Production PM2 dump does not match the running releases (a reboot would restore the wrong set)",
                         f"Live releases not in dump.pm2: {unsaved or '-'}; dump releases with no live app: {missing or '-'}. "
                         "Run `pm2 save` as the production app user after every deploy (production lock holder), then re-check.", grace=30))
        # every production listener must be owned by a supervisor (PM2, systemd service, docker); BP 11 Oct: the BOZP
        # backend :3017 ran orphaned (ppid 1) after leaving PM2, so a crash or reboot would leave it down
        crit = {int(k): v[0] for k, v in CFG["production"]["critical_ports"].items()}
        orphans = sorted({(x["port"], x["pid"], x["comm"]) for x in d.get("listeners", []) if not x["supervised"]})
        crit_orphans = [o for o in orphans if o[0] in crit]
        st = "RED" if crit_orphans else ("AMBER" if orphans else "GREEN")
        rows.append(("listeners-supervised", "production",
                     ", ".join(f":{p_} {c_}" for p_, _, c_ in orphans) or "every listener has a supervisor", st))
        if crit_orphans:
            port_owner = {int(k): v[1] for k, v in CFG["production"]["critical_ports"].items()}
            owner = port_owner[crit_orphans[0][0]]
            out.append(F("prod-orphan-critical", owner, f"{len(crit_orphans)} production product listeners have no supervisor",
                         "\n".join(f"- :{p_} {crit[p_]} (pid {pid_}, {c_}) has ppid 1, no PM2/systemd/docker" for p_, pid_, c_ in crit_orphans) +
                         "\nA crash or reboot leaves it down. Put it back under its supervisor (PM2 app + pm2 save, or a unit) "
                         "inside the product's deploy lock.", grace=10, p0=True))
        elif orphans:
            out.append(F("prod-orphan-listeners", "senior", f"{len(orphans)} production listeners run unsupervised",
                         "\n".join(f"- :{p_} pid {pid_} {c_}" for p_, pid_, c_ in orphans) +
                         "\nInventory: each is either a product (supervise it) or a leftover (retire it).", grace=0, amber=True))
        # every sphere-api start must sit inside a reviewed-start window (LOCKED ... post-health), Senior 11 Oct
        sa = [x for x in live if x.get("sphere_api")]
        wins = d.get("reviewed_locked", [])
        for x in sa:
            ok = any(w - 60 <= x["start"] <= w + 15 for w in wins)
            rows.append(("sphere-restart-reviewed", "sphere-api", f"started {dt.datetime.fromtimestamp(x['start']):%d %b %H:%M:%S}, "
                         + ("inside a reviewed-start window" if ok else "NO reviewed-start log line"), "GREEN" if ok else "RED"))
            if not ok and NOW - x["start"] < 7 * 86400 and wins:
                out.append(F(f"sphere-restart-unreviewed-{int(x['start'])}", "senior",
                             "sphere-api restarted outside ~/sphere-reviewed-start.sh",
                             f"sphere-api started {dt.datetime.fromtimestamp(x['start']):%Y-%m-%d %H:%M:%S} with no matching LOCKED line in the "
                             "reviewed-start log. 11 Oct: a plain `pm2 restart sphere-api --update-env` dropped the custody env (outage "
                             "09:52:24-09:54:58). The only restart path is ~/sphere-reviewed-start.sh inside the Sphere flock.",
                             grace=0, p0=True, repeat=False, nudge=True))
    # 12. estate-critical tool wrappers: every exec target exists and none runs from a worktree (11 Oct: a deleted
    #     worktree broke tentacles-computer, so every --computer jupiter seat message crashed for 30 min)
    crit = [f"{HOME}/.local/bin/tentacles", f"{HOME}/.local/bin/tentacles-computer", f"{ART}/bin/guard/tentacles",
            f"{ART}/bin/hire.sh", f"{HOME}/bin/fj", f"{HOME}/bin/nasvault", f"{HOME}/.local/bin/bridge"]
    broken, fragile = [], []
    for w in crit:
        if not os.path.exists(w):
            broken.append(f"{w.replace(HOME, '~')} (wrapper missing)")
            continue
        try:
            body = open(w, errors="replace").read(20000)
        except OSError:
            continue
        for t in set(re.findall(r"(?:\$HOME|~|" + re.escape(HOME) + r")(/[A-Za-z0-9_./-]+\.(?:mjs|cjs|js|py))", body)):
            tp = HOME + t
            if not os.path.exists(tp):
                broken.append(f"{os.path.basename(w)} -> ~{t}")
            elif "/_worktrees/" in tp or "/scratch" in tp:
                fragile.append(f"{os.path.basename(w)} -> ~{t}")
    st = "RED" if broken else ("AMBER" if fragile else "GREEN")
    rows.append(("tool-targets", "Pro agent tools", "; ".join(broken + fragile) or f"{len(crit)} wrappers, all targets present", st))
    if broken:
        out.append(F("tool-target-missing", "guardian", f"{len(broken)} estate tool wrappers point at missing files",
                     "\n".join(f"- {b_}" for b_ in broken) + "\nEvery seat message through these tools fails. Repoint to an installed "
                     "release (never a worktree).", grace=0, p0=True))
    elif fragile:
        out.append(F("tool-target-worktree", "guardian", f"{len(fragile)} estate tools run from a worktree",
                     "\n".join(f"- {f_}" for f_ in fragile) + "\nA worktree can be pruned at any time; repoint to an installed release.",
                     grace=0, amber=True))
    STATE["risk_rows"] = rows
    return out


def write_risk_md():
    rows = STATE.get("risk_rows") or []
    worst = "RED" if any(r[3] == "RED" for r in rows) else ("AMBER" if any(r[3] == "AMBER" for r in rows) else "GREEN")
    lines = [f"{worst} RISK MATRIX {stamp()} (GUARDIAN prevention matrix; rows in estate-guardian/risk_matrix.json)", "",
             "| indicator | scope | value | state |", "|---|---|---|---|"]
    lines += [f"| {a} | {b} | {c.replace('|', '/')} | {d} |" for a, b, c, d in rows]
    open(RISK_MD + ".new", "w").write("\n".join(lines) + "\n")
    os.replace(RISK_MD + ".new", RISK_MD)


# ---------------------------------------------------------------- guard 7: vault web UI (Founder 11 Oct: key, not web login)
VAULT_PORT_RE = CFG["vault_url_re"]


def guard_vault_web():
    """Agents must use the key-based CLI (~/bin/nasvault), never the Vaultwarden web UI. Detects agent browser commands
    that target the vault port in Pro T3 seat activity, Codex session logs and Claude session transcripts since the last
    run. (Chrome's own history is TCC-protected for launchd, and an unattended job must never raise a privacy prompt.)"""
    out, since = [], STATE.get("vault_web_since", NOW - 600)
    STATE["vault_web_since"] = NOW
    # an actual navigation to the vault, not a mention: new tab / set URL / navigate / open <url>
    nav = re.compile(r"(make new tab|set URL|open location|browser_navigate|navigate|\bopen (-a [^ ]+ )?['\"]?https?://)"
                     r"[^\n]{0,160}" + VAULT_PORT_RE, re.I)
    hits = []
    try:  # Pro T3 seats
        con = sqlite3.connect(f"file:{HOME}/.t3/userdata/state.sqlite?mode=ro", uri=True, timeout=5)
        top = con.execute("select max(rowid) from projection_thread_activities").fetchone()[0] or 0
        last = STATE.get("t3_act_rowid", top - 5000)  # append-only table: read only rows added since the last run
        STATE["t3_act_rowid"] = top
        q = ("select substr(a.thread_id,1,8), t.title, a.created_at, a.payload_json from projection_thread_activities a "
             "join projection_threads t using(thread_id) where a.rowid > ? and a.payload_json like '%9443%'")
        for tid, title, at, payload in con.execute(q, (last,)):
            if nav.search(payload.replace('\\"', '"')):
                hits.append(f"T3 {tid} {title} at {at[11:19]}Z")
    except Exception:
        pass
    dirs = [f"{HOME}/.codex/sessions/{d:%Y/%m/%d}" for d in (dt.date.today(), dt.date.today() - dt.timedelta(days=1))]
    pr = f"{HOME}/.claude/projects"
    dirs += [os.path.join(pr, d) for d in (os.listdir(pr) if os.path.isdir(pr) else [])]
    for dirpath in dirs:  # Codex sessions (today, yesterday) and top-level Claude transcripts on the Pro
        root = dirpath
        try:
            files = os.listdir(dirpath)
        except OSError:
            continue
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, f)
            try:
                if os.path.getmtime(p) < since:
                    continue
            except OSError:
                continue
            if True:
                offs = STATE.setdefault("vault_offsets", {})
                size = os.path.getsize(p)
                start = offs.get(p, max(0, size - 2_000_000))
                offs[p] = size  # read only what was appended since the last run
                if start >= size:
                    continue
                try:
                    with open(p, errors="replace") as fh:
                        fh.seek(start)
                        for line in fh:
                            if "9443" in line and ('"tool_use"' in line or '"function_call"' in line) \
                                    and nav.search(line.replace('\\"', '"')):
                                hits.append(f"{'Codex' if 'codex' in root else 'Claude'} session {f[:20]}")
                                break
                except Exception:
                    continue
    for k in [k for k in STATE.get("vault_offsets", {}) if not os.path.exists(k) or NOW - os.path.getmtime(k) > 86400]:
        del STATE["vault_offsets"][k]
    if not hits:
        return out
    out.append(F(f"vault-web-{int(NOW)}", "senior", "An agent drove a browser to the Vaultwarden WEB UI (must use the key-based CLI)",
                 "Agent activity targeting the vault port in a browser: " + "; ".join(sorted(set(hits))[:6]) + ".\n"
                 "Rule (Founder 11 Oct): credentials only via `~/bin/nasvault unlock|get|list|create` (Keychain master, values "
                 "never printed); never the web vault, never ask the Founder to unlock it. Stop the seat, close the tab, re-brief "
                 "with the nasvault path.", grace=0, repeat=False, nudge=True))
    return out


# ---------------------------------------------------------------- guard 8: refusals -> the Founder's Codex
CODEX_PULSE = CFG["codex_pulse_label"]


# Founder agreement 12 Oct 00:0x (POLICY-VAULT-AND-2FA.md): only these stay the Founder's; everything else a vault seat runs
FOUNDER_ONLY = re.compile(r"2fa|two.factor|otp|authenticator|passkey|payment|pay |top.?up|billing|invoice|card|subscription|plan upgrade|"
                          r"sign.?up|new account|create .{0,20}account|account creation|tcc|accessibility|input monitoring|microphone|"
                          r"camera|gatekeeper|privacy|permission dialog|security dialog|physical|cable|device|legal|counsel|contract|"
                          r"identity|apple team|developer account|play console|tailnet", re.I)
POLICY_VAULT_2FA = 1791756000  # 12 Oct 2026 00:00 CEST: refusals filed after this are checked


def guard_refusals():
    """Founder 11 Oct: refusals filed in Linear go to Codex. New open `refusals` issues kick the Founder's Codex
    refusal pulse at once (it holds the standing authorization, incl. vault briefs via nasvault on the Pro) and get a
    note; a refusal with no Codex reply after 3 h becomes a GUARDIAN finding."""
    out = []
    key = sh(["security", "find-generic-password", "-s", LINEAR_KEY_ITEM, "-w"], t=10)[1].strip()
    if not key:
        return out
    q = ('query{issues(first:50,filter:{labels:{name:{eq:"refusals"}},state:{type:{nin:["completed","canceled"]},name:{neq:"Duplicate"}}})'
         '{nodes{id identifier title description createdAt comments(first:20){nodes{createdAt body}}}}}')
    nodes = linear(q, {}, key)["issues"]["nodes"]
    seen = STATE.setdefault("refusals_seen", None)
    if seen is None:  # first run: everything already open is known; only new ones trigger
        STATE["refusals_seen"] = {n["identifier"]: NOW for n in nodes}
        return out
    new = [n for n in nodes if n["identifier"] not in seen]
    for n in new:
        seen[n["identifier"]] = NOW
    if new and "--dry-run" not in sys.argv:
        lbl = f"gui/{os.getuid()}/{CODEX_PULSE}"
        running = "pid = " in sh(["launchctl", "print", lbl], t=10)[1]
        if not running and NOW - STATE.get("codex_kick", 0) > 600:
            sh(["launchctl", "kickstart", lbl], t=15)
            STATE["codex_kick"] = NOW
        note = (f"GUARDIAN {stamp()}: sent to Codex. The Founder's Codex refusal pulse was "
                f"{'already running and picks this up' if running else 'started now'} (Founder rule 11 Oct: refusals go to "
                "Codex; standing authorization incl. vault briefs via ~/bin/nasvault on the Pro).")
        for n in new:
            body = note
            if not FOUNDER_ONLY.search(f"{n['title']} {n.get('description') or ''}"):
                body += ("\n\nPolicy 12 Oct (POLICY-VAULT-AND-2FA.md): vault passwords and keys are for agents. A vault, "
                         "credential or sign-in step runs on a Luna vault seat on the Pro (`HIRE_HOST=pro hire.sh luna "
                         "<VAULT-BRIEF>.md`) started by the owning line, not as a #refusal. Only 2FA, payments/new accounts and "
                         "macOS permission dialogs stay the Founder's. The plan gate for NEW credentials in external consoles "
                         "still applies.")
            try:
                linear('mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,body:$b}){success}}',
                       {"i": n["id"], "b": body}, key)
            except Exception:
                pass
    # Policy 12 Oct: a refusal that is not a Founder-only kind (2FA, payment/new account, macOS permission, physical,
    # legal/identity) should have been a vault seat run: show it on the status page until it is closed.
    for n in nodes:
        if seen.get(n["identifier"], 0) >= POLICY_VAULT_2FA and not FOUNDER_ONLY.search(f"{n['title']} {n.get('description') or ''}"):
            out.append(F(f"refusal-policy-{n['identifier']}", "guardian", f"{n['identifier']} filed as #refusal but a vault seat can run it",
                         f"{n['title'][:120]}: route to a Luna vault seat on the Pro (POLICY-VAULT-AND-2FA.md).",
                         grace=0, amber=True, file=False))
    # Plan gate (Founder 11 Oct): Codex posts "PLAN READY <issue> sha256:<12hex>"; GUARDIAN reviews and approves.
    pq = f"{STATE_DIR}/plan-reviews.jsonl"
    queued = STATE.setdefault("plans_queued", {})
    for n in nodes:
        cs = sorted(n["comments"]["nodes"], key=lambda c: c["createdAt"])
        ready = [re.search(r"PLAN READY (KRA-\d+) sha256:([0-9a-f]{12})", c["body"]) for c in cs]
        ready = [m for m in ready if m]
        if not ready:
            continue
        iss, h = ready[-1].group(1), ready[-1].group(2)
        # Only a comment that STARTS with the approval counts: Codex's PLAN READY quotes the expected approval line.
        approved = any(c["body"].lstrip().startswith(f"GUARDIAN APPROVE PLAN {iss} sha256:{h}") for c in cs)
        if approved or queued.get(iss) == h:
            continue
        queued[iss] = h
        if "--dry-run" not in sys.argv:
            with open(pq, "a") as q:
                q.write(json.dumps({"at": stamp(), "issue": iss, "sha12": h, "linear_id": n["id"],
                                    "plan": f"{ART}/PLAN-{iss}.md", "title": n["title"][:120]}) + "\n")
        out.append(F(f"plan-review-{iss}", "guardian", f"Codex plan ready for review: {iss}",
                     f"{ART}/PLAN-{iss}.md sha256:{h}. Review and post 'GUARDIAN APPROVE PLAN {iss} sha256:{h}' or changes.",
                     grace=0, repeat=False, file=False))
    for n in nodes:  # chase refusals that Codex has not answered
        created = dt.datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00")).timestamp()
        codex = [c for c in n["comments"]["nodes"] if "GUARDIAN" not in c["body"] and "Senior" not in c["body"][:40]
                 and dt.datetime.fromisoformat(c["createdAt"].replace("Z", "+00:00")).timestamp() > created]
        if not codex and NOW - created > 3 * 3600 and created > NOW - 7 * 86400:
            out.append(F(f"refusal-unanswered-{n['identifier']}", "guardian",
                         f"Refusal {n['identifier']} has no Codex reply after {int((NOW - created) / 3600)} h",
                         f"{n['title'][:120]}. GUARDIAN re-kicks the Codex refusal pulse (hourly at most).",
                         grace=0, repeat=False, file=False))
    if any(f.key.startswith("refusal-unanswered-") for f in out) and "--dry-run" not in sys.argv \
            and NOW - STATE.get("codex_kick", 0) > 3600:
        lbl = f"gui/{os.getuid()}/{CODEX_PULSE}"
        if "pid = " not in sh(["launchctl", "print", lbl], t=10)[1]:
            sh(["launchctl", "kickstart", lbl], t=15)
            STATE["codex_kick"] = NOW
    for k in [k for k, t in seen.items() if NOW - t > 30 * 86400]:
        del seen[k]
    return out


def mirror_leaks():
    """Private Forgejo repos whose push mirror targets a PUBLIC GitHub repo (owner/name pairs)."""
    leaks, page = [], 1
    while True:
        repos = fj(f"/user/repos?limit=50&page={page}")
        for r in repos:
            if not r.get("private"):
                continue
            for m in fj(f"/repos/{r['full_name']}/push_mirrors"):
                addr = m.get("remote_address", "")
                if "github.com" not in addr:
                    continue
                gh_repo = re.sub(r"\.git$", "", addr.split("github.com", 1)[1].lstrip(":/"))
                rc, vis, _ = sh([GH, "api", f"repos/{gh_repo}", "--jq", ".private"], t=20)
                if rc == 0 and vis.strip() == "false":
                    leaks.append((r["full_name"], gh_repo))
        if len(repos) < 50:
            return leaks
        page += 1


def guard_mirrors():
    """11 Oct publication of estate-guardian: the private repo and its public edition share a GitHub name family.
    A private Forgejo repo must never push-mirror into a public GitHub repo (that publishes internal code)."""
    try:
        leaks = cached("mirror_leaks", 3600, mirror_leaks)
    except Exception as e:
        return [F("mirror-check", "guardian", "mirror visibility check failed", str(e)[:200], grace=120, amber=True)]
    return [F(f"mirror-leak-{src.replace('/', '-')}", "guardian", f"private {src} mirrors to PUBLIC GitHub {dst}",
              f"Forgejo {src} is private but its push mirror targets public github.com/{dst}. Delete that push mirror "
              f"now (fj DELETE /repos/{src}/push_mirrors/<name>), then check what reached GitHub.", grace=0, p0=True)
            for src, dst in leaks]


def prom(expr):
    """Instant query through Grafana's datasource proxy (anonymous viewer on the tailnet)."""
    g = CFG.get("grafana") or {}
    url = f"{g['url']}/api/datasources/proxy/uid/{g['prometheus_uid']}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
        return json.load(r)["data"]["result"]


def guard_observability():
    """12 Oct: the estate dashboard showed Sphere pg_up=0 (red tile) for 9 days with no alert rule behind it, so nobody
    acted. Read the dashboard's own signals: firing estate alerts, and any estate scrape or exporter that is down."""
    if not CFG.get("grafana"):
        return []
    out = []
    try:
        for r in prom('ALERTS{estate="true",alertstate="firing"}'):
            m = r["metric"]
            out.append(F(f"obs-alert-{m.get('alertname', '?')}-{m.get('host', m.get('instance', '?'))}", "senior",
                         f"Grafana estate alert firing: {m.get('alertname')} ({m.get('host') or m.get('instance')})",
                         json.dumps({k: v for k, v in m.items() if k not in ('__name__', 'alertstate')})[:300], grace=60, amber=True))
        down = prom('pg_up{estate="true"} == 0 or max_over_time(up{estate="true"}[30m]) == 0')
        for r in down:
            m = r["metric"]
            what = m.get("exported_job") or m.get("job") or "?"
            out.append(F(f"obs-down-{what}-{m.get('host', m.get('instance', '?'))}", "senior",
                         f"Estate monitor blind: {m.get('__name__', 'up')} = 0 for {what} on {m.get('host') or m.get('instance')}",
                         "The dashboard tile is red and no alert rule covers it. Restore the scrape/exporter, or retire the target "
                         "from the dashboard. " + json.dumps({k: v for k, v in m.items() if k != '__name__'})[:300], grace=60, amber=True))
    except Exception as e:
        out.append(F("obs-unreachable", "guardian", "Grafana/Prometheus not readable from the Pro", str(e)[:200], grace=60, amber=True))
    return out


def guard_scheduler():
    """12 Oct: four scheduled runs (GUARDIAN pulse 20:57, Senior pulse 23:55, Syfon 01:51, autoresearch 03:16) never
    ended. Each blocks its own task, and together they filled the app's concurrent-run slots: from 03:16 to 18:39 no
    scheduled task fired anywhere (Tržnice watchdog, the 07:31 digest). Read the app's session records: a run active
    for over 45 min is stuck; an enabled task with no run for 2x its interval (+15 min) is not firing."""
    want = CFG.get("scheduled_tasks") or {}
    base = os.path.expanduser(CFG.get("claude_sessions_dir", ""))
    if not want or not os.path.isdir(base):
        return []
    import glob as _glob
    last, long_runs = {}, []
    for f in _glob.glob(f"{base}/*/*/local_*.json"):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        t, c0, la = d.get("scheduledTaskId"), (d.get("createdAt") or 0) / 1000, (d.get("lastActivityAt") or 0) / 1000
        if not t or not c0:
            continue
        last[t] = max(last.get(t, 0), c0)
        if d.get("isArchived"):
            continue
        # hung: no turn ever completed (12 Oct 18:58 run waited 40 min on an approval prompt; scheduled runs use
        # permissionMode "default"); long: a run someone continued into a conversation (scheduledRunContinued)
        if not d.get("completedTurns") and not d.get("lastTurnCutShort") and 20 * 60 < NOW - c0 < 6 * 3600:
            long_runs.append((t, d.get("sessionId"), (NOW - c0) / 60))
        elif la - c0 > 45 * 60 and NOW - la < 15 * 60:
            long_runs.append((t, d.get("sessionId"), (la - c0) / 60))
    out = []
    for t, sid, mins in long_runs:
        out.append(F(f"sched-stuck-{t}", "guardian" if t.startswith("guardian") else "senior",
                     f"Scheduled run of {t} active for {mins:.0f} min (blocks its task; 3-4 such runs stop ALL tasks)",
                     f"Session {sid}. A pulse run must end within ~20 min; long work goes to a hire. Stop or finish this run.",
                     grace=20, p0=len(long_runs) >= 3, amber=len(long_runs) < 3))
    for t, every in want.items():
        age = (NOW - last.get(t, 0)) / 60
        if age > 2 * every + 15:
            out.append(F(f"sched-silent-{t}", "guardian" if t.startswith("guardian") else "senior",
                         f"Scheduled task {t} has not run for {age:.0f} min (every {every} min)",
                         "Check list_scheduled_tasks: registration present and enabled? A run stuck open (see sched-stuck-*)? "
                         "Too many concurrent scheduled runs?", grace=0, amber=True))
    return out


RESULT_OWNERS = [("ADVERTES-IT", "guardian"), ("TRZNICE", "trznice"), ("SYFON", "syfon"), ("BOZP", "bezpecnost"), ("BEZPECNOST", "bezpecnost"),
                 ("COUNCILNOW", "councilnow"), ("ADVERTES", "advertes"), ("FLAGSHIP", "flagship"), ("SIMS", "flagship"),
                 ("GUARDIAN", "guardian")]


def guard_results():
    """13 Oct: the Tržnice pilot stood still from 22:12 to 05:20 because Senior's seat result
    RESULT-SPHERE-RELEASE-WORKSPACES.md said PARTIAL (backport not applied, no PR) and nobody followed up. Every line
    looked alive (fresh heartbeats, pulses every 30 min) while the work did not move. A worker RESULT whose first line
    is PARTIAL/BLOCKED, with no newer DONE result of the same job, goes to its owner after 60 min and becomes P0 after
    3 h (stalled work is an outage of the line)."""
    out = []
    import glob as _glob
    files = _glob.glob(f"{ART}/RESULT-*.md")
    newest_done = {}
    for f in files:
        base = re.sub(r"(\.run\d+-BLOCK|-BLOCK|-PARTIAL)?\.md$", "", os.path.basename(f))
        try:
            first = open(f, errors="replace").readline().strip()
        except OSError:
            continue
        if re.match(r"(DONE|PASS|READY|OK)\b", first):
            newest_done[base] = max(newest_done.get(base, 0), os.path.getmtime(f))
    old = []
    for f in files:
        name = os.path.basename(f)
        base = re.sub(r"(\.run\d+-BLOCK|-BLOCK|-PARTIAL)?\.md$", "", name)
        try:
            first = open(f, errors="replace").readline().strip()
        except OSError:
            continue
        if not re.match(r"(PARTIAL|BLOCKED|BLOCK)\b", first):
            continue
        mt = os.path.getmtime(f)
        if newest_done.get(base, 0) > mt:
            continue  # a later run of the same job finished
        age = (NOW - mt) / 60
        if age < 60 or age > 48 * 60:
            continue
        owner = next((o for k, o in RESULT_OWNERS if k in name.upper()), "senior")
        try:
            body = open(f, errors="replace").read()
        except OSError:
            body = ""
        # whoever picks a stalled result up appends "FOLLOW-UP <HH:MM> <who>: <seat/issue>"; that counts for 6 h
        fu = re.findall(r"^FOLLOW-UP\b.*$", body, re.M)
        if fu and NOW - os.path.getmtime(f) < 6 * 3600:
            continue
        founder_wait = bool(re.search(r"founder|2fa|payment|account creation|apple id|notary|chrome profile", body[:1500], re.I))
        if age > 12 * 60:
            old.append(f"{name} ({age / 60:.0f} h, {owner})")
            continue
        out.append(F(f"result-stalled-{base.lower()}", owner,
                     f"Worker result {name} says {first.split()[0]} for {age / 60:.1f} h with no follow-up",
                     f"{first[:300]}\nNext step for the owner: re-hire with the missing piece, hand it to the named owner, "
                     "or close it as superseded (write a DONE/SUPERSEDED result). Stalled work is an outage of the line.",
                     grace=0, nudge=(owner == "senior"), p0=age > 180 and not founder_wait, amber=age <= 180 or founder_wait))
    if old:
        out.append(F("result-stalled-backlog", "guardian", f"{len(old)} PARTIAL/BLOCKED worker results 12–48 h old",
                     "\n".join(f"- {x}" for x in sorted(old)), grace=0, amber=True, file=False))
    return out


def agy_model_scan():
    """Callers that pin an Antigravity model instead of using bin/agy-flash (Founder 13 Oct: latest Flash only).
    13 Oct lesson: a scan over all of ~/github timed out at 90 s and looked like "nothing found"; heavy dirs are
    excluded and a timeout raises (so it shows as a failed check, not a clean one)."""
    roots = [os.path.expanduser(p) for p in CFG.get("agy_scan_roots", ["~/github", "~/Developer-artifacts/bin"])]
    skip = CFG.get("agy_scan_exclude", ["opensre"])  # third-party code that passes a user-chosen model through
    ex = []
    for d in ["node_modules", ".git", "_worktrees", ".venv", "venv", "dist", "build", ".next", "site-packages",
              "_archive", *skip]:
        ex.append(f"--exclude-dir={d}")
    rc, txt, err = sh(["grep", "-rIlE", r"[\"' ]agy[\"' ,].*--model|agy +--model", *roots, "--include=*.py",
                       "--include=*.sh", "--include=*.mjs", "--include=*.ts", *ex], t=240)
    if rc == 124:
        raise TimeoutError("agy scan timed out")
    return sorted(l for l in txt.splitlines() if l and not l.endswith("agy-flash") and ".bak" not in l)


def guard_antigravity():
    try:
        hits = cached("agy_model_scan", 86400, agy_model_scan)
    except Exception as e:
        return [F("agy-scan-failed", "guardian", "Antigravity model scan failed", str(e)[:200], grace=0, amber=True, file=False)]
    if not hits:
        return []
    return [F("agy-model-pinned", "guardian", f"{len(hits)} scripts call Antigravity with a pinned model",
              "Founder 13 Oct: on Antigravity use ONLY the latest Gemini Flash. Call ~/Developer-artifacts/bin/agy-flash "
              "(it resolves the newest flash and refuses --model):\n" + "\n".join(f"- {h}" for h in hits[:20]),
              grace=0, amber=True)]


def root_checkouts():
    """Real git checkouts directly under ~/github that duplicate a repo kept under ~/github/current/<category>/
    (layout rule). 12-13 Oct: publications and ai-music-video were created at the root; a stale root copy of
    kraliki-sphere can receive edits meant for the canonical checkout."""
    root = os.path.expanduser("~/github")
    out = []
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d)
        if d == "current" or os.path.islink(p) or not os.path.isdir(os.path.join(p, ".git")):
            continue
        rc, url, _ = sh(["git", "-C", p, "remote", "get-url", "origin"], t=10)
        name = re.sub(r"\.git$", "", url.strip().rstrip("/").split("/")[-1]) if url.strip() else d
        canon = [c for c in _glob_mod.glob(os.path.join(root, "current", "*", name)) if os.path.isdir(c)]
        if canon:
            rc, st, _ = sh(["git", "-C", p, "status", "--porcelain"], t=20)
            rc2, ahead, _ = sh(["git", "-C", p, "log", "--oneline", "@{u}..HEAD"], t=20)
            out.append(f"~/github/{d} duplicates {canon[0].replace(root, '~/github')}"
                       f" ({'dirty' if st.strip() else 'clean'}, {len(ahead.splitlines()) if rc2 == 0 else '?'} unpushed)")
    return out


def guard_layout():
    try:
        dups = cached("root_checkouts", 6 * 3600, root_checkouts)
    except Exception:
        return []
    if not dups:
        return []
    return [F("layout-root-duplicates", "guardian", f"{len(dups)} duplicate checkouts at the ~/github root",
              "Checkouts live only under ~/github/current/<category>/<repo>. Each root copy below: push or move any work, "
              "then replace it with a symlink to the canonical checkout (owner decides; GUARDIAN deletes nothing):\n"
              + "\n".join(f"- {x}" for x in dups), grace=0, amber=True)]


GUARDS = {"ci": guard_ci, "handoffs": guard_handoffs, "lines": guard_lines, "hosts": guard_hosts, "prod": guard_prod,
          "predict": guard_predict, "vault": guard_vault_web, "refusals": guard_refusals, "mirrors": guard_mirrors, "observability": guard_observability, "scheduler": guard_scheduler, "results": guard_results, "antigravity": guard_antigravity, "layout": guard_layout}


# ---------------------------------------------------------------- Linear + state
def linear(query, variables, key):
    req = urllib.request.Request("https://api.linear.app/graphql", data=json.dumps(
        {"query": query, "variables": variables}).encode(), headers={"Authorization": key, "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if d.get("errors"):
        raise RuntimeError(str(d["errors"])[:300])
    return d["data"]


class Board:
    def __init__(self, dry):
        self.dry = dry
        self.key = "" if dry else sh(["security", "find-generic-password", "-s", LINEAR_KEY_ITEM, "-w"])[1].strip()
        self._team = self._done = None

    def team(self):
        if not self._team:
            t = linear('query($k:String!){teams(filter:{key:{eq:$k}}){nodes{id states{nodes{id type name}}}}}',
                       {"k": TEAM_KEY}, self.key)["teams"]["nodes"][0]
            self._team = t["id"]
            done = [s for s in t["states"]["nodes"] if s["type"] == "completed"]
            self._done = (([s for s in done if s["name"] == "Done"] or done)[0])["id"]
        return self._team

    def find(self, title):
        n = linear('query($t:String!){issues(filter:{title:{eq:$t},state:{type:{nin:["completed","canceled"]}}}){nodes{id identifier}}}',
                   {"t": title}, self.key)["issues"]["nodes"]
        return n[0] if n else None

    def file(self, f, body):
        title = f"GUARDIAN: {f.key}"
        if self.dry or not self.key:
            print(f"  [dry] would file/comment {title}")
            return None
        hit = self.find(title)
        if hit:
            linear('mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,body:$b}){success}}',
                   {"i": hit["id"], "b": body}, self.key)
            return hit
        labels = sorted({LABELS[x] for x in OWNER_LABELS.get(f.owner, ["infra"])})
        r = linear('mutation($t:String!,$tm:String!,$d:String!,$p:Int!,$l:[String!]){issueCreate(input:{title:$t,teamId:$tm,'
                   'description:$d,priority:$p,labelIds:$l}){issue{id identifier}}}',
                   {"t": title, "tm": self.team(), "d": body, "p": 1 if f.p0 else (4 if f.amber else 2), "l": labels}, self.key)
        return r["issueCreate"]["issue"]

    def recover(self, issue_id, body):
        if self.dry or not self.key or not issue_id:
            return
        linear('mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,body:$b}){success}}',
               {"i": issue_id, "b": body}, self.key)
        self.team()
        linear('mutation($i:String!,$s:String!){issueUpdate(id:$i,input:{stateId:$s}){success}}',
               {"i": issue_id, "s": self._done}, self.key)


def hist(ev):
    if "--dry-run" in sys.argv:  # dry runs must never touch the evaluator's ground truth
        return
    ev["t"] = int(NOW)
    with open(HISTORY, "a") as h:
        h.write(json.dumps(ev) + "\n")


def sync_severity_history(f, s, cls=None):
    """Keep each continuous severity interval represented in the history log."""
    if not s.get("opened"):
        if cls is None:
            return
        s["opened"], s["class"], s["p0"] = NOW, cls["class"], bool(f.p0)
        s["history_first"] = s.get("first", NOW)
        hist({"ev": "open", "key": f.key, "class": cls["class"], "p0": bool(f.p0),
              "owner": f.owner, "first": s["history_first"]})
        return

    current_p0 = bool(f.p0)
    previous_p0 = s.get("p0")
    if previous_p0 is None or current_p0 != bool(previous_p0):
        current_class = s.get("class") or (cls or {}).get("class")
        hist({"ev": "close", "key": f.key, "class": current_class,
              "first": s.get("history_first", s.get("first")), "last": NOW})
        hist({"ev": "open", "key": f.key, "class": current_class, "p0": current_p0,
              "owner": f.owner, "first": NOW})
        s["opened"], s["class"], s["history_first"] = NOW, current_class, NOW
    s["p0"] = current_p0


def wake_pulse_runner(lines):
    thread = open(f"{ART}/.pulse-runner-thread").read().strip()
    text = (f"GUARDIAN-WAKE {stamp()}: P0 reliability findings need action now (details in "
            f"~/Developer-artifacts/GUARDIAN-STATUS.md and Linear 'GUARDIAN:' issues):\n" + "\n".join(lines[:8]) +
            "\nRoot-cause each one before restoring; never only restart.")
    payload = json.dumps({"action": "thread.continue", "threadId": thread, "runtimeMode": "full-access", "text": text})
    p = f"{STATE_DIR}/wake.json"
    open(p, "w").write(payload)
    return sh(["gtimeout", "60", "tentacles", "act", "--no-wait", "--intent-file", p], t=70)[0]


def main():
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(f"{STATE_DIR}/run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one run at a time; state_edit.py takes the same lock
    except OSError:
        print("another GUARDIAN run (or a state edit) holds the lock; skipping")
        return 0
    dry = "--dry-run" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].split(",")
    os.makedirs(STATE_DIR, exist_ok=True)
    if not dry:
        open(HEARTBEAT, "w").write(str(int(NOW)))
    try:
        state = json.load(open(STATE_PATH))
    except Exception:
        state = {"findings": {}, "last_wake": 0}
    STATE.update(state)
    state = STATE
    findings, crashed = [], []
    with cf.ThreadPoolExecutor(5) as ex:
        futs = {ex.submit(fn): name for name, fn in GUARDS.items() if not only or name in only}
        for fut in cf.as_completed(futs):
            try:
                findings += fut.result()
            except Exception as e:
                crashed.append(f"{futs[fut]}: {type(e).__name__}: {e}"[:200])
                with open(f"{STATE_DIR}/crash.log", "a") as cl:
                    import traceback
                    cl.write(f"{stamp()} {futs[fut]}\n{traceback.format_exc()}\n")
    for c in crashed:
        findings.append(F(f"guard-crash-{c.split(':')[0]}", "guardian", f"Guard crashed: {c.split(':')[0]}", c, grace=30))
    # Maintenance windows: {"<finding key prefix>": {"until": epoch, "why": "..."}} in guardian/maintenance.json.
    # A muted finding is still shown in GUARDIAN-STATUS.md but files nothing and wakes nobody.
    try:
        maint = {k: v for k, v in json.load(open(f"{STATE_DIR}/maintenance.json")).items() if v.get("until", 0) > NOW}
    except Exception:
        maint = {}
    muted = [f for f in findings if any(f.key.startswith(k) for k in maint)]
    findings = [f for f in findings if f not in muted]
    board = Board(dry)
    seen_keys = {f.key for f in findings} | {f.key for f in muted}
    known = state["findings"]
    p0_now = []
    for f in findings:
        s = known.setdefault(f.key, {"first": NOW, "posted": 0, "digest": "", "issue": None, "ok": 0})
        s.update(last=NOW, ok=0, ok_since=None, title=f.title, owner=f.owner, hold=f.hold)
        sync_severity_history(f, s)
        age_min = (NOW - s["first"]) / 60
        digest = hashlib.sha1(f.detail.encode()).hexdigest()
        if age_min < f.grace:
            continue
        if f.nudge and not dry:
            if not s.get("nudged"):
                s["nudged"] = NOW
                with open(NUDGE_Q, "a") as q:
                    q.write(json.dumps({"at": stamp(), "key": f.key, "title": f.title, "detail": f.detail[:600]}) + "\n")
                continue
            if NOW - s["nudged"] < 2700:  # the 30-min durable pulse delivers the wake; give Senior 15 min after it
                continue
        due = s["posted"] == 0 or (f.repeat and NOW - s["posted"] >= 3600) or (not f.repeat and digest != s["digest"])
        if f.p0 and f.owner in ("senior", "captain", "guardian"):  # the Pulse Runner is Senior's; other lines get Linear + a message
            p0_now.append(f"- {f.title}")
        if not due or not f.file:
            continue
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import classify
            cls = classify.classify(f"{f.title}. {f.detail}", f.key, record="--dry-run" not in sys.argv)
            card = classify.card_markdown(cls)
        except Exception as e:
            cls, card = {"class": "unclassified"}, f"(classifier error {type(e).__name__})"
        sync_severity_history(f, s, cls)
        body = (f"**{f.title}**\n\n{f.detail}\n\n{card}\n\n"
                f"First seen {stamp(s['first'])}, still failing at {stamp()}. Owner line: **{f.owner}**. "
                f"Filed by GUARDIAN (no-AI watch, every 5 min; estate-guardian/guardian.py). "
                f"Picture of done: the cause is found and fixed, a guard prevents it, and this issue auto-closes "
                f"after 2 clean runs.")
        try:
            hit = board.file(f, body)
            s["posted"], s["digest"] = NOW, digest
            if hit:
                s["issue"], s["identifier"] = hit["id"], hit.get("identifier")
        except Exception as e:
            print(f"  linear error for {f.key}: {e}", file=sys.stderr)
    for k in list(known):
        if k in seen_keys:
            continue
        if only:  # a partial run never recovers findings it did not check
            continue
        s = known[k]
        s["ok"] = s.get("ok", 0) + 1
        s["ok_since"] = s.get("ok_since") or NOW
        if s["ok"] >= 2 and NOW - s["ok_since"] >= s.get("hold", 0) * 3600:
            if s.get("opened"):
                hist({"ev": "close", "key": k, "class": s.get("class"),
                      "first": s.get("history_first", s.get("first")), "last": s.get("last")})
            if s.get("posted"):
                try:
                    board.recover(s.get("issue"), f"RECOVERED at {stamp()} (2 clean GUARDIAN runs). "
                                  f"Before closing for good: is the root cause recorded and a guard in place?")
                except Exception as e:
                    print(f"  linear recover error for {k}: {e}", file=sys.stderr)
            del known[k]
    if p0_now and not dry and NOW - state.get("last_wake", 0) >= 1800:
        try:
            wake_pulse_runner(p0_now)
            state["last_wake"] = NOW
        except Exception as e:
            print(f"  wake error: {e}", file=sys.stderr)
    lines = [f"{'RED' if p0_now else ('AMBER' if findings else 'GREEN')} GUARDIAN {stamp()}: "
             f"{len(findings)} findings, {len(p0_now)} P0 past grace",
             "", "| finding | owner | since | Linear |", "|---|---|---|---|"]
    for f in sorted(findings, key=lambda x: (not x.p0, x.key)):
        s = known.get(f.key, {})
        lines.append(f"| {'P0 ' if f.p0 else ''}{f.title} | {f.owner} | {stamp(s.get('first', NOW))[11:16]} | "
                     f"{s.get('identifier') or ('grace' if not s.get('posted') else '-')} |")
    for f in muted:
        lines.append(f"| (maintenance) {f.title} | {f.owner} | - | muted |")
    if not dry and (not only or "predict" in only):
        write_risk_md()
    if not dry:
        open(STATUS_MD + ".new", "w").write("\n".join(lines) + "\n")
        os.replace(STATUS_MD + ".new", STATUS_MD)
        json.dump(state, open(STATE_PATH + ".new", "w"), indent=1)
        os.replace(STATE_PATH + ".new", STATE_PATH)
        with open(LOG, "a") as lg:
            lg.write(f"{stamp()} findings={len(findings)} p0={len(p0_now)} crashed={len(crashed)} "
                     f"took={time.time() - NOW:.0f}s\n")
    print("\n".join(lines))
    for f in findings:
        print(f"  {f.key}: {f.detail[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
