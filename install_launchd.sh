#!/bin/bash
# Installs a launchd job to run the Peloton dashboard update daily at 20:00

PLIST="$HOME/Library/LaunchAgents/com.soroosj.peloton-dashboard.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/Library/Logs"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.soroosj.peloton-dashboard</string>

    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/venv/bin/python3</string>
        <string>${SCRIPT_DIR}/update_peloton.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>20</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/peloton-dashboard.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/peloton-dashboard.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

# Unload if already loaded, then load
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"

echo "✅ Installed. Will run daily at 20:00."
echo "   Logs: $LOG_DIR/peloton-dashboard.log"
echo "   To uninstall: launchctl unload $PLIST && rm $PLIST"
