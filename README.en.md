# PomTum Muse

**Give your Muse a small Linux home.** Hold a physical AI key to talk, release to send, watch it think and hear its reply.

[中文](README.md) · [Upstream SDK](https://github.com/facebookincubator/muse-gadget-sdk) · [Development](docs/development.md) · [Third-party notices](THIRD_PARTY.md)

<img src="docs/images/main-screen.png" alt="Muse on a Linux touchscreen with the ESP32 character and purple status ring" width="320">

An independent community hobby project combining the complete Muse Linux Gadget SDK with a touchscreen interface inspired by the official ESP32 UI. The prototype runs on a 4-inch 1080×1200 AMOLED RK3576 device with Ubuntu 24.04 ARM64. Other Linux devices are welcome; hardware integration needs verification on each target.

## Features

- Hold-to-talk using a dedicated physical AI key; no on-screen microphone button. Input capture ends when the UI loses focus.
- Original Linux tools: `system.run`, `file.read`, `file.write`, `device.health`, running with your chosen ordinary account's permissions.
- Idle, listening, thinking, speaking and touch animations; five mouth-animation levels driven by local audio energy.
- Local Piper speech. Main-screen captions use at most three lines and clear about 3 seconds after playback, or 12 seconds after a muted final reply. Full local replies remain in history.
- A password-style SDK token field in Settings. Saving clears the field and writes only to the SDK's protected local credential file; existing values are never returned to the browser.
- Your existing Linux network connection, including cellular or a host network route you have already configured. Pair with **Use current connection**.

The character uses the official ESP32 animation, not the Muse App's real-time 3D model. Speech is local, not Muse's official voice; mouth movement is audio-reactive, not phoneme alignment. You need your own Muse account, SDK access and working network connection.

## Install

Run these commands on the target Linux machine. Skip step 2 if the official SDK is already installed and paired.

### 1. Prerequisites and source

You need systemd, Python 3.10+, Node.js 22.12+, GCC, Firefox, PipeWire and Bluetooth LE for initial pairing. Your desktop account needs existing audio and dedicated input-device permissions. Ubuntu example, with a suitable Node.js version installed separately:

```sh
sudo apt update
sudo apt install git build-essential python3-venv pipewire-bin firefox
git clone https://github.com/pomtum/pomtum-muse.git
cd pomtum-muse
node --version
```

### 2. Install and pair the Linux SDK

Enable **Developer mode** in the Muse App under **Settings → Devices** and obtain your own Gadget SDK token following the App instructions: [gadgets.muse.ai](https://gadgets.muse.ai/).

```sh
bash sdk/install.sh --from "$PWD/sdk" --run-as "$USER"
```

Paste your token at the installer's hidden prompt. Keep it out of command-line arguments, screenshots, issues and Git. The installer starts BLE pairing. Select the advertised device in **Add Device**, then choose **Use current connection**. To reopen pairing:

```sh
sudo musegadget pair
```

Once the SDK and companion bridge are running, add or replace a token from **Settings → SDK 令牌 → 保存** (the UI currently uses Chinese). Tap the second page dot or swipe left to open Settings. Saving preserves the current connection and does not initiate pairing; unpaired devices still need the phone flow above.

### 3. Generate local character assets and build

```sh
python3 scripts/prepare-avatar.py
(cd ui && npm ci && npm run build)
```

The script downloads character source from a pinned official commit, verifies SHA-256 and generates frames using GCC. Those source files and generated frames are not distributed here or covered by this repository's Apache code license. Read [THIRD_PARTY.md](THIRD_PARTY.md) first.

### 4. Set up local speech

Example using Piper's Chinese `zh_CN-huayan-medium` voice. Check the [engine license](https://github.com/OHF-Voice/piper1-gpl) and [model card](https://huggingface.co/rhasspy/piper-voices/blob/main/zh/zh_CN/huayan/medium/MODEL_CARD) before downloading:

```sh
python3 -m venv "$HOME/.local/share/pomtum-muse/voice-venv"
"$HOME/.local/share/pomtum-muse/voice-venv/bin/pip" install piper-tts==1.8.0
"$HOME/.local/share/pomtum-muse/voice-venv/bin/python" -m piper.download_voices \
  --data-dir "$HOME/.local/share/pomtum-muse/voices" zh_CN-huayan-medium
```

Keep the `.onnx` and matching `.onnx.json` together. The companion installer checks these files and Python imports; it does not download a voice for you.

### 5. Install and start the companion

Use the same desktop account you selected for the SDK:

```sh
sudo bash scripts/install-companion.sh \
  --user "$USER" \
  --voice-model "$HOME/.local/share/pomtum-muse/voices/zh_CN-huayan-medium.onnx" \
  --voice-python "$HOME/.local/share/pomtum-muse/voice-venv/bin/python"
```

Add `--dry-run` to inspect the complete plan without changes. Default input: device name `adc-keys-ai`, Linux key code `30`. For other hardware use `--key-name 'dedicated-device-name' --key-code NUMBER`. Choose a dedicated device: Linux's exclusive grab covers the entire input device, making an ordinary keyboard unsuitable. The installer does not change groups or input permissions. See [device I/O](device-io/README.md).

After that account has logged into its desktop:

```sh
sudo systemctl restart musegadget.service pomtum-muse-bridge.service pomtum-muse-io.service
```

Open **PomTum Muse** in the applications menu, or:

```sh
/opt/pomtum-muse/bin/open-companion
```

The launcher uses a dedicated Firefox profile, disables password remembering and opens `http://127.0.0.1:17863/`. Installation enables services; the explicit restart above activates them. Existing custom SDK EnvironmentFile / PYTHONPATH overrides require manual integration and fail preflight.

## Use and troubleshoot

| Action / symptom | What to do |
| --- | --- |
| Talk | Focus the window; hold the AI key and release to send. Maximum recording length: 15 seconds. |
| Full history / typing | Swipe left or tap the second page dot; select 对话记录 / 键盘输入. |
| Mute speech | Tap the speaker or toggle 朗读回复 in Settings. |
| Stop | Stops local waiting/playback; it does not guarantee cancellation of an already-running cloud task. |
| Captions disappeared | Expected main-screen cleanup; the complete reply remains in local history. |
| Key does nothing | Check focus, exact input-device name/code and your account's event-device permissions. |
| No local voice | Check the Piper environment, both model files, user PipeWire session and `pw-record`. |
| Reconnecting | Check SDK pairing, account access and the current network route. Saving a token does not automatically pair again. |

```sh
systemctl status musegadget.service pomtum-muse-bridge.service pomtum-muse-io.service
```

Remove credentials and personal data before sharing diagnostics. History belongs to the dedicated local browser profile; reconnecting does not import arbitrary cloud history. Keep ports 17863/17864 on loopback. Muse tools can access your selected command account's files and permissions.

## Uninstall

```sh
sudo bash scripts/uninstall-companion.sh --dry-run
sudo bash scripts/uninstall-companion.sh
sudo systemctl restart musegadget.service
```

This removes the companion overlay, UI and two companion services while preserving the original SDK, pairing, SDK token, voice environment/models and browser/chat profile.

## Validation and licensing

The physical-key flow, real cloud replies, animations and caption cleanup were checked on the RK3576 prototype. The public version's token entry and portable installer have host checks; a fresh installation of the portable package still needs target-device validation. Automated tests cannot establish your microphone, speaker, BLE or touchscreen behavior.

No personal SDK token, pairing state, chat history, device logs or speech models are included. Code is [Apache-2.0](LICENSE) with original notices retained; character artwork and voice models have separate terms. See [THIRD_PARTY.md](THIRD_PARTY.md). This project is not affiliated with or endorsed by Meta / Muse.

Contributions and additional hardware reports: [CONTRIBUTING.md](CONTRIBUTING.md).
