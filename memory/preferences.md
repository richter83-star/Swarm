# Preferences

Working style, tooling preferences, and conventions to follow.

<!-- Format: - <topic>: <preference> -->

- **Memory management**: Selective updates—only record insights worth preserving across sessions, avoid duplication
- **Session persistence**: Claude reads all memory files silently at session start, updates them at session end via automated Stop hook
- **Infrastructure ownership**: Build systems that run themselves with minimal manual intervention (autonomous, not orchestrated)
- **Local-first solutions**: Prefer local cron/scripts over external scheduling when simpler and more controllable
- **Verification discipline**: Always verify outputs work (e.g., test messages sent) before marking complete
- **Container awareness**: Account for container environment constraints (no systemd, manual daemon startup)
