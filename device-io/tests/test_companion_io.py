import base64
import http.client
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import companion_io as companion


def make_wav(seconds=0.5, *, channels=1, rate=16000, width=2):
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * (int(seconds * rate) * channels * width // 2))
    return data.getvalue()


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class FakeCapture:
    instances = []

    def __init__(self, controller, generation):
        self.controller = controller
        self.generation = generation
        self.stop = threading.Event()
        self.send = False
        self.started = False
        self.thread = threading.Thread(target=lambda: None)
        self.instances.append(self)

    def start(self):
        self.started = True
        self.thread.start()

    def finish(self, data=None, error=None):
        self.controller.capture_finished(self, make_wav() if data is None else data, error)


def events(q):
    result = []
    while True:
        try:
            result.append(q.get_nowait()[0])
        except queue.Empty:
            return result


class ControllerTests(unittest.TestCase):
    def setUp(self):
        FakeCapture.instances = []
        self.clock = Clock()
        self.control = companion.Controller(clock=self.clock, capture_factory=FakeCapture)

    def tearDown(self):
        self.control.close()

    def active(self):
        token, q = self.control.subscribe()
        self.control.focus(True)
        return token, q

    def test_no_capture_without_both_focus_and_subscriber(self):
        self.control.key(1)
        self.control.focus(True)
        self.control.key(1)
        self.assertEqual(FakeCapture.instances, [])
        token, _ = self.control.subscribe()
        self.control.focus(False)
        self.control.key(1)
        self.assertEqual(FakeCapture.instances, [])
        self.control.unsubscribe(token)

    def test_press_repeat_release_produces_one_audio(self):
        _, q = self.active()
        self.control.key(1)
        self.control.key(1)
        self.control.key(2)
        self.assertEqual(len(FakeCapture.instances), 1)
        capture = FakeCapture.instances[0]
        self.assertTrue(capture.started)
        self.assertTrue(self.control.snapshot(False)["recording"])
        self.control.key(0)
        self.assertTrue(capture.stop.is_set())
        self.assertTrue(capture.send)
        self.assertFalse(self.control.snapshot(False)["recording"])
        capture.finish()
        received = events(q)
        self.assertEqual([e["type"] for e in received], ["key", "key", "audio"])
        self.assertEqual([received[0]["phase"], received[1]["phase"]], ["down", "up"])
        self.assertEqual(base64.b64decode(received[2]["audio_wav_base64"]), make_wav())

    def test_focus_loss_cancels_capture_and_late_result(self):
        _, q = self.active()
        self.control.key(1)
        capture = FakeCapture.instances[-1]
        self.control.focus(False)
        self.assertTrue(capture.stop.is_set())
        self.assertFalse(capture.send)
        self.control.key(0)
        capture.finish(error="late error")
        self.assertEqual(events(q), [{"type": "key", "phase": "up", "reason": "focus"}])

    def test_focus_expiry_cancels_and_requires_renewal(self):
        _, q = self.active()
        self.control.key(1)
        capture = FakeCapture.instances[-1]
        self.clock.value += companion.FOCUS_TTL
        self.control.tick()
        self.assertFalse(self.control.eligible())
        self.assertTrue(capture.stop.is_set())
        self.assertFalse(capture.send)
        capture.finish()
        self.assertNotIn("audio", [e["type"] for e in events(q)])

    def test_disconnect_cancels_and_new_sse_needs_fresh_focus(self):
        token, _ = self.active()
        self.control.key(1)
        capture = FakeCapture.instances[-1]
        self.control.unsubscribe(token)
        self.assertTrue(capture.stop.is_set())
        _, q = self.control.subscribe()
        self.assertFalse(self.control.eligible())
        self.control.key(1)
        capture.finish()
        self.assertEqual(events(q), [])

    def test_cancel_drops_audio_already_queued(self):
        _, q = self.active()
        self.control.key(1)
        capture = FakeCapture.instances[-1]
        self.control.key(0)
        capture.finish()
        old_generation = capture.generation
        self.control.cancel()
        self.assertEqual(events(q), [])
        self.assertFalse(self.control.event_valid(old_generation))

    def test_debounce_and_second_press_invalidates_finishing_first(self):
        _, q = self.active()
        self.control.key(1)
        first = FakeCapture.instances[-1]
        self.control.key(0)
        self.control.key(1)
        self.assertEqual(len(FakeCapture.instances), 1)
        self.clock.value += 0.1
        self.control.key(1)
        self.assertEqual(len(FakeCapture.instances), 2)
        first.finish()
        self.assertFalse(first.send)
        self.assertNotIn("audio", [e["type"] for e in events(q)])

    def test_limit_stops_capture_but_requires_physical_release(self):
        _, q = self.active()
        self.control.key(1)
        capture = FakeCapture.instances[-1]
        self.control.limit_reached(capture)
        self.assertTrue(capture.send)
        self.assertTrue(capture.stop.is_set())
        self.assertFalse(self.control.snapshot(False)["recording"])
        self.control.key(1)
        self.control.key(2)
        self.assertEqual(len(FakeCapture.instances), 1)
        capture.finish()
        self.assertEqual(events(q)[-1]["type"], "audio")
        self.control.key(0)
        self.clock.value += 0.1
        self.control.key(1)
        self.assertEqual(len(FakeCapture.instances), 2)

    def test_only_newest_subscriber_gets_audio(self):
        _, old_q = self.active()
        _, latest_q = self.control.subscribe()
        self.control.key(1)
        self.control.key(0)
        FakeCapture.instances[-1].finish()
        self.assertEqual([e["type"] for e in events(old_q)], ["key", "key"])
        self.assertEqual([e["type"] for e in events(latest_q)], ["key", "key", "audio"])

    def test_capture_error_ends_recording_without_repeating(self):
        _, q = self.active()
        self.control.key(1)
        FakeCapture.instances[-1].finish(error="麦克风不可用")
        self.assertFalse(self.control.snapshot(False)["recording"])
        self.assertEqual(events(q)[-1], {"type": "error", "message": "麦克风不可用"})
        self.control.key(2)
        self.assertEqual(len(FakeCapture.instances), 1)


class AudioAndDiscoveryTests(unittest.TestCase):
    def test_wav_is_canonical_bounded_and_preserves_pcm(self):
        source = make_wav(20)
        output = companion.canonical_wav(source)
        self.assertEqual(len(output), 44 + 15 * 16000 * 2)
        with wave.open(io.BytesIO(output), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
            self.assertEqual(wav.getnframes(), 240000)
            self.assertEqual(wav.readframes(2), b"\x01\x00\x01\x00")

    def test_short_capture_discarded_and_wrong_formats_rejected(self):
        self.assertEqual(companion.canonical_wav(make_wav(0.299)), b"")
        for data in (make_wav(channels=2), make_wav(rate=48000), make_wav(width=4)):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                companion.canonical_wav(data)
        with self.assertRaises((wave.Error, EOFError)):
            companion.canonical_wav(b"not wav")

    def test_only_unique_exact_name_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def named(event, name):
                device = root / event / "device"
                device.mkdir(parents=True)
                (device / "name").write_text(name)
            named("event1", "regular keyboard")
            named("event9", "adc-keys-ai-extra")
            named("event10", " adc-keys-ai ")
            self.assertIsNone(companion.discover_key(root, root / "dev"))
            named("event11", "adc-keys-ai\n")
            self.assertEqual(companion.discover_key(root, root / "dev"), root / "dev/event11")
            named("event12", "adc-keys-ai")
            self.assertIsNone(companion.discover_key(root, root / "dev"))


@unittest.skipUnless(os.name == "posix", "Real signal/process-group fixture requires Linux")
class CaptureProcessTests(unittest.TestCase):
    """A real child process substitutes for pw-record, never touches audio hardware."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.report = self.root / "process.json"
        self.capture_dir = self.root / "captures"
        self.capture_dir.mkdir()
        self.command = self.root / "fake-pw-record"
        script = (
            f"#!{sys.executable}\n"
            "import json, os, signal, stat, sys, time, wave\n"
            f"report = {str(self.report)!r}\n"
            "path = sys.argv[-1]\n"
            "def finish(sig, frame):\n"
            "    with wave.open(path, 'wb') as wav:\n"
            "        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)\n"
            "        wav.writeframes(b'\\x01\\x00' * 8000)\n"
            "    with open(report, 'w') as out:\n"
            "        json.dump({'pid': os.getpid(), 'mode': stat.S_IMODE(os.stat(path).st_mode), 'signal': sig}, out)\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGINT, finish)\n"
            "with open(report, 'w') as out:\n"
            "    json.dump({'pid': os.getpid(), 'mode': stat.S_IMODE(os.stat(path).st_mode), 'argv': sys.argv[1:]}, out)\n"
            "while True: time.sleep(0.02)\n"
        )
        self.command.write_text(script)
        self.command.chmod(0o700)
        original = tempfile.mkstemp
        self.patcher = patch.object(companion.tempfile, "mkstemp", side_effect=lambda **kwargs:
                                   original(dir=self.capture_dir, **kwargs))
        self.patcher.start()
        self.control = companion.Controller(capture_factory=lambda control, generation:
                                           companion.Capture(control, generation, str(self.command)))
        _, self.q = self.control.subscribe()
        self.control.focus(True)

    def tearDown(self):
        self.control.close()
        self.patcher.stop()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate())

    def report_ready(self):
        try:
            return bool(json.loads(self.report.read_text()))
        except (OSError, ValueError):
            return False

    def test_sigint_finalizes_wav_private_file_and_reaps_process(self):
        self.control.key(1)
        self.wait_for(self.report_ready)
        self.assertEqual(json.loads(self.report.read_text())["mode"], 0o600)
        capture = self.control.capture
        self.control.key(0)
        capture.thread.join(3)
        self.assertFalse(capture.thread.is_alive())
        result = events(self.q)
        self.assertEqual(result[-1]["type"], "audio")
        self.assertEqual(base64.b64decode(result[-1]["audio_wav_base64"]), make_wav())
        report = json.loads(self.report.read_text())
        self.assertEqual(report["signal"], companion.signal.SIGINT)
        self.assertEqual(list(self.capture_dir.iterdir()), [])
        with self.assertRaises(ProcessLookupError):
            os.kill(report["pid"], 0)

    def test_focus_loss_discards_finalized_child_audio(self):
        self.control.key(1)
        self.wait_for(self.report_ready)
        capture = self.control.capture
        self.control.focus(False)
        capture.thread.join(3)
        self.assertFalse(capture.thread.is_alive())
        self.assertNotIn("audio", [event["type"] for event in events(self.q)])
        self.assertEqual(list(self.capture_dir.iterdir()), [])


class FakeEngine:
    available = True

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def synthesize(self, text, cancelled):
        self.entered.set()
        if self.block:
            if not self.release.wait(4):
                raise RuntimeError("test timeout")
        if cancelled():
            raise companion.Cancelled()
        return make_wav()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.control = companion.Controller(capture_factory=FakeCapture)
        self.engine = FakeEngine()
        self.server = companion.CompanionServer(("127.0.0.1", 0), self.control, self.engine)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.start()

    def tearDown(self):
        self.engine.release.set()
        self.control.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, origin="http://127.0.0.1:17863", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        sent = {} if origin is None else {"Origin": origin}
        if body is not None:
            body = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        connection.request(method, path, body, sent)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def open_sse(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/events", headers={"Origin": "http://localhost:17863"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.readline(), b": connected\n")
        self.assertEqual(response.readline(), b"\n")
        return connection, response

    def test_origin_host_and_sse_enforced(self):
        for path in ("/status", "/events"):
            for origin in (None, "null", "https://127.0.0.1:17863", "http://evil.test"):
                with self.subTest(path=path, origin=origin):
                    status, headers, _ = self.request("GET", path, origin=origin)
                    self.assertEqual(status, 403)
                    self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("POST", "/focus", {"active": True}, origin=None)[0], 403)
        self.assertEqual(self.request("GET", "/status", headers={"Host": "evil.test"})[0], 403)
        status, headers, data = self.request("GET", "/status", origin="http://localhost:17863")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:17863")
        self.assertEqual(json.loads(data), {"available": True, "key_available": False, "recording": False})

    def test_preflight_is_exact(self):
        self.assertEqual(self.request("OPTIONS", "/focus", headers={
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})[0], 204)
        self.assertEqual(self.request("OPTIONS", "/events", headers={
            "Access-Control-Request-Method": "GET"})[0], 204)
        for path, method, headers in (("/focus", "GET", "content-type"),
                                      ("/events", "POST", ""),
                                      ("/tts", "POST", "authorization"),
                                      ("/unknown", "POST", "content-type")):
            self.assertEqual(self.request("OPTIONS", path, headers={
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": headers})[0], 403)

    def test_post_body_shape_and_text_limits(self):
        self.assertEqual(self.request("POST", "/focus", {"active": True})[0], 200)
        for body in ({"active": 1}, {"active": "true"}, {}, {"active": True, "extra": 1}):
            self.assertEqual(self.request("POST", "/focus", body)[0], 400)
        for text in ("", " " * 3, "x" * 2001, 100):
            self.assertEqual(self.request("POST", "/tts", {"text": text})[0], 400)
        self.assertEqual(self.request("POST", "/cancel", {"bad": True})[0], 400)
        self.assertEqual(self.request("POST", "/focus", {"active": True},
                                      headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/tts", {"text": "x" * 17000})[0], 413)

    def test_tts_wav_response_and_concurrent_cancel(self):
        status, headers, body = self.request("POST", "/tts", {"text": "你好，Muse"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(body, make_wav())
        self.engine.block = True
        self.engine.entered.clear()
        result = []
        worker = threading.Thread(target=lambda: result.append(
            self.request("POST", "/tts", {"text": "稍后说话"})))
        worker.start()
        self.assertTrue(self.engine.entered.wait(1))
        self.assertEqual(self.request("GET", "/status")[0], 200)
        connection, response = self.open_sse()
        self.assertEqual(self.request("POST", "/cancel", {})[0], 200)
        self.engine.release.set()
        worker.join(2)
        self.assertEqual(result[0][0], 409)
        response.close()
        connection.close()

    def test_sse_delivers_key_and_audio_over_real_http(self):
        connection, response = self.open_sse()
        self.request("POST", "/focus", {"active": True})
        self.control.key(1)
        self.control.key(0)
        self.control.capture_finished(FakeCapture.instances[-1], make_wav())
        received = []
        for _ in range(3):
            line = response.readline()
            self.assertTrue(line.startswith(b"data: "), line)
            received.append(json.loads(line[6:]))
            self.assertEqual(response.readline(), b"\n")
        self.assertEqual([event["type"] for event in received], ["key", "key", "audio"])
        self.assertEqual(base64.b64decode(received[-1]["audio_wav_base64"]), make_wav())
        # Reset the actual TCP connection to prove subscriber cleanup occurs.
        response.close()
        connection.close()
        deadline = time.monotonic() + 3.1
        while self.control.subscribers and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.control.subscribers, {})
        self.assertFalse(self.control.eligible())

    def test_server_rejects_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            companion.CompanionServer(("0.0.0.0", 0), self.control, self.engine)


if __name__ == "__main__":
    unittest.main()
