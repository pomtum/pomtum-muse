# Source change log

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
