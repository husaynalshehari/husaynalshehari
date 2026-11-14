"""«إنشاء محتوى»: its own page inside the script's panel. Kept beside the script, it is added by the
script itself (same address and port), which then shows «إنشاء محتوى» on its page.

The page makes new posts from tweets: paste a tweet's link (or a few). Each tweet's text and photos,
read through FxTwitter's free API (no browser and no account needed), go to a Gemini picture model,
which makes one new post from their idea: a short text and two pictures in two sizes, a tall one and a
wide one. Every generated post is kept, each in its own folder data/content_generated/from-links/<time>/.

Under the link box, «التغريدات المجموعة»: the tweets the script collected from the groups in the last
7 days, day by day, with «عرض الأكثر مشاهدة» (the script draws that part and answers its requests; an
older script, without it, keeps them on a page of their own).

«المحتوى السابق» shows, read only, what the page's old accounts part left: for each account it kept,
the posts generated from its tweets (data/content_generated/<user>/) and its extracted tweets with their
photos (data/content/<user>/). Nothing of it is deleted.
"""
from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from flask import abort, jsonify, render_template_string, request, send_from_directory, session

# The Gemini models offered on the page (the first is picked by default). Each answers with text and
# pictures; max_photos is the most photos it is sent (Google's limit on reference pictures).
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
# What Gemini is asked to make from the pasted tweets (their texts and photos follow it).
GENERATE_PROMPT = (
    "You write viral posts for X (Twitter). Below are posts, each with its text and photos.\n"
    "These posts got high views. First, for yourself (not in your answer), work out why: what in their first words "
    "makes people stop scrolling, what feeling they stir (curiosity, surprise, anger, laughter), why people reply to "
    "them or share them, and what their photos add. Then build the new post on those same reasons, not only on the "
    "same topic.\n"
    "Take the idea behind them and create ONE new post:\n"
    "- The text: very short, one line of a few words (at most 70 characters), as short as a real tweet. "
    "Thrilling, viral and mysterious: it hints and stops. Do not explain and do not tell the story.\n"
    "- Personal: in the first person, in the account owner's own voice: their own experience, opinion or criticism "
    "(something they saw, tried or cannot stand), said the way they would say it. Still a few words that hint, not a "
    "story. Not a general statement about people, not news, and not a description of the photo.\n"
    "- Controversial: one bold, one-sided remark that people will want to argue with. Do not present both sides, "
    "do not ask for opinions, and never use words like controversy, debate or phenomenon.\n"
    "- Realistic: it sounds like a real person tweeting, in the same language, dialect and tone as the posts "
    "(casual and spontaneous), not like news, an article or an ad. No made-up facts, numbers or news. Bold is fine; no slurs or hate. "
    "No hashtags, no links.\n"
    "- One image for it: edit the photos or create new ones from their idea. Do not return a photo unchanged. "
    "They look like real casual phone photos, like the posts' photos: not staged and not like an ad.\n"
    "- Each image has one detail, tied to the post's idea, that makes people react and comment, the way the posts' "
    "photos do: something odd or funny that does not belong, or a spontaneous 'dumb' slip people will rush to correct "
    "(something in the wrong place, the wrong way round or the wrong size). It feels natural, not like a planned joke, "
    "and it is seen, not written.\n"
    "- No added writing: do not add paper notes, handwritten signs, stickers, labels or captions to explain the idea "
    "or the joke; the scene tells it by itself. Writing shows only where the real scene already has it (a road sign, "
    "a shop sign, a screen), or when the idea itself is about something written.\n"
    "- No person in the images: no people, faces or bodies, even when the photos have them.\n"
    "Answer with the post text only, between <post> and </post> (no title, no notes, no translation), and the image."
)
# «بصيغة أنا»: when ticked, this goes with the text's rules (after "- Personal:"), so the post says "I" in so many
# words. Words that fit a man or a woman alike keep it right for any account.
GENERATE_FIRST_PERSON = (
    "- First person, in so many words: the text itself says what the writer saw, did, tried, asked or was told, "
    "with a first-person verb or word (in Arabic, like شفت، جربت، طلبت، قالوا لي), as their own experience. "
    "Prefer wording that fits a man or a woman alike.\n"
)
# Sent after the tweets for the second and third post of one «إنشاء منشور», with the texts made before it.
GENERATE_DIFFERENT = (
    "Posts already made from these. Yours must be clearly different from them: another angle or another "
    "idea from these posts, and another image:\n"
)
# How many posts one «إنشاء منشور» makes (offered on the page; the first is the default).
GENERATE_COUNTS = (3, 2, 1)
# The page on users' copies: one model, always «تفكير عميق», and no model named or to choose.
USERS_MODEL = "gemini-nano-banana-2.1"
# What that page says instead of the messages about the key (the owner's) or that offer another model
# ("" leaves a note out), and the refusals it says without their details.
UNAVAILABLE = "الإنشاء غير متاح الآن. أخبر صاحب البرنامج."
USERS_WORDING = (
    ("هذا النموذج غير متاح للمفتاح المحفوظ في اللوحة. اختر نموذجًا آخر.", UNAVAILABLE),
    ("المفتاح المحفوظ في اللوحة غير صالح.", UNAVAILABLE),
    ("حصة المفتاح المحفوظ في اللوحة لا تسمح بهذا الطلب الآن: انتهت الحصة، أو أن الفوترة غير مفعّلة عليه.", UNAVAILABLE),
    ("لا يوجد مفتاح محفوظ في اللوحة.", UNAVAILABLE),
    (" أو اختر نموذجًا آخر", ""),
    (" أو اختر نموذجًا أسرع", ""),
    ("جرّب تغريدة أو نموذجًا آخر.", "جرّب تغريدة أخرى."),
    ("هذا النموذج لا يقبل «تفكير عميق»، يكمل بدونه…", ""),
)
USERS_WORDING_PATTERNS = (
    (r"رُفض الطلب: [^\n]*", "رُفض الطلب. أعد المحاولة أو جرّب تغريدة أخرى."),
    (r"رُفض إنشاء الصورة \([^)]*\)\.", "رُفض إنشاء الصورة لهذه التغريدة."),
)
# The day a user's copy counts its posts in (see posts_per_day in attach): Saudi time, UTC+3.
DAY_OFFSET_SECONDS = 3 * 3600
# The pasted tweets are read through FxTwitter's free API, at most this many at once.
FX_STATUS_URL = "https://api.fxtwitter.com/status/{}"
# Gemini's address, in one place.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
# «تفكير عميق»: the model thinks more before it answers (better at keeping every rule at once; slower and a
# little dearer, as its thinking is paid like the text it writes). A model that does not take the setting is
# asked again without it, and is not sent it again.
DEEP_THINKING = {"thinkingLevel": "high"}
thinking_refused: set[str] = set()
LINK_MAX_TWEETS = 10
# The generated posts' folder, under data/content_generated.
LINKS_FOLDER = "from-links"
# Each post gets two images in two sizes: a tall one, then a wide one (asked for one after the other,
# each in its own size).
LINK_SIZES = ("3:4", "16:9")
# Then the second image, for the same post: what goes with it after the first image.
LINK_SECOND_IMAGE = (
    "Now make ONE more image for this same post, as its second photo: another scene or another angle of the same "
    "idea that goes with the first image, following the same image rules as above (no added writing). Answer with the image only.\n"
    "The post: <post>{post}</post>"
)
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


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
    failed (no picture, or refused), so the next one of the same «إنشاء منشور» is still tried."""

    def __init__(self, message: str, fatal: bool = True):
        super().__init__(message)
        self.fatal = fatal


def arabic_count(n: int, one: str, two: str, few: str, many: str) -> str:
    """A count in Arabic: ساعة، ساعتين، 3 ساعات، 11 ساعة."""
    if n == 1:
        return one
    if n == 2:
        return two
    return f"{n} {few if 3 <= n % 100 <= 10 else many}"


def generate_prompt(first_person: bool = False) -> str:
    """What Gemini is asked; with «بصيغة أنا», its rule goes with the text's rules."""
    if not first_person:
        return GENERATE_PROMPT
    head, marker, tail = GENERATE_PROMPT.partition("- Controversial:")
    return head + GENERATE_FIRST_PERSON + marker + tail if marker else GENERATE_PROMPT + "\n" + GENERATE_FIRST_PERSON


