#!/usr/bin/env python3
"""Gate for durable Claude pulses (GUARDIAN, 16 Oct 2026 weekly-cap incident). Usage: pulse_throttle.py <line-id>
Prints RUN or `SKIP <reason>` (exit 0 either way). A SKIP run must end at once: no reads, no heartbeat write.
Reads ~/Developer-artifacts/estate/PULSE-THROTTLE.json (written by guardian.py guard_budget) and the line's heartbeat
from estate/lines.json. Fails open: no file, a stale file or reading (> 3 h), a passed reset, or any error -> RUN."""
import json, os, re, sys, time

ART = os.path.expanduser("~/Developer-artifacts")


def decide(line, now=None):
    now = now or time.time()
    try:
        th = json.load(open(f"{ART}/estate/PULSE-THROTTLE.json"))
        if now - os.path.getmtime(f"{ART}/estate/PULSE-THROTTLE.json") > 3 * 3600:
            return "RUN"
        if now - float(th.get("reading_epoch") or 0) > 3 * 3600 or now > float(th.get("resets_epoch") or 0):
            return "RUN"  # stale reading or past the reset: never keep lines asleep on old data
        level, cadence = th["level"], int(th["cadence_min"])
        if level in ("normal", "warn"):
            return "RUN"
        if level == "stop" and line != "guardian":
            return f"SKIP stop: Claude desktop plan {th.get('weekly_pct')}% weekly, only GUARDIAN pulses until {th.get('resets_at')}"
        if level == "minimal" and line not in (th.get("keep") or []):
            return f"SKIP minimal: Claude desktop plan {th.get('weekly_pct')}% weekly, only {th.get('keep')} pulse"
        hb = next((l.get("heartbeat") for l in json.load(open(f"{ART}/estate/lines.json"))["lines"] if l["id"] == line), None)
        if not hb:
            return "RUN"
        v = int(re.sub(r"\D", "", open(os.path.expanduser(hb)).read())[:10] or 0)
        if v > now:
            return "RUN"
        age = (now - v) / 60
        if age >= cadence - 5:
            return "RUN"
        return f"SKIP {level}: heartbeat {int(age)} min old, cadence {cadence} min (weekly {th.get('weekly_pct')}%)"
    except Exception:
        return "RUN"


if __name__ == "__main__":
    print(decide(sys.argv[1] if len(sys.argv) > 1 else ""))
