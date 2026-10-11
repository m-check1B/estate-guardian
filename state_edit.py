#!/usr/bin/env python3
"""Edit GUARDIAN state safely between runs: holds the run lock while applying a Python expression to the state dict.
Usage: state_edit.py 'd["findings"].pop("vault-web-123", None)'   (d is the state dict)
"""
import fcntl, json, os, sys
D = os.path.expanduser("~/Developer-artifacts/guardian")
with open(f"{D}/run.lock", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)  # waits for a running guard to finish
    p = f"{D}/state.json"
    d = json.load(open(p))
    exec(sys.argv[1], {"d": d, "time": __import__("time")})
    json.dump(d, open(p + ".new", "w"), indent=1)
    os.replace(p + ".new", p)
    print("ok")
