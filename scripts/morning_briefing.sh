#!/usr/bin/env bash
# Morning briefing — reads memory + todos, generates priorities, sends to Telegram
# Scheduled via cron: 0 8 * * * /home/user/Swarm/scripts/morning_briefing.sh

set -euo pipefail

REPO="/home/user/Swarm"
BOT_TOKEN="8420406358:AAHNq6G4sY3aZgj9E9uFzv2qnEky0DDmt4o"
CHAT_ID="7745880788"
CLAUDE="/opt/node22/bin/claude"
LOG="/home/user/Swarm/logs/briefing.log"

mkdir -p "$(dirname "$LOG")"
echo "[$(date)] Starting morning briefing" >> "$LOG"

# Build context from memory files and active todos
CONTEXT=$(cat <<EOF
Today is $(date '+%A, %B %-d, %Y').

--- memory/decisions.md ---
$(cat "$REPO/memory/decisions.md" 2>/dev/null || echo "(empty)")

--- memory/user.md ---
$(cat "$REPO/memory/user.md" 2>/dev/null || echo "(empty)")

--- memory/preferences.md ---
$(cat "$REPO/memory/preferences.md" 2>/dev/null || echo "(empty)")

--- memory/people.md ---
$(cat "$REPO/memory/people.md" 2>/dev/null || echo "(empty)")

--- memory/personality.md ---
$(cat "$REPO/memory/personality.md" 2>/dev/null || echo "(empty)")

--- todos/active.md ---
$(cat "$REPO/todos/active.md" 2>/dev/null || echo "(empty)")
EOF
)

PROMPT="You are generating a concise morning briefing for a developer.

Based on the context below, produce a briefing with exactly this structure:

*Good morning. Here's your briefing for $(date '+%A, %B %-d').*

*Last working on:*
1-2 sentences summarising what was most recently in progress based on decisions and active tasks.

*Today's 3 priorities:*
1. <priority>
2. <priority>
3. <priority>

Keep it tight — no padding, no filler. Priorities should be concrete and actionable given the project state.

---
$CONTEXT"

# Generate briefing via Claude CLI (non-interactive, print mode)
BRIEFING=$(echo "$PROMPT" | "$CLAUDE" --print 2>>"$LOG")

if [ -z "$BRIEFING" ]; then
  echo "[$(date)] ERROR: Claude returned empty output" >> "$LOG"
  exit 1
fi

# Send to Telegram
RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${BRIEFING}" \
  --data-urlencode "parse_mode=Markdown")

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('ok') else 'FAIL: '+str(d))" 2>/dev/null || echo "parse_error")
echo "[$(date)] Telegram send: $STATUS" >> "$LOG"
