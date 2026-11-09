#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The «تعليقات» app's server, on the groups server: it keeps the Claude key and the model here, reads each tweet
without a browser (FxTwitter's free API), asks Claude for one comment with the groups script's rules, and answers
the app. The app keeps only this server's link: its address, a secret, and its certificate's fingerprint (the
connection is encrypted, and the app talks to no other server). No key is ever on a phone.

On the groups server, as root (it asks for the key, runs as a service that starts with the server, checks itself,
and prints the link to paste in the app):
    mkdir -p ~/comments-server && cd ~/comments-server && \\
    curl -fsSL -o comments_server.py https://raw.githubusercontent.com/husaynalshehari/husaynalshehari/ccr-695f6c04-mlgxcr/comments-app/comments_server.py && \\
    python3 comments_server.py --setup

Then, in the same folder:
    python3 comments_server.py --link                       the link again
    python3 comments_server.py --key                        another Claude key
    python3 comments_server.py --model claude-sonnet-5-5    another model
    python3 comments_server.py --new-link                   a new secret (the old link stops working)
    python3 comments_server.py --host my.domain.com         another address in the link (a domain, or the right IP)

If the app says the server does not answer, open the port (8443, TCP) in the hosting's firewall, in its panel.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import http.client
import json
import os
import random
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = 1
SCRIPT = Path(__file__).resolve()
HERE = Path(os.environ.get("COMMENTS_SERVER_DIR") or SCRIPT.parent)
CONFIG = HERE / "comments_server.json"
CERT = HERE / "comments_server_cert.pem"
CERT_KEY = HERE / "comments_server_key.pem"
SERVICE = "comments-server"
UNIT = Path("/etc/systemd/system") / (SERVICE + ".service")
DEFAULT_PORT = 8443
DEFAULT_MODEL = "claude-haiku-5-5"
BIND = os.environ.get("COMMENTS_BIND") or "0.0.0.0"

CLAUDE_URL = os.environ.get("COMMENTS_CLAUDE_URL") or "https://api.anthropic.com/v1/messages"
CLAUDE_VERSION = "2026-10-21"
CLAUDE_EFFORT = "medium"
FX_URL = os.environ.get("COMMENTS_FX_URL") or "https://api.fxtwitter.com/status/"
PHOTO_PREFIX = os.environ.get("COMMENTS_PHOTO_PREFIX") or "https://pbs.twimg.com/"
BUSY_WAITS = tuple(float(v) for v in (os.environ.get("COMMENTS_BUSY_WAITS") or "5,12,20,30").split(","))
MAX_CHARS = 100
TWEET_TEXT = 1200
MAX_PHOTOS = 2
MAX_PHOTO_BYTES = 3_500_000
CLAUDE_AT_ONCE = 4
TWEET_KEPT_SECONDS = 600

KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,250}")
MODEL_PATTERN = re.compile(r"[a-z0-9][a-z0-9.\-]{1,60}")
HOST_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?|\[?[0-9A-Fa-f:]{2,39}\]?")

# ---------------------------------------------------------------- the comment's rules (as in the groups script)

VOICE_RULES = (
    "You write as a man: when you speak of yourself, use masculine forms (in Arabic: مبسوط، متأكد).",
    "You write as a woman: when you speak of yourself, use feminine forms (in Arabic: مبسوطة، متأكدة).",
    "The reply must suit a man or a woman: never use words that show the writer's gender "
    "(in Arabic, no forms like مبسوط or مبسوطة, متأكد or متأكدة).",
)
# How a reply takes the tweet: one is drawn for each reply, so they do not all read the same way.
ANGLES = (
    "a frank remark on its point, the way people talk among friends",
    "you disagree with one point in it, and say why",
    "a detail in it that most people will miss",
    "a light, dry joke about the situation",
    "you doubt one part of it, and say what does not add up",
    "a comparison with something from everyday life",
    "what it will lead to, or what comes next",
    "a short question about one point in it",
)
# How a phone writes: one manner, drawn there once and kept, as a real person keeps their own.
MANNERS = (
    "short and direct, no emojis",
    "calm and down-to-earth",
    "sarcastic and witty",
    "blunt and critical, but not rude",
    "easygoing; now and then one emoji at the end",
    "curious; sometimes ends with a short question",
    "matter-of-fact; sticks to what the tweet says",
    "playful, with everyday slang",
)
LENGTHS = ((6, 10), (8, 13), (10, 16))
DIACRITICS = re.compile(r"[\u064B-\u0652\u0656-\u065F\u0670]")
# Words a reply cannot end on, when one that is too long is shortened.
HANGING_WORDS = {"و", "او", "أو", "بس", "لكن", "لان", "لأن", "عشان", "اللي", "الي", "في", "من", "على", "عن", "مع", "الى",
                 "إلى", "يعني", "ثم", "حتى", "اذا", "إذا", "لو", "ما", "لا", "كان", "and", "or", "but", "the", "to", "of",
                 "in", "a", "an", "with", "for", "that", "ve", "ama", "ile", "için", "bir", "de", "da"}


