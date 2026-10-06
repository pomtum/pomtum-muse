import asyncio
import base64
import io
import json
import uuid
import wave

import pytest

from musegadget.chat import ChatRouter, EventHub, NDJSONDecoder, validate_chat
from musegadget.link_client import IdentityUnavailable, _Subscription
from musegadget.noise import ApplicationResponse, BodyChunk, ServiceFrame
from test_link_client import make_session


def wav_base64(seconds=1, rate=16000):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * rate * seconds)
    return base64.b64encode(out.getvalue()).decode()


def cloud(name, mid="a1", parent="u1", text=None, seq=None, sid=None):
    payload = {"message_id": mid}
    if parent:
        payload["reply_to_message_id"] = parent
    if text is not None:
        payload["text" if name == "delta.text_append" else "display_text"] = text
    if sid:
        payload["session_id"] = sid
    value = {"type": "event", "event": name, "payload": payload}
    if seq is not None:
        value["seq"] = seq
    return value


def records(hub):
    return [r["event"] for r in hub.history]


def test_ndjson_every_byte_utf8_multiple_lines_and_final_tail():
    wire = json.dumps({"text": "你好🐈"}, ensure_ascii=False).encode() + b"\r\n" + b'{"n":2}'
    decoder = NDJSONDecoder()
    events = []
    for byte in wire:
        events += decoder.feed(bytes([byte]))
    events += decoder.feed(b"", final=True)
    assert events == [{"text": "你好🐈"}, {"n": 2}]
    assert not decoder.buffer


def test_ndjson_is_bounded_and_drops_only_malformed_lines():
    decoder = NDJSONDecoder(10)
    assert decoder.feed(b'bad\n{"a":1}\n') == [{"a": 1}]
    with pytest.raises(ValueError):
        decoder.feed(b"x" * 11)


def test_voice_only_input_is_validated_without_an_empty_text_requirement():
    rid = str(uuid.uuid4())
    value = validate_chat({"audio_wav_base64": wav_base64(), "request_id": rid})
    assert value["text"] == "" and value["request_id"] == rid


@pytest.mark.parametrize("payload", [
    {}, {"text": " "}, {"text": "x", "session_id": "../bad"},
    {"text": "x", "request_id": "not-a-uuid"}, {"audio_wav_base64": "bad"},
    {"text": "x" * 32769}, {"audio_wav_base64": wav_base64(rate=24000)},
    {"audio_wav_base64": wav_base64(seconds=21)},
])
def test_invalid_inputs_fail_before_any_cloud_access(payload):
    with pytest.raises(ValueError):
        validate_chat(payload)


def test_ack_before_or_after_deltas_gives_the_same_correlated_reply():
    hub = EventHub()
    router = ChatRouter(hub)
    rid = str(uuid.uuid4())
    router.begin(rid, "side-1")
    router.on_event(cloud("delta.message_start", seq=1))
    router.on_event(cloud("delta.text_append", text="你好", seq=2))
    assert records(hub) == []
    result = router.acknowledge({"response": {"result": {"message_id": "u1"}}}, rid)
    assert result["message_id"] == "u1"
    assert records(hub)[0] == {"type": "delta", "request_id": rid, "session_id": "side-1",
                               "message_id": "a1", "text": "你好"}
    router.on_event(cloud("delta.message_done", text="你好", seq=3))
    router.on_event(cloud("message.assistant", text="你好", seq=4))
    assert len(records(hub)) == 2 and not router.pending


def test_old_parent_wrong_chat_rejected_parentless_and_duplicate_seq_do_not_cross_turns():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r1", "s1")
    router.acknowledge({"response": {"message_id": "u1"}})
    router.on_event(cloud("delta.text_append", text="first", seq=1))
    router.on_event(cloud("delta.text_append", text="first", seq=1))
    router.cancel("r1")
    router.begin("r2", "s2")
    router.acknowledge({"response": {"message_id": "u2"}})
    before = len(records(hub))
    router.on_event(cloud("delta.text_append", mid="late", text="bad", seq=2))
    router.on_event(cloud("delta.text_append", mid="late", parent=None, text="bad", seq=3))
    router.on_event(cloud("delta.text_append", mid="x", parent="u2", text="bad", seq=4, sid="s1"))
    assert len(records(hub)) == before
    router.on_event(cloud("delta.text_append", mid="a2", parent="u2", text="second", seq=5, sid="s2"))
    assert records(hub)[-1]["request_id"] == "r2"


