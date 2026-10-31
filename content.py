"""«إنشاء محتوى»: its own page inside the script's panel. Kept beside the script, it is added by the
script itself (same address and port), which then shows «إنشاء محتوى» on its page.

First step: the saved account, and the X accounts to work with. «تحقق» opens the account's profile
with the saved account and keeps its username with the account's name.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from flask import abort, jsonify, render_template_string, request, session

# Reads the open profile: the account's name and username, or why there is none yet.
PROFILE_JS = r"""
() => {
  const text = (document.body && document.body.innerText) || "";
  if (/This account doesn.t exist|هذا الحساب غير موجود/i.test(text)) return {state: "missing"};
  if (/Account suspended|حساب معلّق|الحساب معلق|تم تعليق/i.test(text)) return {state: "suspended"};
  const box = document.querySelector('[data-testid="UserName"]');
  if (!box) return {state: "wait"};
  const lines = (box.innerText || "").split("\n").map((line) => line.trim()).filter(Boolean);
  const handle = (lines.find((line) => line.startsWith("@")) || "").slice(1);
  const name = lines.find((line) => !line.startsWith("@")) || "";
  return handle ? {state: "ok", name, handle} : {state: "wait"};
}
"""


def username_from(value: str) -> str:
    """@name, name or a profile link -> name; "" when it is not an X username."""
    raw = (value or "").strip()
    link = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", raw)
    name = link.group(1) if link else raw.lstrip("@")
    return name if re.fullmatch(r"[A-Za-z0-9_]{1,15}", name) else ""


def attach(app, access_path: str, tools) -> None:
    """Add the page and its requests to the script's app, under the panel's own address.

    tools (from the script): data_dir, accounts() -> [{"id", "username", "token"}],
    current_token() -> the account picked on the panel, run_browser(token, work), goto_x(page, url).
    """
    store_path = Path(tools.data_dir) / "content_accounts.json"
    store_lock = threading.Lock()
    # One profile opened at a time: each one starts a browser.
    checking = threading.Lock()

    def read_saved() -> list[dict]:
        try:
            loaded = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = loaded.get("accounts") if isinstance(loaded, dict) else None
        return [row for row in rows or [] if isinstance(row, dict) and row.get("username")]

    def write_saved(rows: list[dict]) -> None:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = store_path.with_suffix(store_path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "accounts": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(store_path)

    def allowed(path_part: str) -> None:
        if not hmac.compare_digest(path_part, access_path):
            abort(404)

    def csrf_ok(data: dict) -> bool:
        expected = session.get("csrf_token", "")
        return bool(expected) and hmac.compare_digest(str(data.get("csrf_token") or ""), expected)

    def state() -> dict:
        current = tools.current_token()
        accounts = tools.accounts()
        picked = next((row["id"] for row in accounts if current and hmac.compare_digest(row["token"], current)), "")
        with store_lock:
            saved = read_saved()
        return {
            "success": True,
            # The tokens stay on the server: the page only picks an account by its id.
            "accounts": [{"id": row["id"], "username": row["username"]} for row in accounts],
            "picked": picked or (accounts[0]["id"] if accounts else ""),
            "saved": saved,
        }

    def read_profile(token: str, username: str) -> dict:
        def work(page):
            tools.goto_x(page, f"https://x.com/{username}")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    found = page.evaluate(PROFILE_JS) or {}
                except Exception:
                    found = {}
                if found.get("state") in ("ok", "missing", "suspended"):
                    return found
                page.wait_for_timeout(500)
            return {"state": "timeout"}
        return tools.run_browser(token, work)

    def content_page(access: str):
        allowed(access)
        csrf_token = session.get("csrf_token")
        if not csrf_token:
            csrf_token = session["csrf_token"] = secrets.token_urlsafe(32)
        return render_template_string(PAGE, csrf_token=csrf_token, api_root="/" + access_path, panel="/" + access_path)

    def content_state(access: str):
        allowed(access)
        return jsonify(state())

    def content_verify(access: str):
        """«تحقق»: open the profile with the chosen saved account, read the account's name, save both."""
        allowed(access)
        data = request.get_json(silent=True) or {}
        if not csrf_ok(data):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        username = username_from(str(data.get("username") or ""))
        if not username:
            return jsonify(success=False, message="اكتب يوزر حساب في X، مثل @example"), 400
        account = next((row for row in tools.accounts() if row["id"] == str(data.get("account") or "")), None)
        if not account:
            return jsonify(success=False, message="اختر الحساب المحفوظ أولًا."), 400
        if not checking.acquire(blocking=False):
            return jsonify(success=False, message="تحقق آخر يعمل الآن. انتظر حتى ينتهي."), 409
        try:
            found = read_profile(account["token"], username)
        except Exception:
            return jsonify(success=False, message="تعذّر فتح الملف الشخصي. أعد المحاولة بعد قليل."), 502
        finally:
            checking.release()
        if found.get("state") == "missing":
            return jsonify(success=False, message=f"لا يوجد حساب باليوزر @{username}."), 404
        if found.get("state") == "suspended":
            return jsonify(success=False, message=f"الحساب @{username} معلّق."), 404
        if found.get("state") != "ok":
            return jsonify(success=False, message="لم يظهر الملف الشخصي. أعد المحاولة بعد قليل."), 502
        handle, name = str(found["handle"]), str(found.get("name") or found["handle"])[:80]
        with store_lock:
            rows = [row for row in read_saved() if str(row["username"]).lower() != handle.lower()]
            rows.append({"username": handle, "name": name, "saved_at": time.time()})
            write_saved(rows)
        return jsonify({**state(), "message": f"تم التحقق وحُفظ: {name} (@{handle})."})

    def content_delete(access: str):
        allowed(access)
        data = request.get_json(silent=True) or {}
        if not csrf_ok(data):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        username = str(data.get("username") or "").lower()
        with store_lock:
            write_saved([row for row in read_saved() if str(row["username"]).lower() != username])
        return jsonify({**state(), "message": "حُذف الحساب من القائمة."})

    app.add_url_rule("/<access>/content", "content_page", content_page, methods=["GET"])
    app.add_url_rule("/<access>/content/state", "content_state", content_state, methods=["GET"])
    app.add_url_rule("/<access>/content/verify", "content_verify", content_verify, methods=["POST"])
    app.add_url_rule("/<access>/content/delete", "content_delete", content_delete, methods=["POST"])


PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>إنشاء محتوى</title>
  <style>
    :root { --paper: #f3efe6; --sheet: #fffdf8; --ink: #1c1915; --muted: #6e675e; --line: #e4dcd0; --pine: #0f6b50; --pine-deep: #0b5340;
      --clay: #8d3a32; --clay-bg: #fbf1ee; --ok-bg: #e7f5ee; --ok-ink: #0d5a40; color-scheme: light; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--paper); color: var(--ink); font-family: "IBM Plex Sans Arabic", "Segoe UI", Tahoma, sans-serif; }
    [hidden] { display: none !important; }
    main { width: min(560px, 100%); margin: 0 auto; padding: 16px 16px 32px; }
    .top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    h1 { margin: 0; font-size: 18px; }
    h2 { margin: 0 0 6px; font-size: 13px; }
    .back { color: var(--pine); font-size: 13px; font-weight: 600; }
    .sheet { margin-top: 10px; padding: 10px 12px 12px; border-radius: 14px; background: var(--sheet); box-shadow: inset 0 0 0 1px var(--line); }
    .row { display: flex; gap: 6px; }
    .row input { flex: 1; min-width: 0; }
    input { width: 100%; min-height: 36px; padding: 0 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--sheet); color: var(--ink);
      font: inherit; font-size: 16px; direction: ltr; text-align: start; }
    input:focus { outline: none; border-color: var(--pine); box-shadow: 0 0 0 3px rgba(15, 107, 80, 0.16); }
    button { min-height: 36px; padding: 0 12px; border: 0; border-radius: 8px; background: var(--sheet); color: var(--ink); font: inherit; font-size: 13px;
      font-weight: 600; box-shadow: inset 0 0 0 1px var(--line); cursor: pointer; white-space: nowrap; }
    button.go { background: var(--pine); color: #fff; box-shadow: none; }
    button:disabled { opacity: 0.6; cursor: default; }
    .accounts { display: flex; flex-wrap: wrap; gap: 6px; }
    .accounts button { direction: ltr; }
    .accounts button.is-on { background: var(--ok-bg); color: var(--ok-ink); box-shadow: inset 0 0 0 1.5px var(--pine); }
    .hint { margin: 6px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .notice { margin-top: 10px; padding: 10px 12px; border-radius: 12px; font-size: 13px; line-height: 1.55; }
    .notice.is-ok { color: var(--ok-ink); background: var(--ok-bg); }
    .notice.is-bad { color: var(--clay); background: var(--clay-bg); }
    ul { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
    li { display: flex; gap: 6px; align-items: stretch; }
    .who { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; padding: 6px 10px; border-radius: 10px;
      background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); }
    .who b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .who span { color: var(--muted); font-size: 12px; direction: ltr; text-align: end; }
    li button { color: var(--clay); }
  </style>
</head>
<body>
  <main>
    <div class="top">
      <h1>إنشاء محتوى</h1>
      <a class="back" href="{{ panel }}">العودة للوحة</a>
    </div>
    <div id="notice" class="notice" role="status" hidden></div>
    <section class="sheet">
      <h2>الحساب</h2>
      <div id="accounts" class="accounts"></div>
      <p id="no-account" class="hint" hidden>لا يوجد حساب محفوظ. تحقق من حسابك في اللوحة أولًا ليُحفظ.</p>
    </section>
    <section class="sheet">
      <h2>الحسابات</h2>
      <div class="row">
        <input id="username" type="text" maxlength="80" autocomplete="off" spellcheck="false" placeholder="@username" aria-label="يوزر الحساب">
        <button id="verify" class="go" type="button">تحقق</button>
      </div>
      <p class="hint">يُفتح الملف الشخصي بالحساب المحفوظ، ويُحفظ اليوزر مع اسم الحساب.</p>
      <ul id="saved"></ul>
    </section>
  </main>
  <script>
  (() => {
    const apiRoot = {{ api_root|tojson }};
    const csrf = {{ csrf_token|tojson }};
    const notice = document.getElementById("notice");
    const accountsBox = document.getElementById("accounts");
    const usernameField = document.getElementById("username");
    const verifyButton = document.getElementById("verify");
    const savedList = document.getElementById("saved");
    let picked = "";

    function showNotice(text, ok) {
      notice.textContent = text;
      notice.className = "notice " + (ok ? "is-ok" : "is-bad");
      notice.hidden = !text;
    }

    function render(data) {
      picked = data.accounts.some((row) => row.id === picked) ? picked : data.picked;
      accountsBox.replaceChildren();
      data.accounts.forEach((row) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = row.id === picked ? "is-on" : "";
        button.textContent = row.username ? "@" + row.username : "حساب غير معروف";
        button.addEventListener("click", () => {
          picked = row.id;
          render(data);
        });
        accountsBox.append(button);
      });
      document.getElementById("no-account").hidden = data.accounts.length > 0;
      verifyButton.disabled = !data.accounts.length;
      savedList.replaceChildren();
      data.saved.slice().reverse().forEach((row) => {
        const item = document.createElement("li");
        const who = document.createElement("div");
        who.className = "who";
        const name = document.createElement("b");
        name.textContent = row.name;
        const handle = document.createElement("span");
        handle.textContent = "@" + row.username;
        who.append(name, handle);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "حذف";
        remove.addEventListener("click", () => send("/content/delete", {username: row.username}));
        item.append(who, remove);
        savedList.append(item);
      });
    }

    async function send(path, payload) {
      let data;
      try {
        const response = await fetch(apiRoot + path, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          cache: "no-store",
          body: JSON.stringify({...payload, csrf_token: csrf})
        });
        data = await response.json();
      } catch (_) {
        data = {message: "تعذّر الاتصال."};
      }
      if (data.accounts) render(data);
      showNotice(data.message || "", Boolean(data.success));
      return Boolean(data.success);
    }

    verifyButton.addEventListener("click", async () => {
      const username = usernameField.value.trim();
      if (!username) {
        showNotice("اكتب يوزر الحساب.", false);
        usernameField.focus();
        return;
      }
      verifyButton.disabled = true;
      showNotice("جارٍ فتح الملف الشخصي…", true);
      try {
        if (await send("/content/verify", {account: picked, username})) usernameField.value = "";
      } finally {
        verifyButton.disabled = false;
      }
    });
    usernameField.addEventListener("keydown", (event) => {
      if (event.key === "Enter") verifyButton.click();
    });

    fetch(apiRoot + "/content/state", {cache: "no-store"})
      .then((response) => response.json())
      .then(render)
      .catch(() => showNotice("تعذّر تحميل الصفحة.", false));
  })();
  </script>
</body>
</html>
'''
