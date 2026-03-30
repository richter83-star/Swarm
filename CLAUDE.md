# Claude Instructions

## Persistent Memory

At the start of every session, read all five memory files to restore context:

- `memory/decisions.md` — past architectural and project decisions
- `memory/people.md` — collaborators, stakeholders, and contacts
- `memory/preferences.md` — working style and conventions to follow
- `memory/user.md` — background on the user and their goals
- `memory/personality.md` — how to think, communicate, and make decisions with this user

Do this silently without announcing it unless there is something important to surface.

## Updating Memory

At the end of each session (via the Stop hook), update the memory files with anything new or changed:

- **decisions.md**: any significant decisions made, with date and rationale
- **people.md**: new people mentioned or updated context about existing ones
- **preferences.md**: any new preferences or conventions observed
- **user.md**: updated goals, project status, or other useful background

Be selective — only record things worth remembering across sessions. Do not pad with trivial details.