def test_actual_main_chat_trace_binds_start_and_ignores_later_parent_difference():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r-main", None)
    router.acknowledge({"response": {"session_id": "main-1", "message_id": "u1"}})
    router.on_event({"type": "event", "event": "task.status", "seq": 3004,
                     "payload": {"status": "running"}})
    router.on_event(cloud("message.user", mid="u1", parent=None, text="transcript", seq=2319, sid="main-1"))
    router.on_event({"type": "event", "event": "agent.status", "seq": 3005,
                     "payload": {"status": "responding"}})
    router.on_event(cloud("delta.message_start", parent=None, seq=3007, sid="main-1"))
    router.on_event(cloud("delta.text_append", parent="server-other-parent", text="streamed reply",
                          seq=3008, sid="main-1"))
    router.on_event(cloud("delta.message_done", parent=None, seq=3011, sid="main-1"))
    router.on_event({"type": "event", "event": "task.status", "seq": 3012,
                     "payload": {"status": "completed"}})
    messages = [value for value in records(hub) if value["type"] in ("delta", "message")]
    assert [(value["type"], value.get("role"), value["text"]) for value in messages] == [
        ("message", "user", "transcript"), ("delta", None, "streamed reply"),
        ("message", "assistant", "streamed reply")]
    assert all(value["request_id"] == "r-main" and value["session_id"] == "main-1" for value in messages)
    assert not router.pending


def test_explicit_reject_cannot_revive_parentless_and_reject_overflow_fails_closed():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r", "s1")
    router.acknowledge({"response": {"message_id": "u1"}})
    router.on_event(cloud("delta.message_start", mid="rejected", parent="not-ours", sid="s1"))
    router.on_event(cloud("delta.text_append", mid="rejected", parent=None, text="wrong", sid="s1"))
    assert not records(hub) and "rejected" in router.turn.rejected
    for index in range(130):
        router.on_event(cloud("delta.message_start", mid=f"wrong-{index}", parent="not-ours", sid="s1"))
    assert len(router.turn.rejected) == 128 and router.turn.rejected_overflow
    router.on_event(cloud("delta.text_append", mid="new-parentless", parent=None, text="wrong", sid="s1"))
    assert not records(hub)
    router.on_event(cloud("delta.text_append", mid="explicit-ours", parent="u1", text="right", sid="s1"))
    assert records(hub)[-1]["text"] == "right"


def test_bound_mid_still_rejects_other_session_and_cannot_revive():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r", "s1")
    router.acknowledge({"response": {"message_id": "u1"}})
    router.on_event(cloud("delta.message_start", parent=None, sid="s1"))
    router.on_event(cloud("delta.text_append", parent="other", text="wrong", sid="s2"))
    router.on_event(cloud("delta.text_append", parent=None, text="wrong", sid="s1"))
    assert not records(hub) and "a1" in router.turn.rejected


def test_known_prior_reply_cannot_bind_parentless_in_a_new_turn():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r1", None)
    router.acknowledge({"response": {"message_id": "u1"}})
    router.on_event(cloud("delta.message_start", parent=None))
    router.cancel("r1")
    router.begin("r2", None)
    router.acknowledge({"response": {"message_id": "u2"}})
    before = len(records(hub))
    router.on_event(cloud("delta.text_append", parent=None, text="old"))
    assert len(records(hub)) == before
    router.on_event(cloud("delta.text_append", mid="new", parent=None, text="new"))
    assert records(hub)[-1]["request_id"] == "r2"


def test_no_binding_without_ack_after_error_or_after_local_cancel():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r", None)
    router.on_event(cloud("delta.text_append", parent=None, text="early"))
    assert not router.turn.messages and not records(hub)
    router.fail("delivery_failed", "failed")
    router.on_event(cloud("delta.text_append", parent=None, text="late"))
    assert not router.turn.messages
    router.begin("r2", None)
    router.acknowledge({"response": {"message_id": "u2"}})
    router.cancel("r2")
    before = len(records(hub))
    router.on_event(cloud("delta.text_append", parent=None, text="late"))
    assert len(records(hub)) == before


