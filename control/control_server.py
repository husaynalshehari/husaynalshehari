"""Standalone control server for the «قروبات/تعليقات» app, meant to run on the owner's own hosting.

What it does, all from one page the owner opens with a master password:
  • Access codes — the owner makes a code for each user. The app does not open until the user types a code the
    server knows and the owner has not stopped. The owner can stop any code at any moment.
  • Keys — the owner puts the Gemini key and the Claude key here and picks which one writes the comments. The keys
    stay on this server (kept sealed on disk); they are never sent to the phones. The app sends the tweet's text
    (and its picture) to this server, and the server writes the reply with the owner's key and sends it back.

Nothing of the main groups/users server is touched: this is a separate service. The app reaches it the same way it
reaches the comments server today — an HTTPS link that carries the server's own certificate fingerprint, so no
certificate authority is needed and the app refuses any other certificate:

    https://<your-host>[:port]/<app-secret>#<certificate-fingerprint>

Run it:
    pip install flask
    python control_server.py                 # makes its data, certificate and secrets on first run, and prints
                                              # the owner page link and the app link.
Useful environment variables (all optional):
    CONTROL_DATA=./control-data              # where it keeps its data, certificate and secrets
    CONTROL_HOST=0.0.0.0   CONTROL_PORT=8443 # where it listens
    CONTROL_PUBLIC_HOST=example.com          # the host to print in the links (else the machine's)
    CONTROL_ADMIN_PASSWORD=...               # set/replace the owner password (else one is made and printed once)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, redirect, render_template_string, request, session

# ----------------------------------------------------------------------------- where everything lives
DATA_DIR = Path(os.environ.get("CONTROL_DATA") or (Path(__file__).resolve().parent / "control-data"))
SECRETS_PATH = DATA_DIR / "secrets.json"
CODES_PATH = DATA_DIR / "codes.json"
KEYS_PATH = DATA_DIR / "keys.json"
USAGE_PATH = DATA_DIR / "usage.json"
CERT_PATH = DATA_DIR / "server.crt"
CERT_KEY_PATH = DATA_DIR / "server.key"

LISTEN_HOST = os.environ.get("CONTROL_HOST") or "0.0.0.0"
LISTEN_PORT = int(os.environ.get("CONTROL_PORT") or 8443)

TOKEN_DAYS = 30                      # how long an app stays unlocked before it must check the code again
LOCK = threading.Lock()


# ----------------------------------------------------------------------------- private files (owner-only on disk)
def read_private_json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ----------------------------------------------------------------------------- the server's own secrets
def new_secret(length: int = 40) -> str:
    return secrets.token_urlsafe(length)[:length].replace("-", "x").replace("_", "y")


def hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=1 << 14, r=8, p=1, dklen=32).hex()


def load_secrets() -> dict:
    data = read_private_json(SECRETS_PATH) or {}
    changed = False
    if not data.get("seal_secret"):
        data["seal_secret"] = secrets.token_hex(32); changed = True
    if not data.get("token_secret"):
        data["token_secret"] = secrets.token_hex(32); changed = True
    if not data.get("session_secret"):
        data["session_secret"] = secrets.token_hex(32); changed = True
    if not data.get("api_secret") or not re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", data["api_secret"]):
        data["api_secret"] = new_secret(40); changed = True
    if not data.get("admin_path") or not re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", data["admin_path"]):
        data["admin_path"] = new_secret(40); changed = True
    forced = os.environ.get("CONTROL_ADMIN_PASSWORD")
    if forced:
        data["admin_salt"] = secrets.token_hex(16)
        data["admin_hash"] = hash_password(forced, data["admin_salt"])
        data["admin_plain_once"] = ""
        changed = True
    if not data.get("admin_hash"):
        made = new_secret(12)
        data["admin_salt"] = secrets.token_hex(16)
        data["admin_hash"] = hash_password(made, data["admin_salt"])
        data["admin_plain_once"] = made            # printed once on first start, then forgotten
        changed = True
    if changed:
        write_private_json(SECRETS_PATH, data)
    return data


SECRETS = load_secrets()


# ----------------------------------------------------------------------------- sealing keys at rest
def seal_material() -> bytes:
    return hashlib.sha256(b"control-seal|" + SECRETS["seal_secret"].encode("utf-8")).digest()


def key_stream(material: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(material + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def seal_key(text: str) -> str:
    if not text:
        return ""
    material, nonce, raw = seal_material(), os.urandom(16), text.encode("utf-8")
    body = bytes(a ^ b for a, b in zip(raw, key_stream(material, nonce, len(raw))))
    tag = hmac.new(material, nonce + body, hashlib.sha256).digest()[:16]
    return "s1:" + base64.b64encode(nonce + tag + body).decode("ascii")


def open_key(sealed) -> str:
    if not isinstance(sealed, str) or not sealed.startswith("s1:"):
        return ""
    try:
        data = base64.b64decode(sealed[3:], validate=True)
    except ValueError:
        return ""
    if len(data) <= 32:
        return ""
    nonce, tag, body = data[:16], data[16:32], data[32:]
    material = seal_material()
    if not hmac.compare_digest(tag, hmac.new(material, nonce + body, hashlib.sha256).digest()[:16]):
        return ""
    try:
        return bytes(a ^ b for a, b in zip(body, key_stream(material, nonce, len(body)))).decode("utf-8")
    except UnicodeDecodeError:
        return ""


# ----------------------------------------------------------------------------- access codes
def load_codes() -> list:
    data = read_private_json(CODES_PATH) or {}
    codes = data.get("codes")
    return codes if isinstance(codes, list) else []


def save_codes(codes: list) -> None:
    write_private_json(CODES_PATH, {"version": 1, "codes": codes})


def make_code() -> str:
    # Easy to read and type: letters and digits without the ones people confuse (0/O, 1/I/L).
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def find_code(entered: str) -> dict | None:
    entered = re.sub(r"[^A-Za-z0-9]", "", str(entered or "")).upper()
    if not entered:
        return None
    with LOCK:
        for row in load_codes():
            if re.sub(r"[^A-Za-z0-9]", "", row.get("code", "")).upper() == entered:
                return row
    return None


# ----------------------------------------------------------------------------- the keys and which one writes
def load_keys() -> dict:
    data = read_private_json(KEYS_PATH) or {}
    return {
        "engine": data.get("engine") if data.get("engine") in ("gemini", "claude") else "gemini",
        "gemini_sealed": data.get("gemini_sealed") or "",
        "gemini_model": data.get("gemini_model") or "",
        "claude_sealed": data.get("claude_sealed") or "",
        "claude_model": data.get("claude_model") or "",
    }


def save_keys(keys: dict) -> None:
    write_private_json(KEYS_PATH, {"version": 1, **keys})


# ----------------------------------------------------------------------------- what each user (and account) used
COST_PER_COMMENT = 0.01            # التكلفة لكل تعليق


def load_usage() -> dict:
    data = read_private_json(USAGE_PATH) or {}
    return data if isinstance(data, dict) else {}


def record_comment(code_id: str, account: str, name: str) -> None:
    """Count one written comment against this code and the X account that asked for it."""
    account = (account or "—")[:48]
    with LOCK:
        usage = load_usage()
        code = usage.setdefault(code_id, {"total": 0, "accounts": {}})
        code["total"] = int(code.get("total") or 0) + 1
        who = code["accounts"].setdefault(account, {"n": 0, "name": ""})
        who["n"] = int(who.get("n") or 0) + 1
        if name:
            who["name"] = name[:40]
        write_private_json(USAGE_PATH, usage)


def usage_of(code_id: str) -> dict:
    row = load_usage().get(code_id) or {"total": 0, "accounts": {}}
    accounts = sorted(
        ({"id": key, "name": value.get("name") or "", "n": int(value.get("n") or 0),
          "cost": round(int(value.get("n") or 0) * COST_PER_COMMENT, 2)}
         for key, value in (row.get("accounts") or {}).items()),
        key=lambda item: -item["n"])
    total = int(row.get("total") or 0)
    return {"total": total, "cost": round(total * COST_PER_COMMENT, 2), "accounts": accounts}


GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"
CLAUDE_DEFAULT_MODEL = "claude-haiku-5-5"


def active_engine() -> tuple[str, str, str]:
    """(engine, key, model) of the engine chosen to write, or ("", "", "") when it has no key yet."""
    keys = load_keys()
    engine = keys["engine"]
    if engine == "claude":
        key = open_key(keys["claude_sealed"])
        return ("claude", key, keys["claude_model"] or CLAUDE_DEFAULT_MODEL) if key else ("", "", "")
    key = open_key(keys["gemini_sealed"])
    return ("gemini", key, keys["gemini_model"] or GEMINI_DEFAULT_MODEL) if key else ("", "", "")


# ----------------------------------------------------------------------------- writing one reply (ported from the app)
REPLY_MAX_CHARS = 100
REPLY_TWEET_TEXT = 1200
DEFAULT_REPLY_VOICE = "male"
REPLY_VOICES = {
    "male": "You write as a man: when you speak of yourself, use masculine forms (in Arabic: مبسوط، متأكد).",
    "female": "You write as a woman: when you speak of yourself, use feminine forms (in Arabic: مبسوطة، متأكدة).",
    "neutral": "The reply must suit a man or a woman: never use words that show the writer's gender "
               "(in Arabic, no forms like مبسوط or مبسوطة, متأكد or متأكدة).",
}
VOICE_BY_NUMBER = {0: "male", 1: "female", 2: "neutral"}
REPLY_ANGLES = (
    "a frank remark on its point, the way people talk among friends",
    "you disagree with one point in it, and say why",
    "a detail in it that most people will miss",
    "a light, dry joke about the situation",
    "you doubt one part of it, and say what does not add up",
    "a comparison with something from everyday life",
    "what it will lead to, or what comes next",
    "a short question about one point in it",
)
REPLY_LENGTHS = ((6, 10), (8, 13), (10, 16))
REPLY_MANNERS = (
    "short and direct, no emojis",
    "calm and down-to-earth",
    "sarcastic and witty",
    "blunt and critical, but not rude",
    "easygoing; now and then one emoji at the end",
    "curious; sometimes ends with a short question",
    "matter-of-fact; sticks to what the tweet says",
    "playful, with everyday slang",
)
import random  # noqa: E402  (kept near the reply code it serves)

ARABIC_DIACRITICS = re.compile(r"[ً-ْٖ-ٰٟ]")
HANGING_WORDS = {"و", "او", "أو", "بس", "لكن", "لان", "لأن", "عشان", "اللي", "الي", "في", "من", "على", "عن", "مع", "الى",
                 "إلى", "يعني", "ثم", "حتى", "اذا", "إذا", "لو", "ما", "لا", "كان", "and", "or", "but", "the", "to", "of",
                 "in", "a", "an", "with", "for", "that", "ve", "ama", "ile", "için", "bir", "de", "da"}


def clean_comment(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = text.replace("**", "")
    text = re.sub(r"^(?:reply|comment|التعليق|الرد|الدعاء)\s*[:：\-]\s*", "", text, flags=re.I)
    text = re.sub(r"^[\w؀-ۿ]{2,24}\s*[:：]\s*", "", text)
    text = text.strip("\"'«»“” ")
    if len(text) > 280:
        cut = text[:280]
        end = max(cut.rfind("؟"), cut.rfind("?"), cut.rfind("!"), cut.rfind("."), cut.rfind("…"))
        text = cut[: end + 1] if end >= 40 else cut.rsplit(" ", 1)[0]
    return text.strip()


def polish_comment(text: str) -> str:
    text = ARABIC_DIACRITICS.sub("", text or "")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    text = re.sub(r"\s*[،,؛;]\s*", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fit_comment(text: str, limit: int = REPLY_MAX_CHARS) -> str:
    words = text.split()
    while words and len(" ".join(words)) > limit:
        words.pop()
    while words and words[-1].lower() in HANGING_WORDS:
        words.pop()
    return " ".join(words)


def comment_is_complete(text: str) -> bool:
    text = re.sub(r"[\s‍️☀-➿\U0001f000-\U0001faff]+$", "", text or "")
    words = [word for word in text.split() if word]
    if len(words) < 6:
        return False
    if re.search(r"[،,؛;:：\-–—/\\*]$", text.strip()):
        return False
    last = re.sub(r"[.!?؟…\"'«»“”]+$", "", words[-1])
    return len(last) > 2


def reply_prompt(voice: str, manner_number: int, before: list) -> str:
    low, high = random.choice(REPLY_LENGTHS)
    manner = REPLY_MANNERS[int(manner_number) % len(REPLY_MANNERS)]
    prompt = (
        "Write one public reply to this tweet, as an ordinary person scrolling X who comments on what it says. "
        "Read the whole tweet first and answer what it really says (its point, and its ending when it tells a story); "
        "do not guess beyond it. "
        "Not personal: the reply is about the tweet's topic, not about you. No stories or experiences of yours, "
        "nothing that happened to you, your family or your friends, and nothing about your own life (nothing like: "
        "صار معي، جربت، شفت بنفسي، أخوي، عندنا في البيت، أتذكر لما، لو كنت مكانه). "
        "Speak about its topic, not to please its author: do not compliment, thank or flatter the author, do not "
        "praise the tweet (nothing like: well said, you're right, great post, صدقت، كفو، أبدعت، كلامك ذهب), and do "
        "not just agree. Honest, never rude: no insults, and do not mock the author. "
        f"This time: {random.choice(REPLY_ANGLES)}. Your manner (always the same): {manner}. "
        "If the tweet is a prayer (دعاء), about religion, a death, an illness or other sad news, leave the angle and "
        "the manner: reply sincerely and respectfully, with no joke and no disagreement. "
        f"At most {REPLY_MAX_CHARS} characters, spaces included (about {low} to {high} words). Finish the sentence. "
        "Never stop mid-word or mid-phrase. "
        "Write in the tweet's own language. An Arabic tweet gets everyday spoken Saudi Arabic (عامية سعودية بيضاء), "
        "the way people really type on X, never formal Arabic (فصحى): no words like الأولى، ينبغي، لا تثبت، ما زال، "
        "إذ، لذا، حيث، كما أن. A tweet in another language (English, Turkish…) gets its reply in that language. "
        "Plain letters only: no diacritics (no تشكيل: no fatha, damma, kasra, tanween, shadda or sukun), and no "
        "commas (، or ,). " + REPLY_VOICES.get(voice, REPLY_VOICES[DEFAULT_REPLY_VOICE]) + " "
        "Sound like a real person, specific and casual, not a teacher, a preacher, a brand or an assistant: no "
        "advice or lessons to the author or to people (nothing like: الأولى أن، يجب، الأفضل أن، تذكّر، نصيحتي), no "
        "general wisdom or sayings, no neat conclusion; just make your point about the topic itself. "
        "The image is only a screenshot to read the tweet: never mention it, the picture, the post or the tweet "
        "(no الصورة، المنشور، التغريدة); react to what is in them. "
        "Do not open with filler (like صراحة، فعلاً، والله) or by repeating the tweet. "
        "No hashtags, no links, no quotes, no labels, no introduction. Return only the finished reply."
    )
    before = [str(text) for text in (before or []) if str(text).strip()][:12]
    if before:
        prompt += ("\n\nReplies already written (to this tweet, or your own recent ones). Yours must say something "
                   "else, in another way (another opening, another shape, other words):\n"
                   + "\n".join("- " + text for text in before))
    return prompt


def gemini_thinking_config(model: str) -> dict | None:
    if re.search(r"2\.5", model):
        return {"thinkingBudget": 0}
    if re.search(r"gemini-3|3\.\d", model):
        return {"thinkingLevel": "low"}
    return None


def gemini_is_busy(status: int, detail: str) -> bool:
    if status in (429, 500, 502, 503, 504):
        return True
    return bool(re.search(r"high demand|try again later|unavailable|overloaded|resource exhausted|rate limit|quota",
                          detail or "", re.I))


def gemini_comment(api_key: str, model: str, image: bytes | None, tweet_text: str, voice: str, manner: int, before: list) -> str:
    prompt = reply_prompt(voice, manner, before)
    if image:
        prompt = "Look at the tweet in the image. " + prompt
    if tweet_text:
        prompt += "\n\nTweet text:\n" + tweet_text[:REPLY_TWEET_TEXT]
    parts: list[dict] = [{"text": prompt}]
    if image:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode("ascii")}})
    attempts = [(gemini_thinking_config(model), 2048), (None, 4096)]
    busy_waits = (5, 12, 20, 30)
    best, saw_busy = "", False
    for thinking, max_tokens in attempts:
        config: dict = {"temperature": 1.0, "maxOutputTokens": max_tokens}
        if thinking:
            config["thinkingConfig"] = thinking
        body = {"contents": [{"parts": parts}], "generationConfig": config}
        payload, thinking_rejected = None, False
        for try_index in range(len(busy_waits) + 1):
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models/" + quote(model, safe="") + ":generateContent",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=45) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    parsed = json.loads(exc.read().decode("utf-8", "replace")[:800])
                    detail = str(((parsed.get("error") or {}).get("message") or ""))[:160]
                except Exception:
                    detail = ""
                if api_key and api_key in detail:
                    detail = ""
                if thinking and re.search(r"thinking", detail, re.I):
                    thinking_rejected = True
                    break
                if exc.code in (401, 403):
                    raise RuntimeError("مفتاح Gemini غير صالح أو بلا صلاحية. راجع صاحب البرنامج.") from None
                if gemini_is_busy(getattr(exc, "code", 0) or 0, detail):
                    saw_busy = True
                    if try_index < len(busy_waits):
                        time.sleep(busy_waits[try_index]); continue
                    break
                raise RuntimeError(detail or "رفض Gemini الطلب") from None
            except Exception as exc:
                if try_index < 2:
                    time.sleep(busy_waits[try_index]); continue
                raise RuntimeError("تعذر الاتصال بـ Gemini") from exc
        if thinking_rejected:
            continue
        if not payload:
            break
        candidate = (payload.get("candidates") or [{}])[0]
        texts = [str(part["text"]) for part in ((candidate.get("content") or {}).get("parts") or [])
                 if isinstance(part, dict) and not part.get("thought") and part.get("text")]
        text = clean_comment(" ".join(texts))
        if comment_is_complete(text):
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        return best
    if saw_busy:
        raise RuntimeError("Gemini مشغول مؤقتًا بعد عدة محاولات. أعد المحاولة بعد قليل.")
    return ""


CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION = "2026-10-21"
CLAUDE_EFFORT = "medium"
claude_effort_refused: set = set()


def claude_answer_text(answer: dict) -> str:
    return "\n".join(str(block.get("text") or "") for block in answer.get("content") or []
                     if isinstance(block, dict) and block.get("type") == "text")


def claude_comment(api_key: str, model: str, image: bytes | None, tweet_text: str, voice: str, manner: int, before: list) -> str:
    prompt = reply_prompt(voice, manner, before)
    if image:
        prompt = "Look at the tweet in the image. " + prompt
    if tweet_text:
        prompt += "\n\nTweet text:\n" + tweet_text[:REPLY_TWEET_TEXT]
    content: list[dict] = []
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(image).decode("ascii")}})
    content.append({"type": "text", "text": prompt})
    effort = model not in claude_effort_refused
    busy_waits = (5, 12, 20, 30)
    best, saw_busy = "", False
    for _ in range(2):
        payload = None
        for try_index in range(len(busy_waits) + 1):
            body: dict = {"model": model, "max_tokens": 2048, "messages": [{"role": "user", "content": content}]}
            if effort:
                body["output_config"] = {"effort": CLAUDE_EFFORT}
            req = urllib.request.Request(CLAUDE_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "x-api-key": api_key,
                                                  "anthropic-version": CLAUDE_API_VERSION})
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail, code = "", ""
                try:
                    data = json.loads(exc.read(4096).decode("utf-8", "replace"))
                    error = data.get("error") if isinstance(data, dict) else None
                    if isinstance(error, dict):
                        detail = str(error.get("message") or "")
                        code = str((error.get("details") or {}).get("error_code") or "") if isinstance(error.get("details"), dict) else ""
                except Exception:
                    detail = ""
                detail = re.sub(r"\s+", " ", detail.replace(api_key, "…") if api_key else detail).strip()[:160]
                if effort and exc.code == 400 and re.search(r"effort|output_config", detail, re.I):
                    effort = False
                    claude_effort_refused.add(model)
                    continue
                if exc.code in (401, 403):
                    raise RuntimeError("مفتاح Claude غير صالح أو بلا صلاحية. راجع صاحب البرنامج.") from None
                if code == "enforced_spend_limit_reached" or re.search(r"usage limits|credit balance|billing", detail, re.I):
                    raise RuntimeError("بلغ حساب Claude حد الصرف أو نفد رصيده. راجع صاحب البرنامج.") from None
                if exc.code in (429, 500, 502, 503, 504, 529):
                    saw_busy = True
                    if try_index < len(busy_waits):
                        time.sleep(busy_waits[try_index]); continue
                    break
                raise RuntimeError(("رفض Claude الطلب: " + detail) if detail else f"تعذر الاتصال بـ Claude (رمز {exc.code}).") from None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if try_index < 2:
                    time.sleep(busy_waits[try_index]); continue
                raise RuntimeError("تعذر الاتصال بـ Claude") from exc
        if not isinstance(payload, dict):
            break
        if payload.get("stop_reason") == "refusal":
            raise RuntimeError("امتنع Claude عن التعليق على هذه التغريدة.")
        text = clean_comment(claude_answer_text(payload))
        if comment_is_complete(text):
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        return best
    if saw_busy:
        raise RuntimeError("Claude مشغول مؤقتًا بعد عدة محاولات. أعد المحاولة بعد قليل.")
    return ""


def write_reply(tweet_text: str, image: bytes | None, voice: str, manner: int, before: list) -> str:
    engine, key, model = active_engine()
    if not engine:
        raise RuntimeError("لم يُفعّل أي مفتاح على الخادم بعد. أخبر صاحب البرنامج.")
    writer = claude_comment if engine == "claude" else gemini_comment
    text = polish_comment(writer(key, model, image, tweet_text, voice, manner, before))
    if not text:
        raise RuntimeError("لم يُكتب تعليق. أعد المحاولة.")
    if len(text) > REPLY_MAX_CHARS:
        text = fit_comment(text)
    return text


# ----------------------------------------------------------------------------- the app's unlock token
# The token carries the code and the one device it was bound to, signed. A copy of it on another phone is useless:
# that phone's own device id will not match the device inside the token (and the code is bound to the first device).
def make_token(code_id: str, device: str) -> str:
    exp = int(time.time()) + TOKEN_DAYS * 86400
    head = f"{code_id}.{device}.{exp}"
    sig = hmac.new(SECRETS["token_secret"].encode("utf-8"), head.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{head}.{sig}".encode("utf-8")).decode("ascii")


def read_token(token: str) -> tuple[str, str]:
    """(code id, device) a valid, unexpired token carries, or ("", "")."""
    try:
        head_sig = base64.urlsafe_b64decode(str(token).encode("ascii")).decode("utf-8")
        code_id, device, exp, sig = head_sig.rsplit(".", 3)
    except (ValueError, Exception):
        return "", ""
    good = hmac.new(SECRETS["token_secret"].encode("utf-8"), f"{code_id}.{device}.{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return "", ""
    try:
        if int(exp) < int(time.time()):
            return "", ""
    except ValueError:
        return "", ""
    return code_id, device


def code_from_token(token: str) -> dict | None:
    """The active code a token belongs to, only when the token's device is the one the code is bound to."""
    code_id, device = read_token(token)
    if not code_id:
        return None
    with LOCK:
        for row in load_codes():
            if row.get("id") == code_id and row.get("active") and (row.get("bound_device") or "") == device:
                return row
    return None


