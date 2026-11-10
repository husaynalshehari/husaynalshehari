"""The app's side of running the groups script. The app (Engine.java) calls main() once, in a thread of its own:
the script runs there as it runs on a server (python gg.py), with its page on 127.0.0.1 for the app's own screen
and a stand-in for Playwright (the playwright package beside this file) that drives the app's browser. What the
script prints is also kept here, for «السجل» in the app."""
from __future__ import annotations

import collections
import json
import os
import runpy
import sys
import threading
import time
import traceback
import urllib.request

# Where «تحديث السكربت» takes the script's newest files from.
RAW = "https://raw.githubusercontent.com/husaynalshehari/husaynalshehari/ccr-695f6c04-mlgxcr"
STATE = {"phase": "idle", "error": "", "folder": ""}
# On a computer (the tests) what is printed is kept here; in the app it goes to the app's own log (AppLog.java),
# a file that is still there after a crash, shown by «السجل».
LOG: collections.deque = collections.deque(maxlen=3000)
_log_lock = threading.Lock()
_app_log = None


def _log():
    """The app's log (app.tg.groups.AppLog), or False where there is none (a computer)."""
    global _app_log
    if _app_log is None:
        try:
            from java import jclass
            _app_log = jclass("app.tg.groups.AppLog")
        except Exception:
            _app_log = False
    return _app_log


def _note(line: str) -> None:
    sink = _log()
    if sink:
        try:
            sink.add(line)
            return
        except Exception:
            pass
    with _log_lock:
        LOG.append(time.strftime("%H:%M:%S ") + line)


class _Tee:
    """What is printed goes where it went (Android's own log) and, a line at a time, to the app's log. A line
    that starts an error's trace is marked ✗, as every error in that log is."""

    def __init__(self, stream):
        self._stream = stream
        self._rest = ""
        self._lock = threading.Lock()

    def write(self, text):
        if not isinstance(text, str):          # (as a text stream refuses bytes: what asks takes it for one)
            raise TypeError("write() argument must be str, not " + type(text).__name__)
        try:
            self._stream.write(text)
        except Exception:
            pass
        with self._lock:
            self._rest += text
            *lines, self._rest = self._rest.split("\n")
        for line in lines:
            if line.startswith(("Traceback (most recent call last)", "Exception in thread")) or " ERROR in " in line:
                line = "✗ " + line
            _note(line)
        return len(text)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _keep_output() -> None:
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr)


def main(folder: str, port: int, access: str) -> None:
    """Run the groups script (folder/gg.py) until the app ends. Does not return while the script runs."""
    _keep_output()
    folder = str(folder)
    script = os.path.join(folder, "gg.py")
    os.environ["PORT"] = str(int(port))
    os.environ["BIND_HOST"] = "127.0.0.1"
    os.environ["X_ACCESS_PATH"] = str(access)
    os.environ["GG_IN_APP"] = "1"                 # (the script clicks without scrolling the page: see IN_APP there)
    os.chdir(folder)
    sys.argv = [script]
    try:
        threading.stack_size(4 * 1024 * 1024)      # (room for the script's own threads: Android's are small)
    except (ValueError, RuntimeError):
        pass
    STATE.update(phase="running", error="", folder=folder)
    try:
        print(f"تشغيل السكربت {script} على المنفذ {port} (Python {sys.version.split()[0]})", flush=True)
        runpy.run_path(script, run_name="__main__")
        STATE.update(phase="stopped", error="توقف السكربت.")
    except SystemExit as exc:
        STATE.update(phase="stopped", error=f"توقف السكربت ({exc.code}).")
    except BaseException:
        detail = traceback.format_exc()
        print("✗ توقف السكربت بخطأ:\n" + detail, flush=True)
        STATE.update(phase="failed", error=detail[-3000:])


def state() -> str:
    return json.dumps(STATE, ensure_ascii=False)


def log_text(lines: int = 500) -> str:
    """The newest part of the log: the app's own where there is one, else what was printed here."""
    sink = _log()
    if sink:
        return str(sink.tail(int(lines) * 120)) or "لا شيء في السجل بعد."
    with _log_lock:
        rows = list(LOG)[-int(lines):]
    return "\n".join(rows) if rows else "لا شيء في السجل بعد."


def device_line() -> str:
    """This phone, in one line (it heads the check's report)."""
    try:
        from java import jclass
        build = jclass("android.os.Build")
        version = jclass("android.os.Build$VERSION")
        package = jclass("android.webkit.WebView").getCurrentWebViewPackage()
        host = jclass("app.tg.groups.BrowserHost")
        return (f"الجهاز: {build.MANUFACTURER} {build.MODEL} · أندرويد {version.RELEASE} · "
                f"WebView {package.versionName if package is not None else '؟'} · "
                f"ملف مستقل لكل حساب: {'نعم' if bool(host.profilesSupported()) else 'لا'} · Python {sys.version.split()[0]}")
    except Exception as exc:
        return f"الجهاز: غير معروف ({type(exc).__name__}) · Python {sys.version.split()[0]}"


def check(online, listener=None) -> str:
    """«فحص المتصفح»: the report. listener.add(line), when given (the app's page), gets each line as it comes."""
    _keep_output()
    import ggcheck

    def say(line: str) -> None:
        if listener is not None:
            listener.add(str(line))

    head = device_line()
    say(head)
    say("")
    folder = STATE.get("folder") or os.getcwd()
    try:
        report = ggcheck.run(profile_root=os.path.join(folder, "check"), online=bool(online), say=say)
    except BaseException:
        report = "✗ تعذّر تشغيل الفحص:\n" + traceback.format_exc()[-1500:]
        say(report)
    print("فحص المتصفح:\n" + head + "\n" + report, flush=True)
    return head + "\n\n" + report


def update_script() -> str:
    """Take the script's newest files from its repository, in place of the ones here. They are used the next time
    the app starts. Nothing is replaced unless both arrive whole and are Python that compiles."""
    folder = STATE.get("folder") or os.getcwd()
    fetched = []
    try:
        # (the app's own script, groups-app/gg_app.py: never the servers' script_original.py)
        for name, target in (("groups-app/gg_app.py", "gg.py"), ("content.py", "content.py")):
            request = urllib.request.Request(f"{RAW}/{name}", headers={"User-Agent": "tg-groups-app/1.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(request, timeout=60) as answer:
                body = answer.read(8 << 20)
            text = body.decode("utf-8")
            if len(text) < 20_000 or "flask" not in text:
                return f"لم يكتمل التحديث: ملف {name} وصل ناقصًا."
            compile(text, target, "exec")
            fetched.append((target, body))
    except SyntaxError as exc:
        return f"لم يكتمل التحديث: الملف الجديد فيه خطأ ({exc.msg})."
    except Exception as exc:
        return f"تعذّر التحديث: {type(exc).__name__}: {str(exc)[:160]}"
    same = True
    for target, body in fetched:
        path = os.path.join(folder, target)
        try:
            with open(path, "rb") as handle:
                same = same and handle.read() == body
        except OSError:
            same = False
    if same:
        return "السكربت محدَّث: لا يوجد أحدث منه."
    for target, body in fetched:
        path = os.path.join(folder, target)
        with open(path + ".new", "wb") as handle:
            handle.write(body)
        os.replace(path + ".new", path)
    return "OK"
