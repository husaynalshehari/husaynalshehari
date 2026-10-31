#!/usr/bin/env python3
"""«توزيع البرنامج»: the owner's control for handing the group script (gg.py) to users and serving
them updates. It runs beside the script, as its own program on its own port:

    python3 g.py        (port 5050, or the one in CONTROL_PORT)

Users' copies take updates from here with their own key. This page needs the owner's password,
and the same password guards the script's panel: the script reads it from data/distribution.json.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, jsonify, make_response, render_template_string, request, session

# The program handed out: the script beside this file (another name goes in PROGRAM_FILE).
PROGRAM_PATH = Path(__file__).resolve().with_name(os.getenv("PROGRAM_FILE", "gg.py"))
DATA_DIR = Path(__file__).resolve().with_name("data")
STORE_PATH = DATA_DIR / "distribution.json"
ATTEMPT_WINDOW_SECONDS = 300
MAX_ATTEMPTS_PER_CLIENT = 5

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("X_APP_SECURE_COOKIE", "0") == "1",
    MAX_CONTENT_LENGTH=8 * 1024,
)
store_lock = threading.Lock()
store: dict = {"password_hash": "", "password_salt": "", "address": "", "users": []}
attempts: dict[str, deque[float]] = defaultdict(deque)
attempts_lock = threading.Lock()


def load_store() -> None:
    try:
        loaded = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    users = [
        {"id": str(row["id"]), "name": str(row.get("name") or ""), "key": str(row["key"]), "active": bool(row.get("active", True))}
        for row in loaded.get("users") or []
        if isinstance(row, dict) and row.get("id") and row.get("key")
    ]
    with store_lock:
        store.update({
            "password_hash": str(loaded.get("password_hash") or ""),
            "password_salt": str(loaded.get("password_salt") or ""),
            "address": str(loaded.get("address") or ""),
            "users": users,
        })


def save_store_unlocked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STORE_PATH.with_suffix(STORE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, **store}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(STORE_PATH)


def password_digest(password: str, salt: str) -> str:
    # The script checks its panel password the same way.
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def allow_attempt(client: str) -> bool:
    now = time.monotonic()
    with attempts_lock:
        q = attempts[client]
        while q and now - q[0] > ATTEMPT_WINDOW_SECONDS:
            q.popleft()
        if len(q) >= MAX_ATTEMPTS_PER_CLIENT:
            return False
        q.append(now)
        return True


def logged_in() -> bool:
    with store_lock:
        mark = store["password_hash"][:16]
    return bool(mark) and session.get("control_ok") == mark


def program_version() -> str:
    try:
        text = PROGRAM_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^APP_VERSION = "([0-9][0-9.]*)"', text, re.M)
    return match.group(1) if match else ""


def program_for(user_key: str, address: str) -> bytes:
    """The program as a user gets it: the script with this address and the user's key filled in."""
    text = PROGRAM_PATH.read_text(encoding="utf-8")
    text = re.sub(r'^UPDATE_SOURCE = ".*"$', lambda _: f"UPDATE_SOURCE = {json.dumps(address)}", text, count=1, flags=re.M)
    text = re.sub(r'^UPDATE_KEY = ".*"$', lambda _: f"UPDATE_KEY = {json.dumps(user_key)}", text, count=1, flags=re.M)
    return text.encode("utf-8")


def state_payload() -> dict:
    with store_lock:
        return {
            "success": True,
            "password_set": bool(store["password_hash"]),
            "address": store["address"],
            "users": [{"id": row["id"], "name": row["name"], "active": row["active"]} for row in store["users"]],
            "version": program_version(),
            "program": PROGRAM_PATH.name,
        }


