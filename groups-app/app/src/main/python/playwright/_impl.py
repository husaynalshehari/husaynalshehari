"""Playwright's page, locator, mouse, keyboard and context, as far as the groups script uses them, over the
DevTools protocol. Each call is one or a few protocol commands, as in Playwright itself."""
from __future__ import annotations

import base64
import builtins
import fnmatch
import inspect
import json
import math
import re
import threading
import time

from . import _cdp
from ._script import LOCATOR_JS, MODIFIER_BITS, NAMED_KEYS

CLOSED_MESSAGE = _cdp.CLOSED_MESSAGE
_cdp_timeout = builtins.TimeoutError          # (a command the browser never answered; not Playwright's TimeoutError below)
DEFAULT_TIMEOUT_MS = 30_000
RESOURCE_TYPES = {
    "Document": "document", "Stylesheet": "stylesheet", "Image": "image", "Media": "media", "Font": "font",
    "Script": "script", "TextTrack": "texttrack", "XHR": "xhr", "Fetch": "fetch", "Prefetch": "fetch",
    "EventSource": "eventsource", "WebSocket": "websocket", "Manifest": "manifest", "Ping": "ping",
    "SignedExchange": "other", "CSPViolationReport": "cspreport", "Preflight": "other", "Other": "other",
}
LIFECYCLE_NAMES = {"commit": "commit", "domcontentloaded": "DOMContentLoaded", "load": "load", "networkidle": "networkIdle"}


class Error(Exception):
    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message
        self.name = type(self).__name__
        self.stack = message


class TimeoutError(Error):
    pass


# In the page: a value as text. What JSON leaves out is kept: undefined (None here, and a key holding it stays),
# and the numbers JSON has no word for.
PACK_JS = ("((__v) => { if (__v === undefined) return \"u\"; const __t = JSON.stringify(__v, (key, value) => "
           "value === undefined ? null : (typeof value === \"number\" && !Number.isFinite(value)) ? {__gg_number: String(value)} : "
           "(typeof value === \"bigint\") ? {__gg_number: value.toString()} : value); return __t === undefined ? \"u\" : \"j\" + __t; })")


def _unpack(row: dict):
    if len(row) == 1 and "__gg_number" in row:
        raw = str(row["__gg_number"])
        if raw == "NaN":
            return math.nan
        if raw in ("Infinity", "-Infinity"):
            return math.inf if raw == "Infinity" else -math.inf
        try:
            return int(raw)
        except ValueError:
            return None
    return row


def _ms(timeout, fallback: float) -> float:
    """A timeout as given to Playwright (milliseconds; 0 means none), in milliseconds."""
    if timeout is None:
        return float(fallback)
    return float(timeout) if timeout > 0 else 24 * 3600 * 1000.0


def _glob_matches(pattern, url: str) -> bool:
    if callable(pattern):
        return bool(pattern(url))
    if hasattr(pattern, "search"):
        return bool(pattern.search(url))
    pattern = str(pattern)
    if pattern in ("**/*", "**", "*"):
        return True
    return fnmatch.fnmatchcase(url, pattern.replace("**", "*"))


class Request:
    def __init__(self, page, request_id: str, url: str, method: str = "GET", resource_type: str = "other", headers: dict | None = None):
        self._page = page
        self._id = request_id
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._response = None
        self.failure = None

    def response(self):
        return self._response

    def is_navigation_request(self) -> bool:
        return self.resource_type == "document"


class Response:
    def __init__(self, page, request: Request, data: dict):
        self._page = page
        self.request = request
        self.url = str(data.get("url") or request.url)
        self.status = int(data.get("status") or 0)
        self.status_text = str(data.get("statusText") or "")
        self.headers = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
        self.ok = self.status == 0 or 200 <= self.status <= 299

    def body(self) -> bytes:
        answer = self._page._call("Network.getResponseBody", {"requestId": self.request._id}, 30)
        body = answer.get("body") or ""
        return base64.b64decode(body) if answer.get("base64Encoded") else body.encode("utf-8")

    def text(self) -> str:
        return self.body().decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text())


class Route:
    def __init__(self, request: Request):
        self.request = request
        self._decision = ""

    def abort(self, error_code: str | None = None) -> None:
        self._decision = self._decision or "abort"

    def continue_(self, **_changes) -> None:
        self._decision = self._decision or "continue"

    def fallback(self, **_changes) -> None:
        self._decision = self._decision or "fallback"

    def fulfill(self, **_answer) -> None:
        raise Error("route.fulfill is not available in this browser")


