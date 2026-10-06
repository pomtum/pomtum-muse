#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Modified by PomTum contributors: loopback default and private bounded failures.

"""Forward Pebble ring transcriptions to a Muse side chat.

A small webhook listener: the ring's companion app POSTs each transcription
to ``/ingest`` (multipart form or JSON, authenticated with a shared secret),
and this hands it to ``musegadget send-user-msg``, which delivers it over the device's
existing connection to the Muse. It holds no Muse credentials itself.

Configuration (environment):
  PEBBLE_SESSION_ID   side chat to post into (required)
  PEBBLE_SECRET_FILE  file holding the shared secret (default /etc/pebble-bridge/secret)
  PEBBLE_PORT         port to listen on (default 8787)
  PEBBLE_HOST         bind address (default 127.0.0.1)
  MUSEGADGET          path to the musegadget command

Standard library only; run it with the system Python as an account in the
musegadget socket's group.

Remote companion apps cannot reach the loopback default. Explicitly setting
PEBBLE_HOST to a LAN address (or 0.0.0.0) opts into remote access. Shared-token
authentication still applies, but this example serves plain HTTP: use a trusted
network or a protected transport and limit who can reach the chosen listener.
"""

from __future__ import annotations

import email.parser
import email.policy
import hmac
import json
import logging
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("pebble-ring-bridge")

MAX_BODY_BYTES = 1024 * 1024
TEXT_FIELDS = ("transcription", "text", "transcript")
SEND_TIMEOUT_S = 100
READ_TIMEOUT_S = 10
SEND_FAILURES = frozenset({"side chat not configured", "sender could not start",
                           "sender timed out", "sender rejected message"})


def parse_body(content_type: str, raw: bytes) -> dict:
    """Return the request's fields from a multipart form, JSON, or plain text."""
    if content_type.startswith("multipart/form-data"):
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw
        )
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name:
                payload = part.get_payload(decode=True) or b""
                fields[name] = payload.decode("utf-8", errors="replace").strip()
        return fields
    if content_type.startswith("application/json"):
        data = json.loads(raw or b"{}")
        return data if isinstance(data, dict) else {}
    return {"text": raw.decode("utf-8", errors="replace").strip()}


def transcription(fields: dict) -> str:
    for key in TEXT_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def presented_token(headers, fields: dict) -> str:
    token = headers.get("X-Pebble-Token") or headers.get("Authorization") or ""
    if token.startswith("Bearer "):
        token = token[len("Bearer "):]
    return token or str(fields.get("token") or "")


def send_user_msg(text: str) -> tuple[bool, str]:
    session_id = os.environ.get("PEBBLE_SESSION_ID")
    if not session_id:
        return False, "side chat not configured"
    command = [os.environ.get("MUSEGADGET", "/opt/musegadget/venv/bin/musegadget"),
               "send-user-msg", "--session-id", session_id, "-"]
    try:
        result = subprocess.run(command, input=text, text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=SEND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, "sender timed out"
    except OSError:
        return False, "sender could not start"
    return (True, "Sent to your Muse.") if result.returncode == 0 else (False, "sender rejected message")


class Handler(BaseHTTPRequestHandler):
    secret = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT_S)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/ingest":
            return self._reply(404, {"ok": False, "error": "not found"})
        if self.headers.get("Transfer-Encoding"):
            return self._reply(400, {"ok": False, "error": "unsupported transfer encoding"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(400, {"ok": False, "error": "bad content length"})
        if length < 0:
            return self._reply(400, {"ok": False, "error": "bad content length"})
        if length > MAX_BODY_BYTES:
            return self._reply(413, {"ok": False, "error": "body too large"})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self._reply(400, {"ok": False, "error": "incomplete body"})
            fields = parse_body(self.headers.get("Content-Type", ""), raw)
        except TimeoutError:
            return self._reply(408, {"ok": False, "error": "request body timed out"})
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, {"ok": False, "error": "unreadable body"})
        # Compared as bytes: compare_digest() raises on non-ASCII str input.
        presented = presented_token(self.headers, fields).encode()
        if not hmac.compare_digest(presented, self.secret.encode()):
            return self._reply(401, {"ok": False, "error": "invalid token"})
        text = transcription(fields)
        if not text:
            return self._reply(400, {"ok": False, "error": "no text"})
        delivered, detail = send_user_msg(f"{text} [via Pebble Ring]")
        detail = "Sent to your Muse." if delivered else (
            detail if isinstance(detail, str) and detail in SEND_FAILURES else "sender rejected message")
        log.info("transcription %s", "delivered" if delivered else f"failed: {detail}")
        self._reply(200 if delivered else 502, {"ok": delivered, "detail": detail})

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        pass  # Request paths/queries can contain tokens or other private values.


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not os.environ.get("PEBBLE_SESSION_ID"):
        raise SystemExit("PEBBLE_SESSION_ID is required")
    with open(os.environ.get("PEBBLE_SECRET_FILE", "/etc/pebble-bridge/secret")) as f:
        Handler.secret = f.read().strip()
    if not Handler.secret:
        raise SystemExit("empty secret")
    port = int(os.environ.get("PEBBLE_PORT", "8787"))
    host = os.environ.get("PEBBLE_HOST") or "127.0.0.1"
    log.info("listening on %s:%d; side chat configured", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
