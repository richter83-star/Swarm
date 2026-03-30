# Decisions

Key architectural and project decisions made during sessions.

<!-- Format: ## YYYY-MM-DD: <topic> followed by context and rationale -->

## 2026-03-30: Implement Persistent Memory System

**Decision:** Create a multi-file persistent memory system to preserve knowledge across Claude Code sessions.

**Implementation:**
- Four memory files in `/home/user/Swarm/memory/`:
  - `decisions.md` — architectural/project decisions (format: ## YYYY-MM-DD: topic)
  - `people.md` — collaborators and contacts (format: ## Name — Role/notes)
  - `preferences.md` — working style and conventions (format: - topic: preference)
  - `user.md` — background, goals, project context

**Integration:**
- `CLAUDE.md` instructs Claude to silently read all four files at session start
- Stop hook (agent-type) automatically reviews conversation at session end and updates files with worth-preserving information
- Only selective updates—no duplication of existing content

**Rationale:** Enable long-term project continuity by maintaining institutional knowledge across sessions without manual intervention.

## 2026-03-30: Telegram Morning & Evening Briefing Bot (Complete)

**Decision:** Build automated daily briefing system that sends to Telegram at 8am and 9pm with:
- **Morning (8am):** Last session context + 3 AI-generated priorities for today
- **Evening (9pm):** What moved that day + open tasks + first priority for tomorrow

**Status:** ✅ Live and operational. Test message successfully sent to Telegram.

**Architecture:** Cron-based shell scripts (not Claude scheduled task):
- `morning_briefing.sh` @ 8:00 AM — reads memory files, generates priorities via Claude API, sends to Telegram
- `evening_summary.sh` @ 9:00 PM — reads todos, generates summary via Claude API, sends to Telegram
- Both scripts registered in `/etc/cron.d/swarm-briefing`
- Cron daemon manually started (`/usr/sbin/cron`) since container lacks systemd

**Implementation Details:**
- Telegram bot token and chat ID embedded directly in scripts (retrieved during session)
- Claude API calls for briefing generation
- Uses local environment (no external scheduling service)

**Known Constraint:** Cron daemon requires manual restart if container restarts (not persistent through systemd). Can be added to container startup mechanism if needed.

**Rationale:** Replace manual daily planning with AI-powered, context-aware briefing. Keeps personal priorities aligned with actual project state across sessions. Local cron solution avoids external dependencies.

## 2026-03-30: Real-Time Todo Dashboard with Supabase (In Progress)

**Decision:** Build a real-time to-do dashboard using Next.js and Supabase for live task tracking with instant updates across agents.

**Requirements:**
- **Frontend:** Next.js with dark, minimal, clean styling
- **Backend:** Supabase with Realtime enabled for websocket updates
- **Database schema:** todos table with fields: title, status, priority, assigned_agent, updated_at
- **Real-time sync:** When an agent updates task status in Supabase, UI reflects changes instantly (no refresh needed)

**Status:** ⏳ Awaiting credentials. User must create Supabase project and provide:
1. Project URL (`https://xxxxxxxxxxxx.supabase.co`)
2. Anon public key (`eyJ...` string)

**Rationale:** Enable autonomous agents to update task status directly in database while providing real-time visibility to the system owner. Single source of truth for task state across all components.
