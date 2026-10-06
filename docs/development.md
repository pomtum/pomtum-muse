# Development

See the repository [home page](../README.md) for installation, pairing and usage.

## Layout

| Directory | Purpose |
| --- | --- |
| `sdk/` | Fixed upstream Linux SDK plus local chat, event and token-entry bridge. |
| `ui/` | TypeScript / Canvas frontend, built with Vite. |
| `device-io/` | Dedicated physical-key capture and local Piper speech. |
| `scripts/` | Local asset preparation, install/uninstall and publication checks. |

## Host checks

Run on Linux (WSL also works); tests use temporary state and synthetic credentials.

```sh
python3 -m venv .venv
.venv/bin/pip install -e './sdk[test]'
(cd sdk && ../.venv/bin/python -m pytest -q)
python3 -B -m unittest discover -s device-io/tests -v
python3 -B -m unittest discover -s scripts/tests -v
bash -n scripts/install-companion.sh
bash -n scripts/uninstall-companion.sh
python3 scripts/prepare-avatar.py
python3 ui/tools/avatar-verify.py
(cd ui && npm ci && npm run build && npm audit --audit-level=moderate)
python3 scripts/check-publication.py
git diff --check
```

`prepare-avatar.py --download-only` fetches verified source without requiring
GCC. Full asset building and C-output comparison require GCC. Generated assets
stay ignored by Git and are not uploaded by CI.

For a frontend-only preview, run `npm run dev` inside `ui/`. The SDK and audio
APIs will be unavailable there. Integrated operation uses the loopback bridge
on port 17863; the I/O sidecar permits only that UI origin. Do not relax origin
checks just to make a development server reach live device APIs.

## Token handling

`GET /api/sdk-token` returns only `{configured: boolean}`. Same-origin JSON
`POST /api/sdk-token` accepts `{token: string}`, capped at 512 bytes. The bridge
passes it over the existing peer-checked SDK Unix socket. The root SDK validates
the token, writes a temporary file with mode 0600, fsyncs and replaces its normal
credential file. It returns only saved/pairing-required status; it never returns
the token, fragments, or length. No logging, browser persistence or automatic
re-pairing is added. Initial installation uses the official hidden prompt.

## Device acceptance

Use your own account and a verified development device. Check initial pairing,
real key press/release, recording, cloud reply, audible playback, local stop,
caption clearing, history, key release on blur and desktop return. Verify token
entry with your own credentials locally; never attach credentials to a report.
Host tests and a rendered preview are separate from this hardware acceptance.

Upstream provenance and changes are listed in [upstream.md](upstream.md).
