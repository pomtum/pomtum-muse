# Muse device I/O

`companion_io.py` is a loopback-only Python standard-library sidecar for the
dedicated Linux input device and Piper TTS. The defaults are device name
`adc-keys-ai` and Linux key code `30`; other boards can supply an exact name and
code. Run as the desktop user with existing input-device access and the user's
PipeWire session:

```sh
XDG_RUNTIME_DIR="/run/user/$(id -u)" \
  /path/to/voice-venv/bin/python companion_io.py \
  --model /path/to/zh_CN-huayan-medium.onnx \
  --key-name adc-keys-ai --key-code 30
```

Install `pw-record`, Firefox, a Piper Python environment and an appropriate
model before installing this companion. The script does not install packages,
change input permissions, modify Muse credentials, or change the loopback bridge
port 17863. No screen microphone button is needed.
Piper is loaded once on a worker thread; synthesis is serialized on HTTP worker
threads. The API remains responsive during synthesis. Cancelling invalidates
pending results; it cannot interrupt an already executing ONNX kernel.

## Browser contract

All endpoints require the exact `Origin` `http://127.0.0.1:17863` or
`http://localhost:17863`. All POST requests require `Content-Type: application/json`.
No cookies or private keys are involved. Requests without Origin, from other
origins, with unexpected Host, or with unapproved preflight methods/headers fail.

| Endpoint | Contract |
| --- | --- |
| `GET /status` | `{available, key_available, recording}`; `available` means Piper loaded. |
| `GET /events` | SSE JSON `type: key, phase: down/up`; `type: audio, audio_wav_base64`; `type: error, message`. SSE comment heartbeat each second. |
| `POST /focus` | `{ "active": true/false }`; focus lease lasts 8 seconds. |
| `POST /cancel` | `{}`; cancel capture and invalidate queued audio / speech results. |
| `POST /tts` | `{ "text": "..." }`, 1–2000 characters; returns `audio/wav`, 409 if cancelled, 503 if unavailable. |

After EventSource opens, send focused visibility state and renew every 3 seconds.
Use `document.hasFocus() && document.visibilityState === "visible"`. On window
blur, hidden visibility and page exit send `active:false`; on reconnect, renew
focus after SSE opens. Stop browser playback immediately on key down / cancel.
Treat key-up reason `limit`, `error`, `focus`, `timeout`, or `cancel` as the end of
the listening animation. `audio` optionally carries a generation integer.

The lease **and** a live SSE subscriber are required before grabbing the key or
starting capture. Lose either and capture is cancelled, never submitted. With
multiple subscribers, only the newest receives audio to avoid duplicate Muse
requests; other events are broadcast. Do not use a background monitoring SSE in
the application. Disconnect detection is bounded by SSE heartbeat/socket errors;
the focus lease independently expires after 8 seconds.

## Input and recording behavior

Discovery requires exactly one `/sys/class/input/event*/device/name` equal to
the configured `--key-name` (default `adc-keys-ai`); event numbers are not
hard-coded. Only the configured `EV_KEY` `--key-code` (default 30, allowed 1..767)
is handled; other keys are neither acted on nor logged. Linux `EVIOCGRAB` grabs this dedicated
input device, not all system keyboards. It is released when focus/subscription
expires (reader polls every 50 ms). A key held when acquiring the device must be
released before a new press can record. Repeats and 80 ms bounce presses are
ignored; a dropped input stream cancels recording.

Choose a dedicated input device, not a general keyboard: the Linux grab applies
to the entire selected device even though only the chosen code is interpreted.
Changing the name/code does not bypass the unique-match rule, focus lease, SSE
subscriber requirement, repeat filtering or held-key release requirement.

Key down starts `pw-record --rate=16000 --channels=1 --format=s16`. Key up stops
the process with SIGINT, lets the WAV header finish, validates and re-encodes it
as canonical PCM16 little-endian mono 16 kHz WAV, then sends the complete base64
WAV. Recordings auto-stop at 15 seconds; shorter than 0.3 seconds are discarded.
Private capture files use `mkstemp`, mode 0600, and are deleted in `finally`.
Process-group termination escalates to SIGTERM / SIGKILL with bounded waits.
No audio is captured while there is no active UI.

