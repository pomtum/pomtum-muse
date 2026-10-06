# Source change log

## CHG-20261007-002

- Date: 2026-10-07 00:55 Asia/Shanghai.
- Commit: resolved by the `Wiki-Entry: CHG-20261007-002` Git trailer.
- Title: `fix: prevent credential exposure before publication and during setup`.
- Purpose: resolve operational hardening findings after the initial public
  review. No real credential leak was found in the initial public history.
- Changes: remove installer token-value arguments and unsafe CLI guidance;
  support hidden TTY and owner-only file/stdin input with bounded validation and
  root-only atomic storage. Default the optional Pebble example to loopback,
  remove raw session/output logging and bound request reads. Add local Git
  hooks, staged-byte and full-history Gitleaks checks, a pinned verified CI CLI,
  and exact-value exceptions for documented upstream synthetic vectors only.
- Validation: Linux SDK 256 passed / 1 root-user permission test skipped;
  14 isolated token-input/storage tests passed, including hidden terminal echo,
  no argv/environment transport, owner repair and atomic failure preservation.
  Eight publication regression tests passed on Windows and Linux, including a
  real rejected push to a disposable local bare repository and a secret deleted
  from HEAD but retained in old commits. Existing I/O/installer checks are run
  by CI. Shell syntax, diff checks and staged/full-history secret checks passed.
- Remote controls: GitHub Secret Scanning and Push Protection enabled on this
  repository; these cover supported patterns and complement the local Muse rule.
- Compatibility/risks: `--sdk-token VALUE` is deliberately removed; migrate
  automation to `--sdk-token-file PATH` or redirected stdin. Pebble LAN users
  must explicitly set `PEBBLE_HOST` and provide a protected network/transport.
  Hooks must be enabled per clone and can be bypassed; scans are not a guarantee
  that every possible secret is detectable. No real device was changed.
- Rollback: review and revert affected source changes if necessary; no stored
  credentials, cloud account or existing public Git history was rewritten.
- Image update: none.

## CHG-20261007-001

- Date: 2026-10-07 00:16 Asia/Shanghai.
- Commit: resolved by the `Wiki-Entry: CHG-20261007-001` Git trailer.
- Title: `feat: publish PomTum Muse Linux companion`.
- Purpose: share a reproducible hobby companion with a home-page setup guide
  and a local SDK-token entry, using an independently reviewed initial tree.
- Changes: pinned upstream Linux SDK and local chat/event/token bridge;
  TypeScript UI with transient captions; dedicated input/Piper sidecar;
  portable install/uninstall; verified local-only character asset preparation;
  Chinese and English home-page guides, provenance, security notes and CI.
- Host validation: Linux SDK 243 passed / 1 root-user permission test skipped;
  25 I/O and 8 isolated installer tests passed; both shell scripts passed
  `bash -n`; TypeScript/Vite 7.3.7 production build passed; npm audit found
  zero vulnerabilities. Original-C animation comparisons and source SHA-256
  verification passed. Token UI inspected at desktop and small-screen sizes,
  including invalid-input clearing. Git-index publication scan is required
  before commit and repeated in CI.
- Device boundary: earlier prototype key/cloud/UI/caption behavior was tested
  on RK3576 Ubuntu 24.04. New public token UI and portable fresh installation
  have host verification; they were not installed on a device in this release.
- Compatibility/risks: requires existing Linux desktop/audio/input permissions,
  SDK account and network access. Uses local Piper speech and ESP32 animation,
  not the App 3D model or official voice. Artwork/model rights are separate.
- Rollback: companion uninstall preserves official SDK credentials and profiles;
  explicitly restart the SDK afterward to unload the source overlay.
- Image update: none.
