"""Linux IPC and permission tests; skipped on hosts without POSIX accounts."""
# Modified by PomTum contributors: secure local SDK-token regression tests.
import asyncio
import json
import os
import socket
import stat
import struct
import tempfile
import uuid
from pathlib import Path

import pytest

pytest.importorskip("pwd")
from musegadget.executor import Account, Executor
from musegadget.identity import Identity
from musegadget.service import Service
from musegadget import config


def make_service():
    return Service(identity=Identity("02:00:00:00:00:01"), executor=Executor(Account.current()))


class Session:
    registered_at = 1

    def __init__(self, service):
        self.service = service
        self.calls = 0

    async def send_chat(self, text, sid, **kwargs):
        self.calls += 1
        self.service._router.on_event({"type": "event", "event": "delta.text_append",
            "payload": {"message_id": "a1", "reply_to_message_id": "u1", "text": "early"}})
        await asyncio.sleep(0)
        return {"ok": True, "response": {"message_id": "u1"}}


def test_ipc_mode_peer_uid_status_subscribe_and_idempotent_chat():
    async def scenario():
        service = make_service()
        session = Session(service)
        service._current = session
        service._chat_ready = True
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "m.sock"
            server = await service.serve_local(path)
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b'{"op":"events"}\n')
            await writer.drain()
            assert json.loads(await reader.readline())["event"]["type"] == "status"
            rid = str(uuid.uuid4())
            data = json.dumps({"op": "chat", "text": "hi", "request_id": rid}).encode()
            one, two = await asyncio.gather(service._local_request(data), service._local_request(data))
            assert one == two and one["message_id"] == "u1" and session.calls == 1
            while True:
                record = json.loads(await asyncio.wait_for(reader.readline(), 2))
                if record.get("event", {}).get("type") == "delta":
                    break
            assert record["event"]["request_id"] == rid
            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux SO_PEERCRED")
def test_peer_permission_checks_only_root_and_command_account():
    service = make_service()

    class Peer:
        def __init__(self, uid): self.uid = uid
        def getsockopt(self, *args): return struct.pack("3i", 1, self.uid, 0)

    class Writer:
        def __init__(self, uid): self.uid = uid
        def get_extra_info(self, name): return Peer(self.uid)

    assert service._peer_allowed(Writer(0))
    assert service._peer_allowed(Writer(service.executor.account.uid))
    assert not service._peer_allowed(Writer(service.executor.account.uid + 10000))


def test_disconnect_status_fails_active_turn_and_keeps_credentials_out_of_events():
    service = make_service()
    service._router.begin("r", None)
    service._chat_ready = True
    service._on_event({"type": "subscription", "ready": False})
    events = [record["event"] for record in service._events.history]
    assert events[0]["code"] == "subscription_lost" and not service._router.pending
    serialized = json.dumps(events)
    assert "access_token" not in serialized and "refresh_token" not in serialized


def test_identity_ipc_is_a_dedicated_get_and_does_not_start_a_chat_turn():
    async def scenario():
        service = make_service()

        class IdentitySession:
            registered_at = 1
            async def fetch_identity(self):
                return {"name": "Muse", "avatar": {"image": "/example/avatar.png"}}

        service._current = IdentitySession()
        result = await service._local_request(b'{"op":"identity","path":"/ignored","include_markdown":true}')
        assert result == {"ok": True, "result": {"name": "Muse", "avatar": {"image": "/example/avatar.png"}}}
        assert not service._router.pending
        service._current = None
        assert not (await service._local_request(b'{"op":"identity"}'))["ok"]
    asyncio.run(scenario())


def test_delivery_timeout_and_subscription_loss_end_turn_without_resend_or_late_binding():
    async def scenario():
        service = make_service()
        service._chat_ready = True

        class TimeoutSession:
            registered_at = 1
            calls = 0
            async def send_chat(self, *args, **kwargs):
                self.calls += 1
                raise asyncio.TimeoutError()

        session = TimeoutSession()
        service._current = session
        request = json.dumps({"op": "chat", "text": "hi", "request_id": str(uuid.uuid4())}).encode()
        reply = await service._local_request(request)
        assert reply["code"] == "delivery_failed" and not service._router.pending and session.calls == 1
        count = len(service._events.history)
        late = {"type": "event", "event": "delta.text_append", "payload": {"message_id": "late", "text": "late"}}
        service._router.on_event(late)
        assert len(service._events.history) == count
        service._router.begin("next", None)
        service._router.acknowledge({"response": {"message_id": "u2"}})
        service._on_event({"type": "subscription", "ready": False})
        assert not service._router.pending and not service._status()["connected"]
        count = len(service._events.history)
        service._router.on_event(late)
        assert len(service._events.history) == count and session.calls == 1
    asyncio.run(scenario())