class CDPSession:
    """context.new_cdp_session(page): raw protocol commands for that page."""

    def __init__(self, page):
        self._page = page
        self._network = False

    def send(self, method: str, params: dict | None = None):
        if method == "Network.enable":
            self._network = True
        return self._page._call(method, params or {}, 30)

    def detach(self) -> None:
        if self._network:
            self._network = False
            self._page._network_released()


class Mouse:
    def __init__(self, page):
        self._page = page
        self._x = 0.0
        self._y = 0.0
        self._buttons = 0
        self._button = "none"

    def _event(self, kind: str, **extra) -> None:
        params = {"type": kind, "x": self._x, "y": self._y, "modifiers": self._page.keyboard._modifiers(),
                  "button": self._button, "buttons": self._buttons}
        params.update(extra)
        self._page._call("Input.dispatchMouseEvent", params, 20)

    def move(self, x: float, y: float, steps: int = 1) -> None:
        from_x, from_y = self._x, self._y
        steps = max(1, int(steps or 1))
        for step in range(1, steps + 1):
            self._x = from_x + (float(x) - from_x) * step / steps
            self._y = from_y + (float(y) - from_y) * step / steps
            self._event("mouseMoved")

    def down(self, button: str = "left", click_count: int = 1) -> None:
        self._button = button
        self._buttons |= {"left": 1, "right": 2, "middle": 4}.get(button, 1)
        self._event("mousePressed", clickCount=click_count)

    def up(self, button: str = "left", click_count: int = 1) -> None:
        self._buttons &= ~{"left": 1, "right": 2, "middle": 4}.get(button, 1)
        self._button = button
        self._event("mouseReleased", clickCount=click_count)
        self._button = "none"

    def click(self, x: float, y: float, delay: float = 0, button: str = "left", click_count: int = 1) -> None:
        self.move(x, y)
        self.down(button, click_count)
        if delay:
            time.sleep(delay / 1000)
        self.up(button, click_count)

    def wheel(self, delta_x: float, delta_y: float) -> None:
        self._event("mouseWheel", deltaX=float(delta_x), deltaY=float(delta_y))


class Keyboard:
    def __init__(self, page):
        self._page = page
        self._held: set = set()

    def _modifiers(self) -> int:
        return sum(bit for name, bit in MODIFIER_BITS.items() if name in self._held)

    def _describe(self, key: str) -> dict:
        if key not in NAMED_KEYS:
            if len(key) == 1:
                return {"key": key, "code": "", "keyCode": 0, "text": key}
            raise Error(f'Unknown key: "{key}"')
        code, key_code, text = NAMED_KEYS[key]
        name = " " if key == "Space" else key
        # With Control, Alt or Meta held a key types nothing: it is a shortcut.
        if self._held - {"Shift"}:
            text = ""
        return {"key": name, "code": code, "keyCode": key_code, "text": text}

    def down(self, key: str) -> None:
        described = self._describe(key)
        if key in MODIFIER_BITS:
            self._held.add(key)
        params = {"type": "keyDown" if described["text"] else "rawKeyDown", "modifiers": self._modifiers(),
                  "windowsVirtualKeyCode": described["keyCode"], "code": described["code"], "key": described["key"],
                  "location": 1 if key in MODIFIER_BITS else 0}
        if described["text"]:
            params["text"] = params["unmodifiedText"] = described["text"]
        self._page._call("Input.dispatchKeyEvent", params, 20)

    def up(self, key: str) -> None:
        described = self._describe(key)
        if key in MODIFIER_BITS:
            self._held.discard(key)
        self._page._call("Input.dispatchKeyEvent", {
            "type": "keyUp", "modifiers": self._modifiers(), "windowsVirtualKeyCode": described["keyCode"],
            "code": described["code"], "key": described["key"], "location": 1 if key in MODIFIER_BITS else 0}, 20)

    def press(self, key: str, delay: float = 0) -> None:
        """One key, or a shortcut like "Control+A"."""
        parts = key.split("+") if len(key) > 1 and "+" in key[:-1] else [key]
        if key.endswith("++"):
            parts = key[:-2].split("+") + ["+"]
        for part in parts[:-1]:
            self.down(part)
        try:
            self.down(parts[-1])
            if delay:
                time.sleep(delay / 1000)
            self.up(parts[-1])
        finally:
            for part in reversed(parts[:-1]):
                self.up(part)

    def insert_text(self, text: str) -> None:
        self._page._call("Input.insertText", {"text": text}, 20)

    def type(self, text: str, delay: float = 0) -> None:
        for char in text:
            if char == "\n":
                self.press("Enter", delay)
            elif char in NAMED_KEYS and len(char) == 1:
                self.press(char, delay)
            else:
                if delay:
                    time.sleep(delay / 1000)
                self.insert_text(char)


