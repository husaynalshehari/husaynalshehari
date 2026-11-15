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


def reset_usage(code_id: str) -> None:
    """تصفير تكلفة رمز واحد."""
    with LOCK:
        usage = load_usage()
        if code_id in usage:
            usage.pop(code_id, None)
            write_private_json(USAGE_PATH, usage)


def reset_all_usage() -> None:
    """تصفير كل التكاليف."""
    with LOCK:
        write_private_json(USAGE_PATH, {})


def usage_of(code_id: str) -> dict:
    row = load_usage().get(code_id) or {"total": 0, "accounts": {}}
    accounts = sorted(
        ({"id": key, "name": value.get("name") or "", "n": int(value.get("n") or 0),
          "cost": round(int(value.get("n") or 0) * COST_PER_COMMENT, 2)}
         for key, value in (row.get("accounts") or {}).items()),
        key=lambda item: -item["n"])
    total = int(row.get("total") or 0)
    return {"total": total, "cost": round(total * COST_PER_COMMENT, 2), "accounts": accounts}


def usage_by_account(code_id: str) -> dict:
    """What the app shows its user: the comments of their code and their cost, by X account. Accounts go by
    their @name: one signed in again (in the app, or after the app was installed again) is the same account,
    whatever its new key; one whose name was never read stays on its own."""
    row = load_usage().get(code_id) or {"total": 0, "accounts": {}}
    merged: dict[str, dict] = {}
    for key, value in (row.get("accounts") or {}).items():
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or "").strip().lstrip("@")[:40]
        slot = merged.setdefault(name.lower() if name else "id:" + str(key), {"name": name, "n": 0})
        slot["n"] += int(value.get("n") or 0)
        if name and not slot["name"]:
            slot["name"] = name
    accounts = sorted(
        ({"name": slot["name"], "n": slot["n"], "cost": round(slot["n"] * COST_PER_COMMENT, 2)} for slot in merged.values()),
        key=lambda item: -item["n"])
    total = int(row.get("total") or 0)
    return {"total": total, "cost": round(total * COST_PER_COMMENT, 2), "price": COST_PER_COMMENT, "accounts": accounts}


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

ARABIC_DIACRITICS = re.compile(r"[\u064b-\u0652\u0656-\u0670\u065f]")   # tashkeel marks
HANGING_WORDS = {"و", "او", "أو", "بس", "لكن", "لان", "لأن", "عشان", "اللي", "الي", "في", "من", "على", "عن", "مع", "الى",
                 "إلى", "يعني", "ثم", "حتى", "اذا", "إذا", "لو", "ما", "لا", "كان", "and", "or", "but", "the", "to", "of",
                 "in", "a", "an", "with", "for", "that", "ve", "ama", "ile", "için", "bir", "de", "da"}