def test_sequence_dedup_is_bounded_per_event_domain_without_maximum_seq_filter():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r", None)
    router.acknowledge({"response": {"message_id": "u1"}})
    status = {"type": "event", "event": "task.status", "seq": 3004, "payload": {"status": "running"}}
    router.on_event(status)
    user = cloud("message.user", mid="u1", parent=None, text="transcript", seq=2319)
    router.on_event(user)
    before = len(records(hub))
    router.on_event(user)
    router.on_event(status)
    assert len(records(hub)) == before
    assert records(hub)[-1]["role"] == "user"
    for sequence in range(4000, 5200):
        router.on_event({**status, "seq": sequence})
    assert len(router._seen_sequences) == 1024
    router.reset_sequence()
    assert not router._seen_sequences


@pytest.mark.parametrize("location", ["payload", "outer", "id", "invalid-payload-outer"])
def test_official_message_id_locations_work_with_parentless_binding(location):
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r", None)
    router.acknowledge({"response": {"message_id": "u1"}})
    value = cloud("delta.text_append", parent=None, text="reply")
    if location != "payload":
        del value["payload"]["message_id"]
        if location == "id":
            value["payload"]["id"] = "a1"
        else:
            value["message_id"] = "a1"
        if location == "invalid-payload-outer":
            value["payload"]["message_id"] = {"invalid": True}
    router.on_event(value)
    assert records(hub)[-1]["message_id"] == "a1"


def test_late_ack_after_cancel_does_not_bind_a_new_turn():
    router = ChatRouter(EventHub())
    router.begin("r1", None)
    router.cancel("r1")
    router.begin("r2", None)
    result = router.acknowledge({"response": {"message_id": "u1"}}, "r1")
    assert result["request_id"] == "r1" and not router.turn.acknowledged
    router.fail("old", "old delivery failure", "r1")
    assert router.pending


def test_missing_ack_id_is_reported_instead_of_guessing_another_chat():
    hub = EventHub()
    router = ChatRouter(hub)
    router.begin("r", None)
    result = router.acknowledge({"response": {"accepted": True}})
    assert result["message_id"] is None and not router.pending
    assert records(hub)[-1]["code"] == "ack_missing_id"


def test_event_hub_bounds_slow_consumers_and_reports_lost_replay():
    async def scenario():
        hub = EventHub(history=3, queue_size=2)
        queue = hub.subscribe()
        for i in range(5):
            hub.publish({"type": "delta", "text": str(i)})
        assert queue.qsize() == 1 and queue.get_nowait()["event"]["code"] == "subscriber_overflow"
        replay = hub.subscribe(after=1)
        assert replay.get_nowait()["event"]["replay_lost"]
        assert replay.qsize() == 1 and len(hub.history) == 3
    asyncio.run(scenario())


def test_subscription_refusal_and_reset_fail_cleanly_without_full_response_buffer():
    async def scenario():
        subscription = _Subscription(lambda event: None)
        subscription.on_frame(ServiceFrame.response(1, ApplicationResponse(status=403)))
        for future in (subscription.ready, subscription.done):
            with pytest.raises(ConnectionError):
                await future
    asyncio.run(scenario())


def test_real_noise_single_connection_subscription_before_send_and_chunked_early_events():
    async def scenario():
        connects = []
        session, vm = make_session(lambda *a: {"ok": True}, connects)
        hub = EventHub()
        router = ChatRouter(hub)
        session._on_event = router.on_event
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await vm.handshake()
        await vm.accept_control_stream()
        register = await vm.next_message()
        await vm.send_message({"id": register["id"], "ok": True})
        subscription = await vm.next_frame()
        assert subscription.value.path == "/chat/subscribe" and subscription.value.body == b"{}"
        await vm.send_frame(ServiceFrame.response(subscription.stream_id, ApplicationResponse(status=200)))
        rid = str(uuid.uuid4())
        router.begin(rid, None)
        reply = asyncio.create_task(session.send_chat("hello", request_id=rid))
        request = await vm.next_frame()
        assert request.value.path == "/chat/stream"
        assert json.loads(request.value.body)["device_id"] == "homelink-abcdef"
        wire = (json.dumps(cloud("delta.text_append", text="你好"), ensure_ascii=False) + "\n").encode()
        # Deliberately cut a UTF-8 character inside its three-byte encoding.
        cut = wire.index("你".encode()) + 1
        for part in (wire[:cut], wire[cut:]):
            await vm.send_frame(ServiceFrame.body_chunk(subscription.stream_id, BodyChunk(data=part)))
        await vm.send_frame(ServiceFrame.response(request.stream_id, ApplicationResponse(
            status=200, body=b'{"message_id":"u1"}', end_body=True)))
        router.acknowledge(await asyncio.wait_for(reply, 2), rid)
        assert records(hub)[-1]["text"] == "你好"
        assert len(connects) == 1
        # The original command channel still works with the subscription open.
        await vm.send_message({"method": "link.invoke", "id": "tool", "command": "system.run"})
        result = await vm.next_message()
        assert result["method"] == "link.result"
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


