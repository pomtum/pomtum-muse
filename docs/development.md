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
Install [Gitleaks 8.30.1 or newer](https://github.com/gitleaks/gitleaks/releases),
then enable this checkout's hooks before editing:

```sh
gitleaks version
python3 scripts/install-git-hooks.py
```

The setup changes only this repository's `core.hooksPath`. It refuses to replace
another configured hook directory or existing pre-commit/pre-push hooks. If you
already use hooks, integrate the commands below into that setup.

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
python3 scripts/check-publication.py --history
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

## Before committing or pushing

The **pre-commit** hook checks actual staged bytes, not the working-tree copy,
then runs Gitleaks against staged changes. The **pre-push** hook additionally
scans full local reachable history, including tags, detached HEAD and the exact
object IDs Git is about to push. A secret deleted from the latest file can still
block a push because it remains in an earlier commit. A shallow clone is rejected
until `git fetch --unshallow --tags` completes. Missing or failed Gitleaks blocks
the check; diagnostic output contains paths/rules/line numbers, never token text.

```sh
# The hooks run these automatically once enabled in this clone:
python3 scripts/check-publication.py --with-gitleaks  # staged content
python3 scripts/check-publication.py --history        # staged + full history
```

Gitleaks retains its default provider/generic rules and adds Muse's `mgst_`
format. Allowlisting is restricted to exact, documented public test-vector
values in their original file and a Python type annotation. No whole test
directory, file, or commit is exempted. `gitleaks:allow` comments and an external
ignore file do not silently exempt additional findings in this wrapper.

GitHub Secret Scanning and Push Protection are enabled for the official
repository. Their supported provider patterns complement the local Muse rule.
CI fetches full history and repeats the checks, using a pinned SHA-256-verified
Gitleaks release. **CI is after upload**, so it does not replace local hooks or
server-side protection. Git does not automatically enable hooks in a new clone;
each contributor needs the setup command above. Local hooks remain bypassable,
and neither pattern scanning nor these controls guarantee detection of every
possible secret. Review staged files before publishing.

## Device acceptance

Use your own account and a verified development device. Check initial pairing,
real key press/release, recording, cloud reply, audible playback, local stop,
caption clearing, history, key release on blur and desktop return. Verify token
entry with your own credentials locally; never attach credentials to a report.
Host tests and a rendered preview are separate from this hardware acceptance.

Upstream provenance and changes are listed in [upstream.md](upstream.md).
