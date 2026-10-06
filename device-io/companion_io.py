#!/usr/bin/env python3
"""Loopback AI-key recording and local Piper speech for the Muse companion."""
from __future__ import annotations

import argparse
import base64
import errno
import io
import json
import os
from pathlib import Path
import queue
import select
import signal
import struct
import subprocess
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

ORIGINS = frozenset({"http://127.0.0.1:17863", "http://localhost:17863"})
KEY_NAME = "adc-keys-ai"
KEY_CODE = 30
KEY_MAX = 767  # Linux input-event-codes.h; matches the 96-byte EVIOCGKEY bitmap.
EV_KEY = 1
EV_SYN = 0
SYN_DROPPED = 3
EVIOCGRAB = 0x40044590
# Linux's input_event uses native long timeval members (64-bit on aarch64).
INPUT_EVENT = struct.Struct("@llHHi")
SAMPLE_RATE = 16000
MAX_SECONDS = 15.0
MIN_SECONDS = 0.3
FOCUS_TTL = 8.0
DEBOUNCE = 0.08
MAX_BODY = 16384


def canonical_wav(data: bytes) -> bytes:
    """Validate the capture and emit a bounded, standard PCM WAV header."""
    with wave.open(io.BytesIO(data), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(),
                source.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
            raise ValueError("录音格式不是 16 kHz 单声道 PCM")
        frames = source.readframes(int(MAX_SECONDS * SAMPLE_RATE))
    if len(frames) < int(MIN_SECONDS * SAMPLE_RATE) * 2:
        return b""
    if len(frames) % 2:
        raise ValueError("录音数据不完整")
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(frames)
    return result.getvalue()


class Capture:
    """One explicit key press. All process/file ownership stays in its worker."""

    def __init__(self, controller: Controller, generation: int,
                 command: str = "pw-record"):
        self.controller = controller
        self.generation = generation
        self.command = command
        self.stop = threading.Event()
        self.send = False  # Accessed under the controller lock.
        self.started = controller.clock()
        self.thread = threading.Thread(target=self._run, name="ai-key-capture", daemon=True)

    def start(self) -> None:
        self.thread.start()

    @staticmethod
    def stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        # SIGINT lets pw-record / libsndfile finish the WAV header.
        for sig, timeout in ((signal.SIGINT, 1.5), (signal.SIGTERM, 0.5),
                             (signal.SIGKILL, 0.5)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                continue
        raise RuntimeError("录音进程未能退出")

    def _run(self) -> None:
        process = None
        path = None
        payload = b""
        error = None
        try:
            if not self.controller.capture_valid(self) or self.stop.is_set():
                return
            fd, path = tempfile.mkstemp(prefix="muse-ai-", suffix=".wav")
            os.fchmod(fd, 0o600)
            os.close(fd)
            process = subprocess.Popen(
                [self.command, "--rate=16000", "--channels=1", "--format=s16", path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            while not self.stop.wait(0.025):
                if not self.controller.capture_valid(self):
                    break
                if process.poll() is not None:
                    raise RuntimeError("麦克风录音提前结束，请检查音频输入")
                if self.controller.clock() - self.started >= MAX_SECONDS:
                    self.controller.limit_reached(self)
                    break
            self.stop_process(process)
            if self.send and self.controller.capture_valid(self):
                # A 15-second capture is below 0.5 MB; reject abnormal output.
                if os.path.getsize(path) > 1024 * 1024:
                    raise ValueError("录音文件超过大小限制")
                payload = canonical_wav(Path(path).read_bytes())
        except (OSError, ValueError, wave.Error, EOFError, RuntimeError) as exc:
            error = str(exc) if not isinstance(exc, OSError) else "无法启动或读取麦克风录音"
        finally:
            if process is not None:
                try:
                    self.stop_process(process)
                except (OSError, RuntimeError):
                    error = "录音进程清理失败"
            if path is not None:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    error = "录音临时文件清理失败"
            self.controller.capture_finished(self, payload, error)


class Controller:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 capture_factory: Callable = Capture):
        self.clock = clock
        self.capture_factory = capture_factory
        self.lock = threading.RLock()
        self.deadline = 0.0
        self.closed = False
        self.key_available = False
        self.key_down = False
        self.last_release = -float("inf")
        self.generation = 0
        self.capture = None
        self.captures = set()
        self.subscribers: dict[int, queue.Queue] = {}
        self.next_subscriber = 0

    def _eligible(self) -> bool:
        return not self.closed and bool(self.subscribers) and self.clock() < self.deadline

    def eligible(self) -> bool:
        with self.lock:
            return self._eligible()

    def _publish(self, event: dict, generation: int | None = None,
                 audio_only: bool = False) -> None:
        targets = list(self.subscribers.items())
        if audio_only:
            targets = targets[-1:]
        for _, subscriber in targets:
            try:
                subscriber.put_nowait((event, generation))
            except queue.Full:
                # A slow UI must not retain private audio indefinitely.
                while True:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        break
                subscriber.put_nowait(({"type": "error", "message": "界面事件接收过慢"}, None))

    def subscribe(self) -> tuple[int, queue.Queue]:
        with self.lock:
            self.next_subscriber += 1
            q: queue.Queue = queue.Queue(maxsize=16)
            self.subscribers[self.next_subscriber] = q
            return self.next_subscriber, q

    def unsubscribe(self, subscriber: int) -> None:
        with self.lock:
            self.subscribers.pop(subscriber, None)
            if not self.subscribers:
                # New EventSource connections must obtain a fresh focus lease.
                self.deadline = 0.0
                self._cancel("disconnected")

    def _cancel(self, reason: str) -> None:
        self.generation += 1
        for capture in self.captures:
            capture.send = False
            capture.stop.set()
        self.capture = None
        had_key = self.key_down
        self.key_down = False
        for subscriber in self.subscribers.values():
            while True:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    break
        if had_key:
            self._publish({"type": "key", "phase": "up", "reason": reason})

    def focus(self, active: bool) -> None:
        with self.lock:
            self.deadline = self.clock() + FOCUS_TTL if active else 0.0
            if not active:
                self._cancel("focus")

    def cancel(self) -> None:
        with self.lock:
            self._cancel("cancel")

    def tick(self) -> None:
        with self.lock:
            if self.deadline and self.clock() >= self.deadline:
                self.deadline = 0.0
                self._cancel("timeout")

    def key(self, value: int) -> None:
        """Called only for the configured EV_KEY from the dedicated grabbed device."""
        with self.lock:
            if value == 2:  # Linux autorepeat must never start or end a recording.
                return
            if not self._eligible():
                return
            if value == 1:
                if self.key_down or self.clock() - self.last_release < DEBOUNCE:
                    return
                self.key_down = True
                # A new press interrupts any previous speech or finishing capture.
                self.generation += 1
                for old in self.captures:
                    old.send = False
                    old.stop.set()
                capture = self.capture_factory(self, self.generation)
                self.capture = capture
                self.captures.add(capture)
                self._publish({"type": "key", "phase": "down"})
                capture.start()
            elif value == 0 and self.key_down:
                self.key_down = False
                self.last_release = self.clock()
                self._publish({"type": "key", "phase": "up"})
                if self.capture is not None:
                    self.capture.send = True
                    self.capture.stop.set()
                    self.capture = None

    def limit_reached(self, capture: Capture) -> None:
        with self.lock:
            if self.capture is capture and self._eligible():
                capture.send = True
                capture.stop.set()
                self.capture = None
                # key_down stays true until the physical release, preventing repeats.
                self._publish({"type": "key", "phase": "up", "reason": "limit"})

    def capture_valid(self, capture: Capture) -> bool:
        with self.lock:
            return self._eligible() and capture.generation == self.generation

    def capture_finished(self, capture: Capture, data: bytes,
                         error: str | None = None) -> None:
        with self.lock:
            self.captures.discard(capture)
            valid = self.capture_valid(capture)
            if self.capture is capture:
                self.capture = None
                if error and self.key_down:
                    self._publish({"type": "key", "phase": "up", "reason": "error"})
            if valid and error:
                self._publish({"type": "error", "message": error})
            elif valid and capture.send and data:
                self._publish({"type": "audio", "audio_wav_base64":
                               base64.b64encode(data).decode("ascii"),
                               "generation": capture.generation},
                              capture.generation, audio_only=True)

    def event_valid(self, generation: int | None) -> bool:
        with self.lock:
            return generation is None or (generation == self.generation and self._eligible())

    def snapshot(self, tts_available: bool) -> dict:
        with self.lock:
            return {"available": tts_available, "key_available": self.key_available,
                    "recording": self.capture is not None}

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.deadline = 0.0
            self._cancel("shutdown")
            captures = list(self.captures)
        for capture in captures:
            capture.thread.join(timeout=4.0)


def discover_key(sysfs: Path = Path("/sys/class/input"),
                 dev: Path = Path("/dev/input"), *, key_name: str = KEY_NAME) -> Path | None:
    """Fail closed on ambiguous matches; never open a keyboard by its event number."""
    matches = []
    for event in sorted(sysfs.glob("event*")):
        try:
            if (event / "device/name").read_text().rstrip("\n") == key_name:
                matches.append(dev / event.name)
        except (OSError, UnicodeError):
            continue
    return matches[0] if len(matches) == 1 else None


class KeyReader:
    def __init__(self, controller: Controller, *, key_name: str = KEY_NAME,
                 key_code: int = KEY_CODE):
        validate_key_config(key_name, key_code)
        self.controller = controller
        self.key_name = key_name
        self.key_code = key_code
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="ai-key-reader", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _events(self, data: bytes, waiting_release: bool) -> bool:
        for _, _, event_type, code, value in INPUT_EVENT.iter_unpack(data):
            if event_type == EV_SYN and code == SYN_DROPPED:
                self.controller.cancel()
                waiting_release = True
            elif event_type == EV_KEY and code == self.key_code:
                if waiting_release:
                    if value == 0:
                        waiting_release = False
                else:
                    self.controller.key(value)
        return waiting_release

    def _run(self) -> None:
        try:
            import fcntl
        except ImportError:
            # Host-side unit tests run on Windows; actual input support is Linux only.
            while not self.stop.wait(0.05):
                self.controller.tick()
            return
        fd = None
        path = None
        next_scan = 0.0
        waiting_release = False
        try:
            while not self.stop.is_set():
                self.controller.tick()
                if not self.controller.eligible() and fd is not None:
                    try:
                        fcntl.ioctl(fd, EVIOCGRAB, 0)
                    except OSError:
                        pass  # Hot unplug may race a focus loss; close still releases.
                    finally:
                        os.close(fd)
                    fd = None
                now = time.monotonic()
                if fd is None and now >= next_scan:
                    path = discover_key(key_name=self.key_name)
                    with self.controller.lock:
                        self.controller.key_available = path is not None and os.access(path, os.R_OK)
                    next_scan = now + 1.0
                if fd is None and path is not None and self.controller.eligible():
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                        fcntl.ioctl(fd, EVIOCGRAB, 1)
                        # Discard pre-focus queued events, then inspect the current key state.
                        while True:
                            try:
                                if not os.read(fd, INPUT_EVENT.size * 64):
                                    raise OSError(errno.ENODEV, "input disappeared")
                            except BlockingIOError:
                                break
                        keys = bytearray(96)
                        request = 0x80004518 | (len(keys) << 16)  # EVIOCGKEY(len)
                        fcntl.ioctl(fd, request, keys, True)
                        waiting_release = bool(keys[self.key_code // 8] & (1 << (self.key_code % 8)))
                    except OSError:
                        if fd is not None:
                            os.close(fd)  # Closing also releases any successful grab.
                            fd = None
                        with self.controller.lock:
                            self.controller.key_available = False
                        path = None
                        next_scan = now + 1.0
                if fd is None:
                    self.stop.wait(0.05)
                    continue
                try:
                    if not select.select([fd], [], [], 0.05)[0]:
                        continue
                    data = os.read(fd, INPUT_EVENT.size * 64)
                    if not data or len(data) % INPUT_EVENT.size:
                        raise OSError(errno.ENODEV, "input unavailable")
                    # A dropped stream cancels capture; a held key needs a fresh release.
                    waiting_release = self._events(data, waiting_release)
                except OSError:
                    self.controller.cancel()
                    os.close(fd)
                    fd = None
                    path = None
                    with self.controller.lock:
                        self.controller.key_available = False
        finally:
            if fd is not None:
                try:
                    fcntl.ioctl(fd, EVIOCGRAB, 0)
                except OSError:
                    pass
                finally:
                    os.close(fd)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2.0)


class Cancelled(Exception):
    pass


class PiperEngine:
    def __init__(self, model: str | None):
        self.model = model
        self.lock = threading.Lock()
        self.voice = None
        self.attempted = False

    @property
    def available(self) -> bool:
        return self.voice is not None

    def _load(self) -> None:
        if self.attempted:
            if self.voice is None:
                raise RuntimeError("本地语音模型未就绪")
            return
        self.attempted = True
        if not self.model or not Path(self.model).is_file() or not Path(self.model + ".json").is_file():
            raise RuntimeError("本地语音模型未就绪")
        from piper import PiperVoice
        self.voice = PiperVoice.load(self.model, use_cuda=False)

    def warmup(self) -> None:
        try:
            with self.lock:
                self._load()
        except Exception:
            # Status remains false; no prompts, private text, or paths enter logs.
            pass

    def synthesize(self, text: str, cancelled: Callable[[], bool]) -> bytes:
        with self.lock:
            if cancelled():
                raise Cancelled()
            self._load()
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                self.voice.synthesize_wav(text, wav)
            if cancelled():
                raise Cancelled()
            return output.getvalue()


class CompanionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: Controller,
                 engine: PiperEngine):
        if address[0] != "127.0.0.1":
            raise ValueError("Only 127.0.0.1 is allowed")
        self.controller = controller
        self.engine = engine
        self.tts_slots = threading.BoundedSemaphore(4)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(3.0)

    def log_message(self, format: str, *args) -> None:
        pass

    def _authorized(self) -> bool:
        origins = self.headers.get_all("Origin", [])
        port = self.server.server_address[1]
        hosts = self.headers.get_all("Host", [])
        return len(origins) == 1 and origins[0] in ORIGINS and len(hosts) == 1 and hosts[0] in {
            f"127.0.0.1:{port}", f"localhost:{port}"}

    def _headers(self, status: int, content_type: str, length: int | None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._authorized():
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.close_connection = True

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (OSError, TimeoutError):
            pass

    def _check(self) -> bool:
        if not self._authorized():
            self._json(403, {"error": "Origin or Host rejected"})
            return False
        return True

    def do_OPTIONS(self) -> None:
        if not self._check():
            return
        methods = {"/events": "GET", "/status": "GET", "/focus": "POST",
                   "/tts": "POST", "/cancel": "POST"}
        method = methods.get(self.path)
        requested = self.headers.get("Access-Control-Request-Method")
        headers = {part.strip().lower() for part in self.headers.get(
            "Access-Control-Request-Headers", "").split(",") if part.strip()}
        if method is None or requested != method or not headers.issubset({"content-type"}):
            self._json(403, {"error": "Preflight rejected"})
            return
        self._headers(204, "application/json", 0)
        self.send_header("Access-Control-Allow-Methods", method)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._check():
            return
        if self.path == "/status":
            self._json(200, self.server.controller.snapshot(self.server.engine.available))
        elif self.path == "/events":
            self._events()
        else:
            self._json(404, {"error": "Not found"})

    def _events(self) -> None:
        controller = self.server.controller
        subscriber, events = controller.subscribe()
        try:
            self._headers(200, "text/event-stream; charset=utf-8", None)
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.connection.settimeout(1.0)
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not controller.closed:
                try:
                    event, generation = events.get(timeout=1.0)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                else:
                    if not controller.event_valid(generation):
                        continue
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                self.wfile.flush()
        except (OSError, TimeoutError):
            pass
        finally:
            controller.unsubscribe(subscriber)

    def _body(self) -> dict | None:
        if self.headers.get("Transfer-Encoding") is not None:
            self._json(400, {"error": "Transfer-Encoding not supported"})
            return None
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Expected application/json"})
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            self._json(411, {"error": "One Content-Length is required"})
            return None
        try:
            length = int(lengths[0])
            if length < 2 or length > MAX_BODY:
                self._json(413, {"error": "Request body too large or empty"})
                return None
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("short body")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("object required")
            return body
        except (ValueError, UnicodeError, OSError, RecursionError):
            self._json(400, {"error": "Invalid JSON body"})
            return None

    def do_POST(self) -> None:
        if not self._check():
            return
        if self.path not in {"/focus", "/cancel", "/tts"}:
            self._json(404, {"error": "Not found"})
            return
        body = self._body()
        if body is None:
            return
        controller = self.server.controller
        if self.path == "/focus":
            if set(body) != {"active"} or type(body["active"]) is not bool:
                self._json(400, {"error": "Expected {active: boolean}"})
                return
            controller.focus(body["active"])
            self._json(200, {"ok": True})
        elif self.path == "/cancel":
            if body:
                self._json(400, {"error": "Expected an empty object"})
                return
            controller.cancel()
            self._json(200, {"ok": True})
        else:
            text = body.get("text")
            if set(body) != {"text"} or not isinstance(text, str) or not text.strip() or len(text) > 2000:
                self._json(400, {"error": "Expected text of 1 to 2000 characters"})
                return
            if not self.server.tts_slots.acquire(blocking=False):
                self._json(429, {"error": "Speech queue is full"})
                return
            with controller.lock:
                generation = controller.generation
            def cancelled() -> bool:
                with controller.lock:
                    return controller.closed or controller.generation != generation
            try:
                data = self.server.engine.synthesize(text, cancelled)
                if cancelled():
                    raise Cancelled()
                self._headers(200, "audio/wav", len(data))
                self.end_headers()
                self.wfile.write(data)
            except Cancelled:
                self._json(409, {"error": "Speech cancelled"})
            except (OSError, TimeoutError):
                pass
            except Exception:
                self._json(503, {"error": "本地语音合成暂不可用"})
            finally:
                self.server.tts_slots.release()


def validate_key_config(name: str, code: int) -> None:
    if not isinstance(name, str) or not name.strip() or len(name) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("key name must be a non-empty exact device name without control characters")
    if type(code) is not int or not 1 <= code <= KEY_MAX:
        raise ValueError(f"key code must be an integer from 1 to {KEY_MAX}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Piper .onnx model, with adjacent .onnx.json")
    parser.add_argument("--port", type=int, default=17864)
    parser.add_argument("--key-name", default=KEY_NAME, help="exact sysfs name of one dedicated input device")
    parser.add_argument("--key-code", type=int, default=KEY_CODE, help="Linux EV_KEY code on that device (1..767)")
    args = parser.parse_args(argv)
    try:
        validate_key_config(args.key_name, args.key_code)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> None:
    args = parse_args()
    controller = Controller()
    engine = PiperEngine(args.model)
    server = CompanionServer(("127.0.0.1", args.port), controller, engine)
    reader = KeyReader(controller, key_name=args.key_name, key_code=args.key_code)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    warmup = threading.Thread(target=engine.warmup, name="piper-load", daemon=True)
    http = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                            name="companion-http", daemon=True)
    warmup.start()
    reader.start()
    http.start()
    try:
        stopping.wait()
    finally:
        controller.close()
        reader.close()
        server.shutdown()
        server.server_close()
        http.join(timeout=2.0)
        warmup.join(timeout=2.0)


if __name__ == "__main__":
    main()
