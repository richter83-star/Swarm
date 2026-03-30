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

## 2026-03-30: Telegram Morning Briefing Bot (In Progress)

**Decision:** Build an automated daily briefing bot that sends to Telegram at 8am with:
- Summary of last work session from memory files
- Current active task from `/todos/active.md`
- 3 AI-generated priorities for the day

**Status:** Awaiting Telegram bot token and chat ID (user selected "paste them now" option)

**Architecture:** Claude Code scheduled task (8am daily) that:
1. Reads all persistent memory files
2. Loads `/todos/active.md` for context
3. Generates briefing summary via Claude API
4. Sends formatted message to Telegram

**Rationale:** Replace manual daily planning with AI-powered, context-aware briefing. Keeps personal priorities aligned with actual project state across sessions.
