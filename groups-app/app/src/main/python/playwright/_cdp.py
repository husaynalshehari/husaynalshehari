"""The DevTools protocol over a WebSocket, with the standard library alone.

The browser is either the app's own WebView on Android (its DevTools socket, which Android lets the app itself
reach) or, for tests on a computer, a Chromium started with a debugging port. Both speak the same protocol."""
from __future__ import annotations

import base64
import json
import os
import queue
import socket
import struct
import threading
import time

CLOSED_MESSAGE = "Target page, context or browser has been closed"


class ProtocolError(Exception):
    """The browser answered a command with an error."""


class ConnectionClosed(Exception):
    """The browser's side of the connection is gone."""


class TcpEndpoint:
    """A debugging port on this machine (tests)."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.host, self.port = host, port

    def connect(self):
        return socket.create_connection((self.host, self.port), timeout=15)

    def __repr__(self):
        return f"tcp:{self.host}:{self.port}"


class AbstractEndpoint:
    """A Unix socket in Linux's abstract namespace: where Android's WebView listens for DevTools
    (webview_devtools_remote_<pid>), for adb and for the app's own process."""

    def __init__(self, name: str):
        self.name = name

    def connect(self):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(15)
            sock.connect("\0" + self.name)
            return sock
        except (OSError, AttributeError, ValueError):
            return JavaLocalSocket(self.name)          # (the same socket, opened through Android's own API)

    def __repr__(self):
        return f"abstract:{self.name}"


class JavaLocalSocket:
    """android.net.LocalSocket with a Python socket's few methods, for where Python cannot open the abstract
    socket itself."""

    def __init__(self, name: str):
        from java import jarray, jbyte, jclass
        self._jarray, self._jbyte = jarray, jbyte
        LocalSocket = jclass("android.net.LocalSocket")
        LocalSocketAddress = jclass("android.net.LocalSocketAddress")
        self._socket = LocalSocket()
        self._socket.connect(LocalSocketAddress(name, LocalSocketAddress.Namespace.ABSTRACT))
        self._in = self._socket.getInputStream()
        self._out = self._socket.getOutputStream()

    def settimeout(self, seconds):
        try:
            self._socket.setSoTimeout(int((seconds or 0) * 1000))
        except Exception:
            pass

    def sendall(self, data: bytes) -> None:
        self._out.write(self._jarray(self._jbyte)(bytes(data)))
        self._out.flush()

    def recv(self, size: int) -> bytes:
        buffer = self._jarray(self._jbyte)(bytes(min(size, 65536)))
        count = self._in.read(buffer)
        if count is None or count <= 0:
            return b""
        try:
            return bytes(buffer)[:count]
        except (TypeError, ValueError):
            # (Java's bytes are signed: -128..127)
            return bytes(int(buffer[index]) & 0xFF for index in range(count))

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            pass


def http_get(endpoint, path: str, timeout: float = 10.0) -> bytes:
    """One GET to the browser's DevTools HTTP side (/json/list, /json/version): the answer's body."""
    sock = endpoint.connect()
    try:
        sock.settimeout(timeout)
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode("ascii"))
        data = b""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            head, sep, body = data.partition(b"\r\n\r\n")
            if sep:
                length = None
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip() or 0)
                if length is not None and len(body) >= length:
                    return body[:length]
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        head, sep, body = data.partition(b"\r\n\r\n")
        if not sep or b" 200" not in head.split(b"\r\n")[0]:
            raise ConnectionClosed(f"no answer from the browser's DevTools at {endpoint!r} for {path}")
        return body
    finally:
        try:
            sock.close()
        except Exception:
            pass