class CommentError(Exception):
    """What stops one tweet's comment; its message is shown on the tweet's card in the app."""


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


# ---------------------------------------------------------------- the tweet, without a browser

def read_tweet(status_id: str) -> dict:
    """One tweet through FxTwitter: its link, author, text, the post it quotes, and its photos' addresses (a
    video's picture for a video)."""
    link = f"https://x.com/i/status/{status_id}"
    request = urllib.request.Request(FX_URL + status_id, headers={"User-Agent": "tg-comments/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            raw = answer.read(4 << 20)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410):
            raise CommentError("التغريدة غير موجودة أو حسابها خاص.") from None
        if exc.code in (429, 503):
            raise CommentError("خدمة قراءة التغريدات مشغولة الآن. أعد المحاولة بعد دقائق.") from None
        raise CommentError(f"تعذّرت قراءة التغريدة (رمز {exc.code}).") from None
    except (urllib.error.URLError, OSError):
        raise CommentError("تعذّرت قراءة التغريدة: السيرفر لم يصل إلى خدمة القراءة.") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        data = None                  # (FxTwitter answers a page, not data, for some tweets that are not there)
    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        raise CommentError("التغريدة غير موجودة أو حسابها خاص.")
    photos: list[str] = []
    media = tweet.get("media") if isinstance(tweet.get("media"), dict) else {}
    for item in media.get("all") or []:
        if isinstance(item, dict):
            address = str((item.get("url") if item.get("type") == "photo" else item.get("thumbnail_url")) or "")
            if address.startswith(PHOTO_PREFIX) and address not in photos:
                photos.append(address)
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    quote = tweet.get("quote") if isinstance(tweet.get("quote"), dict) else {}
    return {"id": status_id, "url": str(tweet.get("url") or link)[:300], "author": str(author.get("screen_name") or "")[:30],
            "text": str(tweet.get("text") or "").strip(), "quote": str(quote.get("text") or "").strip(), "photos": photos[:4]}


def download_photos(tweet: dict) -> list:
    """The tweet's first photos, in a medium size: (type, bytes) each; one that cannot be had is left out."""
    photos = []
    for address in tweet["photos"]:
        if len(photos) >= MAX_PHOTOS:
            break
        url = re.sub(r"name=[A-Za-z0-9]+", "name=medium", address) if "name=" in address else address
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tg-comments/1.0"}), timeout=30) as answer:
                kind = (answer.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                body = answer.read(MAX_PHOTO_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if body and len(body) <= MAX_PHOTO_BYTES and kind in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            photos.append((kind, body))
    return photos


# ---------------------------------------------------------------- what Claude is asked

def prompt(voice: int, manner: int, before: list, photos: bool, rng: random.Random) -> str:
    """The ask for one reply, with the angle and the length drawn for it, the phone's manner and voice, and the
    replies it must not look like."""
    low, high = rng.choice(LENGTHS)
    text = (
        ("Look at the tweet's photos too. " if photos else "")
        + "Write one public reply to this tweet, as an ordinary person scrolling X who comments on what it says. "
        "Read the whole tweet first and answer what it really says (its point, and its ending when it tells a story); "
        "do not guess beyond it. "
        "Not personal: the reply is about the tweet's topic, not about you. No stories or experiences of yours, "
        "nothing that happened to you, your family or your friends, and nothing about your own life (nothing like: "
        "صار معي، جربت، شفت بنفسي، أخوي، عندنا في البيت، أتذكر لما، لو كنت مكانه). "
        "Speak about its topic, not to please its author: do not compliment, thank or flatter the author, do not "
        "praise the tweet (nothing like: well said, you're right, great post, صدقت، كفو، أبدعت، كلامك ذهب), and do "
        "not just agree. Honest, never rude: no insults, and do not mock the author. "
        f"This time: {rng.choice(ANGLES)}. Your manner (always the same): {MANNERS[manner % len(MANNERS)]}. "
        "If the tweet is a prayer (دعاء), about religion, a death, an illness or other sad news, leave the angle and "
        "the manner: reply sincerely and respectfully, with no joke and no disagreement. "
        f"At most {MAX_CHARS} characters, spaces included (about {low} to {high} words). Finish the sentence. "
        "Never stop mid-word or mid-phrase. "
        "Write in the tweet's own language. An Arabic tweet gets everyday spoken Saudi Arabic (عامية سعودية بيضاء), "
        "the way people really type on X, never formal Arabic (فصحى): no words like الأولى، ينبغي، لا تثبت، ما زال، "
        "إذ، لذا، حيث، كما أن. A tweet in another language (English, Turkish…) gets its reply in that language. "
        "Plain letters only: no diacritics (no تشكيل: no fatha, damma, kasra, tanween, shadda or sukun), and no "
        "commas (، or ,). " + VOICE_RULES[voice % len(VOICE_RULES)] + " "
        "Sound like a real person, specific and casual, not a teacher, a preacher, a brand or an assistant: no "
        "advice or lessons to the author or to people (nothing like: الأولى أن، يجب، الأفضل أن، تذكّر، نصيحتي), no "
        "general wisdom or sayings, no neat conclusion; just make your point about the topic itself. "
        + ("The photos are the tweet's own: never mention them, the picture, the post or the tweet "
           "(no الصورة، المنشور، التغريدة); react to what is in them. " if photos else
           "Never mention the post or the tweet (no المنشور، التغريدة); react to what it says. ")
        + "Do not open with filler (like صراحة، فعلاً، والله) or by repeating the tweet. "
        "No hashtags, no links, no quotes, no labels, no introduction. Return only the finished reply."
    )
    if before:
        text += ("\n\nYour own last replies to other tweets. This one must not look like them (another opening, "
                 "another shape, other words):" + "".join("\n- " + reply for reply in before))
    return text


def tweet_words(tweet: dict) -> str:
    """The tweet's words as Claude reads them (with the post it quotes)."""
    words = tweet["text"] or "(no text: only its photos)"
    if tweet["quote"]:
        words += "\n(It quotes: " + tweet["quote"] + ")"
    return words[:TWEET_TEXT]


effort_refused: set = set()


def ask_claude(key: str, model: str, photos: list, text: str) -> str:
    """One answer from Claude (its text blocks; it may think first), at medium effort. A model that takes no effort
    setting is asked again without it, a busy Claude a few times, and photos it cannot read are left out."""
    sent = list(photos)
    effort = model not in effort_refused
    busy = lost = 0
    while True:
        content = [{"type": "image", "source": {"type": "base64", "media_type": kind, "data": base64.b64encode(data).decode("ascii")}}
                   for kind, data in sent]
        content.append({"type": "text", "text": text})
        # (Its thinking counts toward max_tokens: room for it, and for the short reply after it.)
        body = {"model": model, "max_tokens": 2048, "messages": [{"role": "user", "content": content}]}
        if effort:
            body["output_config"] = {"effort": CLAUDE_EFFORT}
        request = urllib.request.Request(CLAUDE_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "x-api-key": key,
                                                  "anthropic-version": CLAUDE_VERSION})
        try:
            with urllib.request.urlopen(request, timeout=90) as answer:
                status, raw = answer.status, answer.read(2 << 20)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(2 << 20)
        except (urllib.error.URLError, OSError, ValueError):
            if lost < 2:
                time.sleep(BUSY_WAITS[min(lost, len(BUSY_WAITS) - 1)])
                lost += 1
                continue
            raise CommentError("السيرفر لم يصل إلى Claude. أعد المحاولة بعد قليل.") from None
        try:
            data = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if status == 200:
            if data.get("stop_reason") == "refusal":
                raise CommentError("امتنع Claude عن التعليق على هذه التغريدة.")
            return "\n".join(str(block.get("text") or "") for block in data.get("content") or []
                             if isinstance(block, dict) and block.get("type") == "text")
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        detail = str(error.get("message") or "")
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        code = str(details.get("error_code") or "")
        detail = re.sub(r"\s+", " ", detail.replace(key, "…") if key else detail).strip()[:160]
        lower = detail.lower()
        if effort and status == 400 and ("effort" in lower or "output_config" in lower):
            effort = False                       # this model takes no effort setting: asked again without it,
            effort_refused.add(model)            # and not sent it again
            continue
        if sent and status == 400 and "image" in lower:
            sent = []                            # a photo it cannot read: asked again from the text alone
            continue
        if status == 401:
            raise CommentError("مفتاح Claude في السيرفر غير صالح. غيّره على السيرفر: python3 comments_server.py --key")
        if status == 403:
            if any(word in lower for word in ("not allowed", "forbidden", "region", "country")):
                raise CommentError("Claude لا يقبل الطلبات من هذا السيرفر.")
            raise CommentError("مفتاح Claude في السيرفر بلا صلاحية لهذا الطلب" + (": " + detail if detail else "."))
        if code == "enforced_spend_limit_reached" or any(word in lower for word in ("usage limits", "credit balance", "billing")):
            raise CommentError("بلغ حساب Claude حد الصرف أو نفد رصيده.")
        if status == 404 and "model" in lower:
            raise CommentError("النموذج المضبوط في السيرفر غير موجود. غيّره: python3 comments_server.py --model …")
        if status in (429, 500, 502, 503, 504, 529):
            if busy < len(BUSY_WAITS):
                time.sleep(BUSY_WAITS[busy])
                busy += 1
                continue
            raise CommentError("Claude مشغول الآن. أعد المحاولة بعد قليل.")
        raise CommentError(("رفض Claude الطلب: " + detail) if detail else f"تعذّر الاتصال بـ Claude (رمز {status}).")


def clean_comment(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = text.replace("**", "")
    text = re.sub(r"^(?:reply|comment|التعليق|الرد|الدعاء)\s*[:：\-]\s*", "", text, flags=re.I)
    text = re.sub(r"^[\w\u0600-\u06FF]{2,24}\s*[:：]\s*", "", text)
    text = text.strip("\"'«»“” ")
    if len(text) > 280:
        cut = text[:280]
        end = max(cut.rfind("؟"), cut.rfind("?"), cut.rfind("!"), cut.rfind("."), cut.rfind("…"))
        text = cut[: end + 1] if end >= 40 else cut.rsplit(" ", 1)[0]
    return text.strip()


def comment_is_complete(text: str) -> bool:
    # An emoji or two at the end are fine: the words before them are what is checked.
    text = re.sub(r"[\s\u200D\uFE0F\u2600-\u27BF\U0001F000-\U0001FAFF]+$", "", text or "")
    words = [word for word in text.split() if word]
    if len(words) < 6:
        return False
    if re.search(r"[،,؛;:：\-–—/\\*]$", text.strip()):
        return False
    last = re.sub(r"[.!?؟…\"'«»“”]+$", "", words[-1])
    return len(last) > 2


def polish_comment(text: str) -> str:
    """The reply as it is posted: no Arabic diacritics and no commas (a number's thousands keep their digits)."""
    text = DIACRITICS.sub("", text or "")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    text = re.sub(r"\s*[،,؛;]\s*", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fit_comment(text: str, limit: int = MAX_CHARS) -> str:
    """A reply cut to at most limit characters, at a word, and not on a word that leaves it hanging."""
    words = text.split()
    while words and len(" ".join(words)) > limit:
        words.pop()
    while words and words[-1].lower() in HANGING_WORDS:
        words.pop()
    return " ".join(words)


def complete_reply(key: str, model: str, voice: int, manner: int, tweet: dict, photos: list, before: list, rng) -> str:
    best = ""
    for _ in range(2):               # a reply that comes back cut off is asked for once more
        ask = prompt(voice, manner, before, bool(photos), rng) + "\n\nTweet text:\n" + tweet_words(tweet)
        text = clean_comment(ask_claude(key, model, photos, ask))
        if comment_is_complete(text):
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        return best
    raise CommentError("لم يكتب Claude تعليقًا كاملًا. أعد المحاولة.")


def write_comment(key: str, model: str, voice: int, manner: int, tweet: dict, photos: list, before: list, rng=None) -> str:
    """One reply as it is posted: whole, no diacritics, no commas, at most MAX_CHARS characters (one that comes back
    longer is asked for once more, then shortened at a word)."""
    rng = rng or random.SystemRandom()
    first = polish_comment(complete_reply(key, model, voice, manner, tweet, photos, before, rng))
    if len(first) <= MAX_CHARS:
        return first
    try:
        second = polish_comment(complete_reply(key, model, voice, manner, tweet, photos, before, rng))
    except CommentError:
        second = ""
    if second and len(second) <= MAX_CHARS:
        return second
    return fit_comment(min((text for text in (first, second) if text), key=len))


# ---------------------------------------------------------------- the app's requests

conf: dict = {}
tweets_kept: dict = {}
tweets_lock = threading.Lock()
claude_slots = threading.BoundedSemaphore(CLAUDE_AT_ONCE)


def kept_tweet(status_id: str) -> dict:
    """The tweet, read once and kept a few minutes (the app asks for it, then for its comment)."""
    now = time.time()
    with tweets_lock:
        for old in [k for k, (at, _) in tweets_kept.items() if now - at > TWEET_KEPT_SECONDS]:
            tweets_kept.pop(old, None)
        found = tweets_kept.get(status_id)
    if found:
        return found[1]
    tweet = read_tweet(status_id)
    with tweets_lock:
        if len(tweets_kept) > 300:
            tweets_kept.clear()
        tweets_kept[status_id] = (now, tweet)
    return tweet


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                         # (a phone that went away, or something that does not speak to it properly)


class Handler(BaseHTTPRequestHandler):
    server_version = "comments"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 60                     # (a client that sends nothing is let go)

    def log_message(self, fmt, *args):
        pass                         # (each comment is written to the log by itself, without the link's secret)

    def answer(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")          # (one request a connection: none is left half-closed)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def action(self) -> str:
        """What is asked, under the link's secret ("" when the secret is not this server's)."""
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or not hmac.compare_digest(parts[0].encode("utf-8"), str(conf.get("token") or "").encode("utf-8")):
            return ""
        return parts[1]

    def do_GET(self):
        if self.action() != "ping":
            self.answer(404, {"ok": False})
            return
        self.answer(200, {"ok": True, "model": conf.get("model"), "version": VERSION})

    def do_POST(self):
        action = self.action()
        if action not in ("tweet", "comment"):
            self.answer(404, {"ok": False})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 65536:
                raise ValueError("size")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            status_id = str(data.get("id") or "")
            if not re.fullmatch(r"\d{1,25}", status_id):
                raise ValueError("id")
        except (ValueError, UnicodeDecodeError, AttributeError):
            self.answer(400, {"ok": False, "error": "طلب غير صالح."})
            return
        try:
            tweet = kept_tweet(status_id)
            if action == "tweet":
                self.answer(200, {"ok": True, "tweet": {"id": tweet["id"], "url": tweet["url"], "author": tweet["author"],
                                                        "text": tweet["text"], "quote": tweet["quote"], "photos": len(tweet["photos"])}})
                return
            voice = int(data.get("voice") or 0) % len(VOICE_RULES)
            manner = int(data.get("manner") or 0) % len(MANNERS)
            before = [str(text)[:300] for text in data.get("before")][-8:] if isinstance(data.get("before"), list) else []
            with claude_slots:
                pictures = download_photos(tweet)            # (not kept: a few tweets' photos would fill the memory)
                reply = write_comment(conf["claude_key"], conf["model"], voice, manner, tweet, pictures, before)
            log(f"تعليق للتغريدة {status_id}: {len(reply)} حرفًا")
            self.answer(200, {"ok": True, "comment": reply})
        except CommentError as exc:
            log(f"التغريدة {status_id}: {exc}")
            self.answer(200, {"ok": False, "error": str(exc)})
        except (ValueError, TypeError):
            self.answer(400, {"ok": False, "error": "طلب غير صالح."})


# ---------------------------------------------------------------- setting it up

def load_config() -> dict:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict) -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, CONFIG)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def make_certificate() -> None:
    """The server's own certificate (made once): the app knows it by its fingerprint, which is in the link."""
    if CERT.is_file() and CERT_KEY.is_file():
        return
    if not shutil.which("openssl") and is_root() and shutil.which("apt-get"):
        subprocess.run(["apt-get", "install", "-y", "-qq", "openssl"], capture_output=True)
    if not shutil.which("openssl"):
        raise SystemExit("openssl غير موجود على السيرفر. ثبّته: apt-get install -y openssl")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(CERT_KEY), "-out", str(CERT),
                    "-days", "3650", "-subj", "/CN=comments-server"], check=True, capture_output=True)
    os.chmod(CERT_KEY, 0o600)


def fingerprint() -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(CERT.read_text())).hexdigest()


def public_host(data: dict) -> str:
    """The address the phones reach this server at: the one set with --host, else its public IP."""
    host = os.environ.get("COMMENTS_PUBLIC_HOST") or str(data.get("host") or "")
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


def app_link(data: dict) -> str:
    host = public_host(data)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                      # (an IPv6 address)
    return f"https://{host}:{data['port']}/{data['token']}#{fingerprint()}"


def service_installed() -> bool:
    return UNIT.is_file() and bool(shutil.which("systemctl")) and Path("/run/systemd/system").is_dir()


def install_service() -> bool:
    """Runs as a service: started with the server, and again if it ever stops."""
    if not is_root() or not shutil.which("systemctl") or not Path("/run/systemd/system").is_dir():
        return False
    UNIT.write_text(f"""[Unit]
Description=The comments app's server (تعليقات)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={sys.executable} {SCRIPT}
WorkingDirectory={HERE}
Environment=PYTHONUNBUFFERED=1
Environment=COMMENTS_SERVER_DIR={HERE}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", SERVICE], capture_output=True)
    subprocess.run(["systemctl", "restart", SERVICE], check=True)
    return True


def port_taken(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((BIND, port))
        except OSError:
            return True
    return False


def open_firewall(port: int) -> str:
    """The port opened in this server's own firewall when one is on (ufw, or iptables rules that turn others away,
    as on Oracle's servers): what was done, or "". The hosting's own firewall, in its panel, is not reached from here."""
    if not is_root():
        return ""
    if shutil.which("ufw"):
        status = subprocess.run(["ufw", "status"], capture_output=True, text=True).stdout
        if "Status: active" in status:
            subprocess.run(["ufw", "allow", f"{port}/tcp"], capture_output=True)
            return "ufw"
    if shutil.which("iptables"):
        rules = subprocess.run(["iptables", "-S", "INPUT"], capture_output=True, text=True).stdout
        closed = re.search(r"^-P INPUT DROP", rules, re.M) or re.search(r"^-A INPUT .*-j (?:REJECT|DROP)\b", rules, re.M)
        if closed and not re.search(rf"--dport {port}\b.*-j ACCEPT", rules):
            subprocess.run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"], capture_output=True)
            if shutil.which("netfilter-persistent"):
                subprocess.run(["netfilter-persistent", "save"], capture_output=True)    # (kept after a restart)
            return "iptables"
    return ""


def answers(data: dict, seconds: float = 15.0) -> bool:
    """Whether the server answers on this machine, with its own certificate (known by its fingerprint, as the app
    knows it)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE          # (the fingerprint is checked instead, below)
    want = fingerprint()
    end = time.time() + seconds
    while time.time() < end:
        try:
            connection = http.client.HTTPSConnection("127.0.0.1", int(data["port"]), timeout=5, context=context)
            connection.request("GET", f"/{data['token']}/ping")
            seen = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            reply = json.loads(connection.getresponse().read().decode("utf-8"))
            connection.close()
            if seen == want and reply.get("ok"):
                return True
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(0.5)
    return False


def ask_key() -> str:
    key = os.environ.get("COMMENTS_CLAUDE_KEY") or ""
    if not key:
        key = getpass.getpass("الصق مفتاح Claude ثم Enter (لا يظهر وأنت تلصقه): ").strip()
    if not KEY_PATTERN.fullmatch(key):
        raise SystemExit("صيغة مفتاح Claude غير صالحة: يبدأ بـ sk-ant-")
    return key


def serve(data: dict) -> None:
    if not KEY_PATTERN.fullmatch(str(data.get("claude_key") or "")) or not data.get("token"):
        raise SystemExit("لم يُضبط السيرفر بعد. شغّل: python3 comments_server.py --setup")
    make_certificate()
    conf.update(data)
    server = Server((BIND, int(data["port"])), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(CERT), str(CERT_KEY))
    # (Each connection's encryption is set up in its own thread: a slow phone does not hold up the others.)
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    log(f"سيرفر التعليقات يعمل على المنفذ {data['port']} · النموذج {data['model']}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="The «تعليقات» app's server: the Claude key stays here.")
    parser.add_argument("--setup", action="store_true", help="first set-up: the key, the service, the link")
    parser.add_argument("--key", action="store_true", help="another Claude key")
    parser.add_argument("--model", help="another model, e.g. claude-sonnet-5-5")
    parser.add_argument("--port", type=int, help=f"another port ({DEFAULT_PORT} by default)")
    parser.add_argument("--host", help="the address in the link: a domain, or this server's IP when the one found is wrong")
    parser.add_argument("--new-link", action="store_true", help="a new secret: the old link stops working")
    parser.add_argument("--link", action="store_true", help="print the app's link")
    parser.add_argument("--no-service", action="store_true", help="(with --setup) do not run it as a service")
    args = parser.parse_args()
    data = load_config()
    changing = bool(args.setup or args.key or args.model or args.port or args.host or args.new_link)
    if not changing and not args.link:
        serve(data)
        return
    if args.model is not None and not MODEL_PATTERN.fullmatch(args.model.strip().lower()):
        raise SystemExit("اسم النموذج غير صالح.")
    if args.port is not None and not 1 <= args.port <= 65535:
        raise SystemExit("رقم المنفذ غير صالح.")
    if args.host is not None and not HOST_PATTERN.fullmatch(args.host.strip()):
        raise SystemExit("العنوان غير صالح: اكتب عنوان IP السيرفر أو اسم نطاقه، بلا https:// وبلا منفذ.")
    if changing:
        if args.key or not data.get("claude_key"):
            data["claude_key"] = ask_key()
        elif args.setup:
            print("مفتاح Claude المحفوظ يبقى كما هو (لتغييره: python3 comments_server.py --key).")
        data["model"] = (args.model or data.get("model") or DEFAULT_MODEL).strip().lower()
        data["port"] = int(args.port or data.get("port") or DEFAULT_PORT)
        if args.host is not None:
            data["host"] = args.host.strip().strip("[]")
        before = dict(data)
        if args.new_link or not data.get("token"):
            data["token"] = secrets.token_urlsafe(24)
        installed = service_installed()
        if installed:
            subprocess.run(["systemctl", "stop", SERVICE], capture_output=True)    # (its port is let go meanwhile)
        # (A port in use is this server's own when it was started by hand: it is restarted by hand.)
        by_hand = port_taken(data["port"]) and CERT.is_file() and bool(before.get("token")) and answers(
            {"port": data["port"], "token": before["token"]}, 3)
        if port_taken(data["port"]) and not by_hand:
            if installed:
                subprocess.run(["systemctl", "start", SERVICE], capture_output=True)
            raise SystemExit(f"المنفذ {data['port']} مستخدم من برنامج آخر على هذا السيرفر. اختر غيره، مثلًا: "
                             f"python3 comments_server.py --setup --port {data['port'] + 1}")
        save_config(data)
        make_certificate()
        running = False
        if by_hand:
            print("سيرفر التعليقات يعمل الآن بالإعداد القديم: أوقفه ثم شغّله من جديد ليأخذ الجديد.")
        elif args.setup and not args.no_service:
            running = install_service()
            if running:
                opened = open_firewall(data["port"])
                if opened:
                    print(f"فُتح المنفذ {data['port']} في جدار حماية السيرفر ({opened}).")
        elif installed:
            subprocess.run(["systemctl", "start", SERVICE], check=False)
            running = True
        if running:
            if answers(data):
                print("✓ سيرفر التعليقات يعمل · النموذج", data["model"])
            else:
                print("⚠️ السيرفر لم يرد بعد. راجع سجله: journalctl -u", SERVICE, "-n 30", file=sys.stderr)
        elif args.setup and not by_hand:
            if not args.no_service and not is_root():
                print("ليعمل خدمةً تبدأ مع السيرفر، أعد الأمر بحساب root (أو بـ sudo).")
            print(f"شغّله واتركه يعمل: nohup python3 {SCRIPT.name} >> comments_server.log 2>&1 &")
    if not data.get("token") or not CERT.is_file():
        raise SystemExit("لم يُضبط السيرفر بعد. شغّل: python3 comments_server.py --setup")
    print()
    print("رابط السيرفر، يُلصق في التطبيق: «الإعدادات» ← «رابط السيرفر»:")
    print(app_link(data))
    if args.setup:
        print()
        print(f"إذا قال التطبيق «لا رد من السيرفر»: افتح المنفذ {data['port']} (TCP) في جدار حماية الاستضافة من لوحتها.")


if __name__ == "__main__":
    main()
