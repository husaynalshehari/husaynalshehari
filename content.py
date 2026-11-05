"""«إنشاء محتوى»: its own page inside the script's panel. Kept beside the script, it is added by the
script itself (same address and port), which then shows «إنشاء محتوى» on its page.

1. The saved account, and the X accounts to work with. «تحقق» opens the account's profile with the
   saved account and keeps its username with the account's name.
2. A click on a kept account: extract its posts of the last days (all of them, the most liked or the
   most viewed; with or without "only those with photos"). Each post's link and photos are kept in
   data/content/<user>/ until that account is extracted again.
3. Ticked kept posts are sent to a Gemini picture model («توليد»), which makes one new post from their
   idea: a short text and one or two pictures. Every generated post is kept, each in its own folder
   data/content_generated/<user>/<time>/.
4. «من رابط تغريدة»: the tweets pasted there (their text and photos, read through FxTwitter, no
   browser needed) go to Gemini the same way. Those posts are kept in data/content_generated/from-links/.
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
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import abort, jsonify, render_template_string, request, send_from_directory, session

# «الأكثر إعجابًا» and «الأكثر مشاهدة» keep this many posts of the period.
TOP_POSTS = 10
# A profile is scrolled at most this long, and for at most this many posts.
EXTRACT_SECONDS = 240
EXTRACT_MAX_POSTS = 400
MODES = {"all": "الجميع", "likes": "الأكثر إعجابًا", "views": "الأكثر مشاهدة"}
# «توليد»: the Gemini models offered on the page (the first is picked by default). Each answers with
# text and pictures; max_photos is the most photos it is sent (Google's limit on reference pictures).
GENERATE_MODELS: dict[str, dict] = {
    "gemini-nano-banana-2.1": {"label": "Nano Banana 2.1 · الأحدث", "max_photos": 10},
    "gemini-3.1-flash-image": {"label": "Nano Banana 2 · سريع", "max_photos": 10},
    "gemini-3.1-flash-lite-image": {"label": "Nano Banana 2 Lite · الأرخص", "max_photos": 14},
    "gemini-3-pro-image": {"label": "Nano Banana Pro · أعلى جودة", "max_photos": 6},
}
# The photos sent in one request stay under this (the API takes about 20 MB once encoded).
GENERATE_MAX_BYTES = 14 * 1024 * 1024
# How long one request may take: a picture model can take a minute or more.
GENERATE_SECONDS = 240
# What Gemini is asked to make from the ticked posts (their texts and photos follow it).
GENERATE_PROMPT = (
    "You write viral posts for X (Twitter). Below are posts from one account, each with its text and photos.\n"
    "Take the idea behind them and create ONE new post:\n"
    "- The text: very short, one line of a few words (at most 70 characters), as short as a real tweet. "
    "Thrilling, viral and mysterious: it hints and stops. Do not explain and do not tell the story.\n"
    "- Controversial: one bold, one-sided remark that people will want to argue with. Do not present both sides, "
    "do not ask for opinions, and never use words like controversy, debate or phenomenon.\n"
    "- Realistic: it sounds like a real person tweeting, in the same language, dialect and tone as the posts "
    "(casual and spontaneous), not like news, an article or an ad. Nothing made up. Bold is fine; no slurs or hate. "
    "No hashtags, no links.\n"
    "- One or two images for it: edit the photos or create new ones from their idea. Do not return a photo unchanged. "
    "They look like real casual phone photos, like the posts' photos: not staged and not like an ad.\n"
    "- Each image has one detail, tied to the post's idea, that makes people react and comment, the way the posts' "
    "photos do: a spontaneous 'dumb' slip people will rush to correct (wrong numbers, a wrong label, a misspelled word), "
    "something odd or funny that does not belong, or a short provocative label or number added on the photo. "
    "It feels natural, not like a planned joke.\n"
    "- No person in the images: no people, faces or bodies, even when the photos have them.\n"
    "Answer with the post text only, between <post> and </post> (no title, no notes, no translation), and the image(s)."
)
# Sent after the posts for the second and third post of one «توليد», with the texts made before it.
GENERATE_DIFFERENT = (
    "Posts already made from these. Yours must be clearly different from them: another angle or another "
    "idea from these posts, and another image:\n"
)
# How many posts one «توليد» makes (offered on the page; the first is the default).
GENERATE_COUNTS = (3, 2, 1)
# «من رابط تغريدة»: the pasted tweets are read through FxTwitter's free API, at most this many at once.
FX_STATUS_URL = "https://api.fxtwitter.com/status/{}"
LINK_MAX_TWEETS = 10
# Their generated posts' folder under data/content_generated: not an X username (it has a hyphen), so
# never an account's.
LINKS_FOLDER = "from-links"
IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
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


def tweet_ids_from(text: str) -> list[str]:
    """The tweets in pasted text, each once and in order: x.com / twitter.com links (with or without the
    author's name) or bare tweet numbers."""
    found: list[str] = []
    for match in re.finditer(r"(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}(?:/web)?/status(?:es)?/(\d{6,25})|(?<![\d/])(\d{15,25})(?!\d)", text or ""):
        status_id = match.group(1) or match.group(2)
        if status_id not in found:
            found.append(status_id)
    return found


def read_tweet(status_id: str) -> dict:
    """One tweet through FxTwitter: its link, text (with the post it quotes), and its photos' addresses
    (a video's picture for a video). A tweet that cannot be read raises GenerateError, said plainly."""
    link = f"https://x.com/i/status/{status_id}"
    ask = urllib.request.Request(FX_STATUS_URL.format(status_id), headers={"User-Agent": "x-group-tool/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(ask, timeout=20) as answer:
            data = json.loads(answer.read(4 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410):
            raise GenerateError(f"التغريدة غير موجودة أو حسابها خاص: {link}") from None
        if exc.code in (429, 503):
            raise GenerateError("خدمة قراءة التغريدات مشغولة الآن. أعد المحاولة بعد دقائق.") from None
        raise GenerateError(f"تعذّرت قراءة التغريدة (رمز {exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise GenerateError("تعذّرت قراءة التغريدة. تأكد أن الخادم متصل بالإنترنت.") from None
    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        raise GenerateError(f"التغريدة غير موجودة أو حسابها خاص: {link}")
    photos = []
    media = tweet.get("media") if isinstance(tweet.get("media"), dict) else {}
    for item in media.get("all") or []:
        if isinstance(item, dict):
            address = str((item.get("url") if item.get("type") == "photo" else item.get("thumbnail_url")) or "")
            if address.startswith("https://pbs.twimg.com/") and address not in photos:
                photos.append(address)
    quote = tweet.get("quote") if isinstance(tweet.get("quote"), dict) else {}
    return {"status_id": status_id, "url": str(tweet.get("url") or link)[:300], "text": str(tweet.get("text") or "").strip()[:1000],
            "quote": str(quote.get("text") or "").strip()[:500], "photos": photos[:4]}


def download_photo(address: str) -> tuple[str, bytes] | None:
    """One tweet photo, in its large size: (type, bytes), or None when it cannot be had."""
    url = re.sub(r"name=[A-Za-z0-9]+", "name=large", address) if "name=" in address else address
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "x-group-tool/1.0"}), timeout=30) as answer:
            kind = (answer.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = answer.read(12 * 1024 * 1024)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not body or not kind.startswith("image/"):
        return None
    return kind, body


class GenerateError(Exception):
    """A generation that cannot go on; the message is shown on the page. fatal=False: only this post
    failed (no picture, or refused), so the next one of the same «توليد» is still tried."""

    def __init__(self, message: str, fatal: bool = True):
        super().__init__(message)
        self.fatal = fatal


def gemini_problem(status: int, detail: str, busy: bool) -> str:
    """What to show for a request Gemini turned down."""
    if re.search(r"api key not valid|api_key_invalid|invalid api key", detail, re.I):
        return "مفتاح Gemini المحفوظ غير صالح."
    if status == 404 or re.search(r"not found|is not supported", detail, re.I):
        return "هذا النموذج غير متاح لمفتاح Gemini المحفوظ. اختر نموذجًا آخر."
    if re.search(r"quota|billing", detail, re.I):
        return "حصة مفتاح Gemini لا تسمح بهذا الطلب الآن: انتهت الحصة، أو أن الفوترة غير مفعّلة على المفتاح."
    if busy:
        return "Gemini مشغول الآن. أعد المحاولة بعد قليل أو اختر نموذجًا آخر."
    return "رفض Gemini الطلب" + (f": {detail}" if detail else ".")


def ask_gemini(api_key: str, model: str, parts: list[dict], report) -> dict:
    """One request answered with text and pictures. A busy Gemini is asked again a few times."""
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }).encode("utf-8")
    waits = (5, 12, 20, 30)
    for attempt in range(len(waits) + 1):
        ask = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/" + quote(model, safe="") + ":generateContent",
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(ask, timeout=GENERATE_SECONDS) as answer:
                return json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = getattr(exc, "code", 0) or 0
            detail = ""
            try:
                detail = str((json.loads(exc.read().decode("utf-8", "replace")).get("error") or {}).get("message") or "")
            except Exception:
                pass
            detail = re.sub(r"\s+", " ", detail).strip()[:300]
            if api_key:
                detail = detail.replace(api_key, "…")
            busy = status in (429, 500, 502, 503, 504) or bool(
                re.search(r"high demand|try again later|unavailable|overloaded|resource exhausted|rate limit", detail, re.I))
            # "limit: 0": this key has no quota at all for the model, so asking again is no use.
            if busy and attempt < len(waits) and not re.search(r"limit:\s*0\b", detail):
                report(f"Gemini مشغول الآن، إعادة المحاولة بعد {waits[attempt]} ثانية…")
                time.sleep(waits[attempt])
                continue
            raise GenerateError(gemini_problem(status, detail, busy)) from None
        except urllib.error.URLError as exc:
            # Not connected, so the request did not go out: asked again.
            if attempt < 2:
                report("تعذّر الاتصال بـ Gemini، إعادة المحاولة…")
                time.sleep(waits[attempt])
                continue
            raise GenerateError("تعذّر الاتصال بـ Gemini.") from exc
        # Cut off after the request went out: not asked again, as Gemini may still make (and charge
        # for) its pictures.
        except TimeoutError as exc:
            raise GenerateError(f"لم يرد Gemini خلال {GENERATE_SECONDS // 60} دقائق. أعد المحاولة أو اختر نموذجًا أسرع.") from exc
        except OSError as exc:
            raise GenerateError("انقطع الاتصال بـ Gemini أثناء التوليد. أعد المحاولة.") from exc
        except ValueError as exc:
            raise GenerateError("وصل من Gemini رد غير مفهوم. أعد المحاولة.") from exc
    raise GenerateError("Gemini مشغول الآن. أعد المحاولة بعد قليل أو اختر نموذجًا آخر.")


# A label the model may put before the post ("Post Text:", "المنشور:"), and the start of a note it may
# add around it (a warning, "Here is…", a translation): neither is part of the post.
POST_LABEL = re.compile(r"^\s*[*#>•\-]*\s*(?:post\s*text|post|text|caption|tweet|نص المنشور|المنشور الجديد|المنشور|النص|التغريدة)\s*[:：]\s*", re.I)
NOTE_START = re.compile(r"^\s*(?:[#*⚠>•\-(\[]|note\b|please\b|here\b|translation\b|ملاحظة|تنبيه|ترجمة)", re.I)


def clean_post(raw: str) -> str:
    """The post's own text from the model's answer, without the notes, labels or translation it may add."""
    text = (raw or "").replace("**", "")
    tagged = re.search(r"<post>(.*?)(</post>|$)", text, re.S | re.I)
    if tagged and tagged.group(2):
        # Between <post> and </post>, as asked: that is the post.
        text = tagged.group(1).strip()
    else:
        # No (closing) tag: the post's lines, skipping notes and a label before them, up to the
        # first empty line, note or label after them.
        kept: list[str] = []
        for line in (tagged.group(1) if tagged else text).replace("</post>", "").split("\n"):
            line = line.strip()
            labelled = POST_LABEL.match(line)
            if not line or labelled or NOTE_START.match(line):
                if kept:
                    break
                if labelled and line[labelled.end():].strip():
                    kept.append(line[labelled.end():].strip())
                continue
            kept.append(line)
        text = "\n".join(kept)
    text = POST_LABEL.sub("", text, count=1).strip()
    # An English translation in brackets after an Arabic post.
    if re.search(r"[\u0600-\u06FF]", text):
        text = re.sub(r"\s*\([A-Za-z][^()]*\)\s*$", "", text)
    # Quotes around the whole post only; quotes inside it stay.
    if len(text) > 1 and text[0] in "\"'«“" and text[-1] in "\"'»”":
        text = text[1:-1].strip()
    return text[:1000]


def read_answer(payload: dict) -> tuple[str, list[tuple[str, bytes]], str]:
    """The post's text, its pictures as (type, bytes), and why Gemini stopped."""
    candidate = (payload.get("candidates") or [{}])[0] or {}
    texts: list[str] = []
    pictures: list[tuple[str, bytes]] = []
    for part in (candidate.get("content") or {}).get("parts") or []:
        # Thinking models also send their drafts (text and pictures) marked "thought".
        if not isinstance(part, dict) or part.get("thought"):
            continue
        if part.get("text"):
            texts.append(str(part["text"]))
        data = part.get("inlineData") or part.get("inline_data") or {}
        kind = str(data.get("mimeType") or data.get("mime_type") or "")
        if data.get("data") and kind.startswith("image/"):
            try:
                pictures.append((kind, base64.b64decode(data["data"])))
            except ValueError:
                pass
    blocked = str((payload.get("promptFeedback") or {}).get("blockReason") or "")
    # Each text part on its own: a note sent as a part of its own stays apart from the post.
    return clean_post("\n\n".join(texts)), pictures, blocked or str(candidate.get("finishReason") or "")


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
    # «توليد»: one at a time (each is paid for), and each account's last one, as above.
    generated_root = Path(tools.data_dir) / "content_generated"
    generating = threading.Lock()
    generations: dict[str, dict] = {}
    generated_lock = threading.Lock()
    # «من رابط تغريدة»: its posts' folder and its last «إرسال إلى Gemini» (it shares «توليد»'s turn).
    links_folder = generated_root / LINKS_FOLDER
    link_job: dict = {}

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

    def generated_folder(username: str) -> Path:
        return generated_root / username.lower()

    def move_old_generated(username: str) -> None:
        """The one post kept before (straight in the account's folder) gets a folder of its own, as the others."""
        base = generated_folder(username)
        if not (base / "result.json").is_file():
            return
        try:
            loaded = json.loads((base / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if not isinstance(loaded, dict):
            loaded = {}
        try:
            made_at = float(loaded.get("created_at") or 0) or time.time()
        except (TypeError, ValueError):
            made_at = time.time()
        target = base / str(int(made_at * 1000))
        target.mkdir(parents=True, exist_ok=True)
        for name in [*(loaded.get("images") or []), "result.json"]:
            if re.fullmatch(r"[12]\.(png|jpg|webp)|result\.json", str(name)) and (base / str(name)).is_file():
                (base / str(name)).replace(target / str(name))

    def read_generated(username: str) -> list[dict]:
        """The account's generated posts, newest first. Each has its folder's name as "id"."""
        with generated_lock:
            move_old_generated(username)
        return read_results(generated_folder(username))

    def read_results(base: Path) -> list[dict]:
        """The generated posts kept under base (one folder each), newest first, with the folder's name as "id"."""
        with generated_lock:
            try:
                folders = [entry for entry in base.iterdir() if entry.is_dir() and re.fullmatch(r"\d{10,16}", entry.name)]
            except OSError:
                return []
            found = []
            for entry in folders:
                try:
                    loaded = json.loads((entry / "result.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(loaded, dict):
                    found.append({**loaded, "id": entry.name})
        return sorted(found, key=lambda row: str(row["id"]).zfill(16), reverse=True)

    def listed(username: str) -> bool:
        return any(str(row["username"]).lower() == username.lower() for row in read_saved())

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
        return render_template_string(PAGE, csrf_token=csrf_token, api_root="/" + access_path, panel="/" + access_path, models=GENERATE_MODELS, counts=GENERATE_COUNTS)

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
                shutil.rmtree(generated_folder(username), ignore_errors=True)
            if extractions.get(username, {}).get("status") != "running":
                extractions.pop(username, None)
            if generations.get(username, {}).get("status") != "running":
                generations.pop(username, None)
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

    def request_parts(username: str, model: str, post_ids: list[str]) -> tuple[list[dict], dict]:
        """The request made of the ticked posts (their texts and photos), and what went into it."""
        by_id = {str(row.get("status_id")): row for row in read_posts(username).get("posts") or []}
        chosen = [by_id[status_id] for status_id in post_ids if status_id in by_id]
        if not chosen:
            raise GenerateError("المنشورات المحددة لم تعد محفوظة. حدّدها من جديد.")
        # Every post's first photo before any post's second one, within the model's limit and the size.
        order = []
        for depth in range(4):
            for index, row in enumerate(chosen):
                names = row.get("images") or []
                if depth < len(names):
                    order.append((index, names[depth]))
        picked: dict[tuple[int, str], bytes] = {}
        size = 0
        for index, name in order:
            if len(picked) >= GENERATE_MODELS[model]["max_photos"]:
                break
            try:
                data = (folder(username) / name).read_bytes()
            except OSError:
                continue
            if size + len(data) > GENERATE_MAX_BYTES:
                continue
            picked[(index, name)] = data
            size += len(data)
        parts: list[dict] = [{"text": GENERATE_PROMPT}]
        for index, row in enumerate(chosen):
            parts.append({"text": f"Post {index + 1}:\n" + (str(row.get("text") or "").strip() or "(no text)")})
            for name in row.get("images") or []:
                data = picked.get((index, name))
                if data is not None:
                    kind = IMAGE_TYPES.get(name.rsplit(".", 1)[-1].lower(), "image/jpeg")
                    parts.append({"inlineData": {"mimeType": kind, "data": base64.b64encode(data).decode("ascii")}})
        total = sum(len(row.get("images") or []) for row in chosen)
        return parts, {"posts": [str(row["status_id"]) for row in chosen], "photos_sent": len(picked), "photos_total": total}

    def generate_one(api_key: str, model: str, parts: list[dict], made_before: list[str], progress) -> tuple[str, list]:
        """One post from the request: its text and pictures. The texts made before it are added, so it differs."""
        if made_before:
            parts = parts + [{"text": GENERATE_DIFFERENT + "\n".join(f"{n}. {text}" for n, text in enumerate(made_before, 1))}]
        text, pictures, reason = "", [], ""
        for attempt in range(2):
            text, pictures, reason = read_answer(ask_gemini(api_key, model, parts, progress))
            # Now and then the answer comes without a picture: asked once more.
            if pictures or reason not in ("", "STOP"):
                break
        if not pictures:
            if reason not in ("", "STOP", "MAX_TOKENS"):
                raise GenerateError(f"رفض Gemini توليد الصورة ({reason}).", fatal=False)
            raise GenerateError("لم يُرجع Gemini صورة.", fatal=False)
        return text, pictures[:2]

    def save_generated(username: str, result: dict) -> None:
        """Keep the new post with the account's other generated ones, in a folder of its own. "batch",
        "number" and "of" say which «توليد» made it, and which of its posts it is."""
        made_at = time.time()
        stamp = str(int(made_at * 1000))
        fresh = generated_folder(username) / f".{stamp}.new"
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir(parents=True)
        names = []
        for number, (kind, data) in enumerate(result["pictures"], 1):
            name = f"{number}.{EXTENSIONS.get(kind, 'png')}"
            (fresh / name).write_bytes(data)
            names.append(name)
        kept = {"username": username, "model": result["model"], "text": result["text"], "images": names, "posts": result["posts"],
                "photos_sent": result["photos_sent"], "photos_total": result["photos_total"], "created_at": made_at,
                "batch": result["batch"], "number": result["number"], "of": result["of"]}
        (fresh / "result.json").write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        with generated_lock:
            fresh.rename(generated_folder(username) / stamp)

    def run_generation(api_key: str, username: str, model: str, post_ids: list[str], count: int) -> None:
        """Make count posts one after the other, each kept as soon as it is made."""
        key = username.lower()
        label = GENERATE_MODELS[model]["label"].split(" · ")[0]
        batch = str(int(time.time() * 1000))
        made: list[str] = []
        problem = ""
        last: GenerateError | None = None
        try:
            parts, info = request_parts(username, model, post_ids)
            sent = f"أُرسل {len(info['posts'])} منشورًا و{info['photos_sent']} صورة" + (
                f" من {info['photos_total']}" if info["photos_sent"] < info["photos_total"] else "")
            for number in range(1, count + 1):
                # One post alone is just "the post"; of two or three, "post 2 of 3".
                step = f"المنشور {number} من {count}" if count > 1 else ""

                def progress(message: str, step=step) -> None:
                    generations[key] = {"status": "running", "message": f"{step}: {message}" if step else message}

                generations[key] = {"status": "running", "message": f"جارٍ توليد {step or 'المنشور'} بـ {label}… {sent}. قد يستغرق دقيقة أو أكثر."}
                try:
                    text, pictures = generate_one(api_key, model, parts, made, progress)
                except GenerateError as exc:
                    problem, last = (f"{step}: {exc}" if step else str(exc)), exc
                    if exc.fatal:
                        break
                    continue
                with store_lock:
                    # Deleted from the list while it ran: nothing more is kept for it.
                    if not listed(username):
                        generations.pop(key, None)
                        return
                    save_generated(username, {**info, "model": model, "text": text, "pictures": pictures,
                                              "batch": batch, "number": len(made) + 1, "of": count})
                made.append(text)
            if len(made) == count:
                generations[key] = {"status": "done", "message": "اكتمل التوليد: " + {1: "منشور واحد.", 2: "منشوران."}.get(count, f"{count} منشورات.")}
            elif made:
                generations[key] = {"status": "done", "message": f"اكتمل {len(made)} من {count}. لم يكتمل الباقي: {problem}"}
            elif last is not None and last.fatal:
                generations[key] = {"status": "failed", "message": str(last)}
            else:
                before = "لم يكتمل أي منشور. " if count > 1 else ""
                generations[key] = {"status": "failed", "message": f"{before}{problem} جرّب منشورات أو نموذجًا آخر."}
        except GenerateError as exc:
            generations[key] = {"status": "failed", "message": str(exc)}
        except Exception:
            generations[key] = {"status": "failed", "message": "تعذّر التوليد."}
        finally:
            generating.release()

    def content_generate(access: str):
        """«توليد»: send the ticked posts to the chosen Gemini model; the page follows it."""
        allowed(access)
        data = request.get_json(silent=True) or {}
        if not csrf_ok(data):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        username = username_from(str(data.get("username") or ""))
        with store_lock:
            kept = next((row["username"] for row in read_saved() if str(row["username"]).lower() == username.lower()), "") if username else ""
        if not kept:
            return jsonify(success=False, message="هذا الحساب ليس في القائمة."), 404
        model = str(data.get("model") or "")
        if model not in GENERATE_MODELS:
            return jsonify(success=False, message="اختر نموذج Gemini."), 400
        posts = data.get("posts") if isinstance(data.get("posts"), list) else []
        post_ids = list(dict.fromkeys(str(value) for value in posts if re.fullmatch(r"\d{1,25}", str(value))))[:100]
        known = {str(row.get("status_id")) for row in read_posts(kept).get("posts") or []}
        post_ids = [status_id for status_id in post_ids if status_id in known]
        if not post_ids:
            return jsonify(success=False, message="حدّد منشورًا واحدًا على الأقل."), 400
        try:
            count = int(data.get("count") or GENERATE_COUNTS[0])
        except (TypeError, ValueError):
            count = 0
        if count not in GENERATE_COUNTS:
            return jsonify(success=False, message="اختر عدد المنشورات."), 400
        gemini_key = getattr(tools, "gemini_key", None)
        if gemini_key is None:
            return jsonify(success=False, message="حدّث ملف السكربت لاستخدام التوليد."), 400
        api_key = gemini_key()
        if not api_key:
            return jsonify(success=False, message="لا يوجد مفتاح Gemini محفوظ في اللوحة."), 400
        if not generating.acquire(blocking=False):
            return jsonify(success=False, message="يعمل الآن توليد آخر. انتظر حتى ينتهي."), 409
        generations[kept.lower()] = {"status": "running", "message": "جارٍ إرسال المنشورات إلى Gemini…"}
        threading.Thread(target=run_generation, args=(api_key, kept, model, post_ids, count), name="content-generate", daemon=True).start()
        return jsonify(success=True, message="بدأ التوليد.", job=generations[kept.lower()])

    def link_parts(model: str, tweets: list[dict]) -> tuple[list[dict], dict]:
        """The request made of the pasted tweets (their texts and photos), as «توليد» makes it of kept posts."""
        order = []
        for depth in range(4):
            for index, tweet in enumerate(tweets):
                if depth < len(tweet["files"]):
                    order.append((index, depth))
        picked: set[tuple[int, int]] = set()
        size = 0
        for index, depth in order:
            if len(picked) >= GENERATE_MODELS[model]["max_photos"]:
                break
            data = tweets[index]["files"][depth][1]
            if size + len(data) > GENERATE_MAX_BYTES:
                continue
            picked.add((index, depth))
            size += len(data)
        parts: list[dict] = [{"text": GENERATE_PROMPT.replace("posts from one account, each", "posts, each")}]
        for index, tweet in enumerate(tweets):
            text = tweet["text"] or "(no text)"
            if tweet["quote"]:
                text += "\n(It quotes: " + tweet["quote"] + ")"
            parts.append({"text": f"Post {index + 1}:\n" + text})
            for depth, (kind, data) in enumerate(tweet["files"]):
                if (index, depth) in picked:
                    parts.append({"inlineData": {"mimeType": kind, "data": base64.b64encode(data).decode("ascii")}})
        total = sum(len(tweet["files"]) for tweet in tweets)
        return parts, {"posts": [tweet["status_id"] for tweet in tweets], "links": [tweet["url"] for tweet in tweets],
                       "photos_sent": len(picked), "photos_total": total}

    def save_link_generated(result: dict) -> None:
        """Keep a post made from pasted tweets with the others made that way, in a folder of its own."""
        made_at = time.time()
        stamp = str(int(made_at * 1000))
        fresh = links_folder / f".{stamp}.new"
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir(parents=True)
        names = []
        for number, (kind, data) in enumerate(result["pictures"], 1):
            name = f"{number}.{EXTENSIONS.get(kind, 'png')}"
            (fresh / name).write_bytes(data)
            names.append(name)
        kept = {"model": result["model"], "text": result["text"], "images": names, "posts": result["posts"], "links": result["links"],
                "photos_sent": result["photos_sent"], "photos_total": result["photos_total"], "created_at": made_at,
                "batch": result["batch"], "number": result["number"], "of": result["of"]}
        (fresh / "result.json").write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        with generated_lock:
            fresh.rename(links_folder / stamp)

    def run_link_generation(api_key: str, status_ids: list[str], model: str, count: int) -> None:
        """Read the pasted tweets, then make count posts from them one after the other, as «توليد» does."""
        label = GENERATE_MODELS[model]["label"].split(" · ")[0]
        batch = str(int(time.time() * 1000))
        made: list[str] = []
        problem = ""
        last: GenerateError | None = None
        try:
            tweets = []
            for number, status_id in enumerate(status_ids, 1):
                link_job.update(status="running", message="جارٍ قراءة التغريدة" + (f" {number} من {len(status_ids)}" if len(status_ids) > 1 else "") + "…")
                tweet = read_tweet(status_id)
                tweet["files"] = [photo for photo in (download_photo(address) for address in tweet["photos"]) if photo]
                tweets.append(tweet)
            parts, info = link_parts(model, tweets)
            sent = (f"أُرسلت {len(tweets)} تغريدات" if len(tweets) > 1 else "أُرسلت التغريدة") + f" و{info['photos_sent']} صورة" + (
                f" من {info['photos_total']}" if info["photos_sent"] < info["photos_total"] else "")
            for number in range(1, count + 1):
                step = f"المنشور {number} من {count}" if count > 1 else ""

                def progress(message: str, step=step) -> None:
                    link_job.update(status="running", message=f"{step}: {message}" if step else message)

                link_job.update(status="running", message=f"جارٍ توليد {step or 'المنشور'} بـ {label}… {sent}. قد يستغرق دقيقة أو أكثر.")
                try:
                    text, pictures = generate_one(api_key, model, parts, made, progress)
                except GenerateError as exc:
                    problem, last = (f"{step}: {exc}" if step else str(exc)), exc
                    if exc.fatal:
                        break
                    continue
                save_link_generated({**info, "model": model, "text": text, "pictures": pictures,
                                     "batch": batch, "number": len(made) + 1, "of": count})
                made.append(text)
            if len(made) == count:
                link_job.update(status="done", message="اكتمل التوليد: " + {1: "منشور واحد.", 2: "منشوران."}.get(count, f"{count} منشورات."))
            elif made:
                link_job.update(status="done", message=f"اكتمل {len(made)} من {count}. لم يكتمل الباقي: {problem}")
            elif last is not None and last.fatal:
                link_job.update(status="failed", message=str(last))
            else:
                before = "لم يكتمل أي منشور. " if count > 1 else ""
                link_job.update(status="failed", message=f"{before}{problem} جرّب تغريدة أو نموذجًا آخر.")
        except GenerateError as exc:
            link_job.update(status="failed", message=str(exc))
        except Exception:
            link_job.update(status="failed", message="تعذّر التوليد.")
        finally:
            generating.release()

    def content_link_generate(access: str):
        """«إرسال إلى Gemini»: the pasted tweets go to the chosen Gemini model; the page follows it."""
        allowed(access)
        data = request.get_json(silent=True) or {}
        if not csrf_ok(data):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        status_ids = tweet_ids_from(str(data.get("links") or ""))
        if not status_ids:
            return jsonify(success=False, message="الصق رابط تغريدة، مثل https://x.com/user/status/123…"), 400
        if len(status_ids) > LINK_MAX_TWEETS:
            return jsonify(success=False, message=f"الحد الأقصى {LINK_MAX_TWEETS} تغريدات في المرة الواحدة."), 400
        model = str(data.get("model") or "")
        if model not in GENERATE_MODELS:
            return jsonify(success=False, message="اختر نموذج Gemini."), 400
        try:
            count = int(data.get("count") or GENERATE_COUNTS[0])
        except (TypeError, ValueError):
            count = 0
        if count not in GENERATE_COUNTS:
            return jsonify(success=False, message="اختر عدد المنشورات."), 400
        gemini_key = getattr(tools, "gemini_key", None)
        if gemini_key is None:
            return jsonify(success=False, message="حدّث ملف السكربت لاستخدام التوليد."), 400
        api_key = gemini_key()
        if not api_key:
            return jsonify(success=False, message="لا يوجد مفتاح Gemini محفوظ في اللوحة."), 400
        if not generating.acquire(blocking=False):
            return jsonify(success=False, message="يعمل الآن توليد آخر. انتظر حتى ينتهي."), 409
        link_job.clear()
        link_job.update(status="running", message="جارٍ قراءة التغريدة…")
        threading.Thread(target=run_link_generation, args=(api_key, status_ids, model, count), name="content-link-generate", daemon=True).start()
        return jsonify(success=True, message="بدأ التوليد.", job=dict(link_job))

    def content_link_posts(access: str):
        """The posts made from pasted tweets, and how the last «إرسال إلى Gemini» is going."""
        allowed(access)
        return jsonify(success=True, job=dict(link_job), generated=read_results(links_folder))

    def content_link_media(access: str, made: str, name: str):
        allowed(access)
        if not re.fullmatch(r"\d{10,16}", made) or not re.fullmatch(r"[12]\.(png|jpg|webp)", name):
            abort(404)
        return send_from_directory(links_folder / made, name)

    def content_generated_media(access: str, username: str, made: str, name: str):
        allowed(access)
        if not username_from(username) or not re.fullmatch(r"\d{10,16}", made) or not re.fullmatch(r"[12]\.(png|jpg|webp)", name):
            abort(404)
        return send_from_directory(generated_folder(username) / made, name)

    def content_posts(access: str, username: str):
        """An account's kept posts, and how its extraction is going."""
        allowed(access)
        username = username_from(username)
        if not username:
            abort(404)
        return jsonify(success=True, job=extractions.get(username.lower(), {}), kept=read_posts(username),
                       gen_job=generations.get(username.lower(), {}), generated=read_generated(username))

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
    app.add_url_rule("/<access>/content/generate", "content_generate", content_generate, methods=["POST"])
    app.add_url_rule("/<access>/content/generated/<username>/<made>/<name>", "content_generated_media", content_generated_media, methods=["GET"])
    app.add_url_rule("/<access>/content/link-generate", "content_link_generate", content_link_generate, methods=["POST"])
    app.add_url_rule("/<access>/content/link-posts", "content_link_posts", content_link_posts, methods=["GET"])
    app.add_url_rule("/<access>/content/link-generated/<made>/<name>", "content_link_media", content_link_media, methods=["GET"])


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
    .pick-all { display: flex; align-items: center; gap: 8px; width: fit-content; margin-top: 10px; font-size: 13px; font-weight: 600; cursor: pointer; }
    .pick-all span { color: var(--muted); font-weight: 400; }
    .pick-all input, .pick input { flex: none; width: 18px; height: 18px; min-height: 0; margin: 0; padding: 0; accent-color: var(--pine); }
    .post-meta .pick { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
    .posts li { cursor: pointer; }
    .posts li.is-picked { background: var(--ok-bg); box-shadow: inset 0 0 0 1.5px var(--pine); }
    .generate-bar { position: sticky; bottom: 0; z-index: 1; margin: 10px -14px 0; padding: 10px 14px; background: var(--sheet);
      box-shadow: 0 -1px 0 var(--line), 0 -8px 16px rgba(28, 25, 21, 0.06); }
    .generate-bar .notice { margin: 0 0 8px; }
    .gen-row { display: flex; gap: 6px; }
    .gen-row + .gen-row { margin-top: 6px; }
    .gen-row .gen-count { flex: 0 0 auto; }
    .gen-row button { flex: 1; }
    .gen-row select { flex: 1; min-width: 0; min-height: 36px; padding: 0 8px; border: 1px solid var(--line); border-radius: 8px;
      background: var(--sheet); color: var(--ink); font: inherit; font-size: 16px; }
    .generated { margin-top: 10px; padding: 10px 12px; border-radius: 12px; background: var(--ok-bg); box-shadow: inset 0 0 0 1px rgba(15, 107, 80, 0.25); }
    .gen-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 2px 10px; font-size: 13px; }
    .gen-head span { color: var(--muted); font-size: 12px; }
    .gen-text { margin: 6px 0 8px; font-size: 15px; line-height: 1.6; white-space: pre-wrap; }
    .gen-images { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 6px; margin-bottom: 8px; }
    .gen-images img { width: 100%; border-radius: 10px; display: block; }
    .older { margin-top: 8px; }
    .older summary { padding: 6px 0; color: var(--pine-deep); font-size: 13px; font-weight: 600; cursor: pointer; }
    .older .generated { margin-top: 6px; background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); }
    .older .gen-text { font-size: 14px; }
    .older .gen-images { grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); }
    .link-row { margin-top: 6px; }
    .gen-source { margin: 0 0 8px; font-size: 12px; color: var(--muted); }
    .gen-source a { color: var(--pine); font-weight: 600; }
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
    <section class="sheet">
      <h2>من رابط تغريدة</h2>
      <div class="row">
        <input id="link" type="text" maxlength="2000" autocomplete="off" spellcheck="false" placeholder="https://x.com/user/status/123…" aria-label="رابط التغريدة">
      </div>
      <div class="gen-row link-row">
        <select id="link-model" aria-label="نموذج Gemini">
          {%- for id, model in models.items() %}
          <option value="{{ id }}">{{ model.label }}</option>
          {%- endfor %}
        </select>
      </div>
      <div class="gen-row">
        <select id="link-count" class="gen-count" aria-label="عدد المنشورات">
          {%- for count in counts %}
          <option value="{{ count }}">{{ {1: "منشور واحد", 2: "منشوران"}.get(count, count ~ " منشورات") }}</option>
          {%- endfor %}
        </select>
        <button id="link-go" class="go" type="button">إرسال إلى Gemini</button>
      </div>
      <p class="hint">تُقرأ التغريدة (نصها وصورها) وتُرسل إلى Gemini ليصنع منها منشورًا جديدًا بنص وصورة، كما في «توليد». يمكن لصق أكثر من رابط.</p>
      <div id="link-notice" class="notice" role="status" hidden></div>
      <div id="link-generated"></div>
      <details id="link-older" class="older" hidden>
        <summary></summary>
        <div id="link-older-list"></div>
      </details>
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
        <div id="generated"></div>
        <details id="generated-older" class="older" hidden>
          <summary></summary>
          <div id="generated-older-list"></div>
        </details>
        <label id="pick-all-row" class="pick-all" hidden><input id="pick-all" type="checkbox"> تحديد الكل <span id="picked-count"></span></label>
        <ul id="posts" class="posts"></ul>
        <div id="generate-bar" class="generate-bar" hidden>
          <div id="gen-notice" class="notice" role="status" hidden></div>
          <div class="gen-row">
            <select id="gen-model" aria-label="نموذج Gemini">
              {%- for id, model in models.items() %}
              <option value="{{ id }}">{{ model.label }}</option>
              {%- endfor %}
            </select>
          </div>
          <div class="gen-row">
            <select id="gen-count" class="gen-count" aria-label="عدد المنشورات">
              {%- for count in counts %}
              <option value="{{ count }}">{{ {1: "منشور واحد", 2: "منشوران"}.get(count, count ~ " منشورات") }}</option>
              {%- endfor %}
            </select>
            <button id="gen-go" class="go" type="button" disabled>توليد</button>
          </div>
        </div>
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
    const MODELS = {{ models|tojson }};
    const pickAll = document.getElementById("pick-all");
    const genGo = document.getElementById("gen-go");
    const genModel = document.getElementById("gen-model");
    const genCount = document.getElementById("gen-count");
    const genNotice = document.getElementById("gen-notice");
    const generatedBox = document.getElementById("generated");
    const olderBox = document.getElementById("generated-older");
    let current = "";
    let polling = 0;
    // The extraction the list shows, the posts ticked in it, and the generated post on show.
    let listShows = "";
    const ticked = new Set();
    let generatingNow = false;
    let resultShows = "";

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
      genNotice.hidden = true;
      generatingNow = false;
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
      // The same posts as on show: the list, and what is ticked in it, stay as they are.
      const shows = current + "|" + (kept.extracted_at || "");
      if (shows === listShows) return;
      listShows = shows;
      ticked.clear();
      postsList.replaceChildren();
      posts.forEach((post) => {
        const item = document.createElement("li");
        const meta = document.createElement("div");
        meta.className = "post-meta";
        const pick = document.createElement("label");
        pick.className = "pick";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.dataset.id = post.status_id;
        box.setAttribute("aria-label", "تحديد المنشور");
        box.addEventListener("change", () => {
          if (box.checked) ticked.add(post.status_id);
          else ticked.delete(post.status_id);
          showTicks();
        });
        // A tap anywhere on the card ticks it too, except on its links.
        item.addEventListener("click", (event) => {
          if (event.target.closest("a, label")) return;
          box.checked = !box.checked;
          box.dispatchEvent(new Event("change"));
        });
        const info = document.createElement("span");
        info.textContent = new Date(post.date).toLocaleString("ar", {dateStyle: "medium", timeStyle: "short"})
          + " · إعجاب " + Number(post.likes).toLocaleString("en-US") + " · مشاهدة " + Number(post.views).toLocaleString("en-US");
        const open = document.createElement("a");
        open.href = post.url;
        open.target = "_blank";
        open.rel = "noopener";
        open.textContent = "فتح المنشور";
        pick.append(box, info);
        meta.append(pick, open);
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
      document.getElementById("pick-all-row").hidden = !posts.length;
      document.getElementById("generate-bar").hidden = !posts.length;
      showTicks();
    }

    function showTicks() {
      const boxes = [...postsList.querySelectorAll("input[data-id]")];
      boxes.forEach((box) => {
        box.checked = ticked.has(box.dataset.id);
        box.closest("li").classList.toggle("is-picked", box.checked);
      });
      pickAll.checked = boxes.length > 0 && ticked.size === boxes.length;
      pickAll.indeterminate = ticked.size > 0 && ticked.size < boxes.length;
      document.getElementById("picked-count").textContent = "(" + ticked.size + " من " + boxes.length + ")";
      genGo.textContent = ticked.size ? "توليد (" + ticked.size + ")" : "توليد";
      genGo.disabled = !ticked.size || generatingNow;
    }

    pickAll.addEventListener("change", () => {
      ticked.clear();
      if (pickAll.checked) postsList.querySelectorAll("input[data-id]").forEach((box) => ticked.add(box.dataset.id));
      showTicks();
    });

    function showGenJob(job) {
      generatingNow = job.status === "running";
      genNotice.hidden = !job.status;
      if (job.status) {
        genNotice.textContent = job.message;
        genNotice.className = "notice " + (job.status === "failed" ? "is-bad" : "is-ok");
      }
      showTicks();
    }

    // One generated post: its text («نسخ النص»), its pictures, and what made it. base: where its pictures
    // are (an account's posts by default; «من رابط تغريدة» gives its own).
    function generatedCard(result, title, base) {
      base = base || apiRoot + "/content/generated/" + encodeURIComponent(current) + "/";
      const card = document.createElement("section");
      card.className = "generated";
      const head = document.createElement("div");
      head.className = "gen-head";
      if (title) {
        const name = document.createElement("b");
        name.textContent = title;
        head.append(name);
      }
      const meta = document.createElement("span");
      const made = (result.posts || []).length;
      meta.textContent = ((MODELS[result.model] || {}).label || result.model)
        + " · " + new Date(result.created_at * 1000).toLocaleString("ar", {dateStyle: "medium", timeStyle: "short"})
        + " · من " + (result.links ? (made === 1 ? "تغريدة واحدة" : made + " تغريدات") : made + " منشورات");
      head.append(meta);
      const text = document.createElement("p");
      text.className = "gen-text";
      text.dir = "auto";
      text.textContent = result.text || "(بلا نص)";
      const images = document.createElement("div");
      images.className = "gen-images";
      (result.images || []).forEach((name) => {
        const link = document.createElement("a");
        link.href = base + encodeURIComponent(result.id) + "/" + encodeURIComponent(name);
        link.target = "_blank";
        const image = document.createElement("img");
        image.src = link.href;
        image.alt = "صورة مولَّدة";
        image.loading = "lazy";
        link.append(image);
        images.append(link);
      });
      const copy = document.createElement("button");
      copy.type = "button";
      copy.textContent = "نسخ النص";
      copy.addEventListener("click", async () => {
        const copied = await copyText(result.text || "");
        copy.textContent = copied ? "نُسخ النص" : "تعذّر النسخ";
        setTimeout(() => {
          copy.textContent = "نسخ النص";
        }, 1500);
      });
      card.append(head, text, images);
      // Made from pasted tweets: links to them.
      if (Array.isArray(result.links) && result.links.length) {
        const source = document.createElement("p");
        source.className = "gen-source";
        source.append("المصدر: ");
        result.links.forEach((address, index) => {
          if (!/^https:\/\/(?:x|twitter)\.com\//.test(address)) return;
          const open = document.createElement("a");
          open.href = address;
          open.target = "_blank";
          open.rel = "noopener";
          open.textContent = result.links.length > 1 ? "التغريدة " + (index + 1) : "فتح التغريدة";
          if (source.childNodes.length > 1) source.append(" · ");
          source.append(open);
        });
        card.append(source);
      }
      card.append(copy);
      return card;
    }

    // The account's generated posts: those of the last «توليد» on top, the earlier ones folded under them.
    function showGenerated(list) {
      const shows = current + "|" + list.map((row) => row.id).join(",");
      if (shows === resultShows) return;
      resultShows = shows;
      const batchOf = (row) => row.batch || row.id;
      const last = list.length ? batchOf(list[0]) : "";
      const latest = list.filter((row) => batchOf(row) === last).sort((a, b) => (a.number || 0) - (b.number || 0));
      generatedBox.replaceChildren(...latest.map((row) => generatedCard(row, row.of > 1 ? "المنشور " + row.number + " من " + row.of : "آخر منشور مولَّد")));
      const older = list.filter((row) => batchOf(row) !== last);
      olderBox.hidden = !older.length;
      olderBox.querySelector("summary").textContent = "المنشورات المولَّدة السابقة (" + older.length + ")";
      document.getElementById("generated-older-list").replaceChildren(...older.map((row) => generatedCard(row, "")));
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
      const wasGenerating = generatingNow;
      showGenJob(data.gen_job || {});
      showGenerated(Array.isArray(data.generated) ? data.generated : []);
      if (wasGenerating && !generatingNow && (data.gen_job || {}).status === "done") {
        generatedBox.scrollIntoView({block: "start", behavior: "smooth"});
      }
      clearTimeout(polling);
      if ((data.job || {}).status === "running" || (data.gen_job || {}).status === "running") {
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
    genGo.addEventListener("click", async () => {
      const posts = [...ticked];
      if (!posts.length) return;
      generatingNow = true;
      showTicks();
      let data;
      try {
        const response = await fetch(apiRoot + "/content/generate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          cache: "no-store",
          body: JSON.stringify({csrf_token: csrf, username: current, model: genModel.value, count: Number(genCount.value), posts})
        });
        data = await response.json();
      } catch (_) {
        data = {message: "تعذّر الاتصال."};
      }
      if (!data.success) {
        showGenJob({status: "failed", message: data.message || "تعذّر بدء التوليد."});
        return;
      }
      showGenJob(data.job);
      loadPosts();
    });

    // The model picked last time, on this browser.
    try {
      const saved = localStorage.getItem("content-generate-model");
      if (saved && MODELS[saved]) genModel.value = saved;
    } catch (_) {}
    genModel.addEventListener("change", () => {
      try {
        localStorage.setItem("content-generate-model", genModel.value);
      } catch (_) {}
    });
    try {
      const saved = localStorage.getItem("content-generate-count");
      if (saved && [...genCount.options].some((option) => option.value === saved)) genCount.value = saved;
    } catch (_) {}
    genCount.addEventListener("change", () => {
      try {
        localStorage.setItem("content-generate-count", genCount.value);
      } catch (_) {}
    });

    // «من رابط تغريدة»: the pasted tweets go to Gemini; the posts made from links show under it, the
    // last «إرسال إلى Gemini»'s on top and the earlier ones folded.
    const linkField = document.getElementById("link");
    const linkGo = document.getElementById("link-go");
    const linkModel = document.getElementById("link-model");
    const linkCount = document.getElementById("link-count");
    const linkNotice = document.getElementById("link-notice");
    const linkOlder = document.getElementById("link-older");
    let linkRunning = false;
    let linkPolling = 0;
    let linkShows = "";

    function showLinkJob(job) {
      linkRunning = job.status === "running";
      linkGo.disabled = linkRunning;
      linkNotice.hidden = !job.status;
      if (job.status) {
        linkNotice.textContent = job.message;
        linkNotice.className = "notice " + (job.status === "failed" ? "is-bad" : "is-ok");
      }
    }

    function showLinkGenerated(list) {
      const shows = list.map((row) => row.id).join(",");
      if (shows === linkShows) return;
      linkShows = shows;
      const base = apiRoot + "/content/link-generated/";
      const batchOf = (row) => row.batch || row.id;
      const last = list.length ? batchOf(list[0]) : "";
      const latest = list.filter((row) => batchOf(row) === last).sort((a, b) => (a.number || 0) - (b.number || 0));
      document.getElementById("link-generated").replaceChildren(...latest.map((row) => generatedCard(row, row.of > 1 ? "المنشور " + row.number + " من " + row.of : "آخر منشور مولَّد", base)));
      const older = list.filter((row) => batchOf(row) !== last);
      linkOlder.hidden = !older.length;
      linkOlder.querySelector("summary").textContent = "المنشورات المولَّدة السابقة من الروابط (" + older.length + ")";
      document.getElementById("link-older-list").replaceChildren(...older.map((row) => generatedCard(row, "", base)));
    }

    async function loadLinkPosts() {
      let data;
      try {
        data = await (await fetch(apiRoot + "/content/link-posts", {cache: "no-store"})).json();
      } catch (_) {
        data = {};
      }
      const wasRunning = linkRunning;
      showLinkJob(data.job || {});
      showLinkGenerated(Array.isArray(data.generated) ? data.generated : []);
      if (wasRunning && !linkRunning && (data.job || {}).status === "done") {
        document.getElementById("link-generated").scrollIntoView({block: "start", behavior: "smooth"});
      }
      clearTimeout(linkPolling);
      if ((data.job || {}).status === "running") linkPolling = setTimeout(loadLinkPosts, 2000);
    }

    linkGo.addEventListener("click", async () => {
      const links = linkField.value.trim();
      if (!links) {
        showLinkJob({status: "failed", message: "الصق رابط التغريدة."});
        linkField.focus();
        return;
      }
      showLinkJob({status: "running", message: "جارٍ البدء…"});
      let data;
      try {
        const response = await fetch(apiRoot + "/content/link-generate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          cache: "no-store",
          body: JSON.stringify({csrf_token: csrf, links, model: linkModel.value, count: Number(linkCount.value)})
        });
        data = await response.json();
      } catch (_) {
        data = {message: "تعذّر الاتصال."};
      }
      if (!data.success) {
        showLinkJob({status: "failed", message: data.message || "تعذّر بدء التوليد."});
        return;
      }
      showLinkJob(data.job);
      loadLinkPosts();
    });
    linkField.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !linkGo.disabled) linkGo.click();
    });
    // The same model and count as «توليد», remembered on this browser.
    try {
      const savedModel = localStorage.getItem("content-generate-model");
      if (savedModel && MODELS[savedModel]) linkModel.value = savedModel;
      const savedCount = localStorage.getItem("content-generate-count");
      if (savedCount && [...linkCount.options].some((option) => option.value === savedCount)) linkCount.value = savedCount;
    } catch (_) {}
    linkModel.addEventListener("change", () => {
      try {
        localStorage.setItem("content-generate-model", linkModel.value);
      } catch (_) {}
    });
    linkCount.addEventListener("change", () => {
      try {
        localStorage.setItem("content-generate-count", linkCount.value);
      } catch (_) {}
    });
    loadLinkPosts();

    // Pages opened over plain http (a server IP) have no navigator.clipboard, so fall back to execCommand.
    async function copyText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          return true;
        } catch (_) {}
      }
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.cssText = "position: fixed; top: 0; left: 0; opacity: 0; font-size: 16px;";
      document.body.appendChild(area);
      area.select();
      area.setSelectionRange(0, text.length);
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch (_) {}
      area.remove();
      return copied;
    }
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