def list_targets(endpoint, timeout: float = 10.0) -> list:
    data = json.loads(http_get(endpoint, "/json/list", timeout).decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


class WebSocket:
    """A client WebSocket: text messages out and in, enough for DevTools."""

    def __init__(self, endpoint, path: str):
        self._sock = endpoint.connect()
        self._sock.settimeout(20)
        self._send_lock = threading.Lock()
        self._pending = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self._sock.sendall((f"GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionClosed("the browser closed the connection while it was being opened")
            head += chunk
            if len(head) > 65536:
                raise ConnectionClosed("not a WebSocket answer")
        first, _, rest = head.partition(b"\r\n\r\n")
        if b" 101" not in first.split(b"\r\n")[0]:
            raise ConnectionClosed("the browser refused the DevTools connection: " + first.split(b"\r\n")[0].decode("latin-1")[:120])
        self._pending = rest
        self._sock.settimeout(None)

    def _read(self, size: int) -> bytes:
        parts = []
        if self._pending:
            parts.append(self._pending[:size])
            self._pending = self._pending[size:]
            size -= len(parts[0])
        while size > 0:
            chunk = self._sock.recv(min(size, 1 << 20))
            if not chunk:
                raise ConnectionClosed("the browser closed the connection")
            parts.append(chunk)
            size -= len(chunk)
        return b"".join(parts)

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        size = len(payload)
        # (A client's frames carry a mask; one of zeros leaves the bytes as they are, on this machine's own socket.)
        if size < 126:
            head = struct.pack("!BB", 0x81, 0x80 | size)
        elif size < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, size)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, size)
        with self._send_lock:
            self._sock.sendall(head + b"\x00\x00\x00\x00" + payload)

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        with self._send_lock:
            self._sock.sendall(struct.pack("!BB", 0x80 | opcode, 0x80 | len(payload)) + b"\x00\x00\x00\x00" + payload)

    def receive(self) -> str:
        """The next whole text message (pings answered, pieces joined)."""
        pieces = []
        while True:
            first, second = self._read(2)
            opcode, size = first & 0x0F, second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else b""
            payload = self._read(size) if size else b""
            if mask and payload:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionClosed("the browser closed the connection")
            if opcode == 0x9:
                self._send_control(0xA, payload[:125])
                continue
            if opcode == 0xA:
                continue
            pieces.append(payload)
            if first & 0x80:
                return b"".join(pieces).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_control(0x8)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


class Session:
    """One DevTools connection to one page: commands with their answers, and the page's events.

    Events go first to the quick listeners (in the reading thread: they only note things down), then to the
    others in a thread of their own, where a listener may itself send commands."""

    def __init__(self, endpoint, path: str, name: str = "page"):
        self._socket = WebSocket(endpoint, path)
        self._ids = 0
        self._lock = threading.Lock()
        self._waiting: dict = {}
        self._quick: dict = {}
        self._slow: dict = {}
        self._events: queue.Queue = queue.Queue()
        self.closed = False
        self.close_reason = ""
        threading.Thread(target=self._read_loop, name=f"devtools-{name}", daemon=True).start()
        threading.Thread(target=self._event_loop, name=f"devtools-{name}-events", daemon=True).start()

    def on(self, event: str, listener, quick: bool = False) -> None:
        (self._quick if quick else self._slow).setdefault(event, []).append(listener)

    def off(self, event: str, listener) -> None:
        for table in (self._quick, self._slow):
            if listener in table.get(event, []):
                table[event].remove(listener)

    def send(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        if self.closed:
            raise ConnectionClosed(CLOSED_MESSAGE)
        slot = {"done": threading.Event(), "result": None, "error": None}
        with self._lock:
            self._ids += 1
            number = self._ids
            self._waiting[number] = slot
        try:
            self._socket.send(json.dumps({"id": number, "method": method, "params": params or {}}))
        except (OSError, ConnectionClosed, Exception) as exc:
            with self._lock:
                self._waiting.pop(number, None)
            self._fail(str(exc))
            raise ConnectionClosed(CLOSED_MESSAGE) from None
        if not slot["done"].wait(timeout):
            with self._lock:
                self._waiting.pop(number, None)
            if self.closed:
                raise ConnectionClosed(CLOSED_MESSAGE)
            raise TimeoutError(f"{method}: no answer from the browser in {timeout:g} s")
        if slot["error"] is not None:
            if slot["error"] == CLOSED_MESSAGE:
                raise ConnectionClosed(CLOSED_MESSAGE)
            raise ProtocolError(slot["error"])
        return slot["result"] or {}

    def _fail(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        with self._lock:
            waiting, self._waiting = self._waiting, {}
        for slot in waiting.values():
            slot["error"] = CLOSED_MESSAGE
            slot["done"].set()
        self._events.put(None)

    def _read_loop(self) -> None:
        while not self.closed:
            try:
                message = json.loads(self._socket.receive())
            except (ConnectionClosed, OSError) as exc:
                self._fail(str(exc))
                return
            except ValueError:
                continue
            except Exception as exc:           # (anything else from the socket's side ends it the same way)
                self._fail(f"{type(exc).__name__}: {exc}")
                return
            if "id" in message:
                with self._lock:
                    slot = self._waiting.pop(message["id"], None)
                if slot is not None:
                    if "error" in message:
                        error = message["error"] or {}
                        slot["error"] = str(error.get("message") or "error") + (f" ({error.get('data')})" if error.get("data") else "")
                    else:
                        slot["result"] = message.get("result") or {}
                    slot["done"].set()
                continue
            method = message.get("method")
            if not method:
                continue
            params = message.get("params") or {}
            for listener in list(self._quick.get(method, ())):
                try:
                    listener(params)
                except Exception:
                    pass
            if self._slow.get(method):
                self._events.put((method, params))

    def _event_loop(self) -> None:
        while True:
            item = self._events.get()
            if item is None:
                return
            method, params = item
            for listener in list(self._slow.get(method, ())):
                try:
                    listener(params)
                except Exception:
                    pass

    def close(self) -> None:
        self._fail("closed")
        self._socket.close()
