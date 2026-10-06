import http.client
# Modified by PomTum contributors: same-origin SDK-token API regression tests.
import json
import threading
import uuid

import pytest

from musegadget.http_bridge import Bridge, make_server


@pytest.fixture
def running_bridge(tmp_path):
    (tmp_path / "index.html").write_text("<html>Muse local UI</html>")
    calls = []

    async def fake_request(_socket, value):
        calls.append(value)
        if value["op"] == "status":
            return {"ok": True, "connected": True, "registered": True,
                    "pending": False, "last_error": None, "sdk_version": "0.1.0"}
        if value["op"] == "chat":
            return {"ok": True, "accepted": True, "request_id": value["request_id"],
                    "session_id": value["session_id"], "message_id": "u1"}
        if value["op"] == "identity":
            return {"ok": True, "result": {"name": "Muse", "avatar": {"image": "/example/avatar.png"}}}
        if value["op"] == "sdk-token-status":
            return {"ok": True, "configured": False, "token": "must never reach browser"}
        if value["op"] == "sdk-token-save":
            return {"ok": True, "saved": True, "pairingRequired": True, "token": value["token"]}
        return {"ok": True, "local_cancelled": True, "cloud_cancelled": False}

    bridge = Bridge("unused.sock", tmp_path, 0, request_func=fake_request)
    server = make_server(bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield bridge, calls
    bridge.stop.set()
    with bridge.condition:
        bridge.condition.notify_all()
    server.shutdown()
    server.server_close()
    thread.join(2)


def request(bridge, method, path, value=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=3)
    body = json.dumps(value) if value is not None else None
    merged = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{bridge.port}"}
    if headers:
        merged.update(headers)
    conn.request(method, path, body=body, headers=merged)
    reply = conn.getresponse()
    data = reply.read()
    status = reply.status
    conn.close()
    return status, data


def test_local_status_text_chat_client_request_id_and_local_cancel(running_bridge):
    bridge, calls = running_bridge
    status, data = request(bridge, "GET", "/api/status")
    assert status == 200 and json.loads(data)["connected"]
    rid = str(uuid.uuid4())
    status, data = request(bridge, "POST", "/api/chat", {"text": "hi", "request_id": rid})
    assert status == 200 and json.loads(data)["request_id"] == rid
    assert calls[-1]["op"] == "chat" and calls[-1]["request_id"] == rid
    status, data = request(bridge, "POST", "/api/cancel", {"request_id": rid})
    assert status == 200 and json.loads(data)["cloud_cancelled"] is False


def test_cross_origin_missing_origin_and_dns_rebinding_are_denied(running_bridge):
    bridge, calls = running_bridge
    for headers in ({"Origin": "https://hostile.example"}, {"Origin": ""},
                    {"Host": "hostile.example", "Origin": "http://hostile.example"},
                    {"Sec-Fetch-Site": "cross-site"}):
        status, _ = request(bridge, "POST", "/api/chat", {"text": "private"}, headers)
        assert status == 403
    assert not calls


def test_body_size_and_invalid_audio_rejected_before_ipc(running_bridge):
    bridge, calls = running_bridge
    status, _ = request(bridge, "POST", "/api/chat", {"text": "x"}, {"Content-Length": "2097153"})
    assert status == 413
    status, _ = request(bridge, "POST", "/api/chat", {"audio_wav_base64": "invalid"})
    assert status == 400 and not calls


def test_static_files_cannot_escape_or_read_dotfiles(running_bridge):
    bridge, _ = running_bridge
    status, body = request(bridge, "GET", "/")
    assert status == 200 and b"Muse local UI" in body
    for path in ("/../private", "/%2e%2e/private", "/.secret", "/%5cprivate", "/api/unknown"):
        assert request(bridge, "GET", path)[0] == 404


def test_sse_retains_client_request_id_and_replays_last_event_id(running_bridge):
    bridge, _ = running_bridge
    bridge.publish({"type": "delta", "request_id": "r1", "text": "first"})
    bridge.publish({"type": "message", "request_id": "r1", "message_id": "a1", "text": "done"})
    conn = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=3)
    conn.request("GET", "/api/events", headers={"Last-Event-ID": "1"})
    response = conn.getresponse()
    assert response.status == 200
    assert response.readline() == b"id: 2\n"
    event = json.loads(response.readline().decode()[6:])
    assert event == {"type": "message", "request_id": "r1", "message_id": "a1", "text": "done"}
    conn.close()