@app.after_request
def secure_response(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    return response


@app.get("/healthz")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/")
def control_page():
    with store_lock:
        has_password = bool(store["password_hash"])
    if has_password and not logged_in():
        return render_template_string(LOGIN_PAGE)
    csrf_token = session.get("csrf_token")
    if not csrf_token:
        csrf_token = session["csrf_token"] = secrets.token_urlsafe(32)
    return render_template_string(PAGE, csrf_token=csrf_token)


@app.post("/login")
def login():
    if not allow_attempt("login:" + (request.remote_addr or "unknown")):
        return jsonify(success=False, message="محاولات كثيرة. انتظر بضع دقائق ثم أعد المحاولة."), 429
    password = str((request.get_json(silent=True) or {}).get("password") or "")
    with store_lock:
        expected, salt = store["password_hash"], store["password_salt"]
    if not expected or not hmac.compare_digest(password_digest(password, salt), expected):
        return jsonify(success=False, message="كلمة السر غير صحيحة."), 403
    session["control_ok"] = expected[:16]
    session.permanent = True
    return jsonify(success=True)


@app.get("/api")
def control_state():
    with store_lock:
        first = not store["password_hash"]
    if not first and not logged_in():
        return jsonify(success=False, message="سجّل الدخول."), 401
    return jsonify(state_payload())


@app.post("/api")
def control_change():
    """Password, address, and adding, stopping or deleting users. Before the first password is
    set, only the password can be set."""
    data = request.get_json(silent=True) or {}
    expected_csrf = session.get("csrf_token", "")
    if not expected_csrf or not hmac.compare_digest(str(data.get("csrf_token") or ""), expected_csrf):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها."), 400
    action = str(data.get("action") or "")
    with store_lock:
        first = not store["password_hash"]
    if not first and not logged_in():
        return jsonify(success=False, message="سجّل الدخول."), 401
    with store_lock:
        if action == "password":
            password = str(data.get("password") or "")
            if len(password) < 8:
                return jsonify(success=False, message="كلمة السر ٨ أحرف على الأقل."), 400
            salt = secrets.token_hex(16)
            store.update(password_salt=salt, password_hash=password_digest(password, salt))
            save_store_unlocked()
            session["control_ok"] = store["password_hash"][:16]
            session.permanent = True
        elif first:
            return jsonify(success=False, message="اختر كلمة سر أولًا."), 400
        elif action == "address":
            address = str(data.get("address") or "").strip().rstrip("/")
            if not re.fullmatch(r"https?://[^\s\"'/]+(/[^\s\"']*)?", address):
                return jsonify(success=False, message="اكتب العنوان كاملًا، مثل https://example.com:5050"), 400
            store["address"] = address
            save_store_unlocked()
        elif action == "add":
            name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()[:40]
            if not name:
                return jsonify(success=False, message="اكتب اسم المستخدم."), 400
            store["users"].append({"id": secrets.token_hex(6), "name": name, "key": secrets.token_urlsafe(24), "active": True})
            save_store_unlocked()
        elif action in ("stop", "start", "delete"):
            user_id = str(data.get("id") or "")
            if not any(row["id"] == user_id for row in store["users"]):
                return jsonify(success=False, message="لم يعد هذا المستخدم موجودًا."), 404
            if action == "delete":
                store["users"] = [row for row in store["users"] if row["id"] != user_id]
            else:
                for row in store["users"]:
                    if row["id"] == user_id:
                        row["active"] = action == "start"
            save_store_unlocked()
        else:
            return jsonify(success=False, message="طلب غير معروف."), 400
    return jsonify(state_payload())


@app.get("/users/<user_id>/program")
def user_program(user_id: str):
    """The program file to give this user: this address and the user's key are already inside."""
    if not logged_in():
        return jsonify(success=False, message="سجّل الدخول."), 401
    with store_lock:
        user = next((dict(row) for row in store["users"] if row["id"] == user_id), None)
        address = store["address"]
    if not user or not address:
        return jsonify(success=False, message="المستخدم أو العنوان غير موجود."), 404
    try:
        body = program_for(user["key"], address)
    except OSError:
        return jsonify(success=False, message=f"لم يُعثر على {PROGRAM_PATH.name} بجانب هذا الملف."), 503
    response = make_response(body)
    response.headers["Content-Type"] = "text/x-python; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{PROGRAM_PATH.name}"'
    return response


def update_user() -> tuple[dict | None, tuple | None]:
    """The user's copy asking for updates, or the answer refusing it."""
    key = (request.headers.get("X-Update-Key") or "").strip()
    with store_lock:
        ready = bool(store["password_hash"] and store["address"])
        user = next((dict(row) for row in store["users"] if key and hmac.compare_digest(row["key"], key)), None)
        address = store["address"]
    if not ready:
        return None, (jsonify(success=False, message="التحديثات غير متاحة الآن."), 404)
    if not user:
        if not allow_attempt("update:" + (request.remote_addr or "unknown")):
            return None, (jsonify(success=False, message="محاولات كثيرة. انتظر بضع دقائق."), 429)
        return None, (jsonify(success=False, message="مفتاح التحديث غير صحيح."), 403)
    if not user["active"]:
        return None, (jsonify(success=False, message="أوقف صاحب البرنامج التحديثات لهذه النسخة."), 403)
    user["address"] = address
    return user, None


@app.get("/update/latest")
def update_latest():
    user, refused = update_user()
    if refused:
        return refused
    try:
        body = program_for(user["key"], user["address"])
    except OSError:
        return jsonify(success=False, message="التحديثات غير متاحة الآن."), 503
    version = re.search(r'^APP_VERSION = "([0-9][0-9.]*)"', body.decode("utf-8"), re.M)
    return jsonify(success=True, version=version.group(1) if version else "", sha256=hashlib.sha256(body).hexdigest(), size=len(body))


@app.get("/update/file")
def update_file():
    user, refused = update_user()
    if refused:
        return refused
    try:
        body = program_for(user["key"], user["address"])
    except OSError:
        return jsonify(success=False, message="التحديثات غير متاحة الآن."), 503
    response = make_response(body)
    response.headers["Content-Type"] = "text/x-python; charset=utf-8"
    return response


STYLE = r'''
    :root { --paper: #f3efe6; --sheet: #fffdf8; --ink: #1c1915; --muted: #6e675e; --line: #e4dcd0; --pine: #0f6b50;
      --clay: #8d3a32; --clay-bg: #fbf1ee; --ok-bg: #e7f5ee; --ok-ink: #0d5a40; color-scheme: light; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--paper); color: var(--ink); font-family: "Segoe UI", Tahoma, system-ui, sans-serif; }
    [hidden] { display: none !important; }
    input { width: 100%; min-height: 36px; padding: 0 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--sheet); color: var(--ink); font: inherit; font-size: 16px; }
    input:focus { outline: none; border-color: var(--pine); box-shadow: 0 0 0 3px rgba(15, 107, 80, 0.16); }
    button, a.btn { min-height: 36px; padding: 0 12px; border: 0; border-radius: 8px; background: var(--sheet); color: var(--ink); font: inherit; font-size: 13px; font-weight: 600;
      box-shadow: inset 0 0 0 1px var(--line); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; text-decoration: none; white-space: nowrap; }
'''

LOGIN_PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>توزيع البرنامج</title>
  <style>''' + STYLE + r'''
    body { min-height: 100vh; display: grid; place-items: center; }
    form { width: min(320px, calc(100vw - 32px)); padding: 18px; border-radius: 14px; background: var(--sheet); box-shadow: inset 0 0 0 1px var(--line); }
    h1 { margin: 0 0 10px; font-size: 16px; }
    form button { width: 100%; margin-top: 10px; background: var(--pine); color: #fff; box-shadow: none; }
    p { min-height: 1.4em; margin: 8px 0 0; color: var(--clay); font-size: 13px; }
  </style>
</head>
<body>
  <form id="login">
    <h1>توزيع البرنامج</h1>
    <input id="password" type="password" autocomplete="current-password" required autofocus aria-label="كلمة السر">
    <button type="submit">دخول</button>
    <p id="message" role="status"></p>
  </form>
  <script>
    document.getElementById("login").addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = document.getElementById("message");
      message.textContent = "";
      try {
        const response = await fetch("/login", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({password: document.getElementById("password").value})
        });
        const data = await response.json();
        if (data.success) {
          location.reload();
          return;
        }
        message.textContent = data.message || "تعذّر الدخول.";
      } catch (_) {
        message.textContent = "تعذّر الاتصال.";
      }
    });
  </script>
