#!/usr/bin/env bash
# Evening summary — reviews the day, updates memory, sends to Telegram
# Scheduled via cron: 0 21 * * * /home/user/Swarm/scripts/evening_summary.sh

set -euo pipefail

REPO="/home/user/Swarm"
BOT_TOKEN="8420406358:AAHNq6G4sY3aZgj9E9uFzv2qnEky0DDmt4o"
CHAT_ID="7745880788"
CLAUDE="/opt/node22/bin/claude"
LOG="/home/user/Swarm/logs/briefing.log"

mkdir -p "$(dirname "$LOG")"
echo "[$(date)] Starting evening summary" >> "$LOG"

# Build context
CONTEXT=$(cat <<EOF
Today is $(date '+%A, %B %-d, %Y').

--- memory/decisions.md ---
$(cat "$REPO/memory/decisions.md" 2>/dev/null || echo "(empty)")

--- memory/user.md ---
$(cat "$REPO/memory/user.md" 2>/dev/null || echo "(empty)")

--- todos/active.md ---
$(cat "$REPO/todos/active.md" 2>/dev/null || echo "(empty)")

--- Recent git activity (last 24h) ---
$(git -C "$REPO" log --oneline --since="24 hours ago" 2>/dev/null || echo "(no commits today)")
EOF
)

PROMPT="You are generating a concise end-of-day summary for a developer.

Based on the context below, produce a summary with exactly this structure:

*End of day — $(date '+%A, %B %-d').*

*What moved today:*
1-2 sentences on what was accomplished or changed based on git activity and tasks.

*Still open:*
Bullet list of any active tasks that remain incomplete (max 3). If nothing open, say 'All clear.'

*Tomorrow:*
One sentence on the most important thing to pick up first.

Keep it tight. No padding.

---
$CONTEXT"

SUMMARY=$(echo "$PROMPT" | "$CLAUDE" --print 2>>"$LOG")

if [ -z "$SUMMARY" ]; then
  echo "[$(date)] ERROR: Claude returned empty output" >> "$LOG"
  exit 1
fi

# Send to Telegram
RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${SUMMARY}" \
  --data-urlencode "parse_mode=Markdown")

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('ok') else 'FAIL: '+str(d))" 2>/dev/null || echo "parse_error")
echo "[$(date)] Telegram send: $STATUS" >> "$LOG"
