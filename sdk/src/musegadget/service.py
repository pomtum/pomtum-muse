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
# Modified by PomTum contributors: local chat/events and secure SDK-token IPC.

"""Keep a paired device connected to its Muse.

Each round fetches the leased VMs with the device token (which also yields a
fresh per-VM bearer), connects to the default VM and serves commands until
the connection ends. Failures back off exponentially. A session that stayed
up for a while resets the backoff. The device token is rotated before it
expires, and immediately if the API rejects it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import socket
import struct
import hashlib
import tempfile
from collections import OrderedDict
import time
from dataclasses import dataclass, field

from musegadget import __version__, config, muse_api
from musegadget.executor import COMMAND_SPECS, Executor
from musegadget.identity import Identity
from musegadget.link_client import DeviceDescription, IdentityUnavailable, LinkSession, Outcome
from musegadget.chat import MAX_LOCAL_REQUEST, EventHub, ChatRouter, validate_chat

log = logging.getLogger(__name__)

DEFAULT_NOISE_HOST = "hatch.metaaivm.com"
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 60.0
AUTH_BACKOFF_MIN_S = 15.0
HEALTHY_SESSION_S = 30.0
UNPAIRED_POLL_S = 30.0
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
# Device access tokens live about 4 hours; rotate at 3.
TOKEN_REFRESH_AGE_S = 3 * 3600
TOKEN_RETRY_S = 300


@dataclass
class Backoff:
    failures: int = 0
    floor: float = 0.0

    def next_delay(self) -> float:
        # The delay saturates at BACKOFF_MAX_S after a handful of failures, so
        # cap the exponent: 2 ** failures stops converting to a float at 1024,
        # about 17 hours into an outage at the 60 s ceiling.
        delay = min(BACKOFF_BASE_S * 2.0 ** min(self.failures, 16), BACKOFF_MAX_S)
        self.failures += 1
        return max(delay, self.floor)

    def reset(self) -> None:
        self.failures = 0
        self.floor = 0.0


@dataclass
class Service:
    identity: Identity
    executor: Executor
    sdk_token: str | None = None
    display_name: str = field(default_factory=socket.gethostname)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _last_refresh_attempt: float = float("-inf")
    # The SDK token reaches Muse only in refresh bodies until apps forward it at
    # mint, so each start with a token attempts one refresh to report it.
    _sdk_token_report_attempted: bool = False
    _current: LinkSession | None = None
    _events: EventHub = field(default_factory=EventHub)
    _chat_tasks: OrderedDict = field(default_factory=OrderedDict)
    _last_error: str | None = None
    _chat_ready: bool = False

    def __post_init__(self):
        self._router = ChatRouter(self._events)

    def _status(self) -> dict:
        registered = bool(self._current and self._current.registered_at is not None)
        return {"connected": registered and self._chat_ready, "registered": registered,
                "pending": self._router.pending, "last_error": self._last_error,
                "sdk_version": __version__}

    def _publish_status(self):
        self._events.publish({"type": "status", **self._status()})

    def _on_event(self, event: dict):
        kind = event.get("type")
        if kind == "subscription":
            was_ready = self._chat_ready
            self._chat_ready = bool(event.get("ready"))
            if event.get("restarting"):
                self._router.reset_sequence()
            if not self._chat_ready and was_ready:
                self._router.fail("subscription_lost", "reply subscription interrupted; message was not resent")
            self._publish_status()
        elif kind == "connection":
            self._publish_status()
        elif kind == "tool":
            self._events.publish({"type": "activity", "scope": "device", **self._router.fields(),
                                  "status": event["status"], "command": event["command"][:128],
                                  "invoke_id": event["invoke_id"]})
        else:
            self._router.on_event(event)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = Backoff()
        while not self._stop.is_set():
            pairing = config.load_json(config.PAIRING_FILE)
            if not pairing:
                log.info("not paired; run `musegadget pair` to set up")
                await self._sleep(UNPAIRED_POLL_S)
                continue
            pairing = await self._maybe_refresh(pairing)
            if pairing is None:
                await self._sleep(TOKEN_RETRY_S)
                continue

            api = muse_api.api_root(pairing.get("api_url_v2", ""))
            vms, status = await asyncio.to_thread(
                muse_api.fetch_vms_with_status, pairing["access_token"], api,
            )
            if status == 401:
                log.warning("device token rejected by the API; refreshing")
                if await self._maybe_refresh(pairing, force=True) is None:
                    await self._sleep(TOKEN_RETRY_S)
                continue
            vm = next((v for v in vms if v["is_default"]), vms[0] if vms else None)
            if vm is None:
                await self._sleep(backoff.next_delay())
                continue

            outcome, lasted = await self._session(vm, pairing)
            if outcome is Outcome.STOPPED:
                return
            if outcome is Outcome.UNPAIRED:
                config.delete_json(config.PAIRING_FILE)
                log.warning("pairing removed; run `musegadget pair` to set up again")
                continue
            if lasted >= HEALTHY_SESSION_S:
                backoff.reset()
            if outcome in (Outcome.AUTH_REJECTED, Outcome.FORBIDDEN):
                backoff.floor = AUTH_BACKOFF_MIN_S
            delay = backoff.next_delay()
            log.info("reconnecting in %.0fs", delay)
            await self._sleep(delay)

    async def _session(self, vm: dict, pairing: dict) -> tuple[Outcome, float]:
        device = DeviceDescription(
            node_id=self.identity.node_id,
            display_name=self.display_name,
            version=__version__,
            commands=COMMAND_SPECS,
        )
        session = LinkSession(
            noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
            vm_id=vm["vm_id"] or vm["vm_name"],
            vm_auth_token=vm["vm_auth_token"],
            device=device,
            run_command=self.executor.run,
            on_event=self._on_event,
        )
        log.info("connecting to %s", vm["vm_name"] or vm["vm_id"])
        started = time.monotonic()
        self._current = session
        self._chat_ready = False
        self._router.reset_sequence()
        self._publish_status()
        try:
            outcome = await session.run(self._stop)
        except Exception as exc:
            log.warning("session failed: %s: %s", type(exc).__name__, exc)
            outcome = Outcome.CLOSED
        finally:
            self._current = None
            self._chat_ready = False
            self._router.fail("disconnected", "connection lost; message was not resent")
            self._publish_status()
        lasted = time.monotonic() - (session.registered_at or time.monotonic())
        log.info("session ended: %s after %.0fs", outcome.value, time.monotonic() - started)
        return outcome, lasted

    async def _maybe_refresh(self, pairing: dict, force: bool = False) -> dict | None:
        """Return current pairing, rotating tokens first if they are due.

        Returns None only when a due refresh failed and the old token should
        not be used yet; the pairing file is removed when the pairing itself
        has been revoked.
        """
        age = time.time() - pairing.get("access_token_saved_at", 0)
        report_due = bool(self.sdk_token) and not self._sdk_token_report_attempted
        due = force or age >= TOKEN_REFRESH_AGE_S
        if not due and not report_due:
            return pairing
        if not force and time.monotonic() - self._last_refresh_attempt < TOKEN_RETRY_S:
            return pairing
        self._last_refresh_attempt = time.monotonic()
        if report_due:
            self._sdk_token_report_attempted = True
            log.info("refreshing device token to report the SDK token")
        tokens, status = await asyncio.to_thread(
            muse_api.refresh_device_token,
            pairing["refresh_token"], self.identity.node_id,
            muse_api.api_root(pairing.get("api_url_v2", "")),
            self.sdk_token,
        )
        if tokens:
            pairing = {
                **pairing,
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "access_token_saved_at": int(time.time()),
            }
            config.save_json(config.PAIRING_FILE, pairing)
            log.info("device token rotated")
            return pairing
        if not due:
            # Only reporting the SDK token: nothing has rejected the current
            # token, so a refusal here must never unpair the device.
            log.warning("SDK token report refresh failed (HTTP %s); keeping the pairing", status)
            return pairing
        if status == 401:
            config.delete_json(config.PAIRING_FILE)
            log.error("pairing revoked; run `musegadget pair` to set up again")
            return None
        # Transient failure: keep using the current token while it still works.
        return None if force else pairing

    # -- Local socket -----------------------------------------------------------

    async def serve_local(self, path) -> asyncio.AbstractServer:
        """Accept messages for the Muse from programs on this device.

        Each connection sends one JSON line, ``{"message": "..."}`` plus an
        optional ``"session_id"`` naming a side chat, and gets one JSON line
        back. Only root and the command account's group can
        connect.
        """
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server = await asyncio.start_unix_server(self._handle_local, str(path),
                                                 limit=MAX_LOCAL_REQUEST)
        if os.geteuid() == 0:
            os.chown(path, 0, self.executor.account.gid)
        os.chmod(path, 0o660)
        log.info("accepting messages on %s", path)
        return server

    async def _handle_local(self, reader, writer) -> None:
        if not self._peer_allowed(writer):
            writer.close()
            return
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            request = json.loads(line)
            if isinstance(request, dict) and request.get("op") == "events":
                await self._serve_events(request, reader, writer)
                return
            reply = await self._local_request(line)
        except Exception as exc:
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(reply).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()

    def _peer_allowed(self, writer) -> bool:
        """Socket mode remains 0660; Linux also checks the connecting UID."""
        if not hasattr(socket, "SO_PEERCRED"):
            return True  # non-Linux host tests; filesystem mode still applies
        try:
            peer = writer.get_extra_info("socket")
            _pid, uid, _gid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return uid in (0, self.executor.account.uid)
        except (AttributeError, OSError, struct.error):
            return False

    async def _serve_events(self, request, reader, writer):
        after = request.get("after", 0)
        if not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative event id")
        if after > self._events.sequence:  # daemon restarted
            after = 0
        queue = self._events.subscribe(after)
        closed = asyncio.create_task(reader.read(1))
        try:
            writer.write(json.dumps({"snapshot": True,
                                     "event": {"type": "status", **self._status()}}).encode() + b"\n")
            await writer.drain()
            while not closed.done():
                next_event = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({closed, next_event}, timeout=15,
                                             return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    break
                if next_event in done:
                    record = next_event.result()
                else:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    record = {"heartbeat": True}
                writer.write(json.dumps(record, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
                if record.get("event", {}).get("code") == "subscriber_overflow":
                    break
        finally:
            closed.cancel()
            await asyncio.gather(closed, return_exceptions=True)
            self._events.unsubscribe(queue)
            writer.close()

    async def _local_request(self, line: bytes) -> dict:
        request = json.loads(line)
        if isinstance(request, dict):
            op = request.get("op")
            if op == "status":
                return {"ok": True, **self._status()}
            if op == "sdk-token-status":
                return {"ok": True, "configured": bool(self.sdk_token)}
            if op == "sdk-token-save":
                return self._save_sdk_token(request, len(line))
            if op == "identity":
                session = self._current
                if session is None or session.registered_at is None:
                    return {"ok": False, "error": "not connected to the Muse", "code": "disconnected"}
                try:
                    return {"ok": True, "result": await session.fetch_identity()}
                except IdentityUnavailable as exc:
                    return {"ok": False, "error": "Muse identity unavailable", "code": "identity_unavailable",
                            "diagnostics": exc.diagnostics}
                except Exception:
                    return {"ok": False, "error": "Muse identity unavailable", "code": "identity_unavailable",
                            "diagnostics": {"http_status": None, "top_level_keys": [],
                                            "result_type": "unavailable", "ok": None,
                                            "json_type": "no_completed_response"}}
            if op == "cancel":
                cancelled = self._router.cancel(str(request.get("request_id", "")))
                self._publish_status()
                return {"ok": True, "local_cancelled": cancelled, "cloud_cancelled": False}
            if op == "chat":
                return await self._chat_request(request)
        message = request.get("message") if isinstance(request, dict) else None
        if not isinstance(message, str) or not message.strip():
            return {"ok": False, "error": "expected {\"message\": \"...\"}"}
        session_id = request.get("session_id")
        if session_id is not None and not (
            isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id)
        ):
            return {"ok": False, "error": "session_id must be letters, digits and dashes"}
        session = self._current
        if session is None or session.registered_at is None:
            return {"ok": False, "error": "not connected to the Muse"}
        log.info("forwarding a %d-character message to the Muse", len(message))
        return await session.send_chat(message, session_id)

    def _save_sdk_token(self, request: dict, request_bytes: int) -> dict:
        """Root saves a validated token locally; no pairing or reconnect is initiated."""
        token = request.get("token")
        if (request_bytes > 512 or set(request) != {"op", "token"}
                or not isinstance(token, str) or not config._SDK_TOKEN.fullmatch(token)):
            return {"ok": False, "code": "invalid_token", "error": "invalid SDK token"}
        if os.geteuid() != 0:
            return {"ok": False, "code": "token_save_failed", "error": "SDK token could not be saved"}
        temporary = None
        try:
            directory = config.state_dir()
            # The standard state directory is private to root. Enforce that
            # boundary for existing directories as well as fresh installs.
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(directory, 0, 0)
            os.chmod(directory, 0o700)
            pairing_required = not bool(config.load_json(config.PAIRING_FILE))
            fd, temporary = tempfile.mkstemp(prefix=".sdk-token-", dir=directory)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                os.fchown(stream.fileno(), 0, 0)
                stream.write((token + "\n").encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / config.SDK_TOKEN_FILE)
            temporary = None
        except Exception:
            # Do not stringify exceptions: injected/future errors may echo the
            # submitted secret. Neither events nor diagnostics include it.
            return {"ok": False, "code": "token_save_failed", "error": "SDK token could not be saved"}
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self.sdk_token = token
        self._sdk_token_report_attempted = False
        return {"ok": True, "saved": True, "pairingRequired": pairing_required}

    async def _chat_request(self, request: dict) -> dict:
        value = validate_chat(request)
        rid = value["request_id"]
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).digest()
        old = self._chat_tasks.get(rid)
        if old:
            if old[0] != fingerprint:
                return {"ok": False, "error": "request_id was already used with different content", "code": "conflict"}
            return await asyncio.shield(old[1])
        session = self._current
        if session is None or session.registered_at is None:
            return {"ok": False, "error": "not connected to the Muse", "code": "disconnected"}
        if self._router.pending:
            return {"ok": False, "error": "a foreground turn is pending; locally cancel it first", "code": "conflict"}
        self._router.begin(rid, value["session_id"])
        task = asyncio.create_task(self._send_foreground(session, value))
        self._chat_tasks[rid] = (fingerprint, task)
        while len(self._chat_tasks) > 128:
            key, item = next(iter(self._chat_tasks.items()))
            if not item[1].done():
                break
            del self._chat_tasks[key]
        return await asyncio.shield(task)

    async def _send_foreground(self, session, value):
        rid = value["request_id"]
        self._publish_status()
        try:
            result = await session.send_chat(value["text"], value["session_id"],
                                             audio_wav_base64=value["audio_wav_base64"], request_id=rid)
            if not result.get("ok"):
                raise ConnectionError("Muse refused the message")
            reply = {"ok": True, **self._router.acknowledge(result, rid, value["session_id"])}
            self._last_error = None
        except Exception:
            # Never return the raw VM response or exception: either can echo credentials.
            self._last_error = "message delivery failed or acknowledgement unavailable"
            self._router.fail("delivery_failed", self._last_error, rid)
            reply = {"ok": False, "request_id": rid, "code": "delivery_failed", "error": self._last_error}
        self._publish_status()
        return reply

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass


def run_service(identity: Identity, executor: Executor, sdk_token: str | None = None) -> None:
    async def main() -> None:
        service = Service(identity=identity, executor=executor, sdk_token=sdk_token)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, service.stop)
        server = await service.serve_local(config.socket_path())
        try:
            await service.run()
        finally:
            server.close()

    asyncio.run(main())