def gemini_problem(status: int, detail: str, busy: bool) -> str:
    """What to show for a request Gemini turned down (the page names no service)."""
    if re.search(r"api key not valid|api_key_invalid|invalid api key", detail, re.I):
        return "المفتاح المحفوظ في اللوحة غير صالح."
    if status == 404 or re.search(r"not found|is not supported", detail, re.I):
        return "هذا النموذج غير متاح للمفتاح المحفوظ في اللوحة. اختر نموذجًا آخر."
    if re.search(r"quota|billing", detail, re.I):
        return "حصة المفتاح المحفوظ في اللوحة لا تسمح بهذا الطلب الآن: انتهت الحصة، أو أن الفوترة غير مفعّلة عليه."
    if busy:
        return "الخدمة مشغولة الآن. أعد المحاولة بعد قليل أو اختر نموذجًا آخر."
    return "رُفض الطلب" + (f": {detail}" if detail else ".")


def ask_gemini(api_key: str, model: str, parts: list[dict], report, aspect: str = "", deep: bool = False) -> dict:
    """One request answered with text and pictures. A busy Gemini is asked again a few times. aspect: the
    pictures' shape ("3:4", "16:9"); deep: «تفكير عميق». A model that does not take either is asked again
    without it."""
    def request_body(shaped: bool, thinking: bool) -> bytes:
        config: dict = {"responseModalities": ["TEXT", "IMAGE"]}
        if shaped:
            config["imageConfig"] = {"aspectRatio": aspect}
        if thinking:
            config["thinkingConfig"] = dict(DEEP_THINKING)
        return json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": config}).encode("utf-8")
    shaped = bool(aspect)
    thinking = deep and model not in thinking_refused
    body = request_body(shaped, thinking)
    waits = (5, 12, 20, 30)
    for attempt in range(len(waits) + 1):
        ask = urllib.request.Request(
            GEMINI_URL.format(quote(model, safe="")),
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
            if shaped and status == 400 and re.search(r"image_?config|aspect", detail, re.I):
                shaped = False                   # this model takes no image size: asked again without it
                body = request_body(False, thinking)
                continue
            if thinking and status == 400 and re.search(r"thinking", detail, re.I):
                thinking = False                 # this model takes no thinking setting: asked again without it,
                thinking_refused.add(model)      # and not sent it again
                report("هذا النموذج لا يقبل «تفكير عميق»، يكمل بدونه…")
                body = request_body(shaped, False)
                continue
            busy = status in (429, 500, 502, 503, 504) or bool(
                re.search(r"high demand|try again later|unavailable|overloaded|resource exhausted|rate limit", detail, re.I))
            # "limit: 0": this key has no quota at all for the model, so asking again is no use.
            if busy and attempt < len(waits) and not re.search(r"limit:\s*0\b", detail):
                report(f"الخدمة مشغولة الآن، إعادة المحاولة بعد {arabic_count(waits[attempt], 'ثانية', 'ثانيتين', 'ثوانٍ', 'ثانية')}…")
                time.sleep(waits[attempt])
                continue
            raise GenerateError(gemini_problem(status, detail, busy)) from None
        except urllib.error.URLError as exc:
            # Not connected, so the request did not go out: asked again.
            if attempt < 2:
                report("تعذّر الاتصال، إعادة المحاولة…")
                time.sleep(waits[attempt])
                continue
            raise GenerateError("تعذّر الاتصال بخدمة الإنشاء.") from exc
        # Cut off after the request went out: not asked again, as Gemini may still make (and charge
        # for) its pictures.
        except TimeoutError as exc:
            raise GenerateError(f"لم يكتمل الإنشاء خلال {GENERATE_SECONDS // 60} دقائق. أعد المحاولة أو اختر نموذجًا أسرع.") from exc
        except OSError as exc:
            raise GenerateError("انقطع الاتصال أثناء الإنشاء. أعد المحاولة.") from exc
        except ValueError as exc:
            raise GenerateError("وصل رد غير مفهوم. أعد المحاولة.") from exc
    raise GenerateError("الخدمة مشغولة الآن. أعد المحاولة بعد قليل أو اختر نموذجًا آخر.")


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


def attach(app, access_path: str, tools) -> dict:
    """Add the page and its requests to the script's app, under the panel's own address.

    tools (from the script): data_dir, gemini_key() -> the Gemini key saved on the panel,
    tweets_part() -> the collected tweets' part of the page ({"style", "body", "script"}, ready to put
    in it), when the script has it, and posts_per_day: on a user's copy, the posts it may make in one day.

    Returns what the page took on: {"tweets": True} when it shows the collected tweets.
    """
    # On a user's copy, the posts it may make in one day (Saudi time); None or 0 on the owner's: no limit.
    posts_per_day = getattr(tools, "posts_per_day", None)
    # Users' copies (the only ones given posts_per_day) have the users' page: Nano Banana 2.1, always
    # thinking deeply, no model named, «بصيغة أنا» the only choice.
    users_page = bool(posts_per_day)

    def worded(message: str) -> str:
        """A message as this page says it (on the users' page: no model to name or to choose)."""
        if users_page:
            for old, new in USERS_WORDING:
                message = message.replace(old, new)
            for pattern, new in USERS_WORDING_PATTERNS:
                message = re.sub(pattern, new, message)
        return message
    tweets_part = getattr(tools, "tweets_part", None)
    # The generated posts' folder (kept where it was, so the posts made before stay listed), one
    # generation at a time (each is paid for), and how the last one is going.
    links_folder = Path(tools.data_dir) / "content_generated" / LINKS_FOLDER
    generating = threading.Lock()
    generated_lock = threading.Lock()
    link_job: dict = {}

    def allowed(path_part: str) -> None:
        if not hmac.compare_digest(path_part, access_path):
            abort(404)

    def csrf_ok(data: dict) -> bool:
        expected = session.get("csrf_token", "")
        return bool(expected) and hmac.compare_digest(str(data.get("csrf_token") or ""), expected)

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

    def allowance() -> dict | None:
        """On a user's copy (tools.posts_per_day): the posts left today and when tomorrow's come, with the
        line the page shows. None on the owner's copy, which has no limit. Only a post that was made counts."""
        if not posts_per_day:
            return None
        now = time.time()
        start = (now + DAY_OFFSET_SECONDS) // 86400 * 86400 - DAY_OFFSET_SECONDS
        made = sum(1 for row in read_results(links_folder) if float(row.get("created_at") or 0) >= start)
        left = max(0, posts_per_day - made)
        minutes = max(1, int(start + 86400 - now + 59) // 60)
        hours, minutes = divmod(minutes, 60)
        renews = " و".join(part for part in (
            arabic_count(hours, "ساعة", "ساعتين", "ساعات", "ساعة") if hours else "",
            arabic_count(minutes, "دقيقة", "دقيقتين", "دقائق", "دقيقة") if minutes else "") if part)
        if not left:
            text = (("استخدمت منشور اليوم." if posts_per_day == 1 else "استخدمت منشورات اليوم.")
                    + f" يتجدد بعد {renews}، عند منتصف الليل بتوقيت السعودية.")
        elif posts_per_day == 1:
            text = "لك منشور واحد كل يوم، ومنشور اليوم متاح."
        else:
            text = (f"لك {arabic_count(posts_per_day, 'منشور واحد', 'منشوران', 'منشورات', 'منشورًا')} كل يوم، "
                    f"بقي منها اليوم {arabic_count(left, 'منشور واحد', 'منشوران', 'منشورات', 'منشورًا')}.")
        return {"per_day": posts_per_day, "left": left, "text": text}

    def content_page(access: str):
        allowed(access)
        csrf_token = session.get("csrf_token")
        if not csrf_token:
            csrf_token = session["csrf_token"] = secrets.token_urlsafe(32)
        tweets = None
        if tweets_part:
            try:
                tweets = tweets_part()
            except Exception:
                # The rest of the page still works without them.
                app.logger.warning("content page: the collected tweets could not be shown", exc_info=True)
        # (The users' page is sent no models: none is named on it, not even in its source.)
        return render_template_string(PAGE, csrf_token=csrf_token, api_root="/" + access_path, panel="/" + access_path,
                                      models={} if users_page else GENERATE_MODELS,
                                      counts=GENERATE_COUNTS, tweets=tweets, limit=allowance(), users_page=users_page)

    def link_parts(model: str, tweets: list[dict], first_person: bool = False) -> tuple[list[dict], dict]:
        """The request made of the pasted tweets: the prompt, then each tweet's text and photos."""
        order = []
        for depth in range(4):
            for index, tweet in enumerate(tweets):
                if depth < len(tweet["files"]):
                    order.append((index, depth))
        picked: set[tuple[int, int]] = set()
        size = 0
        # Room is kept for the first image made, which goes with the request for the second one.
        for index, depth in order:
            if len(picked) >= GENERATE_MODELS[model]["max_photos"] - 1:
                break
            data = tweets[index]["files"][depth][1]
            if size + len(data) > GENERATE_MAX_BYTES - 4 * 1024 * 1024:
                continue
            picked.add((index, depth))
            size += len(data)
        parts: list[dict] = [{"text": generate_prompt(first_person)}]
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

    def generate_link_one(api_key: str, model: str, parts: list[dict], made_before: list[str], progress, deep: bool = False) -> tuple[str, list]:
        """One post from pasted tweets: its text with a tall image, then a second, wide image for it (shown
        the first one, so the two go together). Without the second, the post keeps the first alone."""
        if made_before:
            parts = parts + [{"text": GENERATE_DIFFERENT + "\n".join(f"{n}. {text}" for n, text in enumerate(made_before, 1))}]
        text, pictures, reason = "", [], ""
        for attempt in range(2):
            text, pictures, reason = read_answer(ask_gemini(api_key, model, parts, progress, LINK_SIZES[0], deep=deep))
            if pictures or reason not in ("", "STOP"):
                break
        if not pictures:
            if reason not in ("", "STOP", "MAX_TOKENS"):
                raise GenerateError(f"رُفض إنشاء الصورة ({reason}).", fatal=False)
            raise GenerateError("لم تُصنع صورة.", fatal=False)
        first = pictures[0]
        progress("الصورة الثانية بحجم آخر…")
        second_ask = parts + [{"text": "The first image made for this post:"},
                              {"inlineData": {"mimeType": first[0], "data": base64.b64encode(first[1]).decode("ascii")}},
                              {"text": LINK_SECOND_IMAGE.format(post=text or "(no text)")}]
        second: list = []
        for attempt in range(2):
            try:
                _, second, reason = read_answer(ask_gemini(api_key, model, second_ask, progress, LINK_SIZES[1], deep=deep))
            except GenerateError as exc:
                if exc.fatal and not re.search(r"مشغول|busy", str(exc)):
                    raise
                second = []
                break
            if second or reason not in ("", "STOP"):
                break
        return text, [first] + second[:1]

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
        kept = {"model": result["model"], "text": result["text"], "images": names, "sizes": list(LINK_SIZES[:len(names)]),
                "posts": result["posts"], "links": result["links"],
                "photos_sent": result["photos_sent"], "photos_total": result["photos_total"], "created_at": made_at,
                "batch": result["batch"], "number": result["number"], "of": result["of"], "deep": bool(result.get("deep")),
                "first_person": bool(result.get("first_person"))}
        (fresh / "result.json").write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        with generated_lock:
            while (links_folder / stamp).exists():       # (two posts kept within one millisecond)
                stamp = str(int(stamp) + 1)
            fresh.rename(links_folder / stamp)

    def run_link_generation(api_key: str, status_ids: list[str], model: str, count: int, deep: bool = False,
                            first_person: bool = False) -> None:
        """Read the pasted tweets, then make count posts from them one after the other, each kept as soon as
        it is made."""
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
            parts, info = link_parts(model, tweets, first_person)
            photos = info["photos_sent"]
            sent = ("قُرئت " + arabic_count(len(tweets), "التغريدة", "تغريدتان", "تغريدات", "تغريدة")
                    + (" و" + arabic_count(photos, "صورة واحدة", "صورتان", "صور", "صورة") if photos else " بلا صور")
                    + (f" من {info['photos_total']}" if photos < info["photos_total"] else ""))
            for number in range(1, count + 1):
                step = f"المنشور {number} من {count}" if count > 1 else ""

                def progress(message: str, step=step) -> None:
                    message = worded(message)
                    if message:
                        link_job.update(status="running", message=f"{step}: {message}" if step else message)

                # (No service or model named; the owner's page says when it thinks deeply.)
                thought = " بتفكير عميق" if deep and not users_page and model not in thinking_refused else ""
                link_job.update(status="running", message=f"جارٍ توليد {step or 'المنشور'}{thought}… {sent}. قد يستغرق دقيقة أو أكثر.")
                try:
                    text, pictures = generate_link_one(api_key, model, parts, made, progress, deep)
                except GenerateError as exc:
                    problem, last = (f"{step}: {exc}" if step else str(exc)), exc
                    if exc.fatal:
                        break
                    continue
                save_link_generated({**info, "model": model, "text": text, "pictures": pictures, "batch": batch,
                                     "number": len(made) + 1, "of": count, "deep": deep and model not in thinking_refused,
                                     "first_person": first_person})
                made.append(text)
            if len(made) == count:
                link_job.update(status="done", message="اكتمل التوليد: " + {1: "منشور واحد.", 2: "منشوران."}.get(count, f"{count} منشورات."))
            elif made:
                link_job.update(status="done", message=worded(f"اكتمل {len(made)} من {count}. لم يكتمل الباقي: {problem}"))
            elif last is not None and last.fatal:
                link_job.update(status="failed", message=worded(str(last)))
            else:
                before = "لم يكتمل أي منشور. " if count > 1 else ""
                link_job.update(status="failed", message=worded(f"{before}{problem} جرّب تغريدة أو نموذجًا آخر."))
        except GenerateError as exc:
            link_job.update(status="failed", message=worded(str(exc)))
        except Exception:
            link_job.update(status="failed", message="تعذّر التوليد.")
        finally:
            generating.release()

    def content_link_generate(access: str):
        """«إنشاء منشور»: the pasted tweets go to the chosen Gemini model; the page follows it."""
        allowed(access)
        data = request.get_json(silent=True) or {}
        if not csrf_ok(data):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        status_ids = tweet_ids_from(str(data.get("links") or ""))
        if not status_ids:
            return jsonify(success=False, message="الصق رابط تغريدة، مثل https://x.com/user/status/123…"), 400
        if len(status_ids) > LINK_MAX_TWEETS:
            return jsonify(success=False, message=f"الحد الأقصى {LINK_MAX_TWEETS} تغريدات في المرة الواحدة."), 400
        model = USERS_MODEL if users_page else str(data.get("model") or "")
        if model not in GENERATE_MODELS:
            return jsonify(success=False, message="اختر النموذج."), 400
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
            return jsonify(success=False, message=worded("لا يوجد مفتاح محفوظ في اللوحة.")), 400
        if not generating.acquire(blocking=False):
            return jsonify(success=False, message="يعمل الآن توليد آخر. انتظر حتى ينتهي."), 409
        quota = allowance()
        if quota is not None:
            if quota["left"] <= 0:
                generating.release()
                return jsonify(success=False, message=quota["text"], limit=quota), 429
            count = min(count, quota["left"])
        link_job.clear()
        link_job.update(status="running", message="جارٍ قراءة التغريدة…")
        deep = users_page or bool(data.get("deep"))
        first_person = bool(data.get("first_person"))
        threading.Thread(target=run_link_generation, args=(api_key, status_ids, model, count, deep, first_person),
                         name="content-link-generate", daemon=True).start()
        return jsonify(success=True, message="بدأ التوليد.", job=dict(link_job))

    def content_link_posts(access: str):
        """The posts made from pasted tweets, and how the last «إنشاء منشور» is going."""
        allowed(access)
        return jsonify(success=True, job=dict(link_job), generated=read_results(links_folder), limit=allowance())

    # «المحتوى السابق»: what the old accounts part left, read only.
    data_dir = Path(tools.data_dir)
    old_generated_root = data_dir / "content_generated"
    old_media_root = data_dir / "content"

    def old_key(value: str) -> str:
        """An account's folder name (its username, lowercased); "" when it is not one."""
        return value.lower() if re.fullmatch(r"[A-Za-z0-9_]{1,15}", value or "") else ""

    def old_names() -> dict[str, tuple[str, str]]:
        """The accounts the old part kept: folder name → (username, name)."""
        try:
            loaded = json.loads((data_dir / "content_accounts.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rows = loaded.get("accounts") if isinstance(loaded, dict) else None
        return {old_key(str(row["username"])): (str(row["username"]), str(row.get("name") or row["username"])[:80])
                for row in rows or [] if isinstance(row, dict) and old_key(str(row.get("username") or ""))}

    def old_kept(key: str) -> dict:
        try:
            loaded = json.loads((old_media_root / key / "posts.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def old_generated(key: str) -> list[dict]:
        """An account's generated posts, newest first. The one post an older version kept straight in the
        account's folder gets a folder of its own first, as the others."""
        base = old_generated_root / key
        with generated_lock:
            if (base / "result.json").is_file():
                try:
                    loaded = json.loads((base / "result.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    loaded = {}
                loaded = loaded if isinstance(loaded, dict) else {}
                try:
                    made_at = float(loaded.get("created_at") or 0) or time.time()
                except (TypeError, ValueError):
                    made_at = time.time()
                target = base / str(int(made_at * 1000))
                target.mkdir(parents=True, exist_ok=True)
                for name in [*(loaded.get("images") or []), "result.json"]:
                    if re.fullmatch(r"[12]\.(png|jpg|webp)|result\.json", str(name)) and (base / str(name)).is_file():
                        (base / str(name)).replace(target / str(name))
        return read_results(base)

    def content_archive(access: str):
        """The accounts that left content: each with its name and how many generated posts and tweets."""
        allowed(access)
        names = old_names()
        keys: set[str] = set()
        for root in (old_generated_root, old_media_root):
            try:
                keys |= {old_key(entry.name) for entry in root.iterdir() if entry.is_dir() and old_key(entry.name)}
            except OSError:
                pass
        rows = []
        for key in sorted(keys):
            base = old_generated_root / key
            try:
                generated = sum(1 for entry in base.iterdir() if entry.is_dir() and re.fullmatch(r"\d{10,16}", entry.name)
                                and (entry / "result.json").is_file()) + int((base / "result.json").is_file())
            except OSError:
                generated = 0
            kept = len(old_kept(key).get("posts") or [])
            if generated or kept:
                username, name = names.get(key, (key, key))
                rows.append({"key": key, "username": username, "name": name, "generated": generated, "kept": kept})
        return jsonify(success=True, accounts=rows)

    def content_archive_account(access: str, key: str):
        """One account's generated posts and extracted tweets."""
        allowed(access)
        key = old_key(key)
        if not key:
            abort(404)
        return jsonify(success=True, generated=old_generated(key), kept=old_kept(key))

    def content_archive_generated(access: str, key: str, made: str, name: str):
        allowed(access)
        key = old_key(key)
        if not key or not re.fullmatch(r"\d{10,16}", made) or not re.fullmatch(r"[12]\.(png|jpg|webp)", name):
            abort(404)
        return send_from_directory(old_generated_root / key / made, name)

    def content_archive_media(access: str, key: str, name: str):
        allowed(access)
        key = old_key(key)
        if not key or not re.fullmatch(r"\d+_\d+\.(jpg|png|webp|gif)", name):
            abort(404)
        return send_from_directory(old_media_root / key, name)

    def content_link_media(access: str, made: str, name: str):
        allowed(access)
        if not re.fullmatch(r"\d{10,16}", made) or not re.fullmatch(r"[12]\.(png|jpg|webp)", name):
            abort(404)
        return send_from_directory(links_folder / made, name)

    app.add_url_rule("/<access>/content", "content_page", content_page, methods=["GET"])
    app.add_url_rule("/<access>/content/link-generate", "content_link_generate", content_link_generate, methods=["POST"])
    app.add_url_rule("/<access>/content/link-posts", "content_link_posts", content_link_posts, methods=["GET"])
    app.add_url_rule("/<access>/content/link-generated/<made>/<name>", "content_link_media", content_link_media, methods=["GET"])
    app.add_url_rule("/<access>/content/archive", "content_archive", content_archive, methods=["GET"])
    app.add_url_rule("/<access>/content/archive/<key>", "content_archive_account", content_archive_account, methods=["GET"])
    app.add_url_rule("/<access>/content/archive/<key>/generated/<made>/<name>", "content_archive_generated", content_archive_generated, methods=["GET"])
    app.add_url_rule("/<access>/content/archive/<key>/media/<name>", "content_archive_media", content_archive_media, methods=["GET"])
    return {"tweets": tweets_part is not None}


PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>إنشاء محتوى</title>
  <style>
    /* The same look as the panel's page: its colours, its header card, its cards and its buttons. */
    :root { --paper: #f3f5fa; --sheet: #ffffff; --ink: #101828; --muted: #667085; --line: #e4e7ec; --pine: #2f5bea; --pine-deep: #2447c8;
      --clay: #b42318; --clay-bg: #fef3f2; --ok-bg: #ecfdf3; --ok-ink: #067647;
      --shadow: 0 0 0 1px rgba(16, 24, 40, 0.06), 0 1px 2px rgba(16, 24, 40, 0.05), 0 12px 32px rgba(16, 24, 40, 0.06); color-scheme: light; }
    * { box-sizing: border-box; }
    html { -webkit-font-smoothing: antialiased; -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
    body { margin: 0; min-height: 100vh; min-height: 100dvh; background: var(--paper); color: var(--ink);
      font-family: "IBM Plex Sans Arabic", "Segoe UI", Tahoma, sans-serif; font-size: 15px; line-height: 1.55; overflow-x: hidden; }
    body::before { content: ""; position: fixed; inset: 0 0 auto 0; height: 3px; background: linear-gradient(90deg, var(--pine), #7c5cff); }
    [hidden] { display: none !important; }
    :focus-visible { outline: 2px solid rgba(47, 91, 234, 0.4); outline-offset: 2px; }
    main { width: min(520px, 100%); margin: 0 auto; padding: calc(12px + env(safe-area-inset-top)) 14px calc(18px + env(safe-area-inset-bottom)); }
    .top { display: flex; align-items: center; gap: 12px; padding: 14px; border-radius: 18px; background: var(--sheet); box-shadow: var(--shadow); }
    .mark { position: relative; flex: 0 0 auto; width: 38px; height: 38px; border-radius: 12px; background: linear-gradient(135deg, var(--pine), #7c5cff);
      box-shadow: 0 6px 14px rgba(47, 91, 234, 0.28); }
    .mark::before { content: ""; position: absolute; inset-inline-start: 11px; top: 11px; bottom: 11px; width: 16px; border-radius: 4px;
      background: rgba(255, 255, 255, 0.94); }
    .titles { min-width: 0; }
    .eyebrow { margin: 0; color: var(--muted); font-size: 11.5px; font-weight: 500; }
    h1 { margin: 0; font-size: 18px; line-height: 1.2; font-weight: 600; letter-spacing: -0.01em; }
    .back { margin-inline-start: auto; flex: 0 0 auto; display: inline-flex; align-items: center; min-height: 34px; padding: 0 11px; border-radius: 11px;
      background: var(--sheet); box-shadow: inset 0 0 0 1px var(--line), 0 1px 2px rgba(16, 24, 40, 0.04); color: var(--ink); font-size: 12px;
      font-weight: 600; text-decoration: none; white-space: nowrap; transition: background 0.15s; }
    .back:hover { background: #f9fafb; }
    .sheet { margin-top: 12px; padding: 14px; border-radius: 18px; background: var(--sheet); box-shadow: var(--shadow); }
    h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; font-size: 14px; font-weight: 600; }
    h2::before { content: ""; flex: 0 0 auto; width: 4px; height: 15px; border-radius: 2px; background: linear-gradient(var(--pine), #7c5cff); }
    .hint { margin: 0; color: var(--muted); font-size: 13px; }
    #tweets { --tweets-day: #f9fafb; --tweets-tweet: var(--sheet); }
  </style>
  {%- if tweets %}
  {{ tweets["style"]|safe }}
  <style>
    /* The tweets' part comes with its own plain look: here it is finished like the rest of the page. */
    #tweets details { border-radius: 14px; }
    #tweets summary { padding: 11px 12px; }
    #tweets ol { gap: 8px; padding: 0 12px 12px; }
    #tweets li { padding: 10px 12px; border-radius: 12px; }
    #tweets .tw-open { top: 8px; inset-inline-end: 8px; background: #f9fafb; }
    #tweets .vbtn { min-height: 32px; padding: 0 11px; border-radius: 9px; background: linear-gradient(135deg, var(--pine), #5b6ef5); color: #fff;
      box-shadow: 0 4px 10px rgba(47, 91, 234, 0.22); }
    #tweets .vbtn:disabled { box-shadow: none; }
  </style>
  {%- endif %}
</head>
<body>
  <main>
    <header class="top">
      <span class="mark" aria-hidden="true"></span>
      <div class="titles">
        <p class="eyebrow">من قروب الدردشة</p>
        <h1>إنشاء محتوى</h1>
      </div>
      <a class="back" href="{{ panel }}">العودة للوحة</a>
    </header>
    {%- if tweets %}
    <section id="tweets" class="sheet">
      <h2>التغريدات المجموعة</h2>
      {{ tweets["body"]|safe }}
    </section>
    {%- else %}
    <section class="sheet">
      <h2>التغريدات المجموعة</h2>
      <p class="hint">تعذّر عرض التغريدات الآن. أعد فتح الصفحة.</p>
    </section>
    {%- endif %}
  </main>
  {%- if tweets %}
  {{ tweets["script"]|safe }}
  {%- endif %}
</body>
</html>
'''
