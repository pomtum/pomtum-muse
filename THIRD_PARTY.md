# Third-party code and assets

## Muse Gadget SDK

`sdk/` contains the Apache-2.0 Linux SDK from
[facebookincubator/muse-gadget-sdk](https://github.com/facebookincubator/muse-gadget-sdk),
pinned to `b139b45064b4dcecf7bfe97e75bc7f99c10c28b6`, with the local chat and HTTP
bridge changes described in [docs/upstream.md](docs/upstream.md). Original
copyright headers remain. The root [LICENSE](LICENSE) contains Apache-2.0.
The UI layout adapts the same revision's Apache-licensed `muse_ui.c`.

## Jollybot character

The upstream [README](https://github.com/facebookincubator/muse-gadget-sdk/tree/b139b45064b4dcecf7bfe97e75bc7f99c10c28b6#license)
explicitly excludes `esp32/avatar/` from the Apache license. Accordingly this
repository does **not** ship its C renderer, GIF, generated PNG atlases, or a
prebuilt application containing those assets. No new license is granted for
the character. The README screenshot shows the personal demo and is not a
reusable character asset or part of the Apache grant.

`scripts/prepare-avatar.py` retrieves three hash-verified files from that
official revision into a gitignored directory for a local build. The two
`components/muse` headers have their own Apache notices; the avatar C file has
its original Meta copyright notice. Local generation preserves those notices.
Consult upstream terms before using or redistributing its character.

The adapter samples the unchanged C renderer at 20 fps: 8-second sequences,
a 2-second pet reaction, and five audio levels. It preserves the original
64×64 palette and grid mapping. This is sampled ESP32 animation, not the Muse
App's 3D model or a phoneme/viseme API.

## Speech and build dependencies

- [Piper](https://github.com/OHF-Voice/piper1-gpl): GPL-3.0, installed separately
  by the user. This repository includes neither Piper binaries nor models.
- [Piper voice models](https://huggingface.co/rhasspy/piper-voices): each voice
  has its own model card. Review the chosen card; do not infer a voice license
  from the engine license or the repository's top-level metadata.
- Python SDK dependencies: `cryptography` and `websockets`; their licenses
  remain their own. Version constraints and the upstream device lockfile are
  in `sdk/`.
- Development: TypeScript (Apache-2.0), Vite (MIT), and transitive npm
  dependencies, installed from the pinned `ui/package-lock.json`.

No community Muser/Three.js character code is included. Muse and Meta marks
belong to their owners; this project is not endorsed by Meta.