The latest press cancels a previous unfinished capture or queued speech. Browser
code must still discard an already received old HTTP response after local cancel;
the server cannot retract bytes already delivered to the browser.

## Validation

```sh
python -m unittest discover -s tests -v
```

Tests exercise actual loopback HTTP/CORS, cancellation during concurrent TTS,
SSE, focus expiry / recording races, WAV limits and exact input discovery with
temporary fake sysfs. No test accesses real input devices, microphones, or tokens.
Device acceptance still needs a physical AI-key press, microphone recording,
speaker playback and loss-of-focus release on the target.

## Portable installation

From the repository root, after installing the official Linux SDK with its
`--run-as` set to the intended non-root desktop user, generate the official avatar
assets and build `ui/dist` as described in the main README. Initial SDK setup uses
the official installer's credential prompt. The companion installer itself does
not read or write a token and does not require pairing to be complete.

```sh
sudo bash scripts/install-companion.sh \
  --user "$USER" \
  --voice-model /absolute/path/to/zh_CN-huayan-medium.onnx \
  --voice-python /absolute/path/to/voice-venv/bin/python \
  --key-name adc-keys-ai --key-code 30

# After the configured user has logged into the desktop:
sudo systemctl restart musegadget.service pomtum-muse-bridge.service pomtum-muse-io.service
```

The `.onnx.json` file must sit next to the model. The model, voice virtualenv,
original SDK and account home must all live outside `/opt/pomtum-muse`. Add
`--dry-run` to perform preflight and print the complete generated plan without
writing files or modifying services. Preflight needs enough privilege to inspect
the installed SDK and run dependency checks as the chosen account, so use sudo
with `--dry-run` too.

The installer verifies the effective SDK run-as account (CLI `--run-as` takes
precedence over `MUSEGADGET_RUN_AS`), model readability, Python imports, required
executables, UI build and avatar hashes. It supports the official SDK service
layout; custom `EnvironmentFile` configurations or an existing different
`PYTHONPATH` overlay require manual review and fail preflight. It derives the SDK
Python executable from the installed service rather than assuming an account or
UID. It does not change groups, networking, firewall rules or existing SDK state.

Installed files and services:

- `/opt/pomtum-muse/`: copied SDK source overlay, device I/O, built UI and launcher.
- `/etc/systemd/system/musegadget.service.d/70-pomtum-muse.conf`: only adds the
  overlay `PYTHONPATH`, preserving the original service command and credentials.
- `pomtum-muse-bridge.service` and `pomtum-muse-io.service`: system units running
  as the selected normal user, with dynamically determined HOME and
  `/run/user/UID`. They are enabled but not started by installation.
- Desktop application **PomTum Muse**: starts `/opt/pomtum-muse/bin/open-companion`
  manually as the selected desktop user. It uses Firefox with a dedicated
  `${XDG_CONFIG_HOME:-$HOME/.config}/pomtum-muse/firefox` profile, no Marionette,
  and password remembering disabled. Other profile preferences and local chat
  data are retained.

The user must already have read access to the dedicated event device and a
working logged-in PipeWire session. The installer intentionally does not grant
input access or change the existing desktop audio setup. Piper/model selection
and their licenses remain the user's responsibility; no model is downloaded.

```sh
sudo bash scripts/uninstall-companion.sh --dry-run
sudo bash scripts/uninstall-companion.sh
sudo systemctl restart musegadget.service
```

Uninstallation stops/disables only the two companion services and removes only
its marked files, overlay and desktop entry. It leaves the original SDK, pairing,
tokens, model, voice environment and all Firefox/chat profiles in place. The
original SDK restart is explicit; removing files alone cannot change Python
modules already loaded in its running process.

Installer verification on Linux/WSL:

```sh
bash -n scripts/install-companion.sh
bash -n scripts/uninstall-companion.sh
python3 -B -m unittest discover -s scripts/tests -v
python3 -B -m unittest discover -s device-io/tests -v
```

Installer tests use a disposable filesystem namespace and fake system commands;
they never call the real `systemctl` or operate a device. They exercise install,
upgrade, dry-run, mismatch/asset rejection, uninstallation and preservation of
unrelated SDK files and profiles. Hardware acceptance remains separate.
