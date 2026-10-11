#!/bin/zsh
# publish-public.sh: refresh the public edition from this repo's HEAD (Founder, 11 Oct 2026: "make the repo public").
# Copies the tracked files into the public checkout, strips internal ticket refs, keeps the public intro + LICENSE,
# refuses to commit if the leak scan finds IPs, tailnet names, home paths, tokens or ticket ids, then pushes the
# public checkout's main to its Forgejo origin. GitHub gets it only through Forgejo's push mirror.
#   PUBLIC_DIR=../estate-guardian-public ./publish-public.sh [--dry-run]
set -euo pipefail
SRC="${0:A:h}"
PUB="${PUBLIC_DIR:-$SRC/../estate-guardian-public}"
[[ -d "$PUB/.git" ]] || { print -u2 "publish: $PUB is not the public checkout"; exit 2; }
[[ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]] || { print -u2 "publish: commit first"; exit 2; }

intro="$(python3 -c 'import sys;s=open(sys.argv[1]).read();print(s.split("\n---\n",1)[0] if "\n---\n" in s else "")' "$PUB/README.md")"
[[ -n "$intro" ]] || { print -u2 "publish: public README has no intro block (text before the first ---)"; exit 2; }

git -C "$PUB" ls-files | grep -vxE 'LICENSE|README.md' | (cd "$PUB" && xargs rm -f)
git -C "$SRC" archive HEAD | tar -x -C "$PUB"
python3 - "$PUB" "$intro" <<'PY'
import os, re, sys
pub, intro = sys.argv[1], sys.argv[2]
body = open(os.path.join(pub, "README.md")).read().split("\n", 1)[1]  # drop the private H1; the intro has its own
open(os.path.join(pub, "README.md"), "w").write(intro.rstrip() + "\n\n---\n" + body)
for root, _, files in os.walk(pub):
    if ".git" in root.split(os.sep):
        continue
    for f in files:
        p = os.path.join(root, f)
        try:
            s = open(p).read()
        except UnicodeDecodeError:
            continue
        o = s
        s = re.sub(r" \((?:pending )?KRA-\d+[^)]*\)", "", s)   # "(an internal ticket)", "(an internal ticket rule)", "(pending an internal ticket)"
        s = re.sub(r"\(KRA-\d+\), |, KRA-\d+|KRA-\d+: ", "", s)
        s = re.sub(r"KRA-\d+", "an internal ticket", s)
        if s != o:
            open(p, "w").write(s)
PY
leaks="$(grep -rnE '192\.168\.|(^|[^0-9.])100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+|\.ts\.net|/Users/|KRA-[0-9]|lin_api_|ghp_|github_pat_|sk-[A-Za-z0-9]{16}|BEGIN [A-Z ]*PRIVATE KEY' "$PUB" --exclude-dir=.git --exclude-dir=__pycache__ --exclude=publish-public.sh || true)"
if [[ -n "$leaks" ]]; then print -u2 "publish: leak scan FAILED, nothing committed:"; print -u2 -- "$leaks" | cut -c1-160; git -C "$PUB" checkout -q -- .; git -C "$PUB" clean -qfd; exit 1; fi
for f in "$PUB"/*.py; do python3 -c 'import ast,sys;ast.parse(open(sys.argv[1]).read())' "$f"; done  # no __pycache__
for f in "$PUB"/*.json; do python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$f"; done
git -C "$PUB" add -A
if git -C "$PUB" diff --cached --quiet; then print "publish: public edition already current"; exit 0; fi
git -C "$PUB" diff --cached --stat | tail -1
[[ "${1:-}" == "--dry-run" ]] && { git -C "$PUB" reset -q; git -C "$PUB" checkout -q -- .; git -C "$PUB" clean -qfd; print "publish: dry run, reverted"; exit 0; }
git -C "$PUB" commit -q -m "sync from estate-guardian $(git -C "$SRC" rev-parse --short HEAD)"
git -C "$PUB" push -q origin main
print "publish: pushed $(git -C "$PUB" rev-parse --short HEAD) to Forgejo; GitHub follows via the push mirror"
