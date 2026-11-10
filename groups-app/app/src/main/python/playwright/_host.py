"""Where the browser is. In the app: a WebView the app makes (app.tg.groups.BrowserHost), reached through its
DevTools socket. On a computer (the tests): a Chromium started for the profile, on a debugging port. The page code
above is the same for both."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import _cdp
from ._impl import CLOSED_MESSAGE, RESOURCE_TYPES, Error

DEFAULT_VIEWPORT = {"width": 1280, "height": 720}


def on_android() -> bool:
    try:
        from java import jclass          # (Chaquopy: Python inside an Android app)
        jclass("android.os.Build")
        return True
    except Exception:
        return False


def user_agent_metadata(user_agent: str) -> dict:
    """What a browser with this user agent says about itself where sites ask for more than the user agent."""
    full = (re.search(r"Chrome/([\d.]+)", user_agent) or [None, "131.0.0.0"])[1]
    major = full.split(".")[0]
    windows = re.search(r"Windows NT (\d+(?:\.\d+)*)", user_agent)
    android = re.search(r"Android (\d+(?:\.\d+)*)", user_agent)
    mac = re.search(r"Mac OS X (\d+(?:[_.]\d+)*)", user_agent)
    if android:
        platform, version = "Android", android.group(1)
    elif windows:
        platform, version = "Windows", "10.0.0" if windows.group(1).startswith("10") else windows.group(1)
    elif mac:
        platform, version = "macOS", mac.group(1).replace("_", ".")
    else:
        platform, version = "Linux", ""
    brands = [("Chromium", major, full), ("Google Chrome", major, full), ("Not_A Brand", "24", "24.0.0.0")]
    return {
        "brands": [{"brand": name, "version": short} for name, short, _ in brands],
        "fullVersionList": [{"brand": name, "version": long} for name, _, long in brands],
        "fullVersion": full, "platform": platform, "platformVersion": version,
        "architecture": "arm" if (android or "ARM" in user_agent) else "x86", "model": "",
        "mobile": "Mobile" in user_agent, "bitness": "64", "wow64": False,
    }


def _quietly(page, method: str, params: dict) -> None:
    """A setting the browser may not know: asked for, and left when it is refused."""
    try:
        page._call(method, params, 20)
    except Error as exc:
        if CLOSED_MESSAGE in str(exc):
            raise


def _apply_identity(page, options: dict) -> None:
    """The user agent and the language the script asked for, as Playwright sets them; and, as Playwright does for
    every page, the page behaves as the one in front (it takes typing and focus while another view is on top)."""
    _quietly(page, "Emulation.setFocusEmulationEnabled", {"enabled": True})
    _quietly(page, "Emulation.setEmulatedMedia", {"features": [
        {"name": "prefers-color-scheme", "value": "light"}, {"name": "prefers-reduced-motion", "value": "no-preference"},
        {"name": "forced-colors", "value": "none"}]})
    user_agent = str(options.get("user_agent") or "")
    locale = str(options.get("locale") or "")
    if user_agent or locale:
        params: dict = {"userAgent": user_agent}
        if not user_agent:
            try:
                params["userAgent"] = str(page._run("navigator.userAgent", 10) or "")
            except Error:
                params["userAgent"] = ""
        if locale:
            params["acceptLanguage"] = locale
        if user_agent:
            params["userAgentMetadata"] = user_agent_metadata(user_agent)
        try:
            page._call("Emulation.setUserAgentOverride", params, 20)
        except Error as exc:
            if CLOSED_MESSAGE in str(exc):
                raise
            try:
                params.pop("userAgentMetadata", None)
                page._call("Emulation.setUserAgentOverride", params, 20)
            except Error as again:
                if CLOSED_MESSAGE in str(again):
                    raise
    if locale:
        try:
            page._call("Emulation.setLocaleOverride", {"locale": locale}, 20)
        except Error as exc:
            if CLOSED_MESSAGE in str(exc):
                raise


class Backend:
    kind = ""
    session = None

    def started(self, page, options: dict) -> None:
        pass

    def wait_ready(self) -> None:
        pass

    def download_started(self) -> bool:
        return False

    def bring_to_front(self) -> None:
        pass

    def capture(self, page, box: dict, kind: str, quality: int):
        return None


class DesktopBackend(Backend):
    """A Chromium of this machine, one per profile folder, as Playwright would start it."""
    kind = "desktop"

    def __init__(self, profile_dir: str, options: dict):
        program = os.environ.get("GG_CHROMIUM") or options.get("executable_path") or shutil.which("chromium") or shutil.which("chromium-browser")
        if not program:
            raise Error("BrowserType.launch_persistent_context: no Chromium here (set GG_CHROMIUM to its program)")
        viewport = options.get("viewport") or DEFAULT_VIEWPORT
        folder = Path(profile_dir)
        folder.mkdir(parents=True, exist_ok=True)
        port_file = folder / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        command = [program, "--remote-debugging-port=0", f"--user-data-dir={folder}", "--no-first-run",
                   "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                   f"--window-size={int(viewport['width'])},{int(viewport['height'])}"]
        if options.get("headless", True):
            command.append("--headless=new")
        command += [str(arg) for arg in options.get("args") or []] + ["about:blank"]
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._router = None
        self.session = None
        try:
            deadline = time.monotonic() + 30
            port = 0
            while time.monotonic() < deadline and not port:
                if self._process.poll() is not None:
                    raise Error("BrowserType.launch_persistent_context: Chromium stopped as soon as it started")
                try:
                    port = int(port_file.read_text().splitlines()[0])
                except (OSError, ValueError, IndexError):
                    time.sleep(0.05)
            if not port:
                raise Error("BrowserType.launch_persistent_context: Chromium did not open its debugging port")
            self._endpoint = _cdp.TcpEndpoint(port)
            target = None
            while time.monotonic() < deadline and target is None:
                try:
                    target = next((row for row in _cdp.list_targets(self._endpoint) if row.get("type") == "page"), None)
                except (OSError, ValueError, _cdp.ConnectionClosed):
                    target = None
                if target is None:
                    time.sleep(0.05)
            if target is None:
                raise Error("BrowserType.launch_persistent_context: Chromium opened no page")
            self.session = _cdp.Session(self._endpoint, "/devtools/page/" + str(target["id"]), "desktop")
        except BaseException:
            self.close()
            raise

    def started(self, page, options: dict) -> None:
        viewport = options.get("viewport") or DEFAULT_VIEWPORT
        self.resize(page, int(viewport["width"]), int(viewport["height"]))
        _apply_identity(page, options)

    def resize(self, page, width: int, height: int):
        page._call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, 20)
        return width, height

    def set_cookies(self, page, cookies: list) -> None:
        rows = []
        for cookie in cookies:
            row = {key: cookie[key] for key in ("name", "value", "url", "domain", "path", "secure", "expires") if key in cookie}
            if "httpOnly" in cookie:
                row["httpOnly"] = bool(cookie["httpOnly"])
            if cookie.get("sameSite"):
                row["sameSite"] = str(cookie["sameSite"])
            rows.append(row)
        page._call("Network.setCookies", {"cookies": rows}, 20)

    def set_router(self, page, decide) -> None:
        if decide is None:
            self._router = None
            page._call("Fetch.disable", {}, 20)
            return
        first = self._router is None
        self._router = decide

        def paused(params: dict) -> None:
            request = params.get("request") or {}
            stop = False
            if self._router is not None:
                try:
                    stop = bool(self._router(str(request.get("url") or ""), RESOURCE_TYPES.get(str(params.get("resourceType") or "Other"), "other"),
                                             str(request.get("method") or "GET"), request.get("headers")))
                except Exception:
                    stop = False
            try:
                if stop:
                    self.session.send("Fetch.failRequest", {"requestId": params.get("requestId"), "errorReason": "Failed"}, 20)
                else:
                    self.session.send("Fetch.continueRequest", {"requestId": params.get("requestId")}, 20)
            except Exception:
                pass

        if first:
            self.session.on("Fetch.requestPaused", paused)
            page._call("Fetch.enable", {}, 20)

    def close(self) -> None:
        if self.session is not None:
            try:
                version = json.loads(_cdp.http_get(self._endpoint, "/json/version", 5).decode("utf-8", "replace"))
                path = "/" + str(version.get("webSocketDebuggerUrl") or "").split("/", 3)[-1]
                browser = _cdp.Session(self._endpoint, path, "desktop-browser")
                try:
                    browser.send("Browser.close", {}, 5)       # (a proper close: the profile's cookies are written)
                except Exception:
                    pass
                browser.close()
            except Exception:
                pass
            self.session.close()
        try:
            self._process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        except Exception:
            pass


class AndroidBackend(Backend):
    """A WebView of the app, with the profile's own cookies and storage. The app shows it in «المتصفح».

    It is the phone's own browser as it is: the user agent, the language and the page's width that the script
    asks for are not applied, and nothing else about the browser is made to look different. Only the page's height
    is the script's (it needs a tall page while it collects from a group)."""
    kind = "android"
    _open_lock = threading.Lock()
    _slots = None

    def __init__(self, profile_dir: str, options: dict):
        from java import jclass
        self._host = jclass("app.tg.groups.BrowserHost")
        self.id = 0
        self.session = None
        self._router = None
        self._emulated = False
        self._width = 0
        viewport = options.get("viewport") or DEFAULT_VIEWPORT
        with AndroidBackend._open_lock:
            if AndroidBackend._slots is None:
                # Browsers open at once: each account's profile is its own only where this phone's WebView keeps
                # profiles apart; elsewhere one browser at a time, so that accounts never share cookies.
                most = max(1, int(os.environ.get("GG_MAX_BROWSERS") or 2)) if bool(self._host.profilesSupported()) else 1
                AndroidBackend._slots = threading.BoundedSemaphore(most)
        AndroidBackend._slots.acquire()
        self._held = True
        try:
            self.id = int(self._host.open(Path(profile_dir).name, int(viewport["height"])))
            if self.id <= 0:
                raise Error("BrowserType.launch_persistent_context: " + (str(self._host.lastError() or "") or "تعذّر فتح متصفح التطبيق."))
            self._endpoint = _cdp.AbstractEndpoint(f"webview_devtools_remote_{os.getpid()}")
            marker = f"gg-{self.id}"
            target = None
            problem = ""
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and target is None:
                try:
                    rows = _cdp.list_targets(self._endpoint, 5)
                    target = next((row for row in rows if row.get("title") == marker or marker in str(row.get("url") or "")), None)
                except Exception as exc:
                    problem = f"{type(exc).__name__}: {exc}"
                if target is None:
                    time.sleep(0.15)
            if target is None:
                raise Error("BrowserType.launch_persistent_context: تعذّر الوصول إلى متصفح التطبيق من السكربت"
                            + (f" ({problem})" if problem else " (لم يظهر المتصفح الجديد في قائمة DevTools)."))
            self.session = _cdp.Session(self._endpoint, "/devtools/page/" + str(target["id"]), marker)
        except BaseException:
            self.close()
            raise

    def started(self, page, options: dict) -> None:
        # As Playwright does for every page, the page behaves as the one in front (it takes typing while another
        # view is on top). Nothing of the browser's identity is touched.
        _quietly(page, "Emulation.setFocusEmulationEnabled", {"enabled": True})
        viewport = options.get("viewport") or DEFAULT_VIEWPORT
        width, height = self._fit(page, int(viewport["height"]))
        page._viewport = {"width": width, "height": height}

    def wait_ready(self) -> None:
        # While the app is not on the screen its browser draws nothing and takes no clicks: the task rests here
        # until the app is back (or its browser is gone, which the next command then reports).
        while True:
            try:
                if self._host.awaitVisible(30000):
                    return
            except Exception:
                return
            if self.session is None or self.session.closed:
                return

    def _seen(self, page) -> list:
        try:
            seen = page._run("[window.innerWidth, window.innerHeight]", 10) or [0, 0]
        except Error as exc:
            if CLOSED_MESSAGE in str(exc):
                raise
            seen = [0, 0]
        return [int(seen[0] or 0), int(seen[1] or 0)]

    def _settle(self, page, height: int) -> bool:
        """Whether the page became this tall (the WebView is laid out a moment after it is asked to)."""
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            seen = self._seen(page)
            if seen[0] > 0 and abs(seen[1] - height) <= 2:
                self._width = seen[0]
                return True
            time.sleep(0.1)
        return False

    def _fit(self, page, height: int) -> tuple:
        """Wait until the page is this tall. The page's size as it is then: the phone's own width, and the height."""
        if not self._emulated and not self._settle(page, height):
            # The WebView did not take the height as its own: the page is told the height instead (its width is
            # still left alone: a width of 0 there means "as it is").
            print(f"المتصفح لم يأخذ الطول {height} من الشاشة، فيُعطى للصفحة مباشرة.", flush=True)
            self._emulated = True
        if self._emulated:
            try:
                page._call("Emulation.setDeviceMetricsOverride", {"width": 0, "height": height, "deviceScaleFactor": 0, "mobile": False}, 20)
            except Error as exc:
                if CLOSED_MESSAGE in str(exc):
                    raise
        width = self._seen(page)[0] or self._width
        if width > 0:
            self._width = width
        return (self._width or DEFAULT_VIEWPORT["width"]), height

    def resize(self, page, width: int, height: int):
        """Only the height is taken: the width stays the phone's. The size the page has after."""
        self._host.resize(self.id, int(height))
        return self._fit(page, int(height))

    def set_cookies(self, page, cookies: list) -> None:
        for cookie in cookies:
            domain = str(cookie.get("domain") or "")
            path = str(cookie.get("path") or "/")
            url = str(cookie.get("url") or "") or ("https://" if cookie.get("secure", True) else "http://") + domain.lstrip(".") + path
            parts = [f"{cookie.get('name')}={cookie.get('value')}"]
            if domain:
                parts.append("Domain=" + domain)
            parts.append("Path=" + path)
            expires = cookie.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                parts.append("Max-Age=" + str(max(0, int(expires - time.time()))))
            if cookie.get("secure"):
                parts.append("Secure")
            if cookie.get("httpOnly"):
                parts.append("HttpOnly")
            if cookie.get("sameSite"):
                parts.append("SameSite=" + str(cookie["sameSite"]))
            self._host.setCookie(self.id, url, "; ".join(parts))

    def set_router(self, page, decide) -> None:
        if decide is None:
            self._router = None
            self._host.setRouter(self.id, None)
            return

        def from_app(url, accept, has_range, main_frame, method) -> bool:
            """Asked by the app for each request of this browser: whether to stop it."""
            try:
                accept = str(accept or "")
                if main_frame:
                    kind = "document"
                elif accept.startswith("image/"):
                    kind = "image"
                elif has_range:
                    kind = "media"
                elif accept.startswith("text/css"):
                    kind = "stylesheet"
                else:
                    kind = "other"
                return bool(decide(str(url or ""), kind, str(method or "GET"), {"accept": accept}))
            except Exception:
                return False

        self._router = from_app
        self._host.setRouter(self.id, from_app)

    def download_started(self) -> bool:
        try:
            return bool(self._host.downloadStarted(self.id))
        except Exception:
            return False

    def bring_to_front(self) -> None:
        try:
            self._host.show(self.id)
        except Exception:
            pass

    def capture(self, page, box: dict, kind: str, quality: int):
        try:
            data = self._host.capture(self.id, float(box["x"]), float(box["y"]), float(box["width"]), float(box["height"]),
                                      kind == "jpeg", int(quality or 80))
            return base64.b64decode(str(data)) if data else None
        except Exception:
            return None

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        if self.id > 0:
            try:
                self._host.close(self.id)
            except Exception:
                pass
            self.id = 0
        if self._held:
            self._held = False
            AndroidBackend._slots.release()


def open_backend(profile_dir: str, options: dict) -> Backend:
    return AndroidBackend(profile_dir, options) if on_android() else DesktopBackend(profile_dir, options)
