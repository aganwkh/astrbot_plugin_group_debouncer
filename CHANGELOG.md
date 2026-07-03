# Changelog

## 2.4.2 - 2026-07-03

- Prevented the repeat feature from echoing the bot's own freshly sent replies when humans repeat them.

## 2.4.1 - 2026-07-02

- Fixed repeat handling so `repeat_enabled=true` works even when `heartflow_compat_mode=true`.
- Preserved pure image/non-text components when the same sender follows them with text.

## 2.4.0 - 2026-06-23

- Fixed `reset_timer` so a sender's debounce window starts from their newest message.
- Made `@Bot` matching strict by default and added an opt-out for legacy adapters.
- Preserved non-text components when injecting merged messages.
- Added Heartflow compatibility mode, debounce group allow/deny lists, and bounded state cleanup.
- Synchronized configuration documentation and removed generated repository artifacts.