</body>
</html>
'''

PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>توزيع البرنامج</title>
  <style>''' + STYLE + r'''
    main { width: min(560px, 100%); margin: 0 auto; padding: 18px 16px 32px; }
    h1 { margin: 0; font-size: 18px; }
    .sub { margin: 4px 0 12px; color: var(--muted); font-size: 13px; line-height: 1.6; }
    .sheet { margin-top: 10px; padding: 10px 12px 12px; border-radius: 14px; background: var(--sheet); box-shadow: inset 0 0 0 1px var(--line); }
    label { display: block; margin: 8px 0 4px; font-size: 12px; font-weight: 600; }
    .row { display: flex; gap: 6px; }
    .row input { flex: 1; min-width: 0; }
    .hint { margin: 6px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .notice { margin-top: 10px; padding: 10px 12px; border-radius: 12px; font-size: 13px; line-height: 1.55; }
    .notice.is-ok { color: var(--ok-ink); background: var(--ok-bg); }
    .notice.is-bad { color: var(--clay); background: var(--clay-bg); }
    #address { direction: ltr; text-align: start; }
    ul { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
    li { display: flex; gap: 6px; align-items: stretch; }
    .who { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; padding: 6px 10px; border-radius: 10px; background: var(--paper);
      box-shadow: inset 0 0 0 1px var(--line); font-size: 13px; }
    .who b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .who span { color: var(--muted); font-size: 11px; }
    .who.is-off span { color: var(--clay); }
    li .drop { color: var(--clay); }
  </style>
</head>
<body>
  <main>
    <h1>توزيع البرنامج</h1>
    <p class="sub">الإصدار المنشور <b id="version"></b> من <span id="program"></span>. لنشر تحديث ارفع رقم APP_VERSION في ملف البرنامج، فيظهر زر «تحديث» عند المستخدمين.</p>
    <div id="notice" class="notice" role="status" hidden></div>
    <section class="sheet">
      <label for="password">كلمة السر</label>
      <div class="row">
        <input id="password" type="password" autocomplete="new-password" placeholder="٨ أحرف على الأقل">
        <button id="password-save" type="button">حفظ</button>
      </div>
      <p id="password-hint" class="hint"></p>
    </section>
    <section id="users-part" class="sheet" hidden>
      <label for="address">عنوان هذه الصفحة كما يصل إليه المستخدمون</label>
      <div class="row">
        <input id="address" type="text" inputmode="url" spellcheck="false" placeholder="https://example.com:5050">
        <button id="address-save" type="button">حفظ</button>
      </div>
      <label for="name">مستخدم جديد</label>
      <div class="row">
        <input id="name" type="text" maxlength="40" placeholder="الاسم">
        <button id="add" type="button">إضافة</button>
      </div>
      <ul id="users"></ul>
    </section>
  </main>
  <script>
  (() => {
    const csrf = {{ csrf_token|tojson }};
    const notice = document.getElementById("notice");
    const password = document.getElementById("password");
    const address = document.getElementById("address");
    const name = document.getElementById("name");
    const users = document.getElementById("users");
    let savedAddress = "";

    function showNotice(text, ok) {
      notice.textContent = text;
      notice.className = "notice " + (ok ? "is-ok" : "is-bad");
      notice.hidden = !text;
    }

    function button(label, className, onClick) {
      const node = document.createElement("button");
      node.type = "button";
      node.className = className;
      node.textContent = label;
      node.addEventListener("click", async () => {
        node.disabled = true;
        try {
          await onClick();
        } finally {
          node.disabled = false;
        }
      });
      return node;
    }

    function render(data) {
      document.getElementById("version").textContent = data.version || "؟";
      document.getElementById("program").textContent = data.program;
      if (!data.version) showNotice("لم يُعثر على " + data.program + " بجانب هذا الملف.", false);
      document.getElementById("password-hint").textContent = data.password_set
        ? "محفوظة. اكتب كلمة جديدة لتغييرها. هي نفسها كلمة سر لوحة السكربت."
        : "اخترها أولًا: تحمي هذه الصفحة ولوحة السكربت، لأن المستخدمين سيعرفون عنوان استضافتك.";
      document.getElementById("users-part").hidden = !data.password_set;
      savedAddress = data.address || "";
      if (document.activeElement !== address) address.value = savedAddress || location.origin;
      users.replaceChildren();
      (data.users || []).forEach((row) => {
        const item = document.createElement("li");
        const who = document.createElement("div");
        who.className = "who" + (row.active ? "" : " is-off");
        const title = document.createElement("b");
        title.textContent = row.name;
        const status = document.createElement("span");
        status.textContent = row.active ? "فعّال" : "موقوف";
        who.append(title, status);
        const download = document.createElement("a");
        download.className = "btn";
        download.textContent = "تنزيل";
        download.href = "/users/" + encodeURIComponent(row.id) + "/program";
        download.setAttribute("download", "");
        const toggle = button(row.active ? "إيقاف" : "تفعيل", "", () => change(
          {action: row.active ? "stop" : "start", id: row.id},
          row.active ? "أُوقفت تحديثات " + row.name + "." : "عادت تحديثات " + row.name + "."
        ));
        const remove = button("حذف", "drop", async () => {
          if (!window.confirm("حذف " + row.name + "؟ تتوقف تحديثات نسخته.")) return;
          await change({action: "delete", id: row.id}, "حُذف " + row.name + ".");
        });
        item.append(who, download, toggle, remove);
        users.append(item);
      });
    }

    async function change(payload, okMessage) {
      let data;
      try {
        const response = await fetch("/api", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          cache: "no-store",
          body: JSON.stringify({...payload, csrf_token: csrf})
        });
        if (response.status === 401) location.reload();
        data = await response.json();
      } catch (_) {
        data = {message: "تعذّر الاتصال."};
      }
      if (!data.success) {
        showNotice(data.message || "تعذّر الحفظ.", false);
        return false;
      }
      render(data);
      if (okMessage) showNotice(okMessage, true);
      return true;
    }

    async function load() {
      try {
        const response = await fetch("/api", {cache: "no-store"});
        if (response.status === 401) location.reload();
        render(await response.json());
      } catch (_) {
        showNotice("تعذّر الاتصال.", false);
      }
    }

    document.getElementById("password-save").addEventListener("click", async () => {
      if (await change({action: "password", password: password.value}, "حُفظت كلمة السر.")) password.value = "";
    });
    document.getElementById("address-save").addEventListener("click", () => {
      change({action: "address", address: address.value.trim()}, "حُفظ العنوان.");
    });
    document.getElementById("add").addEventListener("click", async () => {
      // The address goes inside the program a user downloads, so it is saved first.
      if (!savedAddress && !(await change({action: "address", address: address.value.trim()}, ""))) return;
      const added = name.value.trim();
      if (await change({action: "add", name: added}, "أُضيف " + added + ". نزّل له البرنامج من «تنزيل».")) name.value = "";
    });
    load();
  })();
  </script>
</body>
</html>
'''


load_store()


if __name__ == "__main__":
    port = int(os.getenv("CONTROL_PORT", "5050"))
    bind_host = os.getenv("BIND_HOST", "0.0.0.0")
    print(f"توزيع البرنامج يعمل على المنفذ {port}: http://127.0.0.1:{port}/", flush=True)
    app.run(host=bind_host, port=port, debug=False, threaded=True)