class Locator:
    def __init__(self, page, steps: list, description: str):
        self.page = page
        self._steps = steps
        self._description = description

    def __repr__(self) -> str:
        return self._description

    def _with(self, step: list, suffix: str) -> "Locator":
        return Locator(self.page, self._steps + [step], self._description + suffix)

    @property
    def first(self) -> "Locator":
        return self._with(["nth", 0], ".first")

    @property
    def last(self) -> "Locator":
        return self._with(["nth", -1], ".last")

    def nth(self, index: int) -> "Locator":
        return self._with(["nth", int(index)], f".nth({int(index)})")

    def locator(self, selector, has=None, has_text=None) -> "Locator":
        if isinstance(selector, Locator):
            raise Error("a locator inside a locator is not available in this browser")
        found = self._with(["css", str(selector)], f".locator({str(selector)!r})")
        return found.filter(has=has, has_text=has_text) if (has is not None or has_text is not None) else found

    def filter(self, has=None, has_text=None, **_others) -> "Locator":
        found = self
        if has is not None:
            found = found._with(["has", list(has._steps)], f".filter(has={has!r})")
        if has_text is not None:
            found = found._with(["hasText", str(has_text)], f".filter(has_text={has_text!r})")
        return found

    def get_by_role(self, role: str, name=None, exact: bool = False, **_others) -> "Locator":
        return self._with(_role_step(role, name, exact), f".get_by_role({role!r})")

    def _op(self, op: str, arg=None):
        return self.page._evaluate_function(LOCATOR_JS, [self._steps, op, arg])

    def count(self) -> int:
        return int(self._op("count") or 0)

    def is_visible(self, timeout=None) -> bool:
        state = self._op("state") or {}
        return bool(state.get("visible"))

    def is_enabled(self, timeout=None) -> bool:
        return bool((self._op("state") or {}).get("enabled"))

    def _wait(self, wanted: str, timeout, action: str) -> dict:
        """Wait until the element is in the wanted state; the state as last read."""
        limit = _ms(timeout, self.page._timeout)
        deadline = time.monotonic() + limit / 1000
        while True:
            state = self._op("state") or {}
            if wanted == "attached" and state.get("attached"):
                return state
            if wanted == "visible" and state.get("visible"):
                return state
            if wanted == "hidden" and not state.get("visible"):
                return state
            if wanted == "detached" and not state.get("attached"):
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{action}: Timeout {limit:g}ms exceeded.\nCall log:\n  - waiting for {self!r} to be {wanted}")
            time.sleep(0.05)

    def wait_for(self, state: str = "visible", timeout=None) -> None:
        self._wait(state, timeout, "Locator.wait_for")

    def scroll_into_view_if_needed(self, timeout=None) -> None:
        self._wait("attached", timeout, "Locator.scroll_into_view_if_needed")
        self._op("scroll")

    def bounding_box(self, timeout=None):
        state = self._wait("attached", timeout, "Locator.bounding_box")
        box = state.get("box")
        if not state.get("visible") or not box:
            return None
        return {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}

    def inner_text(self, timeout=None) -> str:
        self._wait("attached", timeout, "Locator.inner_text")
        found = self._op("text")
        return str((found or {}).get("text") or "")

    def text_content(self, timeout=None):
        return self.inner_text(timeout)

    def focus(self, timeout=None) -> None:
        self._wait("attached", timeout, "Locator.focus")
        self._op("focus")

    def click(self, timeout=None, delay: float = 0, button: str = "left", click_count: int = 1, **_others) -> None:
        """Wait until the element shows, is still, and nothing covers its middle; then a real mouse click there."""
        limit = _ms(timeout, self.page._timeout)
        deadline = time.monotonic() + limit / 1000
        before = None
        why = "not attached"
        while True:
            point = self._op("point", {"scroll": True}) or {}
            if point.get("ready"):
                spot = (float(point["x"]), float(point["y"]))
                if before is not None and abs(before[0] - spot[0]) < 1 and abs(before[1] - spot[1]) < 1:
                    break
                before = spot
                time.sleep(0.05)
                continue
            before = None
            why = str(point.get("why") or why)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Locator.click: Timeout {limit:g}ms exceeded.\nCall log:\n  - waiting for {self!r}\n  - {why}")
            time.sleep(0.1)
        self.page.mouse.click(before[0], before[1], delay=delay, button=button, click_count=click_count)

    def screenshot(self, type: str = "png", quality=None, timeout=None, **_others) -> bytes:
        state = self._wait("visible", timeout, "Locator.screenshot")
        self._op("scroll")
        time.sleep(0.05)
        state = self._op("state") or state
        box = state.get("box")
        if not box or box["width"] < 1 or box["height"] < 1:
            raise Error("Locator.screenshot: the element has no size")
        return self.page._capture(type, quality, box, state)


