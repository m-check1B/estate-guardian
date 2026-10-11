#!/bin/bash
# Install or reload the GUARDIAN launchd agent on the Mac Pro (every 5 min, :01/:06/...).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
label=com.verduona.estate-guardian
plist=$HOME/Library/LaunchAgents/$label.plist
mins=""
for m in 1 6 11 16 21 26 31 36 41 46 51 56; do mins+="<dict><key>Minute</key><integer>$m</integer></dict>"; done
cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$here/guardian.py</string></array>
<key>StartCalendarInterval</key><array>$mins</array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:$HOME/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
<key>StandardOutPath</key><string>$HOME/Developer-artifacts/guardian/launchd.out</string>
<key>StandardErrorPath</key><string>$HOME/Developer-artifacts/guardian/launchd.err</string>
<key>ProcessType</key><string>Background</string>
</dict></plist>
EOF
mkdir -p "$HOME/Developer-artifacts/guardian"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl print "gui/$(id -u)/$label" | grep -E "state|path" | head -3
