# Local client backend

Based on `upstream/linux` at `b139b45064b4dcecf7bfe97e75bc7f99c10c28b6`.
All four commands
(`system.run`, `file.read`, `file.write`, `device.health`), Linux/homehub registration,
paired identity and run-as child permissions are preserved.

One WSS/Noise session now also carries the public ESP32 `POST /chat/subscribe`
NDJSON stream. Subscription is established before sending, and early replies
are buffered until their user-message ACK arrives. Linux sending still includes
the existing device_id. Do not register a second device or use ESP32 family/OTA.

## Launch

The deployment owner installs this package into the existing SDK environment,
preserving pairing and the existing command account, and restarts the SDK service. Run the
bridge as the desktop command account; it never opens credential/state files:

```sh
sudo -u "$USER" /opt/musegadget/venv/bin/python -m musegadget.http_bridge \
  --static-dir /absolute/path/to/client-ui/dist --port 17863
```

HTTP always binds 127.0.0.1. `--socket` defaults to
`/run/musegadget/musegadget.sock`. Socket mode remains 0660; Linux SO_PEERCRED
additionally permits only root and the command account. Host/Origin checks
reject cross-origin mutation and DNS rebinding. POST requires matching Origin,
Content-Type application/json, Content-Length, and a body at most 2 MiB (512
bytes for the SDK token endpoint).
Static files stay inside `--static-dir`; dotfiles and traversal are rejected.
Credentials, command parameters/results and raw cloud errors are never returned
by the new UI APIs. The write-only token endpoint accepts a credential for local
storage, without echoing it. The original legacy CLI socket response is unchanged.

## API

- `GET /api/status` -> `{connected, registered, pending, last_error, sdk_version}`.
  Connected requires registration and an active subscription.
- `GET /api/sdk-token` -> `{configured:boolean}`; no existing token is returned.
- `POST /api/sdk-token {token}` -> `{saved:true, pairingRequired:boolean}`.
  Requires same origin, canonical SDK format and at most 512 bytes. The root
  daemon saves atomically to its normal mode-0600 token file. Errors, events and
  responses do not echo the token. Saving does not restart or pair the device
  and does not verify cloud acceptance of the token.
- `GET /api/identity` -> `{ok:true, result:{...}}`, retaining the official Muse
  identity fields, including existing avatar asset references when provided.
  This is only a dedicated Noise Daemon GET /identity on the registered session,
  without include_markdown, query forwarding, chat messages or generation.
  Asset key names and download routes require actual cloud response validation.
- `GET /api/events` -> SSE default message events; data JSON has
  `type=status|delta|message|error|activity`. SSE id is a bridge-local sequence;
  Last-Event-ID resumes bounded replay. Gaps emit status with replay_lost:true.
- `POST /api/chat` accepts `{text?:string, session_id?:string,
  request_id?:UUID, audio_wav_base64?:string}`. Generate the UUID before posting:
  SSE can arrive before the HTTP ACK. Success returns
  `{accepted:true, request_id, session_id, message_id}`. Main session_id can be
  null; no cloud id is invented. Missing ACK ids emit ack_missing_id and return
  message_id:null rather than attributing an unrelated reply.
- `POST /api/cancel {request_id}` -> `{local_cancelled, cloud_cancelled:false}`.
  It discards local reply events; UI/device-I/O must stop its own playback.
  This does not promise cancellation of cloud tasks or device tools.

Delta/message JSON includes request_id, session_id, message_id and text.
Message includes role:user|assistant and done:true. User text is server-provided
voice-note transcription when available. Activity includes scope:connection for
coarse cloud status or scope:device for local tool start/completion; it is not a
complete cloud tool timeline. Error includes code and text.

Text is limited to 32768 characters. Audio must be complete mono 16 kHz PCM16
WAV, no more than 20 seconds; audio-only input is valid. The wire envelope uses
the public upstream item `{type:"file", mime_type:"audio/wav",
filename:"voice_note.wav", data_base64:"..."}`. Large bodies use 16 KiB chunks.

Only one foreground turn is allowed. Same-UUID retries with identical payload
reuse their ACK in this daemon lifetime; different content is rejected. After
ACK, first reply binding rejects an explicitly unrelated parent but permits a
missing parent, matching the official bind_msg rule. Once bound, the same reply
id wins over later parent-field differences. Cross-session events and rejected
or known prior-turn reply ids remain blocked. A bounded rejection set disables
new parentless binding if it overflows. Missing ancestry cannot establish the
reply producer; the fallback is limited to the current ACKed foreground turn.
Persistent messages and transient activity have different sequence domains;
bounded (domain, event name, sequence) equality deduplication replaces a global
maximum filter, retaining lower-numbered user transcription. Disconnects report
unknown delivery and never automatically resend messages. Subscription/service
reconnection recovers future use, not a lost cloud history. Replay/ACK buffers
are bounded and do not survive restart. The `{}` subscription's side-chat
routing remains a cloud acceptance item.

There is no built-in TTS, ASR, chat-image or dynamic cloud avatar implementation.
The separate device-I/O service supplies WAV and can synthesize final text.
For independent local extensions, --routes-module package.module loads
make_routes(bridge), mapping (method,path) to a callable that accepts the JSON
body and returns (HTTP_status, JSON_dict), under the same Origin checks.

## Checks

No tokens, device access, Bluetooth or external Muse requests are used:

```sh
PYTHONPATH=src python -m pytest -q tests/test_chat_stream.py \
  tests/test_http_bridge.py tests/test_link_client.py tests/test_chat_service.py
```

Unix/account tests skip hosts without pwd. On deployment Linux run the complete
suite with `PYTHONPATH=src python -m pytest -q`. Real cloud/device acceptance must
check streamed/final replies, physical-key WAV transcription, local playback
cancel and a second turn after network/service restart, alongside the unchanged
four-tool service. Host fake-VM success does not claim these have passed.
