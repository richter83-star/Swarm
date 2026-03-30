# User

Background context about the user — goals, project context, and anything useful to remember across sessions.

<!-- Keep this concise and updated. -->

## Project: Swarm

Building **Swarm** — a system related to Kalshi (prediction markets). Focus on autonomous, production-ready infrastructure.

## Key Characteristics

- **Business-driven**: Decisions and infrastructure prioritize MRR (monthly recurring revenue) and operational outcomes
- **Production-minded**: Live money system — operational discipline is non-negotiable
- **Rapid iteration**: Ship, observe, fix, repeat. Tight feedback loops preferred over extensive planning
- **Autonomy**: Builds systems and agents that run themselves with minimal supervision
- **Technical discipline**: Root-cause analysis, no temporary fixes, no suppressed errors

## Working with This User

- Expect short, precise messages — respond in kind
- No hand-holding; they want action not explanation
- Treat every decision as live/production (because it is)
- Use subagents and parallel work to keep main context clean
- Document operational lessons learned, not trivial fixes

## Current Work in Progress

- **Memory Infrastructure**: ✅ Complete. Persistent memory system fully active (decisions, people, preferences, user, personality). Stop hook updates files at session end.
- **Telegram Daily Briefings**: ✅ Complete. Morning (8am) + evening (9pm) cron jobs sending to Telegram via local scripts. Test message confirmed received. Smoke test verified scripts are solid.
- **Real-Time Todo Dashboard**: ⏳ In progress. Next.js + Supabase frontend with Realtime enabled for instant agent task updates. Awaiting Supabase project credentials.
- **Git Hygiene**: ✅ Logs directory gitignored. No ephemeral log files committed to repo.
- **Swarm Bot System**: ⏳ Dormant since March 17 (12 days). Capital preserved ($51–53). LLM gate too tight; vanguard severely underperforming (0% WR, -$2.75 PnL, all negative feature importances). Latest status analysis complete; awaiting SSH access to pull live logs before deciding recovery path (full restart vs. reset vanguard learning vs. other).
