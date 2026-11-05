"""«إنشاء محتوى»: its own page inside the script's panel. Kept beside the script, it is added by the
script itself (same address and port), which then shows «إنشاء محتوى» on its page.

The page makes new posts from tweets: paste a tweet's link (or a few). Each tweet's text and photos,
read through FxTwitter's free API (no browser and no account needed), go to a Gemini picture model,
which makes one new post from their idea: a short text and two pictures in two sizes, a tall one and a
wide one. Every generated post is kept, each in its own folder data/content_generated/from-links/<time>/.
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
    "Take the idea behind them and create ONE new post:\n"
    "- The text: very short, one line of a few words (at most 70 characters), as short as a real tweet. "
    "Thrilling, viral and mysterious: it hints and stops. Do not explain and do not tell the story.\n"
    "- Controversial: one bold, one-sided remark that people will want to argue with. Do not present both sides, "
    "do not ask for opinions, and never use words like controversy, debate or phenomenon.\n"
    "- Realistic: it sounds like a real person tweeting, in the same language, dialect and tone as the posts "
    "(casual and spontaneous), not like news, an article or an ad. Nothing made up. Bold is fine; no slurs or hate. "
    "No hashtags, no links.\n"
    "- One image for it: edit the photos or create new ones from their idea. Do not return a photo unchanged. "
    "They look like real casual phone photos, like the posts' photos: not staged and not like an ad.\n"
    "- Each image has one detail, tied to the post's idea, that makes people react and comment, the way the posts' "
    "photos do: a spontaneous 'dumb' slip people will rush to correct (wrong numbers, a wrong label, a misspelled word), "
    "something odd or funny that does not belong, or a short provocative label or number added on the photo. "
    "It feels natural, not like a planned joke.\n"
    "- No person in the images: no people, faces or bodies, even when the photos have them.\n"
    "Answer with the post text only, between <post> and </post> (no title, no notes, no translation), and the image."
)
# Sent after the tweets for the second and third post of one «إرسال إلى Gemini», with the texts made before it.
GENERATE_DIFFERENT = (
    "Posts already made from these. Yours must be clearly different from them: another angle or another "
    "idea from these posts, and another image:\n"
)
# How many posts one «إرسال إلى Gemini» makes (offered on the page; the first is the default).
GENERATE_COUNTS = (3, 2, 1)
# The pasted tweets are read through FxTwitter's free API, at most this many at once.
FX_STATUS_URL = "https://api.fxtwitter.com/status/{}"
LINK_MAX_TWEETS = 10
# The generated posts' folder, under data/content_generated.
LINKS_FOLDER = "from-links"
# Each post gets two images in two sizes: a tall one, then a wide one (asked for one after the other,
# each in its own size).
LINK_SIZES = ("3:4", "16:9")
# Then the second image, for the same post: what goes with it after the first image.
LINK_SECOND_IMAGE = (
    "Now make ONE more image for this same post, as its second photo: another scene or another angle of the same "
    "idea that goes with the first image, following the same image rules as above. Answer with the image only.\n"
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
    failed (no picture, or refused), so the next one of the same «إرسال إلى Gemini» is still tried."""

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