# ----------------------------------------------------------------------------- the app link + certificate
def ensure_certificate() -> None:
    if CERT_PATH.exists() and CERT_KEY_PATH.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(CERT_KEY_PATH), "-out", str(CERT_PATH),
                    "-days", "3650", "-subj", "/CN=groups-control"], check=True, capture_output=True)
    try:
        os.chmod(CERT_KEY_PATH, 0o600)
    except OSError:
        pass


def certificate_fingerprint() -> str:
    der = ssl.PEM_cert_to_DER_cert(CERT_PATH.read_text(encoding="utf-8"))
    return hashlib.sha256(der).hexdigest()


def public_host() -> str:
    host = os.environ.get("CONTROL_PUBLIC_HOST")
    if host:
        return host
    try:
        import socket
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "YOUR-HOST"


def app_link() -> str:
    host = public_host()
    port = "" if LISTEN_PORT == 443 else f":{LISTEN_PORT}"
    return f"https://{host}{port}/{SECRETS['api_secret']}#{certificate_fingerprint()}"


def admin_link() -> str:
    host = public_host()
    port = "" if LISTEN_PORT == 443 else f":{LISTEN_PORT}"
    return f"https://{host}{port}/{SECRETS['admin_path']}"


# ----------------------------------------------------------------------------- the web app
app = Flask(__name__)
app.secret_key = SECRETS["session_secret"]
API = SECRETS["api_secret"]
ADMIN = SECRETS["admin_path"]