def test_large_voice_note_is_streamed_in_small_body_chunks_on_same_noise_session():
    async def scenario():
        session, vm = make_session(lambda *a: {"ok": True}, [])
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        audio = wav_base64(seconds=10)
        reply = asyncio.create_task(session.send_chat("", audio_wav_base64=audio))
        request = await vm.next_frame()
        assert request.value.path == "/chat/stream" and not request.value.end_body
        body = bytearray()
        while True:
            chunk = await vm.next_frame()
            assert chunk.stream_id == request.stream_id and len(chunk.value.data) <= 16384
            body.extend(chunk.value.data)
            if chunk.value.end_body:
                break
        assert json.loads(body)["items"][0]["data_base64"] == audio
        await vm.send_frame(ServiceFrame.response(request.stream_id, ApplicationResponse(
            status=200, body=b'{"message_id":"u1"}', end_body=True)))
        assert (await reply)["ok"]
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


def test_connection_loss_rejects_pending_ack_and_no_automatic_resend():
    async def scenario():
        connects = []
        session, vm = make_session(lambda *a: {"ok": True}, connects)
        task = asyncio.create_task(session.run(asyncio.Event()))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        reply = asyncio.create_task(session.send_chat("hi"))
        await vm.next_frame()
        await vm.ws.close()
        await asyncio.wait_for(task, 2)
        with pytest.raises(ConnectionError):
            await reply
        assert len(connects) == 1 and not session._requests
    asyncio.run(scenario())


def test_ack_timeout_cleans_only_its_stream_and_next_message_still_works(monkeypatch):
    monkeypatch.setattr("musegadget.link_client.REQUEST_TIMEOUT_S", 0.01)
    async def scenario():
        connects = []
        session, vm = make_session(lambda *a: {"ok": True}, connects)
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        reply = asyncio.create_task(session.send_chat("first"))
        first = await vm.next_frame()
        with pytest.raises(asyncio.TimeoutError):
            await reply
        assert not session._requests
        # The next request uses a realistic scheduling budget on Windows hosts.
        monkeypatch.setattr("musegadget.link_client.REQUEST_TIMEOUT_S", 2)
        await vm.send_frame(ServiceFrame.response(first.stream_id,
            ApplicationResponse(status=200, body=b'{"message_id":"old"}', end_body=True)))
        second_reply = asyncio.create_task(session.send_chat("second"))
        second = await vm.next_frame()
        await vm.send_frame(ServiceFrame.response(second.stream_id,
            ApplicationResponse(status=200, body=b'{"message_id":"new"}', end_body=True)))
        assert (await second_reply)["response"]["message_id"] == "new"
        assert len(connects) == 1
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


def test_ended_subscription_reopens_without_a_second_websocket_or_registration():
    async def scenario():
        connects, events = [], []
        session, vm = make_session(lambda *a: {"ok": True}, connects)
        session._on_event = events.append
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await vm.handshake()
        await vm.accept_control_stream()
        register = await vm.next_message()
        await vm.send_message({"id": register["id"], "ok": True})
        first = await vm.next_frame()
        await vm.send_frame(ServiceFrame.response(first.stream_id,
            ApplicationResponse(status=200, end_body=True)))
        reset = await vm.next_frame()
        assert reset.kind == "reset" and reset.stream_id == first.stream_id
        second = await asyncio.wait_for(vm.next_frame(), 3)
        assert second.value.path == "/chat/subscribe" and second.stream_id != first.stream_id
        await vm.send_frame(ServiceFrame.response(second.stream_id, ApplicationResponse(status=200)))
        await asyncio.wait_for(session._subscription_ready.wait(), 2)
        assert len(connects) == 1 and session.registered_at is not None
        assert any(event.get("type") == "subscription" and event.get("ready") is False for event in events)
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


