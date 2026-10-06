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
# Modified by PomTum contributors: listener, privacy and bounded-request checks.

from __future__ import annotations

import json
import socket
import sys
import threading
import subprocess
import logging
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import pebble_ring_bridge as bridge  # noqa: E402

MULTIPART = (
    b"--XyZ\r\n"
    b'Content-Disposition: form-data; name="transcription"\r\n\r\n'
    b"Turn on the porch light\r\n"
    b"--XyZ\r\n"
    b'Content-Disposition: form-data; name="recordedAt"\r\n\r\n'
    b"1790403830159\r\n"
    b"--XyZ--\r\n"
)


def test_multipart_form_fields():
    fields = bridge.parse_body("multipart/form-data; boundary=XyZ", MULTIPART)
    assert fields == {"transcription": "Turn on the porch light", "recordedAt": "1790403830159"}
    assert bridge.transcription(fields) == "Turn on the porch light"


def test_json_and_plain_text():
    assert bridge.transcription(bridge.parse_body("application/json", b'{"text": " hi "}')) == "hi"
    assert bridge.transcription(bridge.parse_body("text/plain", b"hello")) == "hello"


@pytest.mark.parametrize("headers, fields", [
    ({"X-Pebble-Token": "s3"}, {}),
    ({"Authorization": "Bearer s3"}, {}),
    ({}, {"token": "s3"}),
])
def test_token_sources(headers, fields):
    assert bridge.presented_token(headers, fields) == "s3"


@pytest.fixture
def server(monkeypatch):
    sent = []
    monkeypatch.setattr(bridge, "send_user_msg", lambda text: (sent.append(text) or True, "Sent to your Muse."))
    bridge.Handler.secret = "s3"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", sent
    httpd.shutdown()


