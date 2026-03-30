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