def token_request(token):
    return json.dumps({"op": "sdk-token-save", "token": token}).encode()


def root_token_state(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    monkeypatch.setenv(config.STATE_DIR_ENV, str(directory))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    owners = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: owners.append(("directory", uid, gid)))
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: owners.append(("file", uid, gid)))
    return directory, owners


@pytest.mark.parametrize("paired", [False, True])
def test_sdk_token_atomic_root_only_file_updates_memory_without_pairing_or_connection_changes(
        tmp_path, monkeypatch, caplog, paired):
    directory, owners = root_token_state(tmp_path, monkeypatch)
    directory.mkdir()
    if paired:
        config.save_json(config.PAIRING_FILE, {"marker": "pairing unchanged"})
    pairing_before = (directory / config.PAIRING_FILE).read_bytes() if paired else None
    old = "mgst_" + "B" * 42 + "A"
    new = "mgst_" + "A" * 43
    path = directory / config.SDK_TOKEN_FILE
    path.write_text(old + "\n")
    path.chmod(0o644)
    service = make_service()
    session = Session(service)
    service._current = session
    service._sdk_token_report_attempted = True
    original_replace = os.replace
    replaced = []

    def replace(source, destination):
        assert path.read_text() == old + "\n"  # old value remains until one atomic replace
        assert Path(source).read_text() == new + "\n"
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        replaced.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    reply = asyncio.run(service._local_request(token_request(new)))
    assert reply == {"ok": True, "saved": True, "pairingRequired": not paired}
    assert path.read_text() == new + "\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert owners == [("directory", 0, 0), ("file", 0, 0)] and len(replaced) == 1
    assert service.sdk_token == new and not service._sdk_token_report_attempted
    assert service._current is session and not service._stop.is_set()
    assert not service._events.history and not list(directory.glob(".sdk-token-*"))
    assert asyncio.run(service._local_request(b'{"op":"sdk-token-status"}')) == {"ok": True, "configured": True}
    if paired:
        assert (directory / config.PAIRING_FILE).read_bytes() == pairing_before
    else:
        assert not (directory / config.PAIRING_FILE).exists()
    assert new not in json.dumps(reply) + caplog.text


def test_sdk_token_failed_atomic_replace_keeps_old_file_and_hides_exception(tmp_path, monkeypatch, caplog):
    directory, _ = root_token_state(tmp_path, monkeypatch)
    directory.mkdir()
    path = directory / config.SDK_TOKEN_FILE
    old = "mgst_" + "B" * 42 + "A"
    new = "mgst_" + "A" * 43
    path.write_text(old + "\n")
    service = make_service()
    service.sdk_token = old

    def fail_replace(*args):
        raise OSError(new)

    monkeypatch.setattr(os, "replace", fail_replace)
    reply = asyncio.run(service._local_request(token_request(new)))
    assert not reply["ok"] and reply["code"] == "token_save_failed"
    assert path.read_text() == old + "\n" and service.sdk_token == old
    assert not list(directory.glob(".sdk-token-*"))
    assert new not in json.dumps(reply) + caplog.text


@pytest.mark.parametrize("token", [None, 7, "", "mgst_" + "A" * 42, "mgst_" + "A" * 42 + "B",
                                  "mgst_" + "A" * 43 + "\n", "mgst_" + "A" * 600])
def test_invalid_sdk_token_never_writes_or_echoes(tmp_path, monkeypatch, token):
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "unused"))
    service = make_service()
    reply = asyncio.run(service._local_request(token_request(token)))
    assert reply == {"ok": False, "code": "invalid_token", "error": "invalid SDK token"}
    assert not (tmp_path / "unused").exists() and not service._events.history


def test_sdk_token_save_requires_root_daemon_even_for_authorized_account(tmp_path, monkeypatch):
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "unused"))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    service = make_service()
    reply = asyncio.run(service._local_request(token_request("mgst_" + "A" * 43)))
    assert not reply["ok"] and reply["code"] == "token_save_failed"
    assert not (tmp_path / "unused").exists()


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux SO_PEERCRED")
def test_sdk_token_unauthorized_unix_peer_is_rejected_before_read_or_write(tmp_path, monkeypatch):
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "unused"))
    service = make_service()

    class Reader:
        async def readline(self):
            pytest.fail("an unauthorized peer must not be read")

    class Writer:
        closed = False
        def get_extra_info(self, name): return self
        def getsockopt(self, *args): return struct.pack("3i", 1, service.executor.account.uid + 10000, 0)
        def close(self): self.closed = True

    writer = Writer()
    asyncio.run(service._handle_local(Reader(), writer))
    assert writer.closed and not (tmp_path / "unused").exists()