def post(url, body, headers):
    request = urllib.request.Request(url + "/ingest", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_authorized_transcription_is_forwarded(server):
    url, sent = server
    status, body = post(url, MULTIPART, {
        "Content-Type": "multipart/form-data; boundary=XyZ", "X-Pebble-Token": "s3"})
    assert status == 200 and body["ok"]
    assert sent == ["Turn on the porch light [via Pebble Ring]"]


def test_wrong_token_is_rejected(server):
    url, sent = server
    status, _ = post(url, b'{"text": "hi"}', {"Content-Type": "application/json", "X-Pebble-Token": "no"})
    assert status == 401 and sent == []


def test_empty_transcription_is_rejected(server):
    url, sent = server
    status, _ = post(url, b'{"text": ""}', {"Content-Type": "application/json", "X-Pebble-Token": "s3"})
    assert status == 400 and sent == []


def test_non_ascii_token_is_rejected_not_crashed(server):
    url, sent = server
    status, _ = post(url, b'{"text": "hi"}', {"Content-Type": "application/json", "X-Pebble-Token": "s\xe9"})
    assert status == 401 and sent == []


def test_bad_content_length_gets_a_400(server):
    url, sent = server
    host, port = urllib.parse.urlsplit(url).netloc.split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(b"POST /ingest HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n"
                     b"Content-Type: application/json\r\nX-Pebble-Token: s3\r\n\r\n")
        status_line = sock.makefile("rb").readline()
    assert status_line.split()[1] == b"400" and sent == []


@pytest.mark.parametrize("host", [None, "0.0.0.0", "192.0.2.10"])
def test_listener_defaults_to_loopback_and_lan_requires_explicit_environment(monkeypatch, tmp_path, caplog, host):
    secret = "private-example-token"
    session = "private-example-side-chat"
    secret_file = tmp_path / "secret"
    secret_file.write_text(secret)
    monkeypatch.setenv("PEBBLE_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("PEBBLE_SESSION_ID", session)
    monkeypatch.setenv("PEBBLE_PORT", "8787")
    if host is None:
        monkeypatch.delenv("PEBBLE_HOST", raising=False)
    else:
        monkeypatch.setenv("PEBBLE_HOST", host)
    bound = []
    monkeypatch.setattr(bridge, "ThreadingHTTPServer", lambda address, handler:
                        (bound.append(address) or SimpleNamespace(serve_forever=lambda: None)))
    with caplog.at_level(logging.INFO, logger=bridge.log.name):
        bridge.main()
    assert bound == [(host or "127.0.0.1", 8787)]
    assert secret not in caplog.text and session not in caplog.text


@pytest.mark.parametrize("returncode", [0, 1])
def test_sender_discards_command_output_and_never_returns_session_or_transcript(monkeypatch, caplog, returncode):
    session = "private-example-side-chat"
    transcript = "private example transcript"
    secret = "private-example-token"
    monkeypatch.setenv("PEBBLE_SESSION_ID", session)
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=returncode, stdout=secret + transcript, stderr=session)

    monkeypatch.setattr(bridge.subprocess, "run", run)
    ok, detail = bridge.send_user_msg(transcript)
    assert ok is (returncode == 0)
    command, options = calls[0]
    assert command[-1] == "-" and transcript not in command
    assert options["input"] == transcript
    assert options["stdout"] == subprocess.DEVNULL and options["stderr"] == subprocess.DEVNULL
    for private in (session, transcript, secret):
        assert private not in detail + caplog.text


@pytest.mark.parametrize("timed_out", [False, True])
def test_sender_exception_messages_and_timeout_command_are_not_exposed(monkeypatch, caplog, timed_out):
    session = "private-example-side-chat"
    private = "private-example-token-and-transcript"
    monkeypatch.setenv("PEBBLE_SESSION_ID", session)

    def run(command, **options):
        if timed_out:
            raise subprocess.TimeoutExpired(command, 1, output=private, stderr=private)
        raise OSError(private)

    monkeypatch.setattr(bridge.subprocess, "run", run)
    ok, detail = bridge.send_user_msg(private)
    assert not ok and detail == ("sender timed out" if timed_out else "sender could not start")
    assert private not in detail + caplog.text and session not in detail + caplog.text


def test_http_failure_and_debug_access_logs_hide_private_details(server, monkeypatch, caplog):
    url, sent = server
    private = "private-example-session-token-transcript"
    monkeypatch.setattr(bridge, "send_user_msg", lambda text: (False, private))
    request = urllib.request.Request(url + "/ingest?" + private,
        data=b'{"text":"private example transcription"}', headers={
            "Content-Type": "application/json", "X-Pebble-Token": "s3"}, method="POST")
    with caplog.at_level(logging.DEBUG, logger=bridge.log.name):
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
    body = json.loads(error.value.read())
    assert error.value.code == 502 and body == {"ok": False, "detail": "sender rejected message"}
    assert private not in json.dumps(body) + caplog.text
    assert "private example transcription" not in caplog.text and sent == []


@pytest.mark.parametrize("length", ["-1", "", "1048577"])
def test_negative_missing_and_oversized_body_lengths_are_bounded(server, length):
    url, sent = server
    host, port = urllib.parse.urlsplit(url).netloc.split(":")
    header = f"Content-Length: {length}\r\n" if length else ""
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(("POST /ingest HTTP/1.1\r\nHost: x\r\n" + header + "\r\n").encode())
        status_line = sock.makefile("rb").readline()
    assert status_line.split()[1] == (b"413" if length == "1048577" else b"400") and sent == []


def test_incomplete_and_stalled_body_never_reach_sender(server, monkeypatch):
    url, sent = server
    monkeypatch.setattr(bridge, "READ_TIMEOUT_S", 0.1)
    host, port = urllib.parse.urlsplit(url).netloc.split(":")
    for close_write in (True, False):
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            sock.sendall(b"POST /ingest HTTP/1.1\r\nHost: x\r\nContent-Length: 20\r\n"
                         b"Content-Type: application/json\r\nX-Pebble-Token: s3\r\n\r\n{}")
            if close_write:
                sock.shutdown(socket.SHUT_WR)
            status_line = sock.makefile("rb").readline()
        assert status_line.split()[1] == (b"400" if close_write else b"408")
    assert sent == []


def test_transfer_encoded_requests_are_rejected_before_body_read(server):
    url, sent = server
    status, body = post(url, b"", {"Transfer-Encoding": "chunked"})
    assert status == 400 and body["error"] == "unsupported transfer encoding" and sent == []
