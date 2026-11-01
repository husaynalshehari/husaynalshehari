"""«إنشاء محتوى»: its own page inside the script's panel. Kept beside the script, it is added by the
script itself (same address and port), which then shows «إنشاء محتوى» on its page.

1. The saved account, and the X accounts to work with. «تحقق» opens the account's profile with the
   saved account and keeps its username with the account's name.
2. A click on a kept account: extract its posts of the last days (all of them, the most liked or the
   most viewed; with or without "only those with photos"). Each post's link and photos are kept in
   data/content/<user>/ until that account is extracted again.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import abort, jsonify, render_template_string, request, send_from_directory, session

# «الأكثر إعجابًا» and «الأكثر مشاهدة» keep this many posts of the period.
TOP_POSTS = 10
# A profile is scrolled at most this long, and for at most this many posts.
EXTRACT_SECONDS = 240
EXTRACT_MAX_POSTS = 400
MODES = {"all": "الجميع", "likes": "الأكثر إعجابًا", "views": "الأكثر مشاهدة"}
# A 1×1 picture the profile gets in place of each post photo (see extract_posts).
BLANK_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

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


# The posts on a profile page now: link, date, likes, views, photos (not a quoted post's), and
# whether it is pinned or another account's post shown as a repost.
POSTS_JS = r"""
(handle) => {
  const digits = (text) => String(text || "")
    .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d))
    .replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d))
    .replace(/٬/g, ",").replace(/٫/g, ".");
  // "1,234 Likes", "1.2K", "١٢ ألف" -> a number; null when there is none.
  const count = (raw) => {
    const match = digits(raw).match(/(\d[\d,]*(?:\.\d+)?)\s*(K|M|B|ألف|آلاف|مليون|ملايين|مليار)?/i);
    if (!match) return null;
    let value = parseFloat(match[1].replace(/,/g, ""));
    const unit = (match[2] || "").toLowerCase();
    if (unit === "k" || unit === "ألف" || unit === "آلاف") value *= 1e3;
    else if (unit === "m" || unit === "مليون" || unit === "ملايين") value *= 1e6;
    else if (unit === "b" || unit === "مليار") value *= 1e9;
    return Math.round(value);
  };
  const posts = [];
  for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
    const time = article.querySelector('a[href*="/status/"] time[datetime]');
    if (!time) continue;
    const href = time.closest("a").getAttribute("href") || "";
    const match = href.match(/^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)/);
    if (!match) continue;
    const context = article.querySelector('[data-testid="socialContext"]');
    const like = article.querySelector('[data-testid="like"], [data-testid="unlike"]');
    const views = article.querySelector('a[href$="/analytics"]');
    const text = article.querySelector('[data-testid="tweetText"]');
    // This post's photos, not a quoted post's: those sit in a role="link" box, or link to that post.
    const own = (link) => ((link.getAttribute("href") || "").match(/\/status\/(\d+)\/photo\/\d+/) || [])[1] === match[2];
    const photos = [...new Set([...article.querySelectorAll('[data-testid="tweetPhoto"] img')]
      .filter((img) => /\/media\//.test(img.src) && !img.closest('div[role="link"]'))
      .filter((img) => { const link = img.closest('a[href*="/photo/"]'); return !link || own(link); })
      .map((img) => img.src))];
    posts.push({
      status_id: match[2],
      url: "https://x.com/" + match[1] + "/status/" + match[2],
      date: time.getAttribute("datetime"),
      pinned: /pinned|مثبت/i.test(context ? context.innerText : ""),
      // Shown by a repost: the post is another account's.
      repost: match[1].toLowerCase() !== String(handle).toLowerCase(),
      likes: count(like && (like.getAttribute("aria-label") || like.innerText)) || 0,
      views: count(views && (views.getAttribute("aria-label") || views.innerText)) || 0,
      text: text ? text.innerText.slice(0, 500) : "",
      // The link to the post's own /photo/N is there even when its photo is not.
      has_photos: photos.length > 0 || [...article.querySelectorAll('a[href*="/photo/"]')].some(own),
      photos,
    });
  }
  const body = (document.body && document.body.innerText) || "";
  return {
    posts,
    missing: /This account doesn.t exist|هذا الحساب غير موجود/i.test(body),
    suspended: /Account suspended|حساب معلّق|الحساب معلق|تم تعليق/i.test(body),
  };
}
"""


def when(date: str) -> float:
    try:
        return datetime.fromisoformat(str(date).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


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
    media_root = Path(tools.data_dir) / "content"
    store_lock = threading.Lock()
    # One profile opened at a time (a check or an extraction): each one starts a browser.
    checking = threading.Lock()
    # Each account's last extraction: "running", "done" or "failed", with its message.
    extractions: dict[str, dict] = {}

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

    def folder(username: str) -> Path:
        return media_root / username.lower()

    def read_posts(username: str) -> dict:
        try:
            loaded = json.loads((folder(username) / "posts.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def state() -> dict:
        current = tools.current_token()
        accounts = tools.accounts()
        picked = next((row["id"] for row in accounts if current and hmac.compare_digest(row["token"], current)), "")
        with store_lock:
            saved = [{**row, "kept": len(read_posts(row["username"]).get("posts") or [])} for row in read_saved()]
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
            return jsonify(success=False, message="يعمل الآن تحقق أو استخراج. انتظر حتى ينتهي."), 409
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
            if username_from(username):
                shutil.rmtree(folder(username), ignore_errors=True)
            if extractions.get(username, {}).get("status") != "running":
                extractions.pop(username, None)
        return jsonify({**state(), "message": "حُذف الحساب من القائمة مع منشوراته المحفوظة."})

    def extract_posts(token: str, username: str, days: int, mode: str, photos_only: bool, progress) -> list[dict]:
        """Scroll the profile back to the start of the period and pick its posts; each with its photos
        downloaded (as (file name, bytes) in "files")."""
        since = time.time() - days * 86400

        def photo(route):
            # X takes a photo that fails to load off the page, address and all, and the script's
            # browser blocks photos (to stay fast). So the profile's photos get a blank picture
            # (nothing is downloaded) and keep their addresses. A photo opened as a page of its own,
            # to be saved, goes out as usual.
            try:
                if route.request.resource_type == "image":
                    route.fulfill(status=200, content_type="image/png", body=BLANK_PNG)
                else:
                    route.continue_()
            except Exception:
                pass

        def work(page):
            page.context.route("https://pbs.twimg.com/media/**", photo)
            tools.goto_x(page, f"https://x.com/{username}")
            seen: dict[str, dict] = {}

            def merge(rows: list[dict]) -> int:
                """Add the posts read from the page; returns how many were new."""
                added = 0
                for row in rows:
                    known = seen.get(row["status_id"])
                    if known is None:
                        seen[row["status_id"]] = row
                        added += 1
                        continue
                    # Seen again: a photo shows only once it starts loading, so it may be there now.
                    known["photos"] += [src for src in row["photos"] if src not in known["photos"]]
                    known["has_photos"] = known["has_photos"] or row["has_photos"]
                    known["likes"], known["views"] = max(known["likes"], row["likes"]), max(known["views"], row["views"])
                return added

            quiet = 0
            first_by = time.monotonic() + 20
            end = time.monotonic() + EXTRACT_SECONDS
            while time.monotonic() < end and len(seen) < EXTRACT_MAX_POSTS:
                try:
                    found = page.evaluate(POSTS_JS, username) or {}
                except Exception:
                    found = {}
                if found.get("missing") or found.get("suspended"):
                    raise LookupError("الحساب غير موجود أو معلّق.")
                # A slow timeline is waited for: ending here would replace the kept posts with nothing.
                if not seen and not found.get("posts"):
                    if time.monotonic() > first_by:
                        raise LookupError("لم تظهر أي منشورات في الملف الشخصي.")
                    page.wait_for_timeout(1000)
                    continue
                added = merge(found.get("posts") or [])
                # The account's own posts come newest first; the pinned one and reposts can be older.
                own = [when(row["date"]) for row in seen.values() if not row["repost"] and not row["pinned"]]
                progress(f"جارٍ الاستخراج… وُجد {sum(1 for at in own if at >= since)} منشورًا ضمن المدة.")
                if own and min(own) < since:
                    break
                quiet = 0 if added else quiet + 1
                if quiet >= 6:
                    break
                page.evaluate("() => window.scrollBy(0, Math.round(window.innerHeight * 0.9))")
                page.wait_for_timeout(1500)
            # Once more after a moment, for the photos of the posts that showed last.
            page.wait_for_timeout(1000)
            try:
                merge((page.evaluate(POSTS_JS, username) or {}).get("posts") or [])
            except Exception:
                pass
            posts = sorted(
                (row for row in seen.values() if not row["repost"] and when(row["date"]) >= since),
                key=lambda row: when(row["date"]), reverse=True,
            )
            if photos_only:
                posts = [row for row in posts if row["has_photos"]]
            if mode == "likes":
                posts = sorted(posts, key=lambda row: row["likes"], reverse=True)[:TOP_POSTS]
            elif mode == "views":
                posts = sorted(posts, key=lambda row: row["views"], reverse=True)[:TOP_POSTS]
            total = sum(len(row["photos"]) for row in posts)
            if not total:
                return posts
            # Each photo is opened in a tab of its own: the script's browser does not load the photos
            # inside pages (to stay fast), but a photo opened as the page itself is let through.
            viewer = page.context.new_page()
            done = 0
            try:
                for row in posts:
                    row["files"] = []
                    for number, src in enumerate(row["photos"], 1):
                        progress(f"جارٍ حفظ الصور… {done} من {total}.")
                        done += 1
                        # The photo in its large size, not the small one shown on the profile.
                        url = re.sub(r"name=[A-Za-z0-9]+", "name=large", src)
                        try:
                            answer = viewer.goto(url, wait_until="load", timeout=30_000)
                            body = answer.body() if answer is not None and answer.ok else b""
                        except Exception:
                            continue
                        kind = (answer.headers.get("content-type") or "").split(";")[0].strip()
                        if not body or not kind.startswith("image/"):
                            continue
                        extension = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(kind, "jpg")
                        row["files"].append((f"{row['status_id']}_{number}.{extension}", body))
            finally:
                try:
                    viewer.close()
                except Exception:
                    pass
            return posts

        return tools.run_browser(token, work)

    def save_posts(username: str, days: int, mode: str, photos_only: bool, posts: list[dict]) -> int:
        """Put the new posts in place of the account's kept ones. Returns how many photos were saved."""
        fresh = media_root / f".{username.lower()}.new"
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir(parents=True)
        rows, pictures = [], 0
        for row in posts:
            images = []
            for name, body in row.get("files") or []:
                (fresh / name).write_bytes(body)
                images.append(name)
            pictures += len(images)
            rows.append({**{key: row[key] for key in ("url", "status_id", "date", "likes", "views", "text")}, "images": images})
        kept = {"username": username, "days": days, "mode": mode, "photos_only": photos_only, "extracted_at": time.time(), "posts": rows}
        (fresh / "posts.json").write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(folder(username), ignore_errors=True)
        fresh.rename(folder(username))
        return pictures

    def run_extraction(token: str, username: str, days: int, mode: str, photos_only: bool) -> None:
        key = username.lower()

        def progress(message: str) -> None:
            extractions[key] = {"status": "running", "message": message}

        try:
            posts = extract_posts(token, username, days, mode, photos_only, progress)
            with store_lock:
                # Deleted from the list while it ran: nothing is kept for it.
                if not any(str(row["username"]).lower() == key for row in read_saved()):
                    extractions.pop(key, None)
                    return
                pictures = save_posts(username, days, mode, photos_only, posts)
            nothing = "اكتمل: لا توجد منشورات" + (" فيها صور" if photos_only else "") + f" في آخر {days} أيام."
            message = f"اكتمل: حُفظ {len(posts)} منشورًا و{pictures} صورة." if posts else nothing
            extractions[key] = {"status": "done", "message": message}
        except LookupError as exc:
            extractions[key] = {"status": "failed", "message": f"{exc} بقيت المحفوظات السابقة كما هي."}
        except Exception:
            extractions[key] = {"status": "failed", "message": "تعذّر الاستخراج. بقيت المحفوظات السابقة كما هي."}
        finally:
            checking.release()

    def content_extract(access: str):
        """«استخراج»: start extracting a kept account's posts; the page follows it."""
        allowed(access)
        data = request.get_json(silent=True) or {}
        if not csrf_ok(data):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        username = username_from(str(data.get("username") or ""))
        with store_lock:
            kept = next((row["username"] for row in read_saved() if str(row["username"]).lower() == username.lower()), "") if username else ""
        if not kept:
            return jsonify(success=False, message="هذا الحساب ليس في القائمة."), 404
        try:
            days = int(data.get("days"))
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= 90:
            return jsonify(success=False, message="اكتب عدد الأيام من 1 إلى 90."), 400
        mode = str(data.get("mode") or "")
        photos_only = bool(data.get("photos_only"))
        if mode == "images":  # a page opened before this choice was split in two
            mode, photos_only = "all", True
        if mode not in MODES:
            return jsonify(success=False, message="اختر أي المنشورات تُستخرج."), 400
        account = next((row for row in tools.accounts() if row["id"] == str(data.get("account") or "")), None)
        if not account:
            return jsonify(success=False, message="اختر الحساب المحفوظ أولًا."), 400
        if not checking.acquire(blocking=False):
            return jsonify(success=False, message="يعمل الآن تحقق أو استخراج. انتظر حتى ينتهي."), 409
        extractions[kept.lower()] = {"status": "running", "message": "جارٍ فتح الملف الشخصي…"}
        threading.Thread(target=run_extraction, args=(account["token"], kept, days, mode, photos_only), name="content-extract", daemon=True).start()
        return jsonify(success=True, message="بدأ الاستخراج.", job=extractions[kept.lower()])

    def content_posts(access: str, username: str):
        """An account's kept posts, and how its extraction is going."""
        allowed(access)
        username = username_from(username)
        if not username:
            abort(404)
        return jsonify(success=True, job=extractions.get(username.lower(), {}), kept=read_posts(username))

    def content_media(access: str, username: str, name: str):
        allowed(access)
        if not username_from(username) or not re.fullmatch(r"\d+_\d+\.(jpg|png|webp|gif)", name):
            abort(404)
        return send_from_directory(folder(username), name)

    app.add_url_rule("/<access>/content", "content_page", content_page, methods=["GET"])
    app.add_url_rule("/<access>/content/state", "content_state", content_state, methods=["GET"])
    app.add_url_rule("/<access>/content/verify", "content_verify", content_verify, methods=["POST"])
    app.add_url_rule("/<access>/content/delete", "content_delete", content_delete, methods=["POST"])
    app.add_url_rule("/<access>/content/extract", "content_extract", content_extract, methods=["POST"])
    app.add_url_rule("/<access>/content/posts/<username>", "content_posts", content_posts, methods=["GET"])
    app.add_url_rule("/<access>/content/media/<username>/<name>", "content_media", content_media, methods=["GET"])


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
    .who { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: stretch; justify-content: center; padding: 6px 10px; border-radius: 10px;
      background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); color: var(--ink); text-align: start; white-space: normal; }
    .who b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .who span { color: var(--muted); font-size: 12px; font-weight: 400; direction: ltr; text-align: end; }
    .who .kept { direction: rtl; text-align: start; color: var(--ok-ink); }
    li .drop { color: var(--clay); }
    dialog { width: min(520px, calc(100vw - 24px)); max-height: calc(100vh - 32px); padding: 0; border: 0; border-radius: 14px; background: var(--sheet); color: var(--ink); }
    dialog::backdrop { background: rgba(28, 25, 21, 0.35); }
    .box { padding: 14px; }
    .box h2 { font-size: 15px; margin-bottom: 10px; }
    .days { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; }
    .days input { width: 72px; text-align: center; }
    .modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px; margin: 10px 0 6px; padding: 3px; border-radius: 12px;
      background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); }
    .modes label { position: relative; display: flex; align-items: center; justify-content: center; min-height: 36px; padding: 4px;
      border-radius: 9px; color: var(--muted); font-size: 13px; font-weight: 600; text-align: center; cursor: pointer; }
    .modes label:has(input:checked) { background: var(--sheet); color: var(--pine-deep); box-shadow: inset 0 0 0 1.5px var(--pine); }
    .modes label:has(input:focus-visible) { outline: 2px solid var(--pine); outline-offset: 1px; }
    .modes input { position: absolute; inset: 0; width: 100%; height: 100%; min-height: 0; margin: 0; padding: 0; opacity: 0; cursor: pointer; }
    .photos-only { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; padding: 8px 10px; border-radius: 10px; background: var(--paper);
      box-shadow: inset 0 0 0 1px var(--line); font-size: 13px; font-weight: 600; cursor: pointer; }
    .photos-only:has(input:checked) { background: var(--ok-bg); box-shadow: inset 0 0 0 1.5px var(--pine); }
    .photos-only input { flex: none; width: 18px; height: 18px; min-height: 0; margin: 0; padding: 0; accent-color: var(--pine); }
    .wide { width: 100%; }
    .posts { margin-top: 8px; }
    .posts li { display: block; padding: 8px 10px; border-radius: 10px; background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); }
    .post-meta { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 10px; color: var(--muted); font-size: 12px; }
    .post-meta a { color: var(--pine); font-weight: 600; }
    .post-text { margin: 4px 0 0; font-size: 13px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .photos { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .photos img { width: 72px; height: 72px; object-fit: cover; border-radius: 8px; display: block; }
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
      <p class="hint">يُفتح الملف الشخصي بالحساب المحفوظ، ويُحفظ اليوزر مع اسم الحساب. اضغط على حساب لاستخراج منشوراته.</p>
      <ul id="saved"></ul>
    </section>
    <dialog id="extract" aria-labelledby="extract-title">
      <div class="box">
        <h2 id="extract-title"></h2>
        <div class="days">
          <label for="days">منشورات آخر</label>
          <input id="days" type="number" min="1" max="90" value="7" inputmode="numeric">
          <span>أيام</span>
        </div>
        <div class="modes" role="radiogroup" aria-label="أي المنشورات">
          <label><input type="radio" name="mode" value="all" checked>الجميع</label>
          <label><input type="radio" name="mode" value="likes">الأكثر إعجابًا</label>
          <label><input type="radio" name="mode" value="views">الأكثر مشاهدة</label>
        </div>
        <label class="photos-only"><input id="photos-only" type="checkbox"> التي فيها صور فقط</label>
        <button id="extract-go" class="go wide" type="button">استخراج</button>
        <div id="extract-notice" class="notice" role="status" hidden></div>
        <p id="kept-title" class="hint"></p>
        <ul id="posts" class="posts"></ul>
        <button id="extract-close" class="wide" type="button" style="margin-top: 12px">إغلاق</button>
      </div>
    </dialog>
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
        const who = document.createElement("button");
        who.type = "button";
        who.className = "who";
        const name = document.createElement("b");
        name.textContent = row.name;
        const handle = document.createElement("span");
        handle.textContent = "@" + row.username;
        who.append(name, handle);
        if (row.kept) {
          const kept = document.createElement("span");
          kept.className = "kept";
          kept.textContent = row.kept + " منشورًا محفوظًا";
          who.append(kept);
        }
        who.addEventListener("click", () => openExtract(row));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "drop";
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

    // The popup of one kept account: extract its posts, and what is kept from the last time.
    const dialog = document.getElementById("extract");
    const extractGo = document.getElementById("extract-go");
    const extractNotice = document.getElementById("extract-notice");
    const daysField = document.getElementById("days");
    const postsList = document.getElementById("posts");
    const MODES = {all: "الجميع", likes: "الأكثر إعجابًا", views: "الأكثر مشاهدة", images: "التي فيها صور"};
    let current = "";
    let polling = 0;

    function openExtract(row) {
      current = row.username;
      // Each part in its own direction, so "@user" does not show as "user@" in the Arabic line.
      const titleName = document.createElement("bdi");
      titleName.textContent = row.name;
      const titleHandle = document.createElement("bdi");
      titleHandle.dir = "ltr";
      titleHandle.textContent = "@" + row.username;
      document.getElementById("extract-title").replaceChildren("استخراج من ", titleName, " (", titleHandle, ")");
      extractNotice.hidden = true;
      extractGo.disabled = false;
      dialog.showModal();
      loadPosts();
    }

    function showJob(job) {
      if (!job.status) return;
      extractNotice.textContent = job.message;
      extractNotice.className = "notice " + (job.status === "failed" ? "is-bad" : "is-ok");
      extractNotice.hidden = false;
      extractGo.disabled = job.status === "running";
    }

    function showKept(kept) {
      const posts = kept.posts || [];
      document.getElementById("kept-title").textContent = kept.extracted_at
        ? "المحفوظ: " + posts.length + " منشورًا · " + (MODES[kept.mode] || "") + (kept.photos_only ? " · فيها صور فقط" : "") + " · آخر " + kept.days + " أيام · استُخرج "
          + new Date(kept.extracted_at * 1000).toLocaleString("ar", {dateStyle: "medium", timeStyle: "short"})
        : "لا توجد منشورات محفوظة لهذا الحساب.";
      postsList.replaceChildren();
      posts.forEach((post) => {
        const item = document.createElement("li");
        const meta = document.createElement("div");
        meta.className = "post-meta";
        const info = document.createElement("span");
        info.textContent = new Date(post.date).toLocaleString("ar", {dateStyle: "medium", timeStyle: "short"})
          + " · إعجاب " + Number(post.likes).toLocaleString("en-US") + " · مشاهدة " + Number(post.views).toLocaleString("en-US");
        const open = document.createElement("a");
        open.href = post.url;
        open.target = "_blank";
        open.rel = "noopener";
        open.textContent = "فتح المنشور";
        meta.append(info, open);
        item.append(meta);
        if (post.text) {
          const text = document.createElement("p");
          text.className = "post-text";
          text.dir = "auto";
          text.textContent = post.text;
          item.append(text);
        }
        if (post.images.length) {
          const photos = document.createElement("div");
          photos.className = "photos";
          post.images.forEach((name) => {
            const link = document.createElement("a");
            link.href = apiRoot + "/content/media/" + encodeURIComponent(kept.username) + "/" + encodeURIComponent(name);
            link.target = "_blank";
            const image = document.createElement("img");
            image.src = link.href;
            image.alt = "صورة المنشور";
            image.loading = "lazy";
            link.append(image);
            photos.append(link);
          });
          item.append(photos);
        }
        postsList.append(item);
      });
    }

    async function loadPosts() {
      const username = current;
      let data;
      try {
        data = await (await fetch(apiRoot + "/content/posts/" + encodeURIComponent(username), {cache: "no-store"})).json();
      } catch (_) {
        data = {job: {}, kept: {}};
      }
      if (username !== current) return;
      showJob(data.job || {});
      showKept(data.kept || {});
      clearTimeout(polling);
      if ((data.job || {}).status === "running") {
        polling = setTimeout(() => {
          if (dialog.open) loadPosts();
        }, 2000);
      } else if ((data.job || {}).status) {
        refresh();
      }
    }

    extractGo.addEventListener("click", async () => {
      const mode = (document.querySelector('input[name="mode"]:checked') || {}).value || "all";
      extractGo.disabled = true;
      let data;
      try {
        const response = await fetch(apiRoot + "/content/extract", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          cache: "no-store",
          body: JSON.stringify({csrf_token: csrf, account: picked, username: current, days: Number(daysField.value), mode,
            photos_only: document.getElementById("photos-only").checked})
        });
        data = await response.json();
      } catch (_) {
        data = {message: "تعذّر الاتصال."};
      }
      if (!data.success) {
        showJob({status: "failed", message: data.message || "تعذّر بدء الاستخراج."});
        extractGo.disabled = false;
        return;
      }
      showJob(data.job);
      loadPosts();
    });
    document.getElementById("extract-close").addEventListener("click", () => dialog.close());

    function refresh() {
      fetch(apiRoot + "/content/state", {cache: "no-store"})
        .then((response) => response.json())
        .then(render)
        .catch(() => {});
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