def logged_in() -> bool:
    return bool(session.get("owner"))


LOGIN_PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>لوحة التحكّم</title>
<style>body{font:16px/1.6 system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;height:100vh;margin:0}
.box{background:#1e293b;padding:28px;border-radius:14px;width:min(92vw,360px);box-shadow:0 10px 40px #0006}
h1{font-size:20px;margin:0 0 16px}input{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;margin:6px 0}
button{width:100%;padding:12px;border:0;border-radius:10px;background:#2563eb;color:#fff;font-weight:700;cursor:pointer;margin-top:8px}
.err{color:#f87171;min-height:20px;font-size:14px}</style></head><body>
<form class="box" method="post" action="/{{admin}}/login"><h1>لوحة التحكّم</h1>
<div class="err">{{error}}</div><input type="password" name="password" placeholder="كلمة مرور المالك" autofocus>
<button>دخول</button></form></body></html>"""

DASHBOARD = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>لوحة التحكّم</title>
<style>body{font:16px/1.6 system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:18px}
.wrap{max-width:860px;margin:0 auto}h1{font-size:22px}h2{font-size:18px;border-bottom:1px solid #334155;padding-bottom:6px;margin-top:28px}
.card{background:#1e293b;border-radius:14px;padding:16px;margin:14px 0}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #334155;text-align:right;font-size:14px}
code{background:#0f172a;padding:3px 8px;border-radius:7px;font-size:15px;letter-spacing:1px}
button,.btn{padding:8px 14px;border:0;border-radius:9px;background:#2563eb;color:#fff;font-weight:700;cursor:pointer;font-size:14px}
button.gray{background:#475569}button.red{background:#b91c1c}input,select{padding:9px;border-radius:9px;border:1px solid #334155;background:#0f172a;color:#e2e8f0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.muted{color:#94a3b8;font-size:13px}.on{color:#34d399}.off{color:#f87171}
.link{word-break:break-all;background:#0f172a;padding:10px;border-radius:9px;font-size:13px}.top{display:flex;justify-content:space-between;align-items:center}
.code-card{border:1px solid #334155;border-radius:11px;padding:12px;margin:10px 0;background:#0b1424}</style></head>
<body><div class="wrap"><div class="top"><h1>لوحة التحكّم</h1><a class="btn gray" href="/{{admin}}/logout">خروج</a></div>

<div class="card"><h2>رابط التطبيق</h2>
<p class="muted">انسخه كاملًا وضعه في التطبيق (إعداد الخادم). يحمل بصمة شهادة الخادم، فلا يقبل التطبيق خادمًا آخر.</p>
<div class="link">{{app_link}}</div></div>

<div class="card"><h2>المفاتيح</h2>
<p class="muted">المفتاح يبقى على الخادم (مختوم على القرص)، ولا يُرسل للأجهزة. التطبيق يرسل نص التغريدة فيُكتب الرد هنا.</p>
<form class="row" method="post" action="/{{admin}}/key" style="margin-bottom:10px">
<input type="hidden" name="which" value="gemini">
<b style="width:70px">Gemini</b>
<input name="api_key" placeholder="{{gemini_set and 'مضبوط — الصق مفتاحًا جديدًا للتبديل' or 'الصق مفتاح Gemini'}}" style="flex:1;min-width:180px">
<input name="model" value="{{gemini_model}}" placeholder="{{gemini_default}}" style="width:160px">
<button>حفظ</button></form>
<form class="row" method="post" action="/{{admin}}/key">
<input type="hidden" name="which" value="claude">
<b style="width:70px">Claude</b>
<input name="api_key" placeholder="{{claude_set and 'مضبوط — الصق مفتاحًا جديدًا للتبديل' or 'الصق مفتاح Claude (sk-ant-…)'}}" style="flex:1;min-width:180px">
<input name="model" value="{{claude_model}}" placeholder="{{claude_default}}" style="width:160px">
<button>حفظ</button></form>
<form class="row" method="post" action="/{{admin}}/engine" style="margin-top:14px">
<b>الكاتب الآن:</b>
<label><input type="radio" name="engine" value="gemini" {{engine=='gemini' and 'checked' or ''}}> Gemini {{gemini_set and '✅' or '—'}}</label>
<label><input type="radio" name="engine" value="claude" {{engine=='claude' and 'checked' or ''}}> Claude {{claude_set and '✅' or '—'}}</label>
<button>تفعيل</button></form></div>

<div class="card"><h2>رموز الدخول</h2>
<form class="row" method="post" action="/{{admin}}/code/new" style="margin-bottom:6px">
<input name="label" placeholder="اسم المستخدم (اختياري)" style="flex:1;min-width:160px"><button>+ رمز جديد</button></form>
{% for c in codes %}
<div class="code-card">
  <div class="row" style="justify-content:space-between">
    <div><code>{{c.code}}</code> <b>{{c.label}}</b> <span class="{{c.active and 'on' or 'off'}}">· {{c.active and 'يعمل' or 'موقوف'}}</span></div>
    <div class="muted">آخر دخول {{c.last_seen_text}}</div>
  </div>
  <div class="row" style="margin-top:8px;gap:16px">
    <span>الجهاز: {% if c.bound_device %}<span class="on">مربوط بجهاز</span>{% else %}<span class="muted">غير مربوط</span>{% endif %}</span>
    <span>التعليقات: <b>{{c.usage.total}}</b></span>
    <span>التكلفة: <b>${{'%.2f'|format(c.usage.cost)}}</b></span>
  </div>
  {% if c.usage.accounts %}<table style="margin-top:8px"><tr><th>حساب X</th><th>تعليقات</th><th>التكلفة</th></tr>
  {% for a in c.usage.accounts %}<tr><td>{{ a.name and ('@' + a.name) or ('#' + a.id[:8]) }}</td><td>{{a.n}}</td><td>${{'%.2f'|format(a.cost)}}</td></tr>{% endfor %}</table>{% endif %}
  <div class="row" style="margin-top:10px">
    <form method="post" action="/{{admin}}/code/toggle"><input type="hidden" name="id" value="{{c.id}}"><button class="gray">{{c.active and 'إيقاف' or 'تشغيل'}}</button></form>
    {% if c.bound_device %}<form method="post" action="/{{admin}}/code/release" onsubmit="return confirm('تحرير الرمز من جهازه الحالي؟ يقدر يُفعّل على جوال آخر.')"><input type="hidden" name="id" value="{{c.id}}"><button class="gray">تحرير الجهاز</button></form>{% endif %}
    <form method="post" action="/{{admin}}/code/delete" onsubmit="return confirm('حذف الرمز نهائيًا؟')"><input type="hidden" name="id" value="{{c.id}}"><button class="red">حذف</button></form>
  </div>
</div>{% endfor %}
{% if not codes %}<p class="muted">لا توجد رموز بعد. أنشئ رمزًا وأعطه للمستخدم.</p>{% endif %}
</div>

</div></body></html>"""


def seen_text(row: dict) -> str:
    when = row.get("last_seen") or 0
    if not when:
        return "لم يدخل"
    delta = int(time.time()) - int(when)
    if delta < 3600:
        return f"قبل {delta // 60} د"
    if delta < 86400:
        return f"قبل {delta // 3600} س"
    return f"قبل {delta // 86400} يوم"


@app.get(f"/{ADMIN}")
def dashboard():
    if not logged_in():
        return render_template_string(LOGIN_PAGE, admin=ADMIN, error="")
    keys = load_keys()
    with LOCK:
        codes = load_codes()
    rows = [dict(c, last_seen_text=seen_text(c), usage=usage_of(c["id"])) for c in codes]
    return render_template_string(
        DASHBOARD, admin=ADMIN, app_link=app_link(), codes=rows,
        engine=keys["engine"], gemini_set=bool(keys["gemini_sealed"]), claude_set=bool(keys["claude_sealed"]),
        gemini_model=keys["gemini_model"], claude_model=keys["claude_model"],
        gemini_default=GEMINI_DEFAULT_MODEL, claude_default=CLAUDE_DEFAULT_MODEL)


@app.post(f"/{ADMIN}/login")
def login():
    password = request.form.get("password") or ""
    if hmac.compare_digest(hash_password(password, SECRETS["admin_salt"]), SECRETS["admin_hash"]):
        session["owner"] = True
        return redirect(f"/{ADMIN}")
    return render_template_string(LOGIN_PAGE, admin=ADMIN, error="كلمة المرور غير صحيحة.")


@app.post(f"/{ADMIN}/logout")
def logout():
    session.clear()
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/code/new")
def code_new():
    if not logged_in():
        return redirect(f"/{ADMIN}")
    label = (request.form.get("label") or "").strip()[:40]
    with LOCK:
        codes = load_codes()
        codes.insert(0, {"id": secrets.token_hex(8), "code": make_code(), "label": label,
                         "active": True, "created": int(time.time()), "last_seen": 0, "bound_device": ""})
        save_codes(codes)
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/code/toggle")
def code_toggle():
    if not logged_in():
        return redirect(f"/{ADMIN}")
    code_id = request.form.get("id")
    with LOCK:
        codes = load_codes()
        for row in codes:
            if row.get("id") == code_id:
                row["active"] = not row.get("active")
        save_codes(codes)
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/code/delete")
def code_delete():
    if not logged_in():
        return redirect(f"/{ADMIN}")
    code_id = request.form.get("id")
    with LOCK:
        save_codes([row for row in load_codes() if row.get("id") != code_id])
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/code/release")
def code_release():
    """Unbind a code from its phone, so the user can sign in on a new one with the same code."""
    if not logged_in():
        return redirect(f"/{ADMIN}")
    code_id = request.form.get("id")
    with LOCK:
        codes = load_codes()
        for row in codes:
            if row.get("id") == code_id:
                row["bound_device"] = ""
        save_codes(codes)
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/key")
def set_key():
    if not logged_in():
        return redirect(f"/{ADMIN}")
    which = request.form.get("which")
    api_key = (request.form.get("api_key") or "").strip()
    model = (request.form.get("model") or "").strip()[:60]
    if which not in ("gemini", "claude"):
        return redirect(f"/{ADMIN}")
    with LOCK:
        keys = load_keys()
        if api_key:
            keys[f"{which}_sealed"] = seal_key(api_key)
        keys[f"{which}_model"] = model
        save_keys(keys)
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/engine")
def set_engine():
    if not logged_in():
        return redirect(f"/{ADMIN}")
    engine = request.form.get("engine")
    if engine in ("gemini", "claude"):
        with LOCK:
            keys = load_keys()
            keys["engine"] = engine
            save_keys(keys)
    return redirect(f"/{ADMIN}")


# ----------------------------------------------------------------------------- the app API (link protocol)
def api_ok(**data):
    return jsonify(ok=True, **data)


def api_err(message: str, status: int = 200):
    return jsonify(ok=False, error=message), status


@app.post(f"/{API}/activate")
def api_activate():
    body = request.get_json(silent=True) or {}
    device = re.sub(r"[^A-Za-z0-9]", "", str(body.get("device") or ""))[:64]
    if not device:
        return api_err("لم يصل معرّف الجهاز. حدّث التطبيق.")
    row = find_code(body.get("code"))
    if not row:
        return api_err("الرمز غير معروف. تأكد منه أو اطلب رمزًا من صاحب البرنامج.")
    if not row.get("active"):
        return api_err("هذا الرمز موقوف. راجع صاحب البرنامج.")
    with LOCK:
        codes = load_codes()
        target = next((item for item in codes if item.get("id") == row["id"]), None)
        if target is None:
            return api_err("الرمز غير معروف.")
        bound = target.get("bound_device") or ""
        if bound and bound != device:
            return api_err("هذا الرمز مُستخدم على جهاز آخر. اطلب من صاحب البرنامج تحريره أو رمزًا جديدًا.")
        target["bound_device"] = device
        target["last_seen"] = int(time.time())
        save_codes(codes)
    engine, _, _ = active_engine()
    return api_ok(token=make_token(row["id"], device), engine=engine or "none")


@app.post(f"/{API}/check")
def api_check():
    body = request.get_json(silent=True) or {}
    return api_ok(active=bool(code_from_token(body.get("token"))))


@app.post(f"/{API}/comment")
def api_comment():
    body = request.get_json(silent=True) or {}
    row = code_from_token(body.get("token"))
    if not row:
        return api_err("انتهت صلاحية الدخول أو أوقِف الرمز أو نُقل لجهاز آخر. أعد إدخال الرمز.", 200)
    tweet_text = str(body.get("text") or "")[:4000]
    image = None
    raw_image = body.get("image")
    if isinstance(raw_image, str) and raw_image:
        try:
            image = base64.b64decode(raw_image, validate=True)
            if len(image) > 4_000_000:
                image = None
        except (ValueError, Exception):
            image = None
    voice = body.get("voice")
    if isinstance(voice, (int, float)):
        voice = VOICE_BY_NUMBER.get(int(voice), DEFAULT_REPLY_VOICE)
    elif voice not in REPLY_VOICES:
        voice = DEFAULT_REPLY_VOICE
    try:
        manner = int(body.get("manner") or 0)
    except (TypeError, ValueError):
        manner = 0
    before = body.get("before") if isinstance(body.get("before"), list) else []
    if not tweet_text and not image:
        return api_err("لا نص ولا صورة للتغريدة.")
    try:
        comment = write_reply(tweet_text, image, voice, manner, before)
    except RuntimeError as exc:
        return api_err(str(exc))
    record_comment(row["id"], str(body.get("account") or ""), str(body.get("name") or ""))
    return api_ok(comment=comment)


@app.get("/")
def root():
    return "control server", 200


def run() -> None:
    ensure_certificate()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(CERT_PATH), str(CERT_KEY_PATH))
    print("=" * 70, flush=True)
    print("خادم التحكّم يعمل.", flush=True)
    print("لوحة المالك:", admin_link(), flush=True)
    if SECRETS.get("admin_plain_once"):
        print("كلمة مرور المالك (تظهر مرة واحدة، احفظها):", SECRETS["admin_plain_once"], flush=True)
        data = read_private_json(SECRETS_PATH) or {}
        data["admin_plain_once"] = ""
        write_private_json(SECRETS_PATH, data)
    print("رابط التطبيق:", app_link(), flush=True)
    print("=" * 70, flush=True)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, ssl_context=context, threaded=True)


if __name__ == "__main__":
    run()
