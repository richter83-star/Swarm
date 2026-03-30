# Preferences

Working style, tooling preferences, and conventions to follow.

<!-- Format: - <topic>: <preference> -->

- **Memory management**: Selective updates—only record insights worth preserving across sessions, avoid duplication
- **Session persistence**: Claude reads all memory files silently at session start, updates them at session end via automated Stop hook
- **Infrastructure ownership**: Build systems that run themselves with minimal manual intervention (autonomous, not orchestrated)
- **Local-first solutions**: Prefer local cron/scripts over external scheduling when simpler and more controllable
- **Verification discipline**: Always verify outputs work (e.g., test messages sent) before marking complete
- **Container awareness**: Account for container environment constraints (no systemd, manual daemon startup)
- **Risk management focus**: Capital preservation is paramount; LLM gates are feature, not bug. Overly-tight gating is acceptable vs. losses
- **Performance monitoring**: Track bot win rates, PnL, and feature importance trends as leading indicators of edge degradation
- **Learning state integrity**: Monitor vanguard's feature importances for systemic issues before resuming trading
- **Remote system diagnosis**: When local logs are stale, prefer direct SSH access over speculation; offer user multiple access paths (SSH from external, copy fresh logs, install SSH client locally)