def test_extensions_are_guarded_by_the_same_origin_policy(running_bridge):
    bridge, _ = running_bridge
    bridge.extra_routes[("POST", "/api/local-extra")] = lambda value: (200, {"local": True})
    assert request(bridge, "POST", "/api/local-extra", {})[0] == 200
    assert request(bridge, "POST", "/api/local-extra", {}, {"Origin": "https://remote.example"})[0] == 403


def test_identity_route_is_dedicated_read_only_and_same_origin(running_bridge):
    bridge, calls = running_bridge
    status, body = request(bridge, "GET", "/api/identity?include_markdown=true")
    assert status == 200 and json.loads(body)["result"]["name"] == "Muse"
    assert calls == [{"op": "identity"}]  # query arguments cannot become cloud paths/options
    assert request(bridge, "GET", "/api/identity", headers={"Origin": "https://remote.example"})[0] == 403
    assert len(calls) == 1
    assert request(bridge, "GET", "/api/arbitrary-cloud-path")[0] == 404


def test_sdk_token_status_and_save_whitelist_response_and_keep_secret_out_of_events(running_bridge, caplog):
    bridge, calls = running_bridge
    status, body = request(bridge, "GET", "/api/sdk-token")
    assert status == 200 and json.loads(body) == {"configured": False}
    assert calls[-1] == {"op": "sdk-token-status"}
    token = "mgst_" + "A" * 43
    status, body = request(bridge, "POST", "/api/sdk-token", {"token": token})
    assert status == 200 and json.loads(body) == {"saved": True, "pairingRequired": True}
    assert calls[-1] == {"op": "sdk-token-save", "token": token}
    assert token not in body.decode() + caplog.text and not bridge.history


@pytest.mark.parametrize("headers", [{"Origin": "https://hostile.example"}, {"Origin": ""},
                         {"Host": "hostile.example", "Origin": "http://hostile.example"},
                         {"Sec-Fetch-Site": "cross-site"}])
def test_sdk_token_mutations_require_same_origin_and_valid_host(running_bridge, headers):
    bridge, calls = running_bridge
    assert request(bridge, "POST", "/api/sdk-token", {"token": "mgst_" + "A" * 43}, headers)[0] == 403
    assert not calls


@pytest.mark.parametrize("value", [{}, {"token": None}, {"token": 1}, {"token": "mgst_" + "A" * 42},
                     {"token": "mgst_" + "A" * 42 + "B"}, {"token": "mgst_" + "A" * 43 + "\n"},
                     {"token": "mgst_" + "A" * 43, "path": "/ignored"}])
def test_sdk_token_invalid_input_never_reaches_ipc(running_bridge, value):
    bridge, calls = running_bridge
    status, body = request(bridge, "POST", "/api/sdk-token", value)
    assert status == 400 and json.loads(body) == {"error": "invalid SDK token request"}
    assert not calls


def test_sdk_token_small_body_limit_and_get_origin_guard(running_bridge):
    bridge, calls = running_bridge
    assert request(bridge, "POST", "/api/sdk-token", {"token": "A"}, {"Content-Length": "513"})[0] == 413
    assert request(bridge, "GET", "/api/sdk-token", headers={"Origin": "https://hostile.example"})[0] == 403
    assert not calls


def test_sdk_token_daemon_failures_and_malformed_json_cannot_echo_secret(running_bridge, caplog):
    bridge, _ = running_bridge
    token = "mgst_" + "A" * 43

    async def echoing_failure(_socket, value):
        raise RuntimeError(token)

    bridge.request_func = echoing_failure
    status, body = request(bridge, "POST", "/api/sdk-token", {"token": token})
    assert status == 503 and token not in body.decode() + caplog.text
    status, body = request(bridge, "GET", "/api/sdk-token")
    assert status == 503 and token not in body.decode() + caplog.text
    conn = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=3)
    conn.request("POST", "/api/sdk-token", body='{"token": "' + token + '"', headers={
        "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{bridge.port}"})
    response = conn.getresponse()
    assert response.status == 400 and token.encode() not in response.read()
    conn.close()
