#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«قروبات» on a server: the Android app itself, running in an Android that lives in a container (redroid) on the
hosting, with no emulator. This file sets that up, keeps the app running, and opens a door to it for its owner's
own browser: the app's screen (to look at and to tap, as on a phone) and the app's panel.

On the server, as root (Ubuntu, a KVM server; it installs Docker and what the Android needs, starts the Android,
installs the app in it, and prints the door's link):
    mkdir -p ~/gg-android && cd ~/gg-android && \\
    curl -fsSL -o gg_android.py https://raw.githubusercontent.com/husaynalshehari/husaynalshehari/ccr-695f6c04-mlgxcr/groups-app/hosting/gg_android.py && \\
    python3 gg_android.py --setup

Then, in the same folder:
    python3 gg_android.py --link       the door's link again
    python3 gg_android.py --log        the app's own log («السجل»), to send when something fails
    python3 gg_android.py --update     the app's newest version
    python3 gg_android.py --restart    the Android and the app started afresh
    python3 gg_android.py --new-link   a new secret (the old link stops working)

The door is encrypted with this server's own certificate, so the browser warns once that it does not know it:
choose to go on. The Android's own control port (adb) is never opened to the internet.
"""
from __future__ import annotations

import argparse
import hmac
import http.client
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve()
HERE = Path(os.environ.get("GG_ANDROID_DIR") or SCRIPT.parent)
CONFIG = HERE / "gg_android.json"
CERT = HERE / "gg_android_cert.pem"
CERT_KEY = HERE / "gg_android_key.pem"
DATA = HERE / "android-data"
APK = HERE / "groups.apk"
SERVICE = "gg-android"
UNIT = Path("/etc/systemd/system") / (SERVICE + ".service")
CONTAINER = "gg-android"
PACKAGE = "app.tg.groups"
RAW = "https://raw.githubusercontent.com/husaynalshehari/husaynalshehari/ccr-695f6c04-mlgxcr/groups-app/"
DEFAULT_IMAGE = "redroid/redroid:16.0.0_64only-latest"
DEFAULT_PORT = 8444
ADB = os.environ.get("GG_ADB") or "adb"
DEVICE = os.environ.get("GG_ANDROID_DEVICE") or "127.0.0.1:5555"
PANEL_PORT = int(os.environ.get("GG_PANEL_PORT") or 15177)       # this machine's end of the way to the app's panel
APP_PORT = 5177                                                  # the app's panel, inside the Android
BIND = os.environ.get("GG_ANDROID_BIND") or "0.0.0.0"
SCREEN = (720, 1280, 320)


def say(message: str) -> None:
    print(message, flush=True)


def run(command: list, seconds: float = 120, quiet: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=quiet, text=True, timeout=seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, "", f"{type(exc).__name__}: {exc}")


def adb(*args: str, seconds: float = 60) -> subprocess.CompletedProcess:
    return run([ADB, "-s", DEVICE, *args], seconds)


def shell(command: str, seconds: float = 60) -> str:
    """One command in the Android: what it printed."""
    return adb("shell", command, seconds=seconds).stdout.strip()


def connect() -> bool:
    run([ADB, "connect", DEVICE], 20)
    return shell("getprop sys.boot_completed", 15) == "1"


# ---------------------------------------------------------------- the app, inside the Android

def app_running() -> bool:
    return bool(shell(f"pidof {PACKAGE}", 15))


def start_app() -> None:
    shell(f"am start -n {PACKAGE}/.MainActivity", 30)


def allow_app() -> None:
    """What the app asks its user for on a phone, given here: its window that keeps tasks at speed, its notice, and
    a screen that never sleeps or locks."""
    for command in (f"appops set {PACKAGE} SYSTEM_ALERT_WINDOW allow",
                    f"pm grant {PACKAGE} android.permission.POST_NOTIFICATIONS",
                    f"dumpsys deviceidle whitelist +{PACKAGE}",
                    "settings put system screen_off_timeout 2147483647",
                    "svc power stayon true",
                    "locksettings set-disabled true",
                    "wm dismiss-keyguard"):
        shell(command, 30)


def app_file(*parts: str) -> Path | None:
    """A file of the app's own, read from the Android's data folder on this machine."""
    for root in (DATA / "data" / PACKAGE, DATA / "user" / "0" / PACKAGE):
        path = root.joinpath(*parts)
        try:
            if path.is_file():
                return path
        except OSError:
            pass
    return None


def access_path() -> str:
    """The made-up address of the app's panel (the app makes it at its first start)."""
    path = app_file("shared_prefs", "engine.xml")
    text = ""
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text:
        text = shell(f"cat /data/data/{PACKAGE}/shared_prefs/engine.xml", 15)
    found = re.search(r'name="path">([a-z0-9]{16})<', text)
    return found.group(1) if found else ""


def app_log(most: int = 12000) -> str:
    path = app_file("files", "app-log.txt")
    if path is None:
        return shell(f"tail -c {most} /data/data/{PACKAGE}/files/app-log.txt", 20) or "لا يوجد سجل للتطبيق بعد."
    try:
        data = path.read_bytes()[-most:]
    except OSError as exc:
        return f"تعذّرت قراءة سجل التطبيق: {exc}"
    return data.decode("utf-8", "replace").split("\n", 1)[-1]


def panel_answers(seconds: float = 5) -> bool:
    adb("forward", f"tcp:{PANEL_PORT}", f"tcp:{APP_PORT}", seconds=15)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", PANEL_PORT, timeout=seconds)
        connection.request("GET", "/healthz")
        ok = connection.getresponse().status == 200
        connection.close()
        return ok
    except (OSError, http.client.HTTPException):
        return False


# ---------------------------------------------------------------- the door: the screen and the panel

conf: dict = {}
shot_lock = threading.Lock()
shot_kept = {"at": 0.0, "data": b""}
SCREEN_PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>قروبات — شاشة التطبيق</title>
<style>
body{margin:0;background:#f3efe6;color:#1c1915;font:15px/1.5 system-ui,sans-serif}
header{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:8px 10px;position:sticky;top:0;background:#f3efe6;z-index:2}
header b{flex:1;font-size:17px} a.btn,button{font:inherit;border:1px solid #e4dcd0;background:#fffdf8;color:inherit;border-radius:10px;
padding:7px 12px;text-decoration:none} button:active{background:#e4dcd0} .main{background:#0f6b50;color:#f4fbf7;border-color:#0f6b50}
#wrap{padding:0 10px 10px} #screen{width:100%;max-width:480px;display:block;margin:auto;border:1px solid #e4dcd0;border-radius:8px;
touch-action:none;user-select:none;-webkit-user-drag:none} .row{display:flex;gap:6px;max-width:480px;margin:8px auto}
.row input{flex:1;min-width:0;font:inherit;padding:8px;border:1px solid #e4dcd0;border-radius:10px;direction:ltr}
#note{max-width:480px;margin:4px auto;color:#6e675e;font-size:13px;min-height:1.4em}
</style></head><body>
<header><b>قروبات على السيرفر</b><a class="btn main" id="panel" href="#">اللوحة</a><button id="back">رجوع</button>
<button id="open">فتح التطبيق</button></header>
<div id="wrap"><img id="screen" alt="شاشة التطبيق" draggable="false">
<div class="row"><input id="text" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="نص يُكتب في الخانة المختارة (حروف إنجليزية وأرقام)">
<button id="send">كتابة</button></div>
<div class="row"><button id="enter">Enter</button><button id="del">مسح حرف</button><button id="tab">Tab</button>
<button id="up">▲ تمرير</button><button id="down">▼ تمرير</button></div>
<div id="note">اضغط على الصورة كما تضغط على شاشة الجوال. اسحب للتمرير.</div></div>
<script>
const root = location.pathname.replace(/\\/+$/, "");
const screen_ = document.getElementById("screen"), note = document.getElementById("note");
let busy = false, fails = 0;
async function post(action, body) {
  try {
    const r = await fetch(root + "/" + action, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
    const d = await r.json();
    note.textContent = d.message || (d.ok ? "تم." : "تعذّر.");
    setTimeout(refresh, 350);
    return d;
  } catch (e) { note.textContent = "تعذّر الاتصال بالسيرفر."; return {}; }
}
function refresh() {
  if (busy || document.hidden) return;
  busy = true;
  const next = new Image();
  next.onload = () => { screen_.src = next.src; busy = false; fails = 0; };
  next.onerror = () => { busy = false; if (++fails > 2) note.textContent = "لم تصل صورة الشاشة. قد يكون الأندرويد يقلع: انتظر قليلًا."; };
  next.src = root + "/shot.png?t=" + Date.now();
}
setInterval(refresh, 1500); refresh();
function spot(e) {
  const box = screen_.getBoundingClientRect();
  return {x: Math.round((e.clientX - box.left) * screen_.naturalWidth / box.width), y: Math.round((e.clientY - box.top) * screen_.naturalHeight / box.height)};
}
let from = null, at = 0;
screen_.addEventListener("pointerdown", (e) => { e.preventDefault(); from = spot(e); at = Date.now(); screen_.setPointerCapture(e.pointerId); });
screen_.addEventListener("pointerup", (e) => {
  if (!from) return;
  const to = spot(e), far = Math.hypot(to.x - from.x, to.y - from.y);
  if (far < 24) post("tap", from); else post("swipe", {x1: from.x, y1: from.y, x2: to.x, y2: to.y, ms: Math.max(120, Math.min(1500, Date.now() - at))});
  from = null;
});
const keys = {back: 4, enter: 66, del: 67, tab: 61};
for (const id of Object.keys(keys)) document.getElementById(id).addEventListener("click", () => post("key", {code: keys[id]}));
document.getElementById("up").addEventListener("click", () => post("swipe", {x1: 360, y1: 420, x2: 360, y2: 980, ms: 300}));
document.getElementById("down").addEventListener("click", () => post("swipe", {x1: 360, y1: 980, x2: 360, y2: 420, ms: 300}));
document.getElementById("open").addEventListener("click", () => post("open"));
const text = document.getElementById("text");
document.getElementById("send").addEventListener("click", async () => { const d = await post("text", {text: text.value}); if (d.ok) text.value = ""; });
fetch(root + "/where").then((r) => r.json()).then((d) => { if (d.panel) document.getElementById("panel").href = d.panel; else note.textContent = "التطبيق لم يجهز لوحته بعد. انتظر ثم حدّث الصفحة."; });
</script></body></html>"""


def screenshot() -> bytes:
    """The Android's screen as a PNG (one taken in the last moment is given again)."""
    with shot_lock:
        if time.time() - shot_kept["at"] < 0.6 and shot_kept["data"]:
            return shot_kept["data"]
        try:
            data = subprocess.run([ADB, "-s", DEVICE, "exec-out", "screencap", "-p"], capture_output=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            data = b""
        if data[:4] != b"\x89PNG":
            connect()
            return b""
        shot_kept.update(at=time.time(), data=data)
        return data


def typed(text: str) -> str:
    """The text as `input text` takes it through the Android's shell, or "" when it cannot be typed this way."""
    if not text or len(text) > 300 or any(not 32 <= ord(ch) < 127 for ch in text):
        return ""
    return "'" + text.replace("'", "'\\''").replace(" ", "%s") + "'"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "gg"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, fmt, *args):
        pass                                     # (nothing with the link's secret is written down)

    def send(self, status: int, body: bytes, kind: str, extra: list | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def answer(self, status: int, payload: dict) -> None:
        self.send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= 8 << 20:
            raise ValueError("size")
        return self.rfile.read(length) if length else b""

    def route(self) -> None:
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        first = parts[0] if parts else ""
        if first and hmac.compare_digest(first.encode("utf-8"), str(conf.get("token") or "").encode("utf-8")):
            self.screen(parts[1] if len(parts) > 1 else "")
            return
        panel = access_path() if re.fullmatch(r"[a-z0-9]{16}", first) else ""
        if panel and hmac.compare_digest(first.encode("utf-8"), panel.encode("utf-8")):
            self.panel()
            return
        self.answer(404, {"ok": False})

    do_GET = do_POST = do_HEAD = route

    def screen(self, action: str) -> None:
        """The app's screen: the page, its picture, and what is done on it."""
        if self.command in ("GET", "HEAD"):
            if action == "":
                self.send(200, SCREEN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif action == "shot.png":
                data = screenshot()
                self.send(200 if data else 503, data, "image/png")
            elif action == "where":
                panel = access_path()
                self.answer(200, {"ok": True, "panel": "/" + panel if panel else ""})
            else:
                self.answer(404, {"ok": False})
            return
        try:
            data = json.loads(self.body().decode("utf-8") or "{}")
            number = lambda name: max(0, min(10000, int(data.get(name) or 0)))
            if action == "tap":
                shell(f"input tap {number('x')} {number('y')}", 20)
                self.answer(200, {"ok": True, "message": "ضغطة."})
            elif action == "swipe":
                shell(f"input swipe {number('x1')} {number('y1')} {number('x2')} {number('y2')} {max(50, min(3000, number('ms')))}", 20)
                self.answer(200, {"ok": True, "message": "سحب."})
            elif action == "key":
                code = int(data.get("code") or 0)
                if code not in (4, 61, 66, 67):
                    raise ValueError("key")
                shell(f"input keyevent {code}", 20)
                self.answer(200, {"ok": True, "message": "تم."})
            elif action == "text":
                text = typed(str(data.get("text") or ""))
                if not text:
                    self.answer(200, {"ok": False, "message": "يُكتب هنا الإنجليزي والأرقام والرموز فقط (حتى 300 حرف)."})
                    return
                shell("input text " + text, 30)
                self.answer(200, {"ok": True, "message": "كُتب النص."})
            elif action == "open":
                start_app()
                self.answer(200, {"ok": True, "message": "فُتح التطبيق."})
            else:
                self.answer(404, {"ok": False})
        except (ValueError, TypeError, UnicodeDecodeError):
            self.answer(400, {"ok": False, "message": "طلب غير صالح."})

    def panel(self) -> None:
        """The app's own panel, as the app serves it inside the Android: the request is passed on as it is."""
        try:
            sent = self.body()
        except ValueError:
            self.answer(400, {"ok": False})
            return
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in ("host", "connection", "content-length", "accept-encoding", "keep-alive", "transfer-encoding")}
        for attempt in (0, 1):
            try:
                connection = http.client.HTTPConnection("127.0.0.1", PANEL_PORT, timeout=330)
                connection.request(self.command, self.path, body=sent or None, headers=headers)
                reply = connection.getresponse()
                data = reply.read()
                kept = [(name, value) for name, value in reply.getheaders()
                        if name.lower() not in ("connection", "content-length", "transfer-encoding", "keep-alive", "content-type", "server", "date", "cache-control")]
                connection.close()
                self.send(reply.status, data, reply.getheader("Content-Type") or "application/octet-stream", kept)
                return
            except (OSError, http.client.HTTPException):
                if attempt:
                    break
                connect()
                adb("forward", f"tcp:{PANEL_PORT}", f"tcp:{APP_PORT}", seconds=15)       # (the way to the app, laid again)
        self.send(502, "التطبيق لا يجيب الآن. افتح رابط الشاشة واضغط «فتح التطبيق»، ثم أعد المحاولة.".encode("utf-8"), "text/plain; charset=utf-8")


def keep_running() -> None:
    """The Android is reached again after it restarts, and the app is started again if it is not running."""
    while True:
        try:
            if connect():
                if not app_running():
                    allow_app()
                    start_app()
                    say(time.strftime("%Y-%m-%d %H:%M:%S") + " فُتح التطبيق من جديد")
                adb("forward", f"tcp:{PANEL_PORT}", f"tcp:{APP_PORT}", seconds=15)
        except Exception as exc:
            say(f"{type(exc).__name__}: {exc}")
        time.sleep(30)


def serve(data: dict) -> None:
    if not data.get("token"):
        raise SystemExit("لم يُضبط بعد. شغّل: python3 gg_android.py --setup")
    make_certificate()
    conf.update(data)
    threading.Thread(target=keep_running, name="keep-running", daemon=True).start()
    server = Server((BIND, int(data["port"])), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(CERT), str(CERT_KEY))
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    say(f"باب «قروبات» يعمل على المنفذ {data['port']}")
    server.serve_forever()


# ---------------------------------------------------------------- setting it up

def load_config() -> dict:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(CONFIG)


def make_certificate() -> None:
    if CERT.is_file() and CERT_KEY.is_file():
        return
    done = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(CERT_KEY), "-out", str(CERT),
                "-days", "3650", "-subj", "/CN=gg-android"], 120)
    if done.returncode != 0:
        raise SystemExit("تعذّر إنشاء شهادة السيرفر (openssl): " + done.stderr.strip()[-300:])
    os.chmod(CERT_KEY, 0o600)


def public_host(data: dict) -> str:
    host = os.environ.get("GG_ANDROID_HOST") or str(data.get("host") or "")
    if host:
        return host
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as answer:
            found = answer.read(64).decode("ascii", "replace").strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", found):
            return found
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        return subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return "عنوان-السيرفر"


def door_link(data: dict) -> str:
    return f"https://{public_host(data)}:{data['port']}/{data['token']}/"


def open_firewall(port: int) -> str:
    if shutil.which("ufw"):
        status = run(["ufw", "status"], 20).stdout
        if "Status: active" in status:
            run(["ufw", "allow", f"{port}/tcp"], 20)
            return "ufw"
    return ""


def install_service() -> bool:
    if os.geteuid() != 0 or not shutil.which("systemctl") or not Path("/run/systemd/system").is_dir():
        return False
    python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    UNIT.write_text(f"""[Unit]
Description=The groups app on this server: its door (قروبات)
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart={python} {SCRIPT}
WorkingDirectory={HERE}
Environment=PYTHONUNBUFFERED=1
Environment=GG_ANDROID_DIR={HERE}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")
    run(["systemctl", "daemon-reload"], 60)
    run(["systemctl", "enable", SERVICE], 60)
    return run(["systemctl", "restart", SERVICE], 60).returncode == 0


def stop(why: str, detail: str = "") -> None:
    say("\n✗ " + why)
    if detail.strip():
        say(detail.strip()[-1500:])
    say("\nأرسل ما ظهر أعلاه كما هو.")
    raise SystemExit(1)


def install_what_is_needed() -> None:
    say("١) تثبيت Docker و adb وما يحتاجه أندرويد من نواة السيرفر…")
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    subprocess.run(["apt-get", "update", "-qq"], env=env, capture_output=True, timeout=600)
    kernel = platform.release()
    wanted = [name for name, program in (("docker.io", "docker"), ("adb", "adb"), ("openssl", "openssl")) if not shutil.which(program)]
    if wanted:
        done = subprocess.run(["apt-get", "install", "-y", "-qq", *wanted], env=env, capture_output=True, text=True, timeout=1200)
        if done.returncode != 0:
            stop("تعذّر تثبيت: " + " ".join(wanted), done.stderr)
    if run(["modinfo", "binder_linux"], 20).returncode != 0 and "binder" not in Path("/proc/filesystems").read_text():
        done = subprocess.run(["apt-get", "install", "-y", "-qq", f"linux-modules-extra-{kernel}"], env=env, capture_output=True, text=True, timeout=1200)
        if done.returncode != 0:
            stop(f"لم توجد وحدات النواة الإضافية لهذه النواة ({kernel})، وأندرويد يحتاج منها binder.", done.stderr)
    if "binder" not in Path("/proc/filesystems").read_text():
        done = run(["modprobe", "binder_linux", "devices=binder,hwbinder,vndbinder"], 60)
        if done.returncode != 0 and "binder" not in Path("/proc/filesystems").read_text():
            stop("النواة لم تحمّل binder، وأندرويد لا يعمل بدونه.", done.stderr + "\n" + run(["dmesg"], 20).stdout[-600:])
    try:                                           # (loaded again when the server restarts)
        Path("/etc/modules-load.d/gg-android.conf").write_text("binder_linux\n")
        Path("/etc/modprobe.d/gg-android.conf").write_text("options binder_linux devices=binder,hwbinder,vndbinder\n")
    except OSError:
        pass
    run(["systemctl", "enable", "--now", "docker"], 120)
    if run(["docker", "info"], 60).returncode != 0:
        stop("Docker لا يعمل.", run(["systemctl", "status", "docker", "--no-pager"], 30).stdout)
    say("   ✓ جاهز (النواة " + kernel + ")")


def inside(command: str, seconds: float = 20) -> str:
    """One command in the Android, through Docker itself (it works before adb does)."""
    return run(["docker", "exec", CONTAINER, "sh", "-c", command], seconds).stdout.strip()


def diagnose() -> str:
    """What is known of the Android when it does not come up: for its owner to send."""
    parts = ["الحاوية: " + (run(["docker", "ps", "-a", "--filter", f"name={CONTAINER}", "--format", "{{.Image}} {{.Status}}"], 20).stdout.strip() or "غير موجودة")]
    parts.append("الإقلاع: boot_completed=" + (inside("getprop sys.boot_completed") or "؟") + " · bootanim=" + (inside("getprop init.svc.bootanim") or "؟")
                 + " · zygote=" + (inside("getprop init.svc.zygote") or "؟") + " · system_server=" + (inside("pidof system_server") or "لا"))
    parts.append("adb: " + run([ADB, "devices"], 20).stdout.strip().replace("\n", " | "))
    parts.append("binder: " + run(["sh", "-c", "grep binder /proc/filesystems; lsmod | grep -E 'binder|ashmem'"], 20).stdout.strip().replace("\n", " | "))
    errors = inside("logcat -d -b all 2>/dev/null | grep -E ' (F|E) ' | grep -viE 'netd|wifi|bluetooth|audio|camera|thermal|vibrator|sensor|gnss|nfc' | tail -25", 40)
    parts.append("أخطاء أندرويد:\n" + (errors or "(لا شيء)"))
    kernel = run(["sh", "-c", "dmesg 2>/dev/null | grep -iE 'binder|redroid|apparmor=.DENIED|oom' | tail -12"], 20).stdout.strip()
    parts.append("النواة:\n" + (kernel or "(لا شيء)"))
    return "\n".join(parts)


def start_android(image: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    running = run(["docker", "inspect", "-f", "{{.State.Running}} {{.Config.Image}}", CONTAINER], 20).stdout.strip()
    if running == f"true {image}" and (inside("getprop sys.boot_completed") == "1" or connect()):
        connect()
        say(f"٢) أندرويد يعمل من قبل ({image})")
        return
    say(f"٢) تشغيل أندرويد ({image}). تنزيله أول مرة يأخذ دقائق…")
    run(["docker", "rm", "-f", CONTAINER], 120)
    width, height, dpi = SCREEN
    done = subprocess.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped", "--privileged",
                           "-v", f"{DATA}:/data", "-p", "127.0.0.1:5555:5555", image,
                           f"androidboot.redroid_width={width}", f"androidboot.redroid_height={height}", f"androidboot.redroid_dpi={dpi}",
                           "androidboot.redroid_gpu_mode=guest", "androidboot.use_memfd=true"],
                          capture_output=True, text=True, timeout=1800)
    if done.returncode != 0:
        stop("لم يبدأ أندرويد.", done.stderr)
    started = time.time()
    end = started + 900
    told = 0.0
    while time.time() < end:
        if inside("getprop sys.boot_completed") == "1" and not connect():
            # (the Android is up, adb is not reached yet: a moment more)
            time.sleep(3)
            connect()
        if connect():
            say("   ✓ أندرويد يعمل (الإصدار " + shell("getprop ro.build.version.release", 15) + "، WebView "
                + (re.search(r"versionName=(\S+)", shell("dumpsys package com.android.webview | grep versionName", 20)) or [0, "؟"])[1] + ")")
            return
        state = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], 20).stdout.strip()
        if state != "true":
            stop("توقف أندرويد بعد بدئه.", diagnose())
        if time.time() - told >= 60:
            told = time.time()
            say(f"   … يقلع ({int(time.time() - started)} ث): zygote=" + (inside("getprop init.svc.zygote") or "لم يبدأ")
                + " · system_server=" + ("يعمل" if inside("pidof system_server") else "لم يبدأ"))
        time.sleep(5)
    stop("أندرويد لم يكمل إقلاعه خلال ربع ساعة.", diagnose())


def install_app() -> None:
    name = "groups.apk" if platform.machine() in ("aarch64", "arm64") else "groups-x86_64.apk"
    say(f"٣) تنزيل التطبيق ({name}) وتثبيته…")
    try:
        request = urllib.request.Request(RAW + name, headers={"User-Agent": "gg-android/1.0", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=300) as answer:
            data = answer.read(200 << 20)
    except (urllib.error.URLError, OSError) as exc:
        stop("تعذّر تنزيل التطبيق.", str(exc))
    if data[:2] != b"PK" or len(data) < 5_000_000:
        stop("ملف التطبيق وصل ناقصًا.")
    APK.write_bytes(data)
    done = adb("install", "-r", "-g", str(APK), seconds=600)
    if done.returncode != 0 or "Success" not in done.stdout:
        stop("لم يُثبَّت التطبيق.", done.stdout + done.stderr)
    allow_app()
    start_app()
    say(f"   ✓ ثُبّت ({len(data) // 1_000_000} م.ب) وفُتح")


def wait_for_app() -> bool:
    say("٤) انتظار أن يشغّل التطبيق سكربته…")
    end = time.time() + 240
    while time.time() < end:
        if panel_answers() and access_path():
            say("   ✓ السكربت يعمل داخل التطبيق، ولوحته تجيب")
            return True
        time.sleep(4)
    say("   ✗ لم تُجب لوحة التطبيق بعد أربع دقائق. هذا آخر ما كتبه التطبيق في سجله:\n")
    say(app_log(5000))
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="The groups app (قروبات) in an Android on this server.")
    parser.add_argument("--setup", action="store_true", help="install everything, start the Android and the app, print the link")
    parser.add_argument("--link", action="store_true", help="print the door's link")
    parser.add_argument("--log", action="store_true", help="print the app's own log")
    parser.add_argument("--check", action="store_true", help="print what is known of the Android, to send when it does not come up")
    parser.add_argument("--update", action="store_true", help="install the app's newest version")
    parser.add_argument("--restart", action="store_true", help="start the Android and the app afresh")
    parser.add_argument("--new-link", action="store_true", help="a new secret: the old link stops working")
    parser.add_argument("--image", help=f"another Android ({DEFAULT_IMAGE} by default)")
    parser.add_argument("--port", type=int, help=f"another port for the door ({DEFAULT_PORT} by default)")
    parser.add_argument("--host", help="the address in the link, when the one found is wrong")
    args = parser.parse_args()
    data = load_config()
    if not any((args.setup, args.link, args.log, args.check, args.update, args.restart, args.new_link)):
        serve(data)
        return
    if args.log:
        say(app_log())
        return
    if args.check:
        say(diagnose())
        return
    if os.geteuid() != 0:
        raise SystemExit("شغّله بصلاحية root.")
    if args.port:
        data["port"] = args.port
    if args.host:
        data["host"] = args.host.strip()
    if args.image:
        data["image"] = args.image.strip()
    data.setdefault("port", DEFAULT_PORT)
    data.setdefault("image", DEFAULT_IMAGE)
    if args.new_link or not data.get("token"):
        data["token"] = secrets.token_urlsafe(18)
    save_config(data)
    ready = True
    if args.setup:
        install_what_is_needed()
        start_android(data["image"])
        install_app()
        ready = wait_for_app()
    elif args.update:
        if not connect():
            stop("أندرويد لا يعمل. شغّل: python3 gg_android.py --restart")
        install_app()
        ready = wait_for_app()
    elif args.restart:
        run(["docker", "restart", CONTAINER], 300)
        end = time.time() + 300
        while time.time() < end and not connect():
            time.sleep(5)
        allow_app()
        start_app()
        ready = wait_for_app()
    make_certificate()
    opened = open_firewall(int(data["port"]))
    service = install_service()
    say("")
    if not service:
        say("لم يُشغَّل الباب كخدمة هنا. شغّله بنفسك: python3 gg_android.py")
    say("رابط شاشة التطبيق (ومنه زر «اللوحة»):\n")
    say(door_link(data))
    say("\nالمتصفح سيحذّر مرة أن الشهادة غير معروفة: اختر المتابعة (Advanced ثم Proceed)."
        + ("" if opened else f"\nإن لم يفتح الرابط فافتح المنفذ {data['port']} (TCP) في جدار الاستضافة من لوحتها."))
    if not ready:
        say("\nالتطبيق لم يجهز بعد: أرسل ما ظهر أعلاه.")


if __name__ == "__main__":
    main()
