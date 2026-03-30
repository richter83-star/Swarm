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

## 2026-03-30: Logs Excluded from Git via .gitignore

**Decision:** Do not commit log files; instead add `.logs/` to `.gitignore` to prevent accidental version control of ephemeral logs.

**Rationale:** Log files are environment-specific and generated at runtime (e.g., `briefing.log`, smoke tests). They pollute repo history and are not useful to track. Cleaner repository state without requiring manual cleanup.

## 2026-03-30: Swarm Bot System Analysis — Status & Recovery Path

**Context:** Swarm has been inactive since March 17 (~12 days). Last graceful shutdown via SIGTERM at 01:20 UTC that day.

**Current State (March 15–18 logs):**
- **Capital preserved:** $51.54–$53.09 range; peak was $53.09, currently ~$1.55 below peak
- **No active trading:** Zero trading activity since shutdown
- **LLM gate is overly restrictive:** Central LLM rejecting dozens of trades/hour citing:
  - Confidence floor violations (rejecting 65–69% confidence hits at 70% threshold)
  - Vanguard's 0% win rate / -$2.75 cumulative loss as hard block
  - Oracle over-concentration (4 open positions on ~$51 balance)
  - Tavily research dead (432 quota errors) — falling back to DuckDuckGo only

**Bot Performance Snapshot:**
- **sentinel**: ~$51.54 balance, 12.5% WR (8 samples), marginal profitability — blocked by LLM
- **oracle**: ~$51.54 balance, 0% WR (100% breakeven), flat PnL, 3–4 open positions
- **pulse**: ~$51.54 balance, scanning normally, unknown profitability
- **vanguard**: ~$49.80 balance, **0% WR (40 samples), -$2.75 PnL** — mostly blocked by LLM

**Learning Engine Trends (last recorded):**
- **Oracle:** WR improving (+20%), PnL trending up, but bias still negative (-47)
- **Vanguard:** WR trending down (-30%), PnL down (-33¢), all feature importances negative (edge, volume, timing, momentum all red)

**Next Actions:** Either restart swarm to resume trading or reset vanguard's learning state before bringing system back online. LLM guardian is working correctly (protecting capital), but capital sits idle. Decision pending on vanguard recovery vs. full restart.

**Rationale:** Capital preservation demonstrates LLM risk management is functioning. However, extended inactivity with preserved capital suggests opportunity cost of overly-tight gating. Vanguard's negative feature importances are red flag for systemic edge degradation or training data contamination.