def _role_step(role: str, name, exact: bool) -> list:
    if name is None:
        return ["role", role, None]
    if hasattr(name, "pattern"):
        flags = ("i" if name.flags & re.I else "") + ("m" if name.flags & re.M else "") + ("s" if name.flags & re.S else "")
        return ["role", role, {"regex": name.pattern, "flags": flags}]
    return ["role", role, {"text": str(name), "exact": bool(exact)}]


class Page:
    def __init__(self, context, session):
        self.context = context
        self._session = session
        self._backend = context._backend
        self._timeout = float(DEFAULT_TIMEOUT_MS)
        self._navigation_timeout = float(DEFAULT_TIMEOUT_MS)
        self._viewport = None
        self._closed = False
        self._crashed = False
        self._url = "about:blank"
        self._frame_id = ""
        self._loader_id = ""
        self._lifecycle: dict = {}
        self._commits = 0
        self._commit_of: dict = {}
        self._same_document = 0
        self._download = False
        self._changed = threading.Condition()
        self._listeners: dict = {}
        self._requests: dict = {}
        self._network_users = 0
        self._network_on = False
        self.mouse = Mouse(self)
        self.keyboard = Keyboard(self)
        session.on("Page.frameNavigated", self._on_frame_navigated, quick=True)
        session.on("Page.navigatedWithinDocument", self._on_same_document, quick=True)
        session.on("Page.lifecycleEvent", self._on_lifecycle, quick=True)
        session.on("Page.downloadWillBegin", self._on_download, quick=True)
        session.on("Inspector.targetCrashed", self._on_crash, quick=True)
        session.on("Page.javascriptDialogOpening", self._on_dialog)
        session.on("Network.requestWillBeSent", self._on_request, quick=True)
        session.on("Network.responseReceived", self._on_response, quick=True)
        session.on("Network.loadingFinished", self._on_finished)
        session.on("Network.loadingFailed", self._on_failed, quick=True)
        self._call("Page.enable", {}, 20)
        self._call("Page.setLifecycleEventsEnabled", {"enabled": True}, 20)
        tree = self._call("Page.getFrameTree", {}, 20).get("frameTree") or {}
        frame = tree.get("frame") or {}
        self._frame_id = str(frame.get("id") or "")
        self._loader_id = str(frame.get("loaderId") or "")
        self._url = str(frame.get("url") or "about:blank") + str(frame.get("urlFragment") or "")

    # ------------------------------------------------------------ the protocol

    def _call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        if self._closed:
            raise Error(CLOSED_MESSAGE)
        if self._crashed:
            raise Error(CLOSED_MESSAGE + " (the page crashed)")
        self._backend.wait_ready()
        try:
            return self._session.send(method, params, timeout)
        except _cdp.ConnectionClosed:
            raise Error(CLOSED_MESSAGE) from None
        except _cdp.ProtocolError as exc:
            text = str(exc)
            if "context was destroyed" in text or "Cannot find context" in text or "navigated or closed" in text:
                raise Error("Execution context was destroyed, most likely because of a navigation") from None
            if "Target closed" in text or "Session closed" in text or "No target with given id" in text:
                raise Error(CLOSED_MESSAGE) from None
            raise Error(f"{method}: {text}") from None
        except _cdp_timeout as exc:
            raise Error(str(exc)) from None

    def _on_frame_navigated(self, params: dict) -> None:
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return
        with self._changed:
            self._frame_id = str(frame.get("id") or self._frame_id)
            self._loader_id = str(frame.get("loaderId") or "")
            self._url = str(frame.get("url") or "") + str(frame.get("urlFragment") or "")
            self._lifecycle.setdefault(self._loader_id, set()).add("commit")
            if len(self._lifecycle) > 40:
                for old in list(self._lifecycle)[:-20]:
                    self._lifecycle.pop(old, None)
                    self._commit_of.pop(old, None)
            self._commits += 1
            self._commit_of[self._loader_id] = self._commits
            self._changed.notify_all()

    def _on_same_document(self, params: dict) -> None:
        if self._frame_id and params.get("frameId") != self._frame_id:
            return
        with self._changed:
            self._url = str(params.get("url") or self._url)
            self._same_document += 1
            self._changed.notify_all()

    def _on_lifecycle(self, params: dict) -> None:
        if self._frame_id and params.get("frameId") != self._frame_id:
            return
        with self._changed:
            self._lifecycle.setdefault(str(params.get("loaderId") or ""), set()).add(str(params.get("name") or ""))
            self._changed.notify_all()

    def _on_download(self, _params: dict) -> None:
        self._download = True

    def _on_crash(self, _params: dict) -> None:
        self._crashed = True
        with self._changed:
            self._changed.notify_all()

    def _on_dialog(self, _params: dict) -> None:
        try:
            self._session.send("Page.handleJavaScriptDialog", {"accept": False}, 10)
        except Exception:
            pass

    # ------------------------------------------------------------ going to pages

    @property
    def url(self) -> str:
        return self._url

    def _wait_lifecycle(self, loader: str, wait_until: str, deadline: float, what: str, limit: float, after: int) -> None:
        """Wait until the document loading under this loader got as far as wait_until. after: how many addresses
        the page had taken up when this one was asked for (one taken up later replaces it, as a redirect does)."""
        name = LIFECYCLE_NAMES.get(wait_until)
        if name is None:
            raise Error(f"wait_until: expected one of (load|domcontentloaded|networkidle|commit), got {wait_until!r}")
        with self._changed:
            while True:
                if self._crashed or self._closed:
                    raise Error(CLOSED_MESSAGE)
                if name in self._lifecycle.get(loader, ()):
                    return
                if self._loader_id != loader and self._commit_of.get(self._loader_id, 0) > max(after, self._commit_of.get(loader, 0)):
                    loader = self._loader_id               # (the page moved on to another address: that one is waited for)
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{what}: Timeout {limit:g}ms exceeded.\nCall log:\n  - waiting until \"{wait_until}\"")
                self._changed.wait(min(left, 0.5))

    def goto(self, url: str, wait_until: str = "load", timeout=None, referer: str | None = None):
        limit = _ms(timeout, self._navigation_timeout)
        deadline = time.monotonic() + limit / 1000
        self._download = False
        commits = self._commits
        params = {"url": url}
        if referer:
            params["referrer"] = referer
        try:
            answer = self._call("Page.navigate", params, max(1.0, limit / 1000))
        except Error as exc:
            if "no answer from the browser" in str(exc):
                raise TimeoutError(f"Page.goto: Timeout {limit:g}ms exceeded.\nCall log:\n  - navigating to \"{url}\", waiting until \"{wait_until}\"") from None
            raise
        failure = str(answer.get("errorText") or "")
        if failure:
            if failure == "net::ERR_ABORTED" and (self._download or self._backend.download_started()):
                raise Error("Page.goto: Download is starting")
            raise Error(f"Page.goto: {failure} at {url}")
        loader = str(answer.get("loaderId") or "")
        if loader:
            self._wait_lifecycle(loader, wait_until, deadline, f'Page.goto: navigating to "{url}"', limit, commits)
        return None

    def reload(self, wait_until: str = "load", timeout=None):
        limit = _ms(timeout, self._navigation_timeout)
        deadline = time.monotonic() + limit / 1000
        commits = self._commits
        self._call("Page.reload", {}, max(1.0, limit / 1000))
        with self._changed:
            while self._commits == commits:
                left = deadline - time.monotonic()
                if left <= 0 or self._crashed or self._closed:
                    raise TimeoutError(f"Page.reload: Timeout {limit:g}ms exceeded.")
                self._changed.wait(min(left, 0.5))
            loader = self._loader_id
        self._wait_lifecycle(loader, wait_until, deadline, "Page.reload", limit, commits)
        return None

    def _history_step(self, step: int, wait_until: str, timeout, what: str):
        limit = _ms(timeout, self._navigation_timeout)
        deadline = time.monotonic() + limit / 1000
        history = self._call("Page.getNavigationHistory", {}, 20)
        entries = history.get("entries") or []
        index = int(history.get("currentIndex") or 0) + step
        if index < 0 or index >= len(entries):
            return None
        commits, same = self._commits, self._same_document
        self._call("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]}, max(1.0, limit / 1000))
        with self._changed:
            while self._commits == commits and self._same_document == same:
                left = deadline - time.monotonic()
                if left <= 0 or self._crashed or self._closed:
                    raise TimeoutError(f"{what}: Timeout {limit:g}ms exceeded.")
                self._changed.wait(min(left, 0.5))
            if self._commits == commits:
                return None                                  # (the same page at another of its addresses)
            loader = self._loader_id
        self._wait_lifecycle(loader, wait_until, deadline, what, limit, commits)
        return None

    def go_back(self, wait_until: str = "load", timeout=None):
        return self._history_step(-1, wait_until, timeout, "Page.go_back")

    def go_forward(self, wait_until: str = "load", timeout=None):
        return self._history_step(1, wait_until, timeout, "Page.go_forward")

    def wait_for_load_state(self, state: str = "load", timeout=None) -> None:
        limit = _ms(timeout, self._navigation_timeout)
        self._wait_lifecycle(self._loader_id, state, time.monotonic() + limit / 1000, "Page.wait_for_load_state", limit, self._commits)

    def set_content(self, html: str, wait_until: str = "load", timeout=None) -> None:
        self._call("Page.setDocumentContent", {"frameId": self._frame_id, "html": html}, 30)

    # ------------------------------------------------------------ running JavaScript in the page

    def _run(self, source: str, timeout: float = 30.0):
        """The value of an expression in the page, packed there as JSON (see PACK_JS) and unpacked here."""
        packed = "((__r) => (__r && typeof __r.then === \"function\") ? __r.then(" + PACK_JS + ") : " + PACK_JS + "(__r))(" + source + ")"
        answer = self._call("Runtime.evaluate", {"expression": packed, "awaitPromise": True, "returnByValue": True}, timeout)
        details = answer.get("exceptionDetails")
        if details:
            exception = details.get("exception") or {}
            text = str(exception.get("description") or exception.get("value") or details.get("text") or "error in the page")
            raise Error("Page.evaluate: " + text)
        value = (answer.get("result") or {}).get("value")
        if not isinstance(value, str) or not value.startswith("j"):
            return None
        return json.loads(value[1:], object_hook=_unpack)

    def _evaluate_function(self, function: str, arguments: list):
        """A function of ours called with these arguments (each one JSON). Asked again when the page was just
        moving to another address."""
        source = "(" + function.strip() + ")(" + ", ".join(json.dumps(value) for value in arguments) + ")"
        for attempt in range(4):
            try:
                return self._run(source)
            except Error as exc:
                if attempt == 3 or "context was destroyed" not in str(exc):
                    raise
                time.sleep(0.15)

    @staticmethod
    def _expression(expression: str, arg, truth: bool = False) -> str:
        argument = "undefined" if arg is None else json.dumps(arg)
        body = ("const __f = (\n" + expression.strip() + "\n); "
                "const __v = (typeof __f === \"function\") ? __f(" + argument + ") : __f; ")
        if truth:
            return "(async () => { " + body + "return !!(await __v); })()"
        return "(() => { " + body + "return __v; })()"

    def evaluate(self, expression: str, arg=None):
        """The value of a JavaScript expression in the page; a function is called (with arg) and its result given."""
        return self._run(self._expression(expression, arg))

    def wait_for_function(self, expression: str, arg=None, timeout=None, polling=None):
        limit = _ms(timeout, self._timeout)
        deadline = time.monotonic() + limit / 1000
        source = self._expression(expression, arg, truth=True)
        while True:
            try:
                if self._run(source):
                    return True
            except Error as exc:
                if CLOSED_MESSAGE in str(exc):
                    raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Page.wait_for_function: Timeout {limit:g}ms exceeded.")
            time.sleep(0.1)

    def wait_for_timeout(self, timeout: float) -> None:
        time.sleep(max(0.0, float(timeout)) / 1000)

    def title(self) -> str:
        return str(self.evaluate("document.title") or "")

    def content(self) -> str:
        return str(self.evaluate("document.documentElement ? document.documentElement.outerHTML : ''") or "")

    # ------------------------------------------------------------ finding things

    def locator(self, selector: str, has=None, has_text=None) -> Locator:
        found = Locator(self, [["css", str(selector)]], f"locator({str(selector)!r})")
        return found.filter(has=has, has_text=has_text) if (has is not None or has_text is not None) else found

    def get_by_role(self, role: str, name=None, exact: bool = False, **_others) -> Locator:
        return Locator(self, [_role_step(role, name, exact)], f"get_by_role({role!r})")

    def wait_for_selector(self, selector: str, state: str = "visible", timeout=None):
        self.locator(selector).first.wait_for(state=state, timeout=timeout)
        return None

    def inner_text(self, selector: str, timeout=None) -> str:
        return self.locator(selector).first.inner_text(timeout=timeout)

    def click(self, selector: str, **options) -> None:
        self.locator(selector).first.click(**options)

    def is_visible(self, selector: str, timeout=None) -> bool:
        return self.locator(selector).first.is_visible()

    # ------------------------------------------------------------ its size and its picture

    @property
    def viewport_size(self):
        return dict(self._viewport) if self._viewport else None

    def set_viewport_size(self, viewport_size: dict) -> None:
        width, height = int(viewport_size["width"]), int(viewport_size["height"])
        self._backend.resize(self, width, height)
        self._viewport = {"width": width, "height": height}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                seen = self._run("[window.innerWidth, window.innerHeight]", 10) or [0, 0]
            except Error as exc:
                if CLOSED_MESSAGE in str(exc):
                    raise
                seen = [0, 0]
            if abs(seen[0] - width) <= 2 and abs(seen[1] - height) <= 2:
                return
            time.sleep(0.1)

    def _capture(self, kind: str, quality, box: dict | None, state: dict | None = None, timeout: float = 30.0) -> bytes:
        """A picture of the page (or of one box of it), in CSS pixels as on a computer's screen."""
        kind = "jpeg" if str(kind or "png").lower() in ("jpeg", "jpg") else "png"
        if state is None:
            seen = self._run("[window.scrollX, window.scrollY, window.innerWidth, window.innerHeight, window.devicePixelRatio || 1]", 10)
            state = {"scrollX": seen[0], "scrollY": seen[1], "innerWidth": seen[2], "innerHeight": seen[3], "ratio": seen[4]}
        if box is None:
            box = {"x": 0, "y": 0, "width": state["innerWidth"], "height": state["innerHeight"]}
        left, top = max(0.0, float(box["x"])), max(0.0, float(box["y"]))
        right = min(float(state["innerWidth"]), float(box["x"]) + float(box["width"]))
        bottom = min(float(state["innerHeight"]), float(box["y"]) + float(box["height"]))
        beyond = right - left < 1 or bottom - top < 1
        if beyond:                                   # (not on the screen: taken from the page as a whole)
            left, top, right, bottom = float(box["x"]), float(box["y"]), float(box["x"]) + float(box["width"]), float(box["y"]) + float(box["height"])
        ratio = float(state.get("ratio") or 1) or 1.0
        clip = {"x": left + float(state["scrollX"]), "y": top + float(state["scrollY"]),
                "width": max(1.0, right - left), "height": max(1.0, bottom - top), "scale": 1.0 / ratio}
        params = {"format": kind, "clip": clip, "captureBeyondViewport": bool(beyond)}
        if kind == "jpeg":
            params["quality"] = int(quality if quality is not None else 80)
        data = b""
        try:
            data = base64.b64decode(self._call("Page.captureScreenshot", params, timeout).get("data") or "")
        except Error as exc:
            if CLOSED_MESSAGE in str(exc):
                raise
        if len(data) < 100:
            other = self._backend.capture(self, {"x": left, "y": top, "width": right - left, "height": bottom - top}, kind, params.get("quality", 80))
            if other:
                return other
            raise Error("Page.screenshot: the browser gave no picture")
        return data

    def screenshot(self, type: str = "png", quality=None, timeout=None, clip: dict | None = None, full_page: bool = False, **_others) -> bytes:
        return self._capture(type, quality, clip, None, max(1.0, _ms(timeout, self._timeout) / 1000))

    # ------------------------------------------------------------ the requests it makes

    def _network_wanted(self) -> None:
        self._network_users += 1
        if not self._network_on:
            self._network_on = True
            self._call("Network.enable", {}, 20)

    def _network_released(self) -> None:
        self._network_users = max(0, self._network_users - 1)
        if self._network_users == 0 and self._network_on and not self._listeners.get("requestfinished"):
            self._network_on = False
            self._requests.clear()
            try:
                self._call("Network.disable", {}, 20)
            except Error:
                pass

    def _on_request(self, params: dict) -> None:
        request = params.get("request") or {}
        self._requests[params.get("requestId")] = Request(
            self, str(params.get("requestId") or ""), str(request.get("url") or ""), str(request.get("method") or "GET"),
            RESOURCE_TYPES.get(str(params.get("type") or "Other"), "other"), request.get("headers"))
        if len(self._requests) > 600:
            for old in list(self._requests)[:200]:
                self._requests.pop(old, None)

    def _on_response(self, params: dict) -> None:
        request = self._requests.get(params.get("requestId"))
        if request is not None:
            request._response = Response(self, request, params.get("response") or {})

    def _on_failed(self, params: dict) -> None:
        request = self._requests.pop(params.get("requestId"), None)
        if request is not None:
            request.failure = str(params.get("errorText") or "failed")

    def _on_finished(self, params: dict) -> None:
        request = self._requests.pop(params.get("requestId"), None)
        if request is None:
            return
        for listener in list(self._listeners.get("requestfinished", ())):
            try:
                listener(request)
            except Exception:
                pass

    def on(self, event: str, listener) -> None:
        self._listeners.setdefault(event, []).append(listener)
        if event == "requestfinished" and len(self._listeners[event]) == 1:
            self._network_wanted()

    def once(self, event: str, listener) -> None:
        def run(*values):
            self.remove_listener(event, run)
            listener(*values)
        self.on(event, run)

    def remove_listener(self, event: str, listener) -> None:
        listeners = self._listeners.get(event, [])
        if listener in listeners:
            listeners.remove(listener)
            if event == "requestfinished" and not listeners:
                self._network_released()

    # ------------------------------------------------------------ the rest

    def set_default_timeout(self, timeout: float) -> None:
        self._timeout = _ms(timeout, DEFAULT_TIMEOUT_MS)

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self._navigation_timeout = _ms(timeout, DEFAULT_TIMEOUT_MS)

    def bring_to_front(self) -> None:
        self._backend.bring_to_front()

    def is_closed(self) -> bool:
        return self._closed or self._session.closed

    def close(self, **_others) -> None:
        self.context.close()


class BrowserContext:
    """One browser with its own saved profile, and its one page."""

    def __init__(self, backend, options: dict):
        self._backend = backend
        self._options = options
        self._routes: list = []
        self._closed = False
        self._init_scripts: list = []
        self.browser = None
        page = Page(self, backend.session)
        self.pages = [page]
        viewport = options.get("viewport")
        if viewport:
            page._viewport = {"width": int(viewport["width"]), "height": int(viewport["height"])}
        backend.started(page, options)

    def new_page(self) -> Page:
        if self.pages:
            return self.pages[0]
        raise Error("this browser has one page")

    def add_init_script(self, script: str | None = None, path=None) -> None:
        if script is None and path is not None:
            with open(path, encoding="utf-8") as handle:
                script = handle.read()
        if not script:
            return
        self._init_scripts.append(script)
        self.pages[0]._call("Page.addScriptToEvaluateOnNewDocument", {"source": script}, 20)

    def add_cookies(self, cookies: list) -> None:
        self._backend.set_cookies(self.pages[0], [dict(cookie) for cookie in cookies])

    def cookies(self, urls=None) -> list:
        answer = self.pages[0]._call("Network.getAllCookies", {}, 20)
        return [dict(cookie) for cookie in answer.get("cookies") or []]

    def clear_cookies(self) -> None:
        self.pages[0]._call("Network.clearBrowserCookies", {}, 20)

    def _decide(self, url: str, resource_type: str, method: str = "GET", headers: dict | None = None) -> bool:
        """Whether a request is to be stopped, as the routes' handlers say (the one added last is asked first)."""
        for pattern, handler, takes_request in reversed(list(self._routes)):
            if not _glob_matches(pattern, url):
                continue
            request = Request(self.pages[0], "", url, method, resource_type, headers)
            route = Route(request)
            try:
                if takes_request:
                    handler(route, request)
                else:
                    handler(route)
            except Exception:
                return False
            if route._decision == "fallback":
                continue
            return route._decision == "abort"
        return False

    def route(self, url, handler, times=None) -> None:
        try:
            takes_request = len(inspect.signature(handler).parameters) >= 2
        except (TypeError, ValueError):
            takes_request = False
        self._routes.append((url, handler, takes_request))
        if len(self._routes) == 1:
            self._backend.set_router(self.pages[0], self._decide)

    def unroute(self, url, handler=None) -> None:
        self._routes = [row for row in self._routes if not (row[0] == url and (handler is None or row[1] == handler))]
        if not self._routes:
            self._backend.set_router(self.pages[0], None)

    def new_cdp_session(self, page) -> CDPSession:
        return CDPSession(page)

    def set_default_timeout(self, timeout: float) -> None:
        self.pages[0].set_default_timeout(timeout)

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self.pages[0].set_default_navigation_timeout(timeout)

    def close(self, **_others) -> None:
        if self._closed:
            return
        self._closed = True
        for page in self.pages:
            page._closed = True
        self._backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class BrowserType:
    name = "chromium"

    def launch_persistent_context(self, user_data_dir, **options) -> BrowserContext:
        from ._host import open_backend
        backend = open_backend(str(user_data_dir), options)
        try:
            return BrowserContext(backend, options)
        except BaseException:
            backend.close()
            raise

    def launch(self, **_options):
        raise Error("only launch_persistent_context is available in this browser")


class Playwright:
    def __init__(self):
        self.chromium = BrowserType()
        self.devices = {}

    def stop(self) -> None:
        pass


class PlaywrightContextManager:
    def start(self) -> Playwright:
        return Playwright()

    def __enter__(self) -> Playwright:
        return Playwright()

    def __exit__(self, *_exc) -> None:
        pass


def sync_playwright() -> PlaywrightContextManager:
    return PlaywrightContextManager()