def clean_comment(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = text.replace("**", "")
    text = re.sub(r"^(?:reply|comment|التعليق|الرد|الدعاء)\s*[:\uff1a\-]\s*", "", text, flags=re.I)
    text = re.sub(r"^[\w\u0600-\u06ff]{2,24}\s*[:\uff1a]\s*", "", text)
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
    text = re.sub(r"[\s\u200d\ufe0f\u2600-\u27bf\U0001f000-\U0001faff]+$", "", text or "")
    words = [word for word in text.split() if word]
    if len(words) < 6:
        return False
    if re.search(r"[،,؛;:\uff1a\-\u2013\u2014/\\*]$", text.strip()):
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


LOGIN_PAGE = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0c3d3a"><meta name="robots" content="noindex,nofollow">
<title>تسجيل الدخول</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Readex+Pro:wght@300..700&display=swap">
<script>try{document.documentElement.dataset.theme=localStorage.getItem('th')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}catch(e){}</script>
<style>
:root{--bg:#0c3d3a;--card:#fff;--soft:#f3f6f5;--line:#dce4e2;--ink:#10201f;--mut:#566664;--pri:#0f6b66;--prih:#0b5651;--prik:#fff;--ring:rgba(15,107,102,.22);--bad:#b42318;--badsoft:#fdeae7;color-scheme:light}
[data-theme=dark]{--bg:#0a1f1d;--card:#111b1a;--soft:#182423;--line:#243432;--ink:#e3eeec;--mut:#8c9e9b;--pri:#56c8bf;--prih:#74d6ce;--prik:#05201e;--ring:rgba(86,200,191,.28);--bad:#f6817a;--badsoft:#3a1b19;color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;min-height:100dvh;display:flex;flex-direction:column;background:var(--bg);color:var(--ink);font:400 15px/1.65 "Readex Pro","IBM Plex Sans Arabic","Segoe UI",system-ui,-apple-system,"Noto Sans Arabic",Tahoma,sans-serif;-webkit-tap-highlight-color:transparent}
h1,p{margin:0}
.box{flex:1;display:flex;flex-direction:column;width:100%}
.bh{flex:1;display:flex;flex-direction:column;justify-content:flex-end;padding:40px 24px 28px;color:#eef8f6}
.logo{width:52px;height:52px;border-radius:15px;display:grid;place-items:center;background:#eef8f6;color:#0c3d3a;font-weight:600;font-size:26px;margin-bottom:20px}
h1{font-size:26px;font-weight:600;line-height:1.35}
.bh p{color:#a3cbc6;margin-top:4px}
.bb{background:var(--card);border-radius:24px 24px 0 0;padding:26px 24px calc(28px + env(safe-area-inset-bottom,0px))}
label{display:block;font-size:13.5px;font-weight:500;color:var(--mut);margin-bottom:6px}
.pw{position:relative}
input{width:100%;height:50px;padding:0 14px;padding-left:50px;border-radius:13px;border:1px solid var(--line);background:var(--soft);color:var(--ink);font:inherit;font-size:16px;outline:0}
input:focus{border-color:var(--pri);box-shadow:0 0 0 3px var(--ring);background:var(--card)}
.eye{position:absolute;left:5px;top:5px;width:40px;height:40px;border:0;border-radius:10px;background:none;color:var(--mut);cursor:pointer;display:grid;place-items:center}
.eye[aria-pressed=true]{color:var(--pri)}
.i{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.btn{width:100%;height:50px;margin-top:16px;border:0;border-radius:13px;background:var(--pri);color:var(--prik);font:inherit;font-weight:600;font-size:16px;cursor:pointer}
.btn:hover{background:var(--prih)}
.alert{display:flex;gap:9px;align-items:flex-start;background:var(--badsoft);color:var(--bad);border-radius:13px;padding:11px 14px;font-size:14px;font-weight:500;margin-bottom:18px}
.alert .i{width:18px;height:18px;flex:none;margin-top:3px}
:focus-visible{outline:2px solid var(--pri);outline-offset:2px}
@media(min-width:560px){
body{align-items:center;justify-content:center;padding:24px;background:var(--soft)}
[data-theme=dark] body{background:#0a1211}
.box{flex:none;width:400px;border-radius:22px;overflow:hidden;border:1px solid var(--line);background:var(--bg)}
.bh{flex:none;padding:32px 28px 26px}
.bb{border-radius:0;padding:26px 28px 30px}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body>
<main class="box">
<header class="bh"><div class="logo" aria-hidden="true">ق</div><h1>لوحة التحكّم</h1><p>سجّل الدخول لإدارة المستخدمين والمفاتيح.</p></header>
<form class="bb" method="post" action="/{{admin}}/login">
{% if error %}<div class="alert" role="alert"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg><span>{{error}} تأكّد منها ثم أعد المحاولة.</span></div>{% endif %}
<label for="p">كلمة مرور المالك</label>
<div class="pw"><input id="p" type="password" name="password" autocomplete="current-password" autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="go" autofocus required>
<button type="button" class="eye" aria-label="إظهار كلمة المرور" aria-pressed="false" onclick="var p=document.getElementById('p'),s=p.type=='password';p.type=s?'text':'password';this.setAttribute('aria-pressed',s)"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button></div>
<button class="btn">دخول</button>
</form></main></body></html>
"""

DASHBOARD = r"""{% macro ic(n) %}<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-{{n}}"/></svg>{% endmacro %}{% set nokey = (engine=='gemini' and not gemini_set) or (engine=='claude' and not claude_set) %}<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff"><meta name="robots" content="noindex,nofollow"><meta name="format-detection" content="telephone=no">
<title>لوحة التحكّم</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Readex+Pro:wght@300..700&family=JetBrains+Mono:wght@500;700&display=swap">
<script>try{document.documentElement.dataset.theme=localStorage.getItem('th')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}catch(e){}</script>
<style>
/* ---- tokens: teal means "working", red means "stopped", everything else stays neutral ---- */
:root{--bg:#eef2f1;--card:#fff;--soft:#f3f6f5;--line:#dce4e2;--dash:#b7c5c2;--ink:#10201f;--mut:#566664;
--pri:#0f6b66;--prih:#0b5651;--prik:#fff;--prisoft:#def0ed;--ring:rgba(15,107,102,.22);
--bad:#b42318;--badk:#fff;--badsoft:#fdeae7;--warn:#7a4f00;--warnsoft:#fbefd2;
--hero:#0c3d3a;--heroline:rgba(255,255,255,.14);--herobd:transparent;--off:#a6b4b1;
--pop:0 18px 48px rgba(12,32,30,.22);--nav:60px;--sb:env(safe-area-inset-bottom,0px);
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color-scheme:light}
[data-theme=dark]{--bg:#0a1211;--card:#111b1a;--soft:#182423;--line:#243432;--dash:#3a4d4a;--ink:#e3eeec;--mut:#8c9e9b;
--pri:#56c8bf;--prih:#74d6ce;--prik:#05201e;--prisoft:#12322f;--ring:rgba(86,200,191,.28);
--bad:#f6817a;--badk:#2b0a08;--badsoft:#3a1b19;--warn:#f1c35c;--warnsoft:#33290f;
--hero:#0e2a28;--herobd:#1d3d3a;--off:#3b4d4a;--pop:0 18px 48px rgba(0,0,0,.55);color-scheme:dark}

/* ---- base ---- */
*{box-sizing:border-box}[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;color:var(--ink);background:var(--bg);font:400 15px/1.65 "Readex Pro","IBM Plex Sans Arabic","Segoe UI",system-ui,-apple-system,"Noto Sans Arabic",Tahoma,sans-serif;-webkit-tap-highlight-color:transparent}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer;touch-action:manipulation}
h2,h3,p,dl,dd,ul{margin:0}
h2{font-size:18px;font-weight:600;line-height:1.45}
h3{font-size:16px;font-weight:600;line-height:1.45}
.mut{color:var(--mut)}
.i{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
:focus-visible{outline:2px solid var(--pri);outline-offset:2px}
.wrap{max-width:1120px;margin:0 auto;padding-inline:16px}

/* ---- top bar + navigation (bottom bar on phones, inline tabs on wide screens) ---- */
.top{background:var(--card);border-bottom:1px solid var(--line)}
.topin{display:flex;align-items:center;gap:12px;min-height:60px}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.logo{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;background:var(--hero);color:#eef8f6;font-weight:600;font-size:19px;flex:none}
.brand b{display:block;font-size:16px;font-weight:600;line-height:1.4}
.brand small{display:block;font-size:12.5px;color:var(--mut);line-height:1.4}
.acts{display:flex;gap:6px;margin-inline-start:auto}
.ib{width:42px;height:42px;padding:0;border:1px solid var(--line);background:var(--card);border-radius:12px;display:grid;place-items:center;color:var(--mut);flex:none}
.ib:hover{color:var(--ink);background:var(--soft)}
.th .s{display:none}[data-theme=dark] .th .s{display:block}[data-theme=dark] .th .m{display:none}
.nav{position:fixed;inset-inline:0;bottom:0;z-index:30;display:flex;background:var(--card);border-top:1px solid var(--line);padding-bottom:var(--sb)}
.nav button{position:relative;flex:1;height:var(--nav);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;border:0;background:none;color:var(--mut);font-size:12px;font-weight:500}
.nav .i{width:22px;height:22px}
.nav [aria-current]{color:var(--pri);font-weight:600}
.nav [aria-current]::before{content:"";position:absolute;top:-1px;inset-inline:30%;height:3px;border-radius:0 0 4px 4px;background:var(--pri)}
.nav .dot{position:absolute;top:9px;inset-inline-start:calc(50% + 7px);width:8px;height:8px;border-radius:50%;background:var(--bad);box-shadow:0 0 0 2px var(--card)}
main{padding-bottom:calc(var(--nav) + var(--sb) + 28px)}
.panel{display:none}.panel.on{display:block}

/* ---- controls ---- */
input{width:100%;height:46px;padding:0 14px;border:1px solid var(--line);border-radius:12px;background:var(--soft);outline:0;font-size:16px}
input:focus{border-color:var(--pri);box-shadow:0 0 0 3px var(--ring);background:var(--card)}
input::placeholder{color:var(--mut);opacity:.8}
.btn{display:inline-flex;gap:8px;align-items:center;justify-content:center;min-height:46px;padding:0 18px;border:0;border-radius:12px;font-weight:600;font-size:15px;white-space:nowrap;color:var(--prik);background:var(--pri)}
.btn:hover{background:var(--prih)}
.btn.ghost{background:var(--card);color:var(--ink);border:1px solid var(--line)}.btn.ghost:hover{background:var(--soft)}
.btn.danger{background:var(--bad);color:var(--badk)}
.btn.wide{width:100%}
.busy{opacity:.6;pointer-events:none}
.l{display:block;font-size:13.5px;font-weight:500;color:var(--mut);margin:16px 0 6px}.l small{font-weight:400}
.pw{position:relative}.pw input{padding-left:50px}
.eye{position:absolute;left:4px;top:4px;width:38px;height:38px;padding:0;border:0;border-radius:10px;background:none;color:var(--mut);display:grid;place-items:center}
.eye[aria-pressed=true]{color:var(--pri)}
.kin:placeholder-shown{direction:rtl}

/* ---- summary ---- */
.note{display:flex;gap:10px;align-items:flex-start;background:var(--warnsoft);color:var(--warn);border-radius:14px;padding:12px 14px;font-size:14px;margin-top:16px}
.note .i{width:18px;height:18px;margin-top:3px}
.note button{border:0;background:none;padding:0;color:inherit;font-weight:600;text-decoration:underline;text-underline-offset:3px}
.hero{background:var(--hero);border:1px solid var(--herobd);color:#eef8f6;border-radius:20px;padding:16px 18px 0;margin-top:16px}
.ht{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.tot{display:flex;align-items:baseline;gap:9px}
.tot b{font-size:42px;font-weight:300;line-height:1;font-variant-numeric:tabular-nums}
.tot span{font-size:14px;color:#a3cbc6}
.hb{display:inline-flex;align-items:center;gap:6px;height:40px;padding:0 12px;border:1px solid rgba(255,255,255,.24);border-radius:10px;background:none;color:#d7ebe8;font-size:13px;font-weight:500}
.hb:hover{background:rgba(255,255,255,.08)}
.hb .i{width:16px;height:16px}
.ratio{display:flex;height:8px;border-radius:99px;background:rgba(255,255,255,.17);overflow:hidden;margin-top:14px}
.ratio i{border-radius:99px;background:#63d6ca}
.rl{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:#a3cbc6;margin-top:7px}
.rl b{color:#fff;font-weight:600;font-variant-numeric:tabular-nums}
.kp{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin-top:12px;border-top:1px solid var(--heroline)}
.kp div{padding:10px 0 12px;padding-inline-start:14px;border-inline-start:1px solid var(--heroline)}
.kp div:first-child{padding-inline-start:0;border:0}
.kp dt{font-size:12px;color:#a3cbc6;line-height:1.5}
.kp dd{font-size:20px;font-weight:500;line-height:1.4;font-variant-numeric:tabular-nums}

/* ---- search, filter, sort ---- */
.tb{position:sticky;top:0;z-index:10;display:flex;gap:8px;padding:10px 0 8px;margin-top:4px;background:var(--bg)}
.search{position:relative;flex:1;min-width:0}
.search .i{position:absolute;inset-inline-start:13px;top:13px;color:var(--mut);pointer-events:none}
.search input{padding-inline:42px;border-radius:14px;background:var(--card)}
.search input::-webkit-search-cancel-button{display:none}
.qx{position:absolute;inset-inline-end:4px;top:4px;width:38px;height:38px;padding:0;border:0;border-radius:10px;background:none;color:var(--mut);display:grid;place-items:center}
.qx .i{width:17px;height:17px}
.add{border-radius:14px;padding:0 15px}
.add .wd{display:none}
.fr{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.seg{display:flex;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:3px}
.seg button{height:38px;padding:0 13px;border:0;border-radius:9px;background:none;color:var(--mut);font-weight:500;font-size:14px}
.seg [aria-pressed=true]{background:var(--ink);color:var(--card);font-weight:600}
.sort{position:relative;display:inline-flex;align-items:center;gap:7px;height:46px;padding:0 12px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--mut);font-size:14px;font-weight:500}
.sort .i{width:17px;height:17px}
.sort select{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer;font-size:16px}
.sort:focus-within{outline:2px solid var(--pri);outline-offset:2px}

/* ---- one user ---- */
.list{display:grid;grid-template-columns:minmax(0,1fr);gap:10px}
.u{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px 10px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
.who{display:flex;gap:12px;align-items:center;min-width:0}
.av{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;flex:none;font-weight:600;font-size:18px;line-height:1;background:hsl(var(--h,174) 50% 50%/.15);color:hsl(var(--h,174) 62% 33%)}
[data-theme=dark] .av{color:hsl(var(--h,174) 58% 72%)}
.nm{min-width:0}
.nm h3{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm p{display:flex;flex-wrap:wrap;align-items:center;column-gap:9px;font-size:12.5px;line-height:1.6;color:var(--mut)}
.st{display:inline-flex;align-items:center;gap:5px;font-weight:600;color:var(--pri)}
.st::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.sw{position:relative;width:52px;height:32px;padding:0;border:0;border-radius:99px;background:var(--off);flex:none;transition:background .2s}
.sw::after{content:"";position:absolute;inset:-7px -5px}
.sw i{position:absolute;top:3px;inset-inline-start:3px;width:26px;height:26px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:inset-inline-start .2s}
.sw.on{background:var(--pri)}.sw.on i{inset-inline-start:23px}
.cr{grid-column:1/-1;display:flex;gap:8px}
.code{flex:1;min-width:0;display:flex;align-items:center;gap:10px;min-height:46px;padding:0 14px;border:1px dashed var(--dash);border-radius:12px;background:var(--soft);transition:background .2s,border-color .2s}
.code bdi{font:700 15.5px/1 var(--mono);letter-spacing:.05em;direction:ltr;white-space:nowrap}
.code .i{width:18px;height:18px;margin-inline-start:auto;color:var(--mut)}
.code:hover{border-color:var(--pri)}
.code.ok{border-style:solid;border-color:var(--pri);background:var(--prisoft)}.code.ok .i{color:var(--pri)}
.cr .ib{width:46px;height:46px}
.facts{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:center;gap:0 14px;font-size:13.5px;color:var(--mut)}
.facts b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.dev{display:inline-flex;align-items:center;gap:5px;margin-inline-start:auto;font-size:12.5px;font-weight:500}
.dev .i{width:15px;height:15px}
.dev.on{color:var(--pri)}
.u.off .st{color:var(--bad)}
.u.off .av{filter:grayscale(1);opacity:.55}
.u.off h3{color:var(--mut)}
.u.off .code bdi{text-decoration:line-through;color:var(--mut)}
.acc{grid-column:1/-1;min-width:0}
.acc summary{min-height:44px;margin-block:-10px;list-style:none;cursor:pointer;border-radius:8px}
.acc summary::-webkit-details-marker{display:none}
.acc summary>.i{width:17px;height:17px;margin-inline-start:-6px;transition:transform .2s}
.acc[open] summary>.i{transform:rotate(180deg)}
.alh{margin-top:14px;padding-top:12px;border-top:1px solid var(--line);font-size:12.5px;font-weight:500;color:var(--mut)}
.al{list-style:none;padding:0;margin-top:8px;display:grid;gap:11px}
.al li{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:5px 14px;align-items:baseline;font-size:13.5px;color:var(--mut)}
.al b{font-weight:500;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.al span{font-variant-numeric:tabular-nums}
.pg{grid-column:1/-1;height:4px;border-radius:9px;background:var(--line);overflow:hidden}
.pg i{display:block;height:100%;border-radius:9px;background:var(--pri)}
.empty{text-align:center;padding:40px 20px;color:var(--mut);background:var(--card);border:1px dashed var(--dash);border-radius:16px}
.empty h3{color:var(--ink);margin-bottom:4px}
.empty .btn{margin-top:16px}

/* ---- keys + link ---- */
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;margin-top:16px}
.pad{padding:18px}
.eng{display:grid;gap:10px;margin-top:14px}
.opt{position:relative;display:flex;gap:12px;align-items:center;min-height:66px;padding:10px 14px;border:1.5px solid var(--line);border-radius:14px;background:var(--soft);cursor:pointer}
.opt input{position:absolute;opacity:0;width:1px;height:1px}
.opt b{display:block;font-weight:600;line-height:1.45}
.opt small{display:block;font-size:12.5px;color:var(--mut)}
.ck{margin-inline-start:auto;width:24px;height:24px;border-radius:50%;border:1.5px solid var(--dash);display:grid;place-items:center;color:transparent;flex:none}
.ck .i{width:14px;height:14px;stroke-width:3}
.opt:has(input:checked){border-color:var(--pri);background:var(--prisoft)}
.opt:has(input:checked) .ck{background:var(--pri);border-color:var(--pri);color:var(--prik)}
.opt:has(input:focus-visible){outline:2px solid var(--pri);outline-offset:2px}
.pi{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;flex:none;font:700 17px/1 var(--mono);background:var(--card);border:1px solid var(--line)}
.g2{display:grid;gap:12px;margin-top:12px}.g2 .card{margin:0}
.ph{display:flex;gap:12px;align-items:center}
.b{display:inline-flex;gap:6px;align-items:center;font-size:12.5px;font-weight:600}
.b::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.b.on{color:var(--pri)}.b.off{color:var(--bad)}
.tag{margin-inline-start:auto;background:var(--prisoft);color:var(--pri);border-radius:99px;padding:2px 11px;font-size:12.5px;font-weight:600;white-space:nowrap}
.hint{display:flex;gap:8px;align-items:flex-start;font-size:13px;color:var(--mut);margin:14px 0 16px}
.hint .i{width:16px;height:16px;margin-top:3px}
.lk{display:block;direction:ltr;text-align:left;word-break:break-all;font:500 13px/1.75 var(--mono);background:var(--soft);border:1px dashed var(--dash);border-radius:12px;padding:12px 14px;margin-top:14px;-webkit-user-select:all;user-select:all}
.da{display:flex;gap:10px;margin-top:20px}.da .btn{flex:1}
.lk + .da{margin-top:12px}
.notes{list-style:none;padding:0;margin-top:20px;display:grid;gap:10px;color:var(--mut);font-size:14px}
.notes li{display:flex;gap:10px;align-items:flex-start}
.notes .i{width:18px;height:18px;margin-top:3px;color:var(--pri)}

/* ---- dialogs: sheets rise from the bottom on phones ---- */
dialog{border:0;padding:0;background:var(--card);color:var(--ink);box-shadow:var(--pop);max-height:calc(100vh - 24px);overflow:auto}
dialog::backdrop{background:rgba(6,14,13,.55)}
dialog[open]{animation:pop .22s cubic-bezier(.2,.8,.2,1)}
.ask{width:calc(100vw - 32px);max-width:420px;margin:12vh auto auto;border-radius:20px}
.sheet{width:100%;max-width:100%;margin:auto 0 0;border-radius:22px 22px 0 0;padding-bottom:var(--sb)}
.sheet[open]{animation:rise .26s cubic-bezier(.2,.8,.2,1)}
@keyframes rise{from{transform:translateY(100%)}}
@keyframes pop{from{transform:translateY(12px);opacity:0}}
.grab{width:38px;height:4px;border-radius:9px;background:var(--dash);margin:9px auto 0}
.dg{padding:20px 18px 18px}
.dg h3{font-size:18px}
.shh{display:flex;gap:12px;align-items:center;padding:12px 16px 14px;border-bottom:1px solid var(--line)}
.shh div:nth-child(2){min-width:0;flex:1}
.shh h3{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.shh bdi{display:block;font:500 13px/1.5 var(--mono);color:var(--mut);direction:ltr;text-align:right}
.shl{display:grid;padding:6px 8px 10px}
.shl button{display:flex;gap:13px;align-items:center;width:100%;min-height:52px;padding:6px 10px;border:0;border-radius:12px;background:none;font-size:15.5px;font-weight:500;text-align:start}
.shl button:hover{background:var(--soft)}
.shl button .i{color:var(--mut)}
.shl small{display:block;font-size:12.5px;font-weight:400;line-height:1.5;color:var(--mut)}
.shl hr{border:0;border-top:1px solid var(--line);margin:6px 10px}
.shl .del button,.shl .del button .i{color:var(--bad)}
.big{display:block;margin-top:16px;padding:22px 8px;border:1.5px dashed var(--dash);border-radius:14px;background:var(--soft);text-align:center;direction:ltr;font:700 clamp(19px,6.2vw,26px)/1 var(--mono);letter-spacing:.06em;-webkit-user-select:all;user-select:all}
.toasts{position:fixed;inset-inline:0;bottom:calc(var(--nav) + var(--sb) + 14px);z-index:99;display:flex;flex-direction:column;align-items:center;gap:8px;padding:0 16px;pointer-events:none}
.toast{background:var(--ink);color:var(--card);padding:10px 18px;border-radius:12px;font-weight:500;font-size:14px;box-shadow:var(--pop);animation:pop .2s}
.toast.out{opacity:0;transition:opacity .3s}

/* ---- wider screens ---- */
@media(min-width:420px){.add .wd{display:inline}}
@media(min-width:600px){
.eng{grid-template-columns:1fr 1fr}
.sheet{width:420px;margin:auto;border-radius:20px;padding-bottom:0}
.sheet[open]{animation-name:pop}
.ask{margin:auto}
.grab{display:none}
.shh{padding-top:16px}
.lk + .da .btn{flex:none}
}
@media(min-width:700px){.list{grid-template-columns:repeat(auto-fill,minmax(330px,1fr));align-items:start;gap:12px}}
@media(min-width:800px){.g2{grid-template-columns:1fr 1fr}.pad{padding:22px}}
@media(min-width:900px){
.wrap{padding-inline:24px}
.topin{min-height:64px}
.nav{position:static;gap:4px;margin-inline-start:20px;padding:0;border:0;background:none}
.nav button{flex:none;flex-direction:row;gap:8px;height:40px;padding:0 14px;border-radius:10px;font-size:14.5px}
.nav .i{width:19px;height:19px}
.nav [aria-current]{background:var(--prisoft)}
.nav [aria-current]::before{display:none}
.nav .dot{position:static;margin-inline-start:-2px;box-shadow:none}
main{padding-bottom:56px}
.toasts{bottom:24px}
.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);align-items:center;gap:0 30px;padding:22px 26px;margin-top:22px}
.kp{margin:0;border-top:0;border-inline-start:1px solid var(--heroline)}
.kp div{padding:6px 0;padding-inline-start:22px}
.kp div:first-child{padding-inline-start:26px}
.kp dd{font-size:24px}
.tb{padding-top:14px}
.search{max-width:420px}
.add{margin-inline-start:auto}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-moon" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></symbol>
<symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></symbol>
<symbol id="i-out" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-more" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></symbol>
<symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></symbol>
<symbol id="i-phone" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/></symbol>
<symbol id="i-reset" viewBox="0 0 24 24"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></symbol>
<symbol id="i-trash" viewBox="0 0 24 24"><path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></symbol>
<symbol id="i-chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
<symbol id="i-users" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></symbol>
<symbol id="i-key" viewBox="0 0 24 24"><circle cx="7.5" cy="15.5" r="5.5"/><path d="m11.4 11.6 9.6-9.6M17 6l3 3M14 9l2 2"/></symbol>
<symbol id="i-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></symbol>
<symbol id="i-share" viewBox="0 0 24 24"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 6l-4-4-4 4M12 2v13"/></symbol>
<symbol id="i-edit" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></symbol>
<symbol id="i-eye" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></symbol>
<symbol id="i-sort" viewBox="0 0 24 24"><path d="M3 6h18M6 12h12M10 18h4"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01"/></symbol>
<symbol id="i-lock" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></symbol>
<symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></symbol>
</defs></svg>

<header class="top"><div class="wrap topin">
<div class="brand"><div class="logo" aria-hidden="true">ق</div><div><b>لوحة التحكّم</b><small>رموز الدخول والمفاتيح</small></div></div>
<nav class="nav" aria-label="أقسام اللوحة">
<button type="button" data-t="users" aria-current="page">{{ic('users')}}<span>المستخدمون</span></button>
<button type="button" data-t="keys">{{ic('key')}}<span>المفاتيح</span>{% if nokey %}<i class="dot" title="لا يوجد مفتاح للكاتب الحالي"></i>{% endif %}</button>
<button type="button" data-t="link">{{ic('link')}}<span>الرابط</span></button>
</nav>
<div class="acts"><button class="ib th" id="th" type="button" aria-label="تبديل المظهر"><span class="m">{{ic('moon')}}</span><span class="s">{{ic('sun')}}</span></button>
<form method="post" action="/{{admin}}/logout"><button class="ib" aria-label="تسجيل الخروج" title="تسجيل الخروج">{{ic('out')}}</button></form></div>
</div></header>

<main class="wrap">
<section class="panel on" id="p-users">
{% if nokey and codes %}<div class="note" role="status">{{ic('alert')}}<p>لا يوجد مفتاح للكاتب الحالي، فلن تُكتب أي تعليقات. <button type="button" data-t="keys">أضف المفتاح</button></p></div>{% endif %}
{% if codes %}
<section class="hero" aria-label="ملخّص المستخدمين">
<div>
<div class="ht"><div class="tot"><b>{{stats.users}}</b><span>مستخدم</span></div>
{% if stats.comments %}<form method="post" action="/{{admin}}/reset-cost-all" data-title="تصفير كل التكاليف؟" data-confirm="سيُصفَّر عدّاد التعليقات والتكلفة لكل المستخدمين." data-yes="تصفير التكاليف" data-kind="danger" data-toast="تم تصفير كل التكاليف"><button class="hb">{{ic('reset')}}تصفير التكاليف</button></form>{% endif %}</div>
<div class="ratio" aria-hidden="true"><i style="width:{{ stats.active * 100 / stats.users }}%"></i></div>
<div class="rl"><span><b>{{stats.active}}</b> يعمل</span><span><b>{{stats.users - stats.active}}</b> موقوف</span></div>
</div>
<dl class="kp">
<div><dt>أجهزة مربوطة</dt><dd>{{stats.bound}}</dd></div>
<div><dt>تعليقات مكتوبة</dt><dd>{{ '{:,}'.format(stats.comments) }}</dd></div>
<div><dt>التكلفة التقديرية</dt><dd>${{ '%.2f'|format(stats.cost) }}</dd></div>
</dl>
</section>
<div class="tb">
<label class="search">{{ic('search')}}<input id="q" type="search" placeholder="ابحث بالاسم أو الرمز" aria-label="ابحث بالاسم أو الرمز" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="search"><button type="button" class="qx" id="qx" aria-label="مسح البحث" hidden>{{ic('x')}}</button></label>
<button class="btn add" type="button" data-open="nu" aria-label="إضافة مستخدم">{{ic('plus')}}<span>إضافة<span class="wd"> مستخدم</span></span></button>
</div>
<div class="fr">
<div class="seg" id="flt" role="group" aria-label="تصفية حسب الحالة"><button type="button" data-f="all" aria-pressed="true">الكل</button><button type="button" data-f="on" aria-pressed="false">يعمل</button><button type="button" data-f="off" aria-pressed="false">موقوف</button></div>
<label class="sort">{{ic('sort')}}<span id="sol">الأحدث</span><select id="so" aria-label="ترتيب المستخدمين"><option value="new" data-s="الأحدث">الأحدث إضافةً</option><option value="seen" data-s="آخر دخول">آخر دخول</option><option value="top" data-s="الأكثر تعليقًا">الأكثر تعليقًا</option><option value="name" data-s="الاسم">الاسم</option></select></label>
</div>
{% endif %}

<div class="list" id="list">
{% for c in codes %}{% set hue = (c.id[:2]|int(0,16)) * 360 // 256 %}
<article class="u{{ '' if c.active else ' off' }}" data-id="{{c.id}}" data-s="{{ 'on' if c.active else 'off' }}" data-name="{{c.label}}" data-code="{{c.code}}" data-bound="{{ '1' if c.bound_device else '' }}" data-n="{{c.usage.total}}" data-seen="{{c.last_seen or 0}}" data-q="{{ (c.label ~ ' ' ~ c.code ~ ' ' ~ c.code|replace('-','') ~ ' ' ~ c.usage.accounts|map(attribute='name')|join(' '))|lower }}">
<div class="who"><div class="av" style="--h:{{hue}}" aria-hidden="true">{{ (c.label or '؟')[:1] }}</div>
<div class="nm"><h3>{{ c.label or 'بدون اسم' }}</h3><p><span class="st">{{ 'يعمل' if c.active else 'موقوف' }}</span><span>{% if c.last_seen %}آخر دخول {{c.last_seen_text}}{% else %}لم يدخل بعد{% endif %}</span></p></div></div>
<form method="post" action="/{{admin}}/code/toggle" data-toast="{{ 'تم إيقاف الرمز' if c.active else 'تم تشغيل الرمز' }}"><input type="hidden" name="id" value="{{c.id}}"><button class="sw{{ ' on' if c.active else '' }}" role="switch" aria-checked="{{ 'true' if c.active else 'false' }}" aria-label="رمز {{ c.label or 'بدون اسم' }} يعمل"><i></i></button></form>
<div class="cr"><button type="button" class="code" data-copy="{{c.code}}" aria-label="نسخ الرمز {{c.code}}"><bdi>{{c.code}}</bdi>{{ic('copy')}}</button><button type="button" class="ib" data-more aria-label="خيارات {{ c.label or 'المستخدم' }}">{{ic('more')}}</button></div>
{% set facts %}<span><b>{{ '{:,}'.format(c.usage.total) }}</b> تعليق</span><span title="التكلفة التقديرية"><b>${{ '%.2f'|format(c.usage.cost) }}</b></span><span class="dev{{ ' on' if c.bound_device else '' }}">{{ic('phone')}}{{ 'جهاز مربوط' if c.bound_device else 'غير مربوط' }}</span>{% endset %}
{% if c.usage.accounts %}<details class="acc"><summary class="facts" title="التعليقات حسب حساب X">{{facts}}{{ic('chev')}}</summary>
<p class="alh">حسابات X ({{c.usage.accounts|length}})</p><ul class="al">
{% for a in c.usage.accounts %}<li><b><bdi>{{ ('@' ~ a.name) if a.name else ('#' ~ a.id[:8]) }}</bdi></b><span>{{ '{:,}'.format(a.n) }} تعليق</span><span>${{ '%.2f'|format(a.cost) }}</span><div class="pg"><i style="width:{{ ((a.n * 100 / c.usage.total)|round|int) if c.usage.total else 0 }}%"></i></div></li>{% endfor %}
</ul></details>{% else %}<div class="facts">{{facts}}</div>{% endif %}
</article>
{% endfor %}
</div>
{% if not codes %}<div class="empty" style="margin-top:16px"><h3>لا يوجد مستخدمون بعد</h3><p>أضف مستخدمًا ليُنشأ له رمز دخول يفتح به التطبيق.</p><button class="btn" type="button" data-open="nu">{{ic('plus')}}إضافة مستخدم</button></div>{% endif %}
<div class="empty" id="nomatch" hidden><h3>لا نتائج</h3><p>جرّب اسمًا أو رمزًا آخر، أو غيّر التصفية.</p><button class="btn ghost" type="button" id="clr">عرض كل المستخدمين</button></div>
</section>

<section class="panel" id="p-keys">
<div class="card pad"><h2>الكاتب الحالي</h2><p class="mut">اختر المحرّك الذي يكتب التعليقات. يمكنك التبديل بينهما في أي لحظة.</p>
<form class="eng" method="post" action="/{{admin}}/engine" data-toast="تم تغيير الكاتب">
<label class="opt"><input type="radio" name="engine" value="gemini" {{ 'checked' if engine=='gemini' else '' }} onchange="this.form.requestSubmit()"><span class="pi" aria-hidden="true">G</span><span><b>Gemini</b><small>{{ 'المفتاح مضبوط' if gemini_set else 'لا يوجد مفتاح' }}</small></span><span class="ck">{{ic('check')}}</span></label>
<label class="opt"><input type="radio" name="engine" value="claude" {{ 'checked' if engine=='claude' else '' }} onchange="this.form.requestSubmit()"><span class="pi" aria-hidden="true">C</span><span><b>Claude</b><small>{{ 'المفتاح مضبوط' if claude_set else 'لا يوجد مفتاح' }}</small></span><span class="ck">{{ic('check')}}</span></label>
<noscript><button class="btn">تفعيل</button></noscript></form>
{% if nokey %}<div class="note" role="status">{{ic('alert')}}<p>الكاتب المختار بلا مفتاح، فلن تُكتب أي تعليقات. أضف مفتاحه في الأسفل أو اختر الكاتب الآخر.</p></div>{% endif %}
</div>
<div class="g2">
{% for k in [('gemini','Gemini',gemini_set,gemini_model,gemini_default,'G','الصق مفتاح Gemini'),('claude','Claude',claude_set,claude_model,claude_default,'C','الصق مفتاح Claude (sk-ant-…)')] %}
<form class="card pad" method="post" action="/{{admin}}/key" data-toast="تم حفظ إعدادات {{k[1]}}"><input type="hidden" name="which" value="{{k[0]}}">
<div class="ph"><span class="pi" aria-hidden="true">{{k[5]}}</span><div><h3>{{k[1]}}</h3><span class="b {{ 'on' if k[2] else 'off' }}">{{ 'المفتاح مضبوط' if k[2] else 'لا يوجد مفتاح' }}</span></div>{% if engine==k[0] %}<span class="tag">الكاتب الحالي</span>{% endif %}</div>
<label class="l" for="k-{{k[0]}}">مفتاح API</label>
<div class="pw"><input class="kin" id="k-{{k[0]}}" type="password" name="api_key" dir="ltr" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="{{ 'الصق مفتاحًا جديدًا للتبديل' if k[2] else k[6] }}"><button type="button" class="eye" data-eye aria-label="إظهار المفتاح" aria-pressed="false">{{ic('eye')}}</button></div>
<label class="l" for="m-{{k[0]}}">النموذج</label><input id="m-{{k[0]}}" name="model" dir="ltr" value="{{k[3]}}" placeholder="{{k[4]}}" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
<p class="hint">{{ic('lock')}}<span>يُحفظ المفتاح مختومًا على الخادم ولا يُرسل إلى الأجهزة.</span></p>
<button class="btn wide">حفظ الإعدادات</button></form>
{% endfor %}
</div>
</section>

<section class="panel" id="p-link">
<div class="card pad"><h2>رابط التطبيق</h2><p class="mut">انسخه كاملًا وضعه في التطبيق عند إعداد الخادم.</p>
<code class="lk">{{app_link}}</code>
<div class="da"><button type="button" class="btn" data-copy="{{app_link}}">{{ic('copy')}}نسخ الرابط</button><button type="button" class="btn ghost" data-share="{{app_link}}">{{ic('share')}}مشاركة</button></div>
<ul class="notes"><li>{{ic('shield')}}<span>يحمل الرابط بصمة شهادة الخادم، فلا يقبل التطبيق أي خادم آخر.</span></li><li>{{ic('lock')}}<span>الرابط سرّي: شاركه فقط مع من تعطيه رمز دخول.</span></li></ul></div>
</section>
</main>

<dialog id="sheet" class="sheet" aria-labelledby="sh-name"><div class="grab"></div>
<div class="shh"><div class="av" id="sh-av" aria-hidden="true"></div><div><h3 id="sh-name"></h3><bdi id="sh-code"></bdi></div><button type="button" class="ib" data-close aria-label="إغلاق">{{ic('x')}}</button></div>
<div class="shl">
<button type="button" id="sh-copy" data-copy="" data-close>{{ic('copy')}}نسخ الرمز</button>
<button type="button" id="sh-share" data-share="" data-close>{{ic('share')}}مشاركة الرمز</button>
<button type="button" id="sh-rename">{{ic('edit')}}تعديل الاسم</button>
<form id="sh-release" method="post" action="/{{admin}}/code/release" data-title="تحرير الجهاز؟" data-confirm="يُفكّ ارتباط الرمز بجواله الحالي ليتمكّن صاحبه من تفعيله على جوال آخر." data-yes="تحرير الجهاز" data-toast="تم تحرير الجهاز"><input type="hidden" name="id"><button>{{ic('phone')}}<span>تحرير الجهاز<small>ليُفعَّل الرمز على جوال آخر</small></span></button></form>
<form id="sh-reset" method="post" action="/{{admin}}/code/reset-cost" data-title="تصفير التكلفة؟" data-confirm="سيُصفَّر عدّاد التعليقات والتكلفة لهذا المستخدم فقط." data-yes="تصفير التكلفة" data-kind="danger" data-toast="تم تصفير التكلفة"><input type="hidden" name="id"><button>{{ic('reset')}}تصفير التكلفة</button></form>
<hr>
<form class="del" method="post" action="/{{admin}}/code/delete" data-title="حذف المستخدم؟" data-confirm="يُحذف رمزه نهائيًا وتُفقد إحصائياته، ولا يمكن التراجع." data-yes="حذف المستخدم" data-kind="danger" data-toast="تم حذف المستخدم"><input type="hidden" name="id"><button>{{ic('trash')}}حذف المستخدم</button></form>
</div></dialog>

<dialog id="nu" class="ask" aria-labelledby="nu-t"><form class="dg" method="post" action="/{{admin}}/code/new" data-fresh><h3 id="nu-t">إضافة مستخدم</h3><p class="mut">يُنشأ له رمز دخول فريد تعطيه إيّاه.</p>
<label class="l" for="nu-l">اسم المستخدم <small>(اختياري)</small></label><input id="nu-l" name="label" maxlength="40" placeholder="مثال: أحمد" autocomplete="off" enterkeyhint="done">
<div class="da"><button type="button" class="btn ghost" data-close>إلغاء</button><button class="btn">إضافة المستخدم</button></div></form></dialog>

<dialog id="rn" class="ask" aria-labelledby="rn-t"><form class="dg" method="post" action="/{{admin}}/code/rename" data-toast="تم حفظ الاسم"><input type="hidden" name="id"><h3 id="rn-t">تعديل الاسم</h3><p class="mut">الاسم يظهر لك وحدك في هذه اللوحة، والرمز لا يتغيّر.</p>
<label class="l" for="rn-l">اسم المستخدم</label><input id="rn-l" name="label" maxlength="40" autocomplete="off" enterkeyhint="done">
<div class="da"><button type="button" class="btn ghost" data-close>إلغاء</button><button class="btn">حفظ الاسم</button></div></form></dialog>

<dialog id="rd" class="sheet" aria-labelledby="rd-h"><div class="grab"></div><div class="dg"><h3 id="rd-h">الرمز جاهز</h3><p class="mut" id="rd-t">أعطِ هذا الرمز للمستخدم ليفتح به التطبيق. يرتبط الرمز بأول جوال يُدخَل فيه.</p>
<bdi class="big" id="rd-code"></bdi>
<div class="da"><button type="button" class="btn ghost" id="rd-share" data-share="" data-close>{{ic('share')}}مشاركة</button><button type="button" class="btn" id="rd-copy" data-copy="" data-close>{{ic('copy')}}نسخ الرمز</button></div></div></dialog>

<dialog id="cf" class="sheet" aria-labelledby="cft"><div class="grab"></div><div class="dg"><h3 id="cft"></h3><p class="mut" id="cfm"></p><div class="da"><button type="button" class="btn ghost" data-close>إلغاء</button><button type="button" class="btn" id="cfok">تأكيد</button></div></div></dialog>
<div class="toasts" aria-live="polite"></div>

<script>
(function(){
var D=document,R=D.documentElement,
$=function(s,r){return(r||D).querySelector(s)},
$$=function(s,r){return[].slice.call((r||D).querySelectorAll(s))},
NOW=Date.now();
function lget(k){try{return localStorage.getItem(k)}catch(e){return null}}
function lset(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function sget(k){try{return sessionStorage.getItem(k)}catch(e){return null}}
function sset(k,v){try{v==null?sessionStorage.removeItem(k):sessionStorage.setItem(k,v)}catch(e){}}
/* a value left for the very next page load only (it is dropped if that load comes late) */
function leave(k,v){sset(k,Date.now()+'|'+v)}
function take(k){var v=sget(k);if(v==null)return null;sset(k);var at=v.indexOf('|');return NOW-(+v.slice(0,at))<15000?v.slice(at+1):null}
function toast(m){var t=D.createElement('div');t.className='toast';t.textContent=m;$('.toasts').appendChild(t);setTimeout(function(){t.classList.add('out');setTimeout(function(){t.remove()},300)},2400)}

/* sections */
var TABS=['users','keys','link'];
function tab(n,byUser){if(TABS.indexOf(n)<0)n='users';
$$('.panel').forEach(function(p){p.classList.toggle('on',p.id==='p-'+n)});
$$('.nav button').forEach(function(b){if(b.dataset.t===n)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
lset('tab',n);if(byUser)window.scrollTo(0,0)}
$$('[data-t]').forEach(function(b){b.addEventListener('click',function(){tab(b.dataset.t,true)})});
tab(lget('tab'));

/* light / dark */
var tc=$('meta[name=theme-color]');
function paint(){tc.content=R.dataset.theme==='dark'?'#111b1a':'#ffffff'}
paint();
$('#th').onclick=function(){R.dataset.theme=R.dataset.theme==='dark'?'light':'dark';lset('th',R.dataset.theme);paint()};

/* search, filter and sort of the users */
var list=$('#list'),rows=$$('.u'),q=$('#q'),so=$('#so'),nomatch=$('#nomatch'),fl=sget('fl')||'all';
rows.forEach(function(u,i){u._i=i});
var ORDER={
'new':function(a,b){return a._i-b._i},
seen:function(a,b){return b.dataset.seen-a.dataset.seen||a._i-b._i},
top:function(a,b){return b.dataset.n-a.dataset.n||a._i-b._i},
name:function(a,b){return a.dataset.name.localeCompare(b.dataset.name,'ar')||a._i-b._i}};
function filter(){if(!q)return;var s=q.value.trim().toLowerCase(),n=0;
rows.forEach(function(u){var ok=(fl==='all'||u.dataset.s===fl)&&(!s||u.dataset.q.indexOf(s)>-1);u.hidden=!ok;if(ok)n++});
nomatch.hidden=n>0;$('#qx').hidden=!s;
$$('#flt button').forEach(function(b){b.setAttribute('aria-pressed',String(b.dataset.f===fl))});sset('fl',fl)}
function sort(){if(!so)return;if(!ORDER[so.value])so.value='new';
$('#sol').textContent=so.options[so.selectedIndex].dataset.s;
rows.slice().sort(ORDER[so.value]).forEach(function(u){list.appendChild(u)});sset('so',so.value)}
if(q){
q.value=take('q')||'';so.value=sget('so')||'new';
q.oninput=filter;so.onchange=sort;
$('#qx').onclick=function(){q.value='';filter();q.focus()};
$('#clr').onclick=function(){q.value='';fl='all';filter()};
$$('#flt button').forEach(function(b){b.onclick=function(){fl=b.dataset.f;filter()}});
}

/* a user just added: show the new code, ready to copy or share */
var added=take('new');
if(added!=null&&rows.length===+added+1){var nu=rows[0],rd=$('#rd'),nc=nu.dataset.code;
if(q){q.value='';fl='all';so.value='new'}
$('#rd-code').textContent=nc;$('#rd-copy').dataset.copy=nc;$('#rd-share').dataset.share='رمز الدخول: '+nc;
if(nu.dataset.name)$('#rd-t').textContent='أعطِ هذا الرمز لـ«'+nu.dataset.name+'» ليفتح به التطبيق. يرتبط الرمز بأول جوال يُدخَل فيه.';
rd.showModal()}
sort();filter();
var y=take('y');if(y)window.scrollTo(0,+y);
var note=take('toast');if(note)toast(note);

/* copy and share */
function copy(v,el){
function ok(){toast('تم النسخ');if(el&&el.classList.contains('code')){var us=$('use',el),h=us.getAttribute('href');el.classList.add('ok');us.setAttribute('href','#i-check');setTimeout(function(){el.classList.remove('ok');us.setAttribute('href',h)},1300)}}
function old(){var a=D.createElement('textarea');a.value=v;a.style.cssText='position:fixed;top:0;opacity:0';($('dialog[open]')||D.body).appendChild(a);a.select();var done=false;try{done=D.execCommand('copy')}catch(x){}a.remove();done?ok():toast('تعذّر النسخ. انسخه يدويًا.')}
if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(v).then(ok,old);else old()}
if(!navigator.share)$$('[data-share]').forEach(function(b){b.hidden=true});

/* the options sheet of one user */
var sheet=$('#sheet'),cur=null;
function openSheet(u){cur=u;var d=u.dataset,av=$('.av',u),sa=$('#sh-av');
$('#sh-name').textContent=d.name||'بدون اسم';$('#sh-code').textContent=d.code;
sa.textContent=av.textContent;sa.style.setProperty('--h',av.style.getPropertyValue('--h'));
$$('input[name=id]',sheet).forEach(function(i){i.value=d.id});
$('#sh-copy').dataset.copy=d.code;$('#sh-share').dataset.share='رمز الدخول: '+d.code;
$('#sh-release').hidden=!d.bound;$('#sh-reset').hidden=!+d.n;
sheet.showModal()}
$('#sh-rename').onclick=function(){sheet.close();var d=$('#rn'),i=$('#rn-l');$('input[name=id]',d).value=cur.dataset.id;i.value=cur.dataset.name;d.showModal();i.focus();i.select()};

D.addEventListener('click',function(e){var t=e.target,el;
if((el=t.closest('[data-more]'))){openSheet(el.closest('.u'));return}
if((el=t.closest('[data-open]'))){var d=$('#'+el.dataset.open),i=$('input:not([type=hidden])',d);d.showModal();if(i){i.value='';i.focus()}return}
if((el=t.closest('[data-eye]'))){var p=$('input',el.parentNode),show=p.type==='password';p.type=show?'text':'password';el.setAttribute('aria-pressed',String(show));return}
if((el=t.closest('[data-copy]')))copy(el.dataset.copy,el);
if((el=t.closest('[data-share]'))&&navigator.share)navigator.share({text:el.dataset.share}).catch(function(){});
if((el=t.closest('[data-close]'))){el=el.closest('dialog');if(el)el.close()}
else if(t.tagName==='DIALOG')t.close()});

/* every change is a form post; risky ones ask first */
var cf=$('#cf'),pending=null;
function keep(f){if(f.dataset.toast)leave('toast',f.dataset.toast);
if(f.hasAttribute('data-fresh'))leave('new',rows.length);
else{leave('y',window.scrollY);if(q)leave('q',q.value)}}
$('#cfok').onclick=function(){var f=pending;pending=null;cf.close();if(f){keep(f);f.submit()}};
D.addEventListener('submit',function(e){var f=e.target;
if(f.dataset.confirm){e.preventDefault();var o=f.closest('dialog'),b=$('#cfok');if(o)o.close();pending=f;
$('#cft').textContent=f.dataset.title||'تأكيد الإجراء';$('#cfm').textContent=f.dataset.confirm;
b.textContent=f.dataset.yes||'تأكيد';b.classList.toggle('danger',f.dataset.kind==='danger');cf.showModal();return}
keep(f);
var sw=$('.sw',f),go=$('button:not([type=button])',f);
if(sw){sw.classList.toggle('on');sw.setAttribute('aria-checked',String(sw.classList.contains('on')))}
if(go&&!sw)go.classList.add('busy')});
})();
</script>
</body></html>
"""

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
    stats = {"users": len(rows), "active": sum(1 for r in rows if r.get("active")),
             "bound": sum(1 for r in rows if r.get("bound_device")),
             "comments": sum(r["usage"]["total"] for r in rows),
             "cost": round(sum(r["usage"]["cost"] for r in rows), 2)}
    return render_template_string(
        DASHBOARD, admin=ADMIN, app_link=app_link(), codes=rows, stats=stats,
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


@app.post(f"/{ADMIN}/code/rename")
def code_rename():
    """Change the name the owner sees for a code; the code itself and its phone stay as they are."""
    if not logged_in():
        return redirect(f"/{ADMIN}")
    code_id = request.form.get("id")
    label = (request.form.get("label") or "").strip()[:40]
    with LOCK:
        codes = load_codes()
        for row in codes:
            if row.get("id") == code_id:
                row["label"] = label
        save_codes(codes)
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/code/reset-cost")
def code_reset_cost():
    if not logged_in():
        return redirect(f"/{ADMIN}")
    reset_usage(request.form.get("id"))
    return redirect(f"/{ADMIN}")


@app.post(f"/{ADMIN}/reset-cost-all")
def reset_cost_all():
    if not logged_in():
        return redirect(f"/{ADMIN}")
    reset_all_usage()
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


@app.post(f"/{API}/usage")
def api_usage():
    """The app's page shows these at its top: the comments written for this code and their cost, in all and by
    X account (kept here, so they stay when the app is installed again)."""
    body = request.get_json(silent=True) or {}
    row = code_from_token(body.get("token"))
    if not row:
        return api_err("انتهت صلاحية الدخول أو أوقِف الرمز أو نُقل لجهاز آخر. أعد إدخال الرمز.", 200)
    return api_ok(**usage_by_account(row["id"]))


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
