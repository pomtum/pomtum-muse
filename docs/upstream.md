# What changed from upstream

Base: [Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk/tree/b139b45064b4dcecf7bfe97e75bc7f99c10c28b6),
commit `b139b45064b4dcecf7bfe97e75bc7f99c10c28b6`, `linux/` directory.

The four original tools remain: `system.run`, `file.read`, `file.write`, and
`device.health`. Registration stays `platform=linux`, `device_family=homehub`.
BLE pairing, Noise, token storage, and command execution as the chosen user
retain upstream behavior. There is no ESP32 firmware/OTA impersonation.

Changes by PomTum contributors:

- `chat.py`: reply subscription, bounded ACK/replay state and turn correlation;
  parentless initial replies and per-event-domain sequence deduplication.
- `link_client.py`: same encrypted session handles chat subscription and a
  dedicated identity request. The latter is optional and may be unavailable.
- `service.py`: Unix-socket operations for foreground chat and event delivery,
  plus root-only atomic SDK token saving, preserving peer credential checks and
  the original command executor.
- `http_bridge.py`: loopback-only static UI, HTTP and SSE; Host/Origin checks,
  bounded requests, a write-only SDK token entry endpoint, configured-only token
  status and no automatic resend after a lost ACK.
- `pyproject.toml`: bridge entry point. Corresponding chat, identity, HTTP and
  service tests cover the additions; the remaining upstream tests are retained.
- SDK docs are adjusted to this repository; the original installer is retained.

The independent `device-io/` sidecar and `ui/` implement physical-key audio,
optional local speech, animation and transient main-screen captions. Those
components do not replace Muse's cloud identity, conversation or Linux tools.

Stopping a turn stops local waiting/playback. It does **not** promise to cancel
an already-running cloud task or shell command. A reconnection permits future
turns; it does not retrieve arbitrary cloud history. History here is local to
the dedicated browser profile.

The character source and generated frames are fetched/built locally, as
described in [THIRD_PARTY.md](../THIRD_PARTY.md). They are not Apache-licensed
repository content.
