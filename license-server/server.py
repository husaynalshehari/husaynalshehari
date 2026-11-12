#!/usr/bin/env python3
"""لوحة التراخيص: تنشئ رمزاً لكل مستخدم، والتطبيق لا يُفتح إلا به.

التشغيل:
    python server.py                              # للتجربة
    gunicorn -w 2 -b 0.0.0.0:8787 server:app      # على الاستضافة

واجهة التحقق اللي يستدعيها التطبيق:
    POST /api/check   {"code": "...", "device": "...", "device_name": "..."}
    ← {"ok": true, "name": "..."}   أو   {"ok": false, "message": "..."}

كل رمز يرتبط بأول جهاز يفعّله، ولا يشتغل على جهاز ثاني إلا إذا فككت الربط من اللوحة.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template_string, request, session

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "license.db"
SECRET_PATH = DATA / "secret.txt"
ADMIN_PATH = DATA / "admin_password.txt"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
HERE = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8787"))

# عدد البروكسيات أمام التطبيق (Nginx / Cloudflare ...). اتركه 0 إذا ما فيه بروكسي.
TRUSTED_PROXIES = int(os.getenv("TRUSTED_PROXIES", "0"))

API_LIMIT = (30, 60)  # 30 طلب تحقق في الدقيقة لكل IP
LOGIN_LIMIT = (5, 15 * 60)  # 5 محاولات دخول خاطئة للوحة كل ربع ساعة لكل IP


def read_or_create(path: Path, make) -> tuple[str, bool]:
    """يقرأ الملف، أو ينشئه مرة واحدة فقط (حتى لو اشتغلت أكثر من نسخة في نفس اللحظة)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(40):
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value, False
            time.sleep(0.05)
        raise RuntimeError(f"الملف فارغ: {path}")
    value = make()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    return value, True


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or read_or_create(
    SECRET_PATH, lambda: secrets.token_hex(32)
)[0]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE") == "1",
)
if TRUSTED_PROXIES > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXIES, x_proto=TRUSTED_PROXIES)

_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with closing(db()) as conn, conn:
        conn.execute(
            """create table if not exists users (
                id integer primary key,
                name text not null,
                code text not null unique,
                active integer not null default 1,
                created text not null,
                last_seen text,
                last_ip text
            )"""
        )
        # أعمدة جديدة لربط الرمز بالجهاز (تُضاف تلقائياً لقاعدة البيانات القديمة بدون ما تضيع بياناتك)
        columns = {row["name"] for row in conn.execute("pragma table_info(users)")}
        for column in ("device", "device_name"):
            if column not in columns:
                conn.execute(f"alter table users add column {column} text")


def admin_password() -> str:
    env = os.getenv("ADMIN_PASSWORD", "").strip()
    if env:
        return env
    value, made = read_or_create(ADMIN_PATH, lambda: secrets.token_urlsafe(9))
    if made:
        print(f"كلمة دخول اللوحة (احفظها): {value}", flush=True)
    return value


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def norm(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isascii() and ch.isalnum())


def device_key(device: str) -> str:
    return hashlib.sha256(("device:" + device).encode()).hexdigest()


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- الحماية من التخمين


def _recent(key: str, window: float) -> list[float]:
    stamp = time.time()
    if len(_hits) > 10_000:  # تنظيف حتى ما تكبر الذاكرة بلا حد
        for old in [k for k, v in _hits.items() if not v or stamp - v[-1] > 3600]:
            del _hits[old]
    rows = [t for t in _hits.get(key, []) if stamp - t < window]
    _hits[key] = rows
    return rows


def too_many(key: str, limit: tuple[int, int]) -> bool:
    """يسجّل الطلب ويرجع True إذا تجاوز الحد."""
    count, window = limit
    with _hits_lock:
        rows = _recent(key, window)
        rows.append(time.time())
        return len(rows) > count


def is_blocked(key: str, limit: tuple[int, int]) -> bool:
    count, window = limit
    with _hits_lock:
        return len(_recent(key, window)) >= count


def record_failure(key: str) -> None:
    with _hits_lock:
        _hits.setdefault(key, []).append(time.time())


def client_ip() -> str:
    return request.remote_addr or ""


