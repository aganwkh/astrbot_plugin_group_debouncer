# Changelog

## 2.4.3 - 2026-09-27

- Kept `raw_message` in sync with the injected component chain. Non-text OneBot
  segments from the debounce window are carried back into `raw_message`, so
  downstream plugins can still read `sub_type`/`summary` to tell stickers apart
  from normal images.
- Fixed stickers being treated as ordinary images: `Image` components carry only
  `file`/`url`/`path`, so sticker detection must read `raw_message`. When a pure
  sticker was followed by text, the merged event's `raw_message` held no image
  segment at all, so stickers were described in full and sent to the main model.
- Added regression tests covering raw-segment sync across injection strategies,
  including a negative control that ordinary images stay ordinary.

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
