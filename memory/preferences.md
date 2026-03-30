# Preferences

Working style, tooling preferences, and conventions to follow.

<!-- Format: - <topic>: <preference> -->

- **Memory management**: Selective updates—only record insights worth preserving across sessions, avoid duplication
- **Session persistence**: Claude reads all memory files silently at session start, updates them at session end via automated Stop hook
