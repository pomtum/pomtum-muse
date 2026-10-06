# Security

Muse can run commands and read/write files as the account you pair with it.
Choose that account deliberately. An account with passwordless sudo can expose
the entire machine. Pair through the official SDK flow and keep its token and
pairing state out of Git and public logs.

Initial token input is hidden. For automation, use `--sdk-token-file PATH`
with a private file, or `--sdk-token-file -` with token-only stdin. The former
`--sdk-token VALUE` argument is rejected; do not put credentials in process
arguments. See the home-page setup guide for interactive installation.

The HTTP bridge and I/O sidecar bind only to loopback. Do not expose ports
17863/17864 through a public tunnel or reverse proxy. Browser-origin and Unix
peer checks complement loopback binding; they are not a multi-user web login.

Microphone capture requires the configured dedicated key, a focused visible UI,
and a live event subscriber. Losing focus cancels capture. Audio uses temporary
private files and is sent to your paired Muse when you release the key.

History is stored in the dedicated browser profile. “Clear local history” does
not delete Muse cloud history. Uninstalling the companion preserves the profile,
official SDK, pairing state and voice models.

For a suspected vulnerability, use the repository's **Security → Report a
vulnerability** channel if enabled. Do not put credentials or private exploit
details in a public issue.

Contributors should enable this checkout's pre-commit/pre-push checks as described
in [docs/development.md](docs/development.md). The hooks combine repository rules
and Gitleaks; pre-push inspects full reachable history. The official repository
also enables GitHub Secret Scanning and Push Protection. A green CI check occurs
after upload and is not a substitute for preventing a secret from being pushed.