# ---------------------------------------------------------------- جلسة المدير


def admin_fingerprint() -> str:
    # لو غيّرت كلمة المرور، تنتهي كل الجلسات القديمة
    return hmac.new(app.secret_key.encode(), admin_password().encode(), "sha256").hexdigest()[:32]


def signed_in() -> bool:
    saved = session.get("admin")
    return isinstance(saved, str) and hmac.compare_digest(saved, admin_fingerprint())


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method != "POST" or request.path == "/api/check":
        return None
    expected = session.get("csrf", "")
    sent = request.form.get("csrf", "")
    if expected and sent and hmac.compare_digest(expected, sent):
        return None
    flash("انتهت صلاحية الصفحة، حاول مرة أخرى.")
    return redirect("/login" if request.path == "/login" else "/")


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>تراخيص التطبيق</title>
  <style>
    :root { color-scheme: light; --ink:#1c1915; --muted:#6d645b; --line:#e4ddd4; --paper:#f6f3ee; --card:#fff; --pine:#1f6b4a; --pine-soft:#e3efe8; --red:#9a3b32; }
    * { box-sizing: border-box; }
    body { margin:0; font:16px/1.5 "Segoe UI", Tahoma, sans-serif; background:var(--paper); color:var(--ink); }
    main { width:min(980px, calc(100% - 28px)); margin:28px auto 48px; }
    h1 { font-size:26px; margin:0 0 6px; }
    p.sub { color:var(--muted); margin:0 0 18px; }
    form.card, .table-wrap, .new { background:var(--card); border:1px solid var(--line); border-radius:16px; }
    form.card { padding:16px; display:flex; gap:10px; flex-wrap:wrap; align-items:end; margin-bottom:16px; }
    label { display:block; font-size:13px; color:var(--muted); margin-bottom:4px; }
    input { font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:10px; min-width:220px; }
    button { font:inherit; border:0; border-radius:10px; padding:10px 14px; background:var(--pine); color:#fff; cursor:pointer; white-space:nowrap; }
    button.ghost { background:#efeae3; color:var(--ink); }
    button.danger { background:#f6e6e3; color:var(--red); }
    button.small { padding:6px 10px; font-size:14px; }
    .table-wrap { overflow-x:auto; }
    table { width:100%; border-collapse:collapse; min-width:760px; }
    th, td { text-align:right; padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }
    tbody tr:last-child td { border-bottom:0; }
    th { font-size:13px; color:var(--muted); font-weight:600; }
    code { font-size:16px; letter-spacing:.04em; direction:ltr; unicode-bidi:isolate; white-space:nowrap; }
    .muted { color:var(--muted); font-size:13px; }
    .off { color:var(--red); }
    .on { color:var(--pine); }
    .row { display:flex; gap:6px; flex-wrap:wrap; }
    .row form { margin:0; }
    .top { display:flex; justify-content:space-between; align-items:center; gap:12px; }
    .flash { background:#f6e6e3; color:var(--red); border-radius:10px; padding:10px 14px; margin-bottom:14px; }
    .new { border-color:var(--pine); background:var(--pine-soft); padding:16px; margin-bottom:16px; display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; }
    .new code { font-size:24px; font-weight:700; letter-spacing:.08em; background:#fff; border:1px dashed var(--pine); border-radius:10px; padding:4px 12px; }
    tr.hl td { background:var(--pine-soft); }
    @media (max-width:600px) { input { min-width:0; width:100%; } form.card > div { flex:1 1 100%; } h1 { font-size:22px; } }
  </style>
</head>
<body>
<main>
  <div class="top">
    <div>
      <h1>تراخيص التطبيق</h1>
      <p class="sub">أنشئ مستخدماً، انسخ الرمز، وأرسله له. لا يُفتح التطبيق إلا برمز فعّال، ويرتبط الرمز بأول جهاز يُفعَّل عليه.</p>
    </div>
    <form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="ghost" type="submit">خروج</button></form>
  </div>

  {% for message in get_flashed_messages() %}<div class="flash">{{ message }}</div>{% endfor %}

  {% if new %}
  <div class="new">
    <div>
      <div class="muted">رمز «{{ new.name }}» — انسخه وأرسله له:</div>
      <code id="code-new">{{ new.code }}</code>
    </div>
    <button type="button" data-copy="code-new">نسخ الرمز</button>
  </div>
  {% endif %}

  <form class="card" method="post" action="/users">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <div>
      <label for="name">اسم المستخدم</label>
      <input id="name" name="name" required maxlength="80" placeholder="مثال: محمد">
    </div>
    <button type="submit">إنشاء رمز</button>
  </form>

  <div class="table-wrap">
  <table>
    <thead><tr><th>الاسم</th><th>الرمز</th><th>الحالة</th><th>الجهاز</th><th>آخر دخول</th><th></th></tr></thead>
    <tbody>
    {% for user in users %}
      <tr class="{{ 'hl' if new and new.id == user.id else '' }}">
        <td>{{ user.name }}</td>
        <td>
          <code id="code-{{ user.id }}">{{ user.code }}</code>
          <button class="ghost small" type="button" data-copy="code-{{ user.id }}">نسخ</button>
        </td>
        <td class="{{ 'on' if user.active else 'off' }}">{{ 'فعّال' if user.active else 'موقوف' }}</td>
        <td>
          {% if user.device %}{{ user.device_name or 'جهاز' }}{% else %}<span class="muted">لم يُفعَّل بعد</span>{% endif %}
        </td>
        <td>{{ user.last_seen or '—' }}<br><span class="muted">{{ user.last_ip or '' }}</span></td>
        <td>
          <div class="row">
            {% if user.active %}
            <form method="post" action="/users/{{ user.id }}/off"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="ghost small" type="submit">إيقاف</button></form>
            {% else %}
            <form method="post" action="/users/{{ user.id }}/on"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="ghost small" type="submit">تفعيل</button></form>
            {% endif %}
            {% if user.device %}
            <form method="post" action="/users/{{ user.id }}/unbind" data-confirm="فك ربط الجهاز؟ سيقدر «{{ user.name }}» يفعّل الرمز على جهاز جديد.">
              <input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="ghost small" type="submit">فك ربط الجهاز</button></form>
            {% endif %}
            <form method="post" action="/users/{{ user.id }}/newcode" data-confirm="إنشاء رمز جديد لـ «{{ user.name }}»؟ الرمز القديم سيتوقف فوراً.">
              <input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="ghost small" type="submit">رمز جديد</button></form>
            <form method="post" action="/users/{{ user.id }}/delete" data-confirm="حذف «{{ user.name }}» نهائياً؟">
              <input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="danger small" type="submit">حذف</button></form>
          </div>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="6">لا يوجد مستخدمون بعد.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</main>
<script>
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = document.getElementById(btn.dataset.copy).textContent.trim();
      var label = btn.textContent;
      var done = function () { btn.textContent = "تم ✓"; setTimeout(function () { btn.textContent = label; }, 1400); };
      if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(done); }
      else { window.prompt("انسخ الرمز:", text); }
    });
  });
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) { if (!confirm(form.dataset.confirm)) e.preventDefault(); });
  });