def ask_gemini(api_key: str, model: str, parts: list[dict], report, aspect: str = "") -> dict:
    """One request answered with text and pictures. A busy Gemini is asked again a few times. aspect: the
    pictures' shape ("3:4", "16:9"); a model that does not take it is asked again without it."""
    def request_body(shaped: bool) -> bytes:
        config: dict = {"responseModalities": ["TEXT", "IMAGE"]}
        if shaped:
            config["imageConfig"] = {"aspectRatio": aspect}
        return json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": config}).encode("utf-8")
    shaped = bool(aspect)
    body = request_body(shaped)
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
            if shaped and status == 400 and re.search(r"image_?config|aspect", detail, re.I):
                shaped = False                   # this model takes no image size: asked again without it
                body = request_body(False)
                continue
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

    tools (from the script): data_dir, gemini_key() -> the Gemini key saved on the panel.
    """
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

    def content_page(access: str):
        allowed(access)
        csrf_token = session.get("csrf_token")
        if not csrf_token:
            csrf_token = session["csrf_token"] = secrets.token_urlsafe(32)
        return render_template_string(PAGE, csrf_token=csrf_token, api_root="/" + access_path, panel="/" + access_path, models=GENERATE_MODELS, counts=GENERATE_COUNTS)

    def link_parts(model: str, tweets: list[dict]) -> tuple[list[dict], dict]:
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
        parts: list[dict] = [{"text": GENERATE_PROMPT}]
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

    def generate_link_one(api_key: str, model: str, parts: list[dict], made_before: list[str], progress) -> tuple[str, list]:
        """One post from pasted tweets: its text with a tall image, then a second, wide image for it (shown
        the first one, so the two go together). Without the second, the post keeps the first alone."""
        if made_before:
            parts = parts + [{"text": GENERATE_DIFFERENT + "\n".join(f"{n}. {text}" for n, text in enumerate(made_before, 1))}]
        text, pictures, reason = "", [], ""
        for attempt in range(2):
            text, pictures, reason = read_answer(ask_gemini(api_key, model, parts, progress, LINK_SIZES[0]))
            if pictures or reason not in ("", "STOP"):
                break
        if not pictures:
            if reason not in ("", "STOP", "MAX_TOKENS"):
                raise GenerateError(f"رفض Gemini توليد الصورة ({reason}).", fatal=False)
            raise GenerateError("لم يُرجع Gemini صورة.", fatal=False)
        first = pictures[0]
        progress("الصورة الثانية بحجم آخر…")
        second_ask = parts + [{"text": "The first image made for this post:"},
                              {"inlineData": {"mimeType": first[0], "data": base64.b64encode(first[1]).decode("ascii")}},
                              {"text": LINK_SECOND_IMAGE.format(post=text or "(no text)")}]
        second: list = []
        for attempt in range(2):
            try:
                _, second, reason = read_answer(ask_gemini(api_key, model, second_ask, progress, LINK_SIZES[1]))
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
                "batch": result["batch"], "number": result["number"], "of": result["of"]}
        (fresh / "result.json").write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        with generated_lock:
            fresh.rename(links_folder / stamp)

    def run_link_generation(api_key: str, status_ids: list[str], model: str, count: int) -> None:
        """Read the pasted tweets, then make count posts from them one after the other, each kept as soon as
        it is made."""
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
                    text, pictures = generate_link_one(api_key, model, parts, made, progress)
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

    app.add_url_rule("/<access>/content", "content_page", content_page, methods=["GET"])
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
    .hint { margin: 6px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .notice { margin-top: 10px; padding: 10px 12px; border-radius: 12px; font-size: 13px; line-height: 1.55; }
    .notice.is-ok { color: var(--ok-ink); background: var(--ok-bg); }
    .notice.is-bad { color: var(--clay); background: var(--clay-bg); }
    .gen-row { display: flex; gap: 6px; margin-top: 6px; }
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
    .gen-images figure { margin: 0; display: flex; flex-direction: column; gap: 3px; min-width: 0; }
    .gen-images figcaption { color: var(--muted); font-size: 12px; text-align: center; }
    .gen-source { margin: 0 0 8px; font-size: 12px; color: var(--muted); }
    .gen-source a { color: var(--pine); font-weight: 600; }
    .older { margin-top: 8px; }
    .older summary { padding: 6px 0; color: var(--pine-deep); font-size: 13px; font-weight: 600; cursor: pointer; }
    .older .generated { margin-top: 6px; background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); }
    .older .gen-text { font-size: 14px; }
    .older .gen-images { grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); }
  </style>
</head>
<body>
  <main>
    <div class="top">
      <h1>إنشاء محتوى</h1>
      <a class="back" href="{{ panel }}">العودة للوحة</a>
    </div>
    <section class="sheet">
      <h2>من رابط تغريدة</h2>
      <div class="row">
        <input id="link" type="text" maxlength="2000" autocomplete="off" spellcheck="false" placeholder="https://x.com/user/status/123…" aria-label="رابط التغريدة">
      </div>
      <div class="gen-row">
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
      <p class="hint">تُقرأ التغريدة (نصها وصورها) وتُرسل إلى Gemini ليصنع منها منشورًا جديدًا بنص وصورتين بحجمين مختلفين: طولية وعرضية. يمكن لصق أكثر من رابط.</p>
      <div id="link-notice" class="notice" role="status" hidden></div>
      <div id="link-generated"></div>
      <details id="link-older" class="older" hidden>
        <summary></summary>
        <div id="link-older-list"></div>
      </details>
    </section>
  </main>
  <script>
  (() => {
    const apiRoot = {{ api_root|tojson }};
    const csrf = {{ csrf_token|tojson }};
    const MODELS = {{ models|tojson }};

    // One generated post: its text («نسخ النص»), its pictures with their sizes, and the tweets it came from.
    function generatedCard(result, title) {
      const base = apiRoot + "/content/link-generated/";
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
        + " · من " + (made === 1 ? "تغريدة واحدة" : made + " تغريدات");
      head.append(meta);
      const text = document.createElement("p");
      text.className = "gen-text";
      text.dir = "auto";
      text.textContent = result.text || "(بلا نص)";
      const images = document.createElement("div");
      images.className = "gen-images";
      const SIZES = {"3:4": "طولية 3:4", "16:9": "عرضية 16:9"};
      (result.images || []).forEach((name, index) => {
        const link = document.createElement("a");
        link.href = base + encodeURIComponent(result.id) + "/" + encodeURIComponent(name);
        link.target = "_blank";
        const image = document.createElement("img");
        image.src = link.href;
        image.alt = "صورة مولَّدة";
        image.loading = "lazy";
        link.append(image);
        const size = (result.sizes || [])[index];
        if (size) {
          // With its size under it (the posts made from links have two, in two sizes).
          const figure = document.createElement("figure");
          const caption = document.createElement("figcaption");
          caption.textContent = SIZES[size] || size;
          figure.append(link, caption);
          images.append(figure);
        } else images.append(link);
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
      // Links to the tweets it came from.
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
      const batchOf = (row) => row.batch || row.id;
      const last = list.length ? batchOf(list[0]) : "";
      const latest = list.filter((row) => batchOf(row) === last).sort((a, b) => (a.number || 0) - (b.number || 0));
      document.getElementById("link-generated").replaceChildren(...latest.map((row) => generatedCard(row, row.of > 1 ? "المنشور " + row.number + " من " + row.of : "آخر منشور مولَّد")));
      const older = list.filter((row) => batchOf(row) !== last);
      linkOlder.hidden = !older.length;
      linkOlder.querySelector("summary").textContent = "المنشورات المولَّدة السابقة (" + older.length + ")";
      document.getElementById("link-older-list").replaceChildren(...older.map((row) => generatedCard(row, "")));
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
    // The model and count chosen last, remembered on this browser.
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
  })();
  </script>
</body>
</html>
'''
