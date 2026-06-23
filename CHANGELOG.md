# Changelog

## 2.4.0 - 2026-06-23

- Fixed `reset_timer` so a sender's debounce window starts from their newest message.
- Made `@Bot` matching strict by default and added an opt-out for legacy adapters.
- Preserved non-text components when injecting merged messages.
- Added Heartflow compatibility mode, debounce group allow/deny lists, and bounded state cleanup.
- Synchronized configuration documentation and removed generated repository artifacts.
