"""Local chat validation, bounded events and conservative reply correlation.

No credentials or cloud HTTP clients belong in this module. Unattributed
cloud messages are deliberately omitted rather than attached to a wrong turn.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import uuid
import wave
from collections import OrderedDict, deque
from dataclasses import dataclass, field

MAX_LOCAL_REQUEST = 2 * 1024 * 1024
MAX_TEXT = 32768
MAX_WAV = 640128  # 20 seconds of mono 16 kHz PCM16, plus container headers
SESSION_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
MAX_SEEN_SEQUENCES = 1024
MAX_REJECTED_MESSAGES = 128
MAX_RETIRED_MESSAGES = 1024
log = logging.getLogger(__name__)


def _diagnostic_name(value):
    if not isinstance(value, str):
        return None
    return "".join(ch if ch.isprintable() else "?" for ch in value[:64])


def _diagnostic_keys(value):
    return [_diagnostic_name(key) for key in list(value)[:32]] if isinstance(value, dict) else []


def validate_chat(request: dict) -> dict:
    text = request.get("text", "")
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError("text must be a string of at most 32768 characters")
    sid = request.get("session_id")
    if sid is not None and not (isinstance(sid, str) and SESSION_RE.fullmatch(sid)):
        raise ValueError("session_id must be letters, digits and dashes (1-64)")
    rid = request.get("request_id") or str(uuid.uuid4())
    try:
        if not isinstance(rid, str) or str(uuid.UUID(rid)) != rid.lower():
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError("request_id must be a UUID") from None
    audio = request.get("audio_wav_base64")
    if audio is not None:
        if not isinstance(audio, str) or len(audio) > ((MAX_WAV + 2) // 3) * 4:
            raise ValueError("audio exceeds the 20-second WAV limit")
        try:
            raw = base64.b64decode(audio, validate=True)
            if len(raw) > MAX_WAV:
                raise ValueError()
            with wave.open(io.BytesIO(raw), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise ValueError()
                count = wav.getnframes()
                if not 0 < count <= 320000 or len(wav.readframes(count)) != count * 2:
                    raise ValueError()
        except (ValueError, binascii.Error, wave.Error, EOFError):
            raise ValueError("audio must be complete mono 16 kHz PCM16 WAV, at most 20 seconds") from None
    if not text.strip() and audio is None:
        raise ValueError("text or audio_wav_base64 is required")
    return {"text": text, "session_id": sid, "request_id": rid.lower(), "audio_wav_base64": audio}


class NDJSONDecoder:
    """Split on bytes, so a UTF-8 character may straddle arbitrary chunks."""

    def __init__(self, max_line: int = 1024 * 1024):
        self.buffer = bytearray()
        self.max_line = max_line

    def feed(self, data: bytes, final: bool = False) -> list[dict]:
        self.buffer.extend(data)
        lines = []
        while True:
            end = self.buffer.find(b"\n")
            if end < 0:
                break
            if end > self.max_line:
                raise ValueError("subscription line too large")
            lines.append(bytes(self.buffer[:end]))
            del self.buffer[:end + 1]
        if len(self.buffer) > self.max_line:
            raise ValueError("subscription line too large")
        if final and self.buffer:
            lines.append(bytes(self.buffer))
            self.buffer.clear()
        decoded = []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                decoded.append(value)
        return decoded


class EventHub:
    def __init__(self, history: int = 256, queue_size: int = 128):
        self.history = deque(maxlen=history)
        self.queues: set[asyncio.Queue] = set()
        self.queue_size = queue_size
        self.sequence = 0

    def publish(self, event: dict) -> None:
        self.sequence += 1
        record = {"event_id": self.sequence, "event": event}
        self.history.append(record)
        for queue in tuple(self.queues):
            if queue.full():
                # Fail this subscriber explicitly; never grow an unbounded queue.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"event_id": self.sequence, "event": {
                    "type": "error", "code": "subscriber_overflow", "text": "reconnect to resume events"}})
                self.queues.discard(queue)
            else:
                queue.put_nowait(record)

    def subscribe(self, after: int = 0) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=self.queue_size)
        records = [r for r in self.history if r["event_id"] > after]
        if len(records) >= self.queue_size or (after and self.history and after < self.history[0]["event_id"] - 1):
            queue.put_nowait({"event_id": self.sequence, "event": {
                "type": "status", "replay_lost": True}})
            records = records[-(self.queue_size - 1):]
        for record in records:
            queue.put_nowait(record)
        self.queues.add(queue)
        return queue

    def unsubscribe(self, queue) -> None:
        self.queues.discard(queue)


@dataclass
class Turn:
    request_id: str
    session_id: str | None
    acknowledged: bool = False
    cancelled: bool = False
    complete: bool = False
    roots: set[str] = field(default_factory=set)
    messages: dict[str, str] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)
    rejected: set[str] = field(default_factory=set)
    rejected_overflow: bool = False
    early: list[dict] = field(default_factory=list)
    early_bytes: int = 0


class ChatRouter:
    def __init__(self, hub: EventHub):
        self.hub = hub
        self.turn: Turn | None = None
        self._seen_sequences = OrderedDict()
        self._retired_messages = OrderedDict()
        self._protocol_diagnostics = os.environ.get("MUSE_UI_PROTOCOL_DIAGNOSTICS") == "1"
        self._protocol_event_count = 0
        self._protocol_ack_count = 0

    def reset_sequence(self):
        self._seen_sequences.clear()

    def _duplicate_event(self, event):
        """Persistent messages and transient activity have different counters.

        Equality-based bounded deduplication also permits delayed/out-of-order
        events within either domain. A smaller sequence is not evidence of replay.
        Names distinguish counters without assuming a closed server event schema.
        """
        seq, name = event.get("seq"), event.get("event")
        if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < (1 << 64):
            return False
        if not isinstance(name, str) or len(name) > 128:
            return False
        domain = "persistent" if name.startswith("message.") else "transient"
        key = (domain, name, seq)
        if key in self._seen_sequences:
            return True
        self._seen_sequences[key] = None
        if len(self._seen_sequences) > MAX_SEEN_SEQUENCES:
            self._seen_sequences.popitem(last=False)
        return False

    def _reject(self, turn, mid):
        if not isinstance(mid, str):
            return
        if len(turn.rejected) < MAX_REJECTED_MESSAGES:
            turn.rejected.add(mid)
        else:
            turn.rejected_overflow = True

    def _diagnose_event(self, event):
        if not self._protocol_diagnostics or self._protocol_event_count >= 100:
            return
        self._protocol_event_count += 1
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        turn = self.turn
        sid = payload.get("session_id", event.get("session_id"))
        mid = payload.get("message_id") or event.get("message_id") or payload.get("id")
        parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
        sequence = event.get("seq")
        metadata = {
            "kind": "event", "sample": self._protocol_event_count,
            "type": _diagnostic_name(event.get("type")), "event": _diagnostic_name(event.get("event")),
            "seq": sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None,
            "payload_keys": _diagnostic_keys(payload),
            "sid_present": "session_id" in payload or "session_id" in event,
            "sid_matches_turn": bool(turn and sid == turn.session_id),
            "sid_filter_applies": bool(turn and turn.session_id is not None and sid is not None),
            "mid_present": isinstance(mid, str), "parent_present": isinstance(parent, str),
            "mid_in_roots": bool(turn and isinstance(mid, str) and mid in turn.roots),
            "parent_in_roots": bool(turn and isinstance(parent, str) and parent in turn.roots),
            "mid_bound": bool(turn and isinstance(mid, str) and mid in turn.messages),
            "turn_present": turn is not None, "turn_acknowledged": bool(turn and turn.acknowledged),
        }
        log.info("Muse protocol structure %s", json.dumps(metadata, separators=(",", ":")))

    @property
    def pending(self) -> bool:
        return bool(self.turn and not self.turn.complete and not self.turn.cancelled)

    def begin(self, request_id: str, session_id: str | None) -> None:
        if self.pending:
            raise ValueError("a foreground turn is already pending; locally cancel it first")
        if self.turn:
            for mid in self.turn.messages.keys() | self.turn.rejected:
                self._retired_messages[mid] = None
            while len(self._retired_messages) > MAX_RETIRED_MESSAGES:
                self._retired_messages.popitem(last=False)
        self.turn = Turn(request_id, session_id)

    def fields(self) -> dict:
        turn = self.turn
        return {"request_id": turn.request_id, "session_id": turn.session_id} if turn else {}

    def acknowledge(self, result: dict, request_id: str | None = None,
                    session_id: str | None = None) -> dict:
        turn = self.turn
        value = result.get("response")
        if isinstance(value, dict) and isinstance(value.get("result"), dict):
            value = value["result"]
        value = value if isinstance(value, dict) else {}
        if self._protocol_diagnostics and self._protocol_ack_count < 16:
            self._protocol_ack_count += 1
            log.info("Muse protocol structure %s", json.dumps({
                "kind": "ack", "top_level_keys": _diagnostic_keys(result.get("response")),
                "message_id_present": isinstance(value.get("message_id"), str),
            }, separators=(",", ":")))
        mid = value.get("message_id")
        sid = value.get("session_id")
        if request_id and (not turn or turn.request_id != request_id):
            return {"accepted": True, "request_id": request_id, "session_id": session_id,
                    "message_id": mid if isinstance(mid, str) else None}
        if turn:
            if isinstance(sid, str) and SESSION_RE.fullmatch(sid):
                turn.session_id = sid
            for key in ("message_id", "reply_to_message_id"):
                if isinstance(value.get(key), str):
                    turn.roots.add(value[key])
            turn.acknowledged = True
            buffered, turn.early = turn.early, []
            turn.early_bytes = 0
            if not turn.roots:
                turn.complete = True
                self.hub.publish({"type": "error", **self.fields(), "code": "ack_missing_id",
                                  "text": "message accepted but reply correlation is unavailable"})
            else:
                for event in buffered:
                    self._event(event)
        return {"accepted": True, **self.fields(), "message_id": mid if isinstance(mid, str) else None}

    def fail(self, code: str, text: str, request_id: str | None = None) -> None:
        if self.turn and not self.turn.cancelled and not self.turn.complete and (request_id is None or self.turn.request_id == request_id):
            self.turn.complete = True
            self.hub.publish({"type": "error", **self.fields(), "code": code, "text": text})

    def cancel(self, request_id: str) -> bool:
        if not self.turn or self.turn.request_id != request_id:
            return False
        self.turn.cancelled = True
        self.turn.complete = True
        self.turn.early.clear()
        self.hub.publish({"type": "activity", **self.fields(), "status": "locally_cancelled",
                          "cloud_cancelled": False})
        return True

    def on_event(self, event: dict) -> None:
        self._diagnose_event(event)
        if event.get("type") != "event":
            return
        if self._duplicate_event(event):
            return
        if event.get("event") in ("agent.status", "task.status"):
            payload = event.get("payload") or {}
            if isinstance(payload, dict):
                self.hub.publish({"type": "activity", "scope": "connection", **self.fields(),
                                  "status": str(payload.get("status", ""))[:128],
                                  "activity_code": str(payload.get("activity_code", ""))[:128]})
            return
        turn = self.turn
        if not turn or turn.cancelled or turn.complete:
            return
        if not turn.acknowledged:
            size = len(json.dumps(event))
            if len(turn.early) >= 512 or turn.early_bytes + size > 1024 * 1024:
                self.fail("early_event_overflow", "reply buffer exceeded its limit")
                return
            turn.early.append(event)
            turn.early_bytes += size
        else:
            self._event(event)

    def _event(self, event: dict) -> None:
        turn = self.turn
        if not turn or turn.cancelled or turn.complete or not turn.acknowledged:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        name = event.get("event")
        mid = next((value for value in (payload.get("message_id"), event.get("message_id"), payload.get("id"))
                    if isinstance(value, str) and value), None)
        if mid is None:
            return
        sid = payload.get("session_id", event.get("session_id"))
        if sid is not None and turn.session_id is not None and sid != turn.session_id:
            self._reject(turn, mid)
            return
        parent = next((value for value in (payload.get("reply_to_message_id"), payload.get("parent_message_id"))
                       if isinstance(value, str) and value), None)
        if name == "message.user":
            if mid not in turn.roots:
                return
            text = payload.get("display_text") or payload.get("content")
            if isinstance(text, str) and text and text != "[Voice note]":
                text = text.split("\n[file:", 1)[0]
                self.hub.publish({"type": "message", **self.fields(), "message_id": mid,
                                  "role": "user", "text": text[:MAX_TEXT], "done": True})
            return
        if name not in ("delta.message_start", "delta.text_append", "delta.message_done", "message.assistant"):
            return
        if mid in turn.rejected:
            return
        if mid not in turn.messages:
            if mid in self._retired_messages:
                self._reject(turn, mid)
                return
            # Match the official bind_msg: reject an unrelated explicit parent,
            # but allow first binding without one during an ACKed foreground turn.
            if parent and parent not in turn.roots:
                self._reject(turn, mid)
                return
            if (not parent and turn.rejected_overflow) or len(turn.messages) >= 32:
                return
            turn.messages[mid] = ""
            turn.roots.add(mid)
        # Once bound, the same reply ID wins over later parent-field differences.
        # Cross-session rejection above still applies to every event.
        if mid in turn.finished:
            return
        if name == "delta.text_append":
            text = payload.get("text")
            if isinstance(text, str) and len(turn.messages[mid]) + len(text) <= MAX_TEXT:
                turn.messages[mid] += text
                self.hub.publish({"type": "delta", **self.fields(), "message_id": mid, "text": text})
        elif name in ("delta.message_done", "message.assistant"):
            if name == "message.assistant" and payload.get("display_text_ready") is False:
                return
            text = payload.get("display_text") or payload.get("content") or turn.messages[mid]
            if not isinstance(text, str):
                text = turn.messages[mid]
            turn.finished.add(mid)
            turn.complete = True
            self.hub.publish({"type": "message", **self.fields(), "message_id": mid,
                              "role": "assistant", "text": text[:MAX_TEXT], "done": True})
