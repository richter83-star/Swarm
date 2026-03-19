# CLAUDE.md — Powerhouse AI Global Standard

## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update tasks/lessons.md with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

---

## Task Management

1. **Plan First** — Write plan to tasks/todo.md with checkable items
2. **Verify Plan** — Check in before starting implementation
3. **Track Progress** — Mark items complete as you go
4. **Explain Changes** — High-level summary at each step
5. **Document Results** — Add review section to tasks/todo.md
6. **Capture Lessons** — Update tasks/lessons.md after corrections

---

## Core Principles

- **Simplicity First** — Make every change as simple as possible. Impact minimal code.
- **No Laziness** — Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact** — Only touch what's necessary. No side effects with new bugs.

---

## Project Context — Kalshi Swarm

- **4 bots:** sentinel (politics/news), oracle (economics/macro), pulse (weather/science), vanguard (general/sports)
- **VPS:** `vmi3134862.contaboserver.net` — `ssh root@vmi3134862.contaboserver.net` (pw: Kosh1997!)
- **Project path:** `/root/Swarm/Swarm-Kalshi`
- **Start:** `bash launch.sh` (sources .env, starts daemon → 4x bot_runner)
- **Research:** Tavily DISABLED (quota blown). DuckDuckGo active (free). Re-enable: `providers.tavily.enabled: true` + Starter plan ~$20/mo
- **Always check logs before touching DB** — `tail -f logs/swarm.log`
- **Never restart without confirming all 4 bots come back up** — `ps -ef | grep bot_runner | grep -v grep`
- **DB reset pattern:** use paramiko Python scripts, not raw SSH multiline SQL
- **LLM gate:** Central LLM controller tracks its own decision outcomes separately from bot DBs — check both when diagnosing trade blocks
- **Lessons learned:**
  - Tavily burns 1000 free calls in a single day at default settings (max_queries:6, min_researchability:25)
  - In-memory research cache does NOT persist across restarts or share between bot processes
  - LLM self-assessment (central_llm_controller.db) is separate from bot trade DBs — clear both when resetting learning
  - `pkill` alone won't stop the swarm — must kill `swarm_daemon.py` or the daemon auto-revives bots
  - Weight history and category_stats must be cleared together for a true fresh learning start
