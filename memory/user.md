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

- **Telegram Morning Briefing**: Scheduled daily task to send AI-powered briefings at 8am. Needs Telegram bot token + chat ID from user.
- **Memory Infrastructure**: Persistent memory system now active (decisions, people, preferences, user). Stop hook updates files at session end.
