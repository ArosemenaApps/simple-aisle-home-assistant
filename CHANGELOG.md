# Changelog

## 0.2.0 (2026-11-16)
- The pairing QR code now scales to fit. In 0.1.0 it was drawn at a fixed size and Home Assistant's panel cropped
  it to one corner, so phones couldn't scan it.
- A store made private, archived or deleted stays paused until you turn it back on in Simple Aisle. A phone that
  hasn't caught up with iCloud can no longer share it again.
- Removing a private store's items from a list is retried until it's done, even if the list was unavailable or
  Home Assistant restarted.
- An item bought or removed in Simple Aisle is confirmed only once it's really checked off or gone in Home
  Assistant; nothing is lost while a list is unavailable.
- Changes to one item made in Home Assistant reach Simple Aisle in order, even with several phones.
- When Home Assistant and Simple Aisle change the same item, Simple Aisle's newer change wins; changes to different
  fields are both kept. (Needs Simple Aisle 1.2 build 19 or later for the field-by-field part.)

## 0.1.0 (2026-11-16)
- First release. QR pairing with per-device keys; stores linked to Shopping List or Local To-do lists (including new
  lists); two-way sync with a durable change queue; `simple_aisle.move_item`; Request sync notifications; admin
  panel; status sensors; diagnostics.