</script>
</body>
</html>
"""

LOGIN = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>دخول اللوحة</title>
  <style>
    body { margin:0; min-height:100vh; display:grid; place-items:center; background:#f6f3ee; font:16px/1.5 "Segoe UI", Tahoma, sans-serif; }
    form { width:min(420px, calc(100% - 28px)); background:#fff; border:1px solid #e4ddd4; border-radius:16px; padding:22px; }
    h1 { margin:0 0 12px; font-size:22px; }
    input { width:100%; font:inherit; padding:10px 12px; border:1px solid #e4ddd4; border-radius:10px; }
    button { margin-top:12px; font:inherit; border:0; border-radius:10px; padding:10px 14px; background:#1f6b4a; color:#fff; cursor:pointer; }
    .err { color:#9a3b32; min-height:1.2em; }
  </style>
</head>
<body>
  <form method="post">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <h1>لوحة التراخيص</h1>
    <p class="err">{{ error }}{% for message in get_flashed_messages() %}{{ message }}{% endfor %}</p>
    <input name="password" type="password" required autofocus placeholder="كلمة المرور" autocomplete="current-password">
    <button type="submit">دخول</button>
  </form>
</body>
</html>
"""


# ---------------------------------------------------------------- اللوحة


@app.get("/")
def home():
    if not signed_in():
        return redirect("/login")
    with closing(db()) as conn:
        users = conn.execute("select * from users order by id desc").fetchall()
    new = None
    new_id = session.pop("new", None)
    if new_id is not None:
        new = next((u for u in users if u["id"] == new_id), None)
    return render_template_string(PAGE, users=users, new=new)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        key = "login:" + client_ip()
        given = request.form.get("password") or ""
        if is_blocked(key, LOGIN_LIMIT):
            error = "محاولات خاطئة كثيرة. انتظر ربع ساعة."
        elif hmac.compare_digest(given.encode(), admin_password().encode()):
            session.clear()
            session["admin"] = admin_fingerprint()
            return redirect("/")
        else:
            record_failure(key)
            error = "كلمة المرور غير صحيحة."
    return render_template_string(LOGIN, error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")


def insert_with_new_code(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    """ينفّذ الأمر برمز جديد، ويعيد المحاولة في الحالة النادرة جداً لو تكرر الرمز."""
    for _ in range(5):
        try:
            return conn.execute(sql, (new_code(), *params)).lastrowid
        except sqlite3.IntegrityError:
            continue
    abort(500)


@app.post("/users")
def add_user():
    if not signed_in():
        abort(401)
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect("/")
    with closing(db()) as conn, conn:
        user_id = insert_with_new_code(
            conn, "insert into users(code, name, created) values (?, ?, ?)", (name[:80], now())
        )
    session["new"] = user_id
    return redirect("/")


@app.post("/users/<int:user_id>/<action>")
def change_user(user_id: int, action: str):
    if not signed_in():
        abort(401)
    with closing(db()) as conn, conn:
        if action == "off":
            conn.execute("update users set active=0 where id=?", (user_id,))
        elif action == "on":
            conn.execute("update users set active=1 where id=?", (user_id,))
        elif action == "unbind":
            conn.execute("update users set device=null, device_name=null where id=?", (user_id,))
        elif action == "newcode":
            insert_with_new_code(
                conn, "update users set code=?, device=null, device_name=null where id=?", (user_id,)
            )
            session["new"] = user_id
        elif action == "delete":
            conn.execute("delete from users where id=?", (user_id,))
        else:
            abort(404)
    return redirect("/")


# ---------------------------------------------------------------- واجهة التطبيق


@app.post("/api/check")
def check():
    ip = client_ip()
    if too_many("api:" + ip, API_LIMIT):
        return {"ok": False, "message": "محاولات كثيرة. انتظر دقيقة."}, 429
    data = request.get_json(silent=True) or {}
    wanted = norm(str(data.get("code") or ""))
    device = str(data.get("device") or "").strip()[:200]
    device_name = str(data.get("device_name") or "").strip()[:80]
    if len(wanted) != 12:
        return {"ok": False, "message": "الرمز غير صحيح."}
    code = f"{wanted[0:4]}-{wanted[4:8]}-{wanted[8:12]}"

    with closing(db()) as conn, conn:
        match = conn.execute("select * from users where code=?", (code,)).fetchone()
        if match is None or not match["active"]:
            return {"ok": False, "message": "الرمز غير مقبول."}

        bound = match["device"]
        if not bound and device:
            # أول تفعيل: نربط الرمز بهذا الجهاز
            conn.execute(
                "update users set device=?, device_name=? where id=? and device is null",
                (device_key(device), device_name or None, match["id"]),
            )
            bound = conn.execute("select device from users where id=?", (match["id"],)).fetchone()[0]
        if bound and not (device and hmac.compare_digest(bound, device_key(device))):
            return {"ok": False, "message": "هذا الرمز مفعّل على جهاز آخر. تواصل مع صاحب التطبيق."}

        conn.execute(
            "update users set last_seen=?, last_ip=? where id=?",
            (now(), ip[:80], match["id"]),
        )
        return {"ok": True, "name": match["name"]}


init()
admin_password()  # تُطبع كلمة مرور اللوحة أول مرة فقط

if __name__ == "__main__":
    print(f"اللوحة: http://{HERE}:{PORT}/", flush=True)
    app.run(host=HERE, port=PORT, threaded=True)
