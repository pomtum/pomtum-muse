"""Loopback-only UI bridge. Accesses the SDK Unix socket, never its identity files.

Run as the existing desktop command account, not root. Extra JSON routes may
be supplied by a separate module with --routes-module (make_routes(bridge)).
"""
# Modified by PomTum contributors: loopback UI and write-only SDK-token route.
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import mimetypes
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from musegadget.chat import MAX_LOCAL_REQUEST, validate_chat
from musegadget.config import _SDK_TOKEN


async def unix_request(path: str, payload: dict) -> dict:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=MAX_LOCAL_REQUEST), 5)
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 90)
        if not line:
            raise ConnectionError("SDK socket closed")
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError("invalid SDK response")
        return reply
    finally:
        writer.close()
        await writer.wait_closed()


class Bridge:
    def __init__(self, socket_path: str, static_dir: Path, port: int = 17863,
                 request_func=unix_request):
        self.socket_path = socket_path
        self.static_dir = static_dir.resolve()
        self.port = port
        self.request_func = request_func
        self.condition = threading.Condition()
        self.history = deque(maxlen=256)
        self.sequence = 0
        self.stop = threading.Event()
        self.extra_routes = {}
        self.last_unix_id = 0

    def request(self, payload: dict) -> dict:
        return asyncio.run(self.request_func(self.socket_path, payload))

    def publish(self, event: dict):
        with self.condition:
            self.sequence += 1
            self.history.append((self.sequence, event))
            self.condition.notify_all()

    def events_after(self, after: int, timeout: float = 15) -> list:
        with self.condition:
            if self.sequence <= after:
                self.condition.wait(timeout)
            records = [(seq, event) for seq, event in self.history if seq > after]
            if after and self.history and (after < self.history[0][0] - 1 or after > self.sequence):
                records.insert(0, (self.sequence, {"type": "status", "replay_lost": True}))
            return records

    def start_events(self):
        thread = threading.Thread(target=lambda: asyncio.run(self._read_events()), daemon=True,
                                  name="muse-local-events")
        thread.start()
        return thread

    async def _read_events(self):
        while not self.stop.is_set():
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.socket_path, limit=MAX_LOCAL_REQUEST), 5)
                writer.write(json.dumps({"op": "events", "after": self.last_unix_id}).encode() + b"\n")
                await writer.drain()
                while not self.stop.is_set():
                    line = await asyncio.wait_for(reader.readline(), 35)
                    if not line:
                        raise ConnectionError("SDK disconnected")
                    record = json.loads(line)
                    event = record.get("event")
                    if isinstance(event, dict) and event.get("type") in ("status", "delta", "message", "error", "activity"):
                        self.publish(event)
                    seq = record.get("event_id")
                    if isinstance(seq, int):
                        self.last_unix_id = seq
            except (OSError, ValueError, asyncio.TimeoutError, ConnectionError):
                self.publish({"type": "status", "connected": False, "registered": False,
                              "pending": False, "last_error": "SDK socket unavailable"})
            finally:
                if writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
            await asyncio.sleep(2)


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *args):
            pass  # Access logs can expose chat/session ids. Keep this bridge quiet.

        def _allowed(self, mutation=False):
            host = self.headers.get("Host", "")
            valid_hosts = {f"127.0.0.1:{bridge.port}", f"localhost:{bridge.port}"}
            if host not in valid_hosts:
                return False
            origin = self.headers.get("Origin")
            if mutation and not origin:
                return False
            if origin is not None and origin != f"http://{host}":
                return False
            if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                return False
            return True

        def _headers(self, status, content_type, length=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _json(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self._headers(status, "application/json; charset=utf-8", len(data))
            self.wfile.write(data)

        def do_GET(self):
            if not self._allowed():
                self._json(403, {"error": "same-origin access required"})
                return
            path = urlsplit(self.path).path
            if path == "/api/events":
                self._sse()
                return
            if path == "/api/sdk-token":
                try:
                    value = bridge.request({"op": "sdk-token-status"})
                    if not value.get("ok") or not isinstance(value.get("configured"), bool):
                        raise ValueError()
                    self._json(200, {"configured": value["configured"]})
                except Exception:
                    self._json(503, {"error": "SDK token status unavailable"})
                return
            if path == "/api/identity":
                try:
                    value = bridge.request({"op": "identity"})
                    self._json(200 if value.get("ok") else 503, value)
                except (OSError, ValueError, asyncio.TimeoutError, ConnectionError):
                    self._json(503, {"ok": False, "error": "Muse identity unavailable"})
                return
            if path == "/api/status":
                try:
                    value = bridge.request({"op": "status"})
                    value.pop("ok", None)
                    self._json(200, value)
                except (OSError, ValueError, asyncio.TimeoutError, ConnectionError):
                    self._json(200, {"connected": False, "registered": False, "pending": False,
                                     "last_error": "SDK socket unavailable", "sdk_version": None})
                return
            extra = bridge.extra_routes.get(("GET", path))
            if extra:
                self._extra(extra, None)
                return
            if path.startswith("/api/"):
                self._json(404, {"error": "unknown route"})
                return
            self._static(path)

        def _sse(self):
            try:
                after = int(self.headers.get("Last-Event-ID", "0"))
                if after < 0:
                    raise ValueError()
            except ValueError:
                self._json(400, {"error": "invalid Last-Event-ID"})
                return
            self._headers(200, "text/event-stream; charset=utf-8")
            try:
                while not bridge.stop.is_set():
                    records = bridge.events_after(after)
                    if not records:
                        self.wfile.write(b": heartbeat\n\n")
                    for seq, event in records:
                        data = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(f"id: {seq}\ndata: {data}\n\n".encode())
                        after = seq
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True

        def _static(self, path):
            decoded = unquote(path)
            if "\x00" in decoded or "\\" in decoded or any(part.startswith(".") for part in decoded.split("/") if part):
                self._json(404, {"error": "not found"})
                return
            target = (bridge.static_dir / (decoded.lstrip("/") or "index.html")).resolve()
            try:
                target.relative_to(bridge.static_dir)
                if not target.is_file():
                    raise ValueError()
            except ValueError:
                self._json(404, {"error": "not found"})
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix in (".glb", ".vrm"):
                content_type = "model/gltf-binary"
            try:
                with target.open("rb") as stream:
                    self._headers(200, content_type, target.stat().st_size)
                    while True:
                        data = stream.read(65536)
                        if not data:
                            break
                        self.wfile.write(data)
            except (OSError, BrokenPipeError):
                self.close_connection = True

        def do_POST(self):
            if not self._allowed(mutation=True):
                self.close_connection = True
                self._json(403, {"error": "same-origin access required"})
                return
            path = urlsplit(self.path).path
            token_route = path == "/api/sdk-token"
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self._json(400, {"error": "Transfer-Encoding is not supported"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > (512 if token_route else MAX_LOCAL_REQUEST):
                    self.close_connection = True
                    self._json(413, {"error": "invalid request body size" if token_route else
                                     "request body exceeds 2 MiB or has no length"})
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    self._json(415, {"error": "application/json required"})
                    self.close_connection = True
                    return
                self.connection.settimeout(10)
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("incomplete request")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError("JSON object required")
                if token_route:
                    token = value.get("token")
                    if set(value) != {"token"} or not isinstance(token, str) or not _SDK_TOKEN.fullmatch(token):
                        raise ValueError("invalid SDK token")
                    reply = bridge.request({"op": "sdk-token-save", "token": token})
                    if reply.get("ok") and reply.get("saved") is True and isinstance(reply.get("pairingRequired"), bool):
                        self._json(200, {"saved": True, "pairingRequired": reply["pairingRequired"]})
                    else:
                        self._json(400 if reply.get("code") == "invalid_token" else 503,
                                   {"error": "SDK token could not be saved"})
                    return
                extra = bridge.extra_routes.get(("POST", path))
                if extra:
                    self._extra(extra, value)
                    return
                if path == "/api/chat":
                    payload = {"op": "chat", **validate_chat(value)}
                elif path == "/api/cancel":
                    payload = {"op": "cancel", "request_id": str(value.get("request_id", ""))}
                else:
                    self._json(404, {"error": "unknown route"})
                    return
                reply = bridge.request(payload)
                ok = reply.pop("ok", False)
                status = 200 if ok else (409 if reply.get("code") == "conflict" else 503)
                self._json(status, reply)
            except ValueError as exc:
                self._json(400, {"error": "invalid SDK token request" if token_route else str(exc)})
            except (OSError, asyncio.TimeoutError, ConnectionError):
                self._json(503, {"error": "SDK token could not be saved" if token_route else
                                 "SDK socket unavailable; delivery may be unknown"})
                self.close_connection = True
            except Exception:
                if not token_route:
                    raise
                self._json(503, {"error": "SDK token could not be saved"})
                self.close_connection = True

        def _extra(self, route, payload):
            # Extension contract: (HTTP status, JSON dictionary). Parent owns TTS.
            try:
                status, value = route(payload)
                self._json(status, value)
            except Exception:
                self._json(500, {"error": "local extension failed"})

    return Handler


def make_server(bridge: Bridge):
    server = ThreadingHTTPServer(("127.0.0.1", bridge.port), make_handler(bridge))
    bridge.port = server.server_port
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Muse desktop UI loopback bridge")
    parser.add_argument("--port", type=int, default=17863)
    parser.add_argument("--socket", default="/run/musegadget/musegadget.sock")
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--routes-module", help="optional local module providing make_routes(bridge)")
    args = parser.parse_args()
    if not args.static_dir.is_dir():
        parser.error("static directory does not exist")
    bridge = Bridge(args.socket, args.static_dir, args.port)
    if args.routes_module:
        bridge.extra_routes.update(importlib.import_module(args.routes_module).make_routes(bridge))
    server = make_server(bridge)
    bridge.start_events()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop.set()
        with bridge.condition:
            bridge.condition.notify_all()
        server.server_close()


if __name__ == "__main__":
    main()