def test_identity_get_uses_the_same_noise_session_without_markdown_or_chat():
    async def scenario():
        connects = []
        session, vm = make_session(lambda *a: {"ok": True}, connects)
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        reply = asyncio.create_task(session.fetch_identity())
        request = await vm.next_frame()
        assert request.value.verb == "GET" and request.value.path == "/identity"
        assert request.value.body == b"" and request.value.end_body
        wire = json.dumps({"ok": True, "result": {"name": "Muse", "avatar": {"image": "/example/avatar.png"}}}).encode()
        await vm.send_frame(ServiceFrame.response(request.stream_id,
            ApplicationResponse(status=200, body=wire[:13])))
        await vm.send_frame(ServiceFrame.body_chunk(request.stream_id,
            BodyChunk(data=wire[13:], end_body=True)))
        assert await reply == {"name": "Muse", "avatar": {"image": "/example/avatar.png"}}
        assert len(connects) == 1 and request.stream_id not in session._requests
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


@pytest.mark.parametrize("status,body,expected_type,keys", [
    (200, b'{"name":"PRIVATE_NAME","avatar":"PRIVATE_URL"}', "missing", ["name", "avatar"]),
    (200, b'{"ok":true,"result":["PRIVATE_URL"]}', "list", ["ok", "result"]),
    (403, b'{"ok":false,"error":"PRIVATE_CREDENTIAL"}', "missing", ["ok", "error"]),
    (404, b'not json PRIVATE_CREDENTIAL', "unavailable", []),
])
def test_identity_failure_reports_structure_without_identity_values(status, body, expected_type, keys):
    async def scenario():
        session, vm = make_session(lambda *a: {"ok": True}, [])
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        reply = asyncio.create_task(session.fetch_identity())
        request = await vm.next_frame()
        await vm.send_frame(ServiceFrame.response(request.stream_id,
            ApplicationResponse(status=status, body=body, end_body=True)))
        with pytest.raises(IdentityUnavailable) as raised:
            await reply
        diagnostic = raised.value.diagnostics
        assert diagnostic["http_status"] == status
        assert diagnostic["result_type"] == expected_type
        assert diagnostic["top_level_keys"] == keys
        assert "PRIVATE" not in json.dumps(diagnostic)
        assert "PRIVATE" not in str(raised.value)
        stop.set()
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


def test_protocol_diagnostics_are_opt_in_bounded_and_never_log_content_or_ids(monkeypatch, caplog):
    hub = EventHub()
    router = ChatRouter(hub)
    with caplog.at_level("INFO", logger="musegadget.chat"):
        router.on_event(cloud("delta.text_append", text="PRIVATE_TEXT"))
    assert not caplog.records
    monkeypatch.setenv("MUSE_UI_PROTOCOL_DIAGNOSTICS", "1")
    router = ChatRouter(hub)
    router.begin("PRIVATE_REQUEST_ID", None)
    with caplog.at_level("INFO", logger="musegadget.chat"):
        router.acknowledge({"response": {"message_id": "PRIVATE_USER_ID"}})
        for seq in range(120):
            router.on_event(cloud("delta.text_append", mid="PRIVATE_ASSISTANT_ID",
                                 parent="PRIVATE_USER_ID", text="PRIVATE_TEXT", seq=seq))
    assert len(caplog.records) == 101  # one ACK plus at most 100 event structures
    lines = [record.getMessage() for record in caplog.records]
    assert "PRIVATE" not in "\n".join(lines)
    metadata = json.loads(lines[-1].split("Muse protocol structure ", 1)[1])
    assert metadata["sample"] == 100 and metadata["parent_in_roots"] is True
    assert metadata["payload_keys"] == ["message_id", "reply_to_message_id", "text"]
