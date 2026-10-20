#!/usr/bin/env python3
"""Watch one X group chat and record every new tweet with the account that sent it."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path

import queue

from flask import Flask, jsonify, render_template_string, request, session
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

if Path("/opt/pw-browsers").is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("X_APP_SECURE_COOKIE", "0") == "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)

READ_GROUPS_JS = r"""
(wanted) => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const timeLike = /^(now|الآن|\d{1,2}:\d{2}(?:\s*[ap]m)?|\d+\s*[smhdw]|\d+\s*(?:د|دقائق|دقيقة|س|ساعة|ساعات|ي|يوم|أيام|ث|أسبوع|أسابيع))\.?$/i;
  const chrome = /^(all|unread|direct|groups|group|messages|search|new chat|chat|requests|people|الكل|غير مقروء|المباشرة|قروبات|القروبات|مجموعات|المجموعات|رسائل|بحث|تم)$/i;
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const queryAll = (selector) => {
    const out = [];
    for (const root of roots) if (root.querySelectorAll) out.push(...root.querySelectorAll(selector));
    return out;
  };
  const rows = queryAll('[data-testid^="dm-conversation-item-"], [data-testid^="dm-message-request-item-"]');
  const nameOf = (row) => {
    const titled = [...row.querySelectorAll(".line-clamp-1")].map((el) => clean(el.textContent || "")).find((text) => text.length >= 2 && text.length <= 80 && !timeLike.test(text) && !chrome.test(text));
    if (titled) return titled;
    const leaves = [...row.querySelectorAll("span, div")].filter((el) => {
      if (el.querySelector("span, div")) return false;
      const text = clean(el.textContent || "");
      return text.length >= 2 && text.length <= 80 && !timeLike.test(text) && !chrome.test(text);
    });
    if (leaves.length) return clean(leaves[0].textContent || "");
    return "";
  };
  if (wanted) {
    // A group is found by its id (the row's data-testid), which stays the same when the
    // group is renamed. The name is only used for groups saved before ids were kept.
    const wantedId = typeof wanted === "object" ? String(wanted.id || "") : "";
    const target = clean(typeof wanted === "object" ? wanted.name : wanted);
    const match = wantedId
      ? rows.find((row) => (row.getAttribute("data-testid") || "") === wantedId)
      : rows.find((row) => target && nameOf(row) === target);
    if (!match) return {clicked: false, groups: [], rows: rows.length, loading: false};
    const link = match.querySelector('a[href*="/i/chat/"], a[href*="/messages/"]');
    if (link) link.click();
    else match.click();
    return {clicked: true, name: nameOf(match), groups: [], rows: rows.length, loading: false};
  }
  const groups = [];
  const seen = new Set();
  for (const row of rows) {
    const id = row.getAttribute("data-testid") || "";
    const desc = row.getAttribute("aria-description") || "";
    const isGroup = /dm-conversation-item-g\d/.test(id) || /جماعية|group chat/i.test(desc);
    if (!isGroup) continue;
    const name = nameOf(row);
    if (!id || !name || seen.has(id)) continue;
    seen.add(id);
    groups.push({id, name});
  }
  const loading = queryAll('[data-testid="dm-conversation-list-loading-footer"]').some((el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
  return {clicked: false, groups, rows: rows.length, loading};
}
"""

SCROLL_INBOX_JS = r"""
() => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const query = (selector) => {
    for (const root of roots) {
      const found = root.querySelector && root.querySelector(selector);
      if (found) return found;
    }
    return null;
  };
  const panel = query('[data-testid="dm-inbox-panel"]');
  const row = query('[data-testid^="dm-conversation-item-"]');
  let node = panel || (row ? row.parentElement : null);
  while (node && node !== document.body) {
    const style = getComputedStyle(node);
    if (/(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 40) {
      const before = node.scrollTop;
      node.scrollTop = before + Math.max(220, node.clientHeight * 0.75);
      return node.scrollTop > before;
    }
    node = node.parentElement;
  }
  return false;
}
"""


GROUP_CHAT_SCROLL_JS = r"""
(action) => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const query = (selector) => {
    for (const root of roots) {
      const found = root.querySelector && root.querySelector(selector);
      if (found) return found;
    }
    return null;
  };
  const list = query('[data-testid="dm-message-list"]') || query('[data-testid="dm-conversation-content"]');
  const scroller = query('[data-testid="dm-message-scroller"]') || list;
  const spinner = query('[data-testid="dm-message-list-spinner-slot"], [data-testid="dm-message-list-spinner"], [role="progressbar"]');
  let spinnerVisible = false;
  if (spinner) {
    const rect = spinner.getBoundingClientRect();
    spinnerVisible = rect.width > 8 && rect.height > 8;
  }
  const messageNodes = list
    ? [...list.querySelectorAll('[data-testid^="message-"]')].filter((el) => {
        const id = el.getAttribute("data-testid") || "";
        return id.startsWith("message-") && !id.startsWith("message-text-");
      })
    : [];
  const messages = messageNodes.length;
  const ids = messageNodes.map((el) => el.getAttribute("data-testid") || "");
  const fingerprint = ids.slice(0, 8).concat(ids.slice(-8)).join("|");
  let moved = 0;
  let atOlderEdge = false;
  if (scroller) {
    atOlderEdge = scroller.scrollTop <= 48;
    if (action === "latest") {
      const before = scroller.scrollTop;
      scroller.scrollTop = scroller.scrollHeight;
      moved = Math.abs(scroller.scrollTop - before);
    }
    if (action === "older") {
      const before = scroller.scrollTop;
      const step = Math.max(420, Math.round(scroller.clientHeight * 0.75));
      scroller.scrollTop = Math.max(0, before - step);
      moved = Math.abs(scroller.scrollTop - before);
    }
  }
  return {
    hasList: !!list,
    messages,
    fingerprint,
    loading: spinnerVisible,
    atOlderEdge,
    reversed: false,
    olderSign: -1,
    moved: Math.round(moved),
    scrollHeight: scroller ? Math.round(scroller.scrollHeight) : 0,
    scrollTop: scroller ? Math.round(scroller.scrollTop) : 0,
    clientHeight: scroller ? Math.round(scroller.clientHeight) : 0
  };
}
"""

CHAT_URLS = ("https://x.com/i/chat", "https://x.com/messages")


def open_x_context(browser, auth_token: str):
    context = browser.new_context(
        viewport={"width": 1280, "height": 1600},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    if auth_token:
        context.add_cookies([{
            "name": "auth_token",
            "value": auth_token,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }])
    page = context.new_page()
    page.set_default_timeout(20_000)
    page.set_default_navigation_timeout(60_000)
    return context, page


class BrowserHub:
    """One Chromium process. The account changes only when the auth token changes."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._token = ""

    def _loop(self) -> None:
        while True:
            fn, box = self._queue.get()
            try:
                box["value"] = fn()
            except Exception as exc:
                box["error"] = exc
                text = str(exc).lower()
                if any(word in text for word in ("closed", "disconnected", "driver", "browser", "target", "connection")):
                    self._reset_driver()
                else:
                    self._drop_page()
            finally:
                box["event"].set()

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="x-browser", daemon=True)
            self._thread.start()

    def call(self, fn):
        self._ensure_thread()
        box = {"event": threading.Event(), "value": None, "error": None}
        self._queue.put((fn, box))
        box["event"].wait()
        if box["error"] is not None:
            raise box["error"]
        return box["value"]

    def _drop_page(self) -> None:
        self._page = None
        browser = self._browser
        if browser is None:
            return
        try:
            connected = browser.is_connected()
        except Exception:
            connected = False
        if not connected:
            self._browser = None
            self._context = None
            self._token = ""

    def _browser_alive(self) -> bool:
        if self._browser is None:
            return False
        try:
            return bool(self._browser.is_connected())
        except Exception:
            return False

    def _reset_driver(self) -> None:
        context, browser, playwright = self._context, self._browser, self._playwright
        self._browser = None
        self._context = None
        self._page = None
        self._token = ""
        self._playwright = None
        for obj in (context, browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass

    def _launch_browser(self) -> None:
        error: Exception | None = None
        for _ in range(2):
            try:
                if self._playwright is None:
                    self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-gpu",
                    ],
                )
                self._context = None
                self._page = None
                self._token = ""
                return
            except Exception as exc:
                error = exc
                self._reset_driver()
        if error is not None:
            raise error

    def page_for(self, auth_token: str):
        if not self._browser_alive():
            self._launch_browser()
        if self._context is None or self._token != auth_token:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
            self._context, self._page = open_x_context(self._browser, auth_token)
            self._token = auth_token
            return self._page
        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
            self._page.set_default_timeout(20_000)
            self._page.set_default_navigation_timeout(60_000)
        return self._page

    def reload_open_page(self) -> str:
        def work():
            page = self._page
            if not self._browser_alive() or page is None:
                return ""
            try:
                if page.is_closed():
                    return ""
            except Exception:
                return ""
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            return page.url or "ok"
        return self.call(work)


browser_hub = BrowserHub()



def run_on_browser(auth_token: str, work):
    def wrapped():
        page = browser_hub.page_for(auth_token)
        return work(page)
    return browser_hub.call(wrapped)


def ensure_logged_in(page) -> bool:
    probe = page.locator(
        'button[aria-label="Account menu"], '
        '[data-testid="SideNav_AccountSwitcher_Button"], '
        'a[data-testid="AppTabBar_Profile_Link"]'
    )
    try:
        if "x.com" in (page.url or "") and probe.first.is_visible(timeout=1500):
            return True
    except Exception:
        pass
    page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
    return session_is_logged_in(page)


def session_is_logged_in(page) -> bool:
    try:
        page.locator(
            'button[aria-label="Account menu"], '
            '[data-testid="SideNav_AccountSwitcher_Button"], '
            'a[data-testid="AppTabBar_Profile_Link"]'
        ).first.wait_for(state="visible", timeout=20_000)
        return True
    except PlaywrightTimeoutError:
        return False



def valid_chat_pin(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4,8}", (value or "").strip()))


def chat_body_text(page) -> str:
    try:
        return page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)") or ""
    except Exception:
        return ""


def pin_error_message(page) -> str | None:
    text = chat_body_text(page)
    if re.search(r"too many attempts|attempt limit|permanently", text, re.I):
        return "توقفت المحاولات عند X. لا تُعد إدخال الرمز."
    if re.search(r"incorrect pin|wrong pin|invalid pin|رمز.{0,16}غير صحيح", text, re.I):
        return "رمز الدردشة غير صحيح."
    return None


def inbox_ready(page) -> bool:
    try:
        if page.locator('[data-testid^="dm-conversation-item-"], [data-testid="dm-inbox-panel"]').count():
            return True
    except Exception:
        pass
    return "/i/chat" in (page.url or "") and "pin" not in (page.url or "")


def pin_fields(page):
    locator = page.locator("input[type='password']:visible, input[type='tel']:visible, input[inputmode='numeric']:visible")
    boxes = []
    for index in range(min(locator.count(), 8)):
        box = locator.nth(index)
        try:
            if box.is_visible():
                boxes.append(box)
        except Exception:
            continue
    return boxes


def pin_screen(page) -> bool:
    if pin_fields(page):
        return True
    return bool(re.search(r"enter your (chat )?pin|chat pin|passcode|أدخل.{0,24}رمز", chat_body_text(page), re.I))


def type_pin_once(page, pin: str) -> None:
    boxes = pin_fields(page)
    if not boxes:
        raise RuntimeError("ظهرت شاشة الرمز دون حقل يمكن الكتابة فيه.")
    if len(boxes) >= len(pin):
        for box, digit in zip(boxes, pin):
            box.click(timeout=4000)
            page.keyboard.type(digit, delay=70)
    else:
        boxes[0].click(timeout=4000)
        page.keyboard.type(pin, delay=70)
    page.wait_for_timeout(1200)


def chat_inbox_open(page) -> bool:
    if "pin" in (page.url or ""):
        return False
    try:
        return page.locator('[data-testid^="dm-conversation-item-"]').count() > 0
    except Exception:
        return False


def open_chat(page, pin: str) -> str | None:
    if chat_inbox_open(page):
        return None
    page.goto(CHAT_URLS[0], wait_until="domcontentloaded", timeout=60_000)
    typed = False
    deadline = time.monotonic() + 28
    while time.monotonic() < deadline:
        if page.locator('[data-testid^="dm-conversation-item-"]').count():
            return None
        if not typed and "pin" in (page.url or ""):
            if pin_fields(page):
                type_pin_once(page, pin)
                typed = True
                error = pin_error_message(page)
                if error:
                    return error
        page.wait_for_timeout(400)
    if page.locator('[data-testid="dm-inbox-panel"]').count():
        return None
    return pin_error_message(page) or "تعذّر فتح الدردشة بعد إدخال الرمز."


def choose_inbox_filter(page, testid: str, label: str) -> None:
    trigger = page.locator('[data-testid="dm-inbox-dropdown-trigger"]')
    try:
        trigger.first.wait_for(state="visible", timeout=8_000)
        trigger.first.click(timeout=5000)
        page.wait_for_timeout(400)
    except Exception:
        return
    item = page.locator(f'[data-testid="{testid}"]')
    if item.count() == 0:
        item = page.get_by_role("menuitem", name=re.compile(label, re.I))
    if item.count() == 0:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return
    try:
        item.first.click(timeout=5000)
    except Exception:
        return
    page.wait_for_timeout(500)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def show_groups_only(page) -> None:
    choose_inbox_filter(page, "dm-inbox-dropdown-groups", r"groups|قروب|مجموع")


def show_all_inbox(page) -> None:
    choose_inbox_filter(page, "dm-inbox-dropdown-all", r"^all$|الكل|all conversations|كل الرسائل")


def collect_groups(page) -> list[dict]:
    """Every group in the inbox as {"id": ..., "name": ...}. The id does not change when a group is renamed."""
    found: list[dict] = []
    seen: set[str] = set()
    idle = 0
    for _ in range(18):
        try:
            payload = page.evaluate(READ_GROUPS_JS, "") or {}
        except Exception:
            payload = {}
        added = 0
        for row in payload.get("groups") or []:
            if not isinstance(row, dict):
                continue
            group_id = str(row.get("id") or "").strip()
            name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
            if not valid_group_id(group_id) or not name or group_id in seen:
                continue
            seen.add(group_id)
            found.append({"id": group_id, "name": name})
            added += 1
        try:
            page.evaluate(SCROLL_INBOX_JS)
        except Exception:
            pass
        try:
            panel = page.locator('[data-testid="dm-inbox-panel"]').first
            box = panel.bounding_box()
            if box:
                page.mouse.move(box["x"] + min(box["width"] / 2, 280), box["y"] + min(box["height"] * 0.55, 420))
                page.mouse.wheel(0, 700)
        except Exception:
            pass
        page.wait_for_timeout(600)
        if added:
            idle = 0
            continue
        idle += 1
        if found and idle >= 3:
            break
        if idle >= 8:
            break
    return found


def valid_group_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9:_\-]{1,160}", value or ""))


def open_group(page, group_id: str, name: str) -> str:
    """Open the group by its saved id, so it still opens after a rename.
    Returns the name the group has now."""
    wanted = {"id": group_id or "", "name": re.sub(r"\s+", " ", name or "").strip()}
    for _ in range(30):
        try:
            payload = page.evaluate(READ_GROUPS_JS, wanted) or {}
        except Exception:
            payload = {}
        if payload.get("clicked"):
            current = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip() or wanted["name"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    state = page.evaluate(GROUP_CHAT_SCROLL_JS, "state") or {}
                except Exception:
                    state = {}
                if state.get("hasList") and state.get("messages"):
                    page.wait_for_timeout(800)
                    return current
                page.wait_for_timeout(300)
        try:
            page.evaluate(SCROLL_INBOX_JS)
        except Exception:
            pass
        page.wait_for_timeout(600)
    raise RuntimeError("تم البحث عن القروب لكن قائمة الرسائل لم تُفتح.")



def valid_auth_token(value: str) -> bool:
    return 20 <= len(value) <= 512


# Both waits are typed in seconds: before reading the groups list, and after entering
# the group before the start mark is taken.
MAX_GROUPS_WAIT_SECONDS = 300
MAX_WATCH_WAIT_SECONDS = 24 * 60 * 60
WAIT_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
STOPPED_DURING_WAIT = "توقفت المراقبة أثناء الانتظار قبل حفظ البداية. لم يُلتقط شيء."


def parse_seconds(value, limit: int) -> int | None:
    """Seconds typed in a wait box (Arabic digits allowed); empty means 0, None means invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value or "").strip().translate(WAIT_DIGITS).replace(",", ".")
        if not text:
            return 0
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            return None
        seconds = float(text)
    if not 0 <= seconds <= limit:
        return None
    return int(round(seconds))


DATA_DIR = Path(__file__).resolve().with_name("data")
WATCH_PATH = DATA_DIR / "watch.json"
MAX_ITEMS = 1000
TEXT_WATCH_SECONDS = 30 * 60
_ACK_MARKS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u200B-\u200F\u202A-\u202E\u2060\u2066-\u2069\uFEFF]")
_ACK_EDGE = " \t\r\n.。!！?؟،,…~～:：;؛\"'«»“”"
# The send time and date X shows inside a message bubble, e.g. "10:21 PM", "١٠:٢١ م",
# "Oct 20", "٢٠ أكتوبر ٢٠٢٦", "Yesterday". They are removed before checking for تم / f / F.
_ACK_DIGIT = "[0-9\u0660-\u0669\u06F0-\u06F9]"
_ACK_TIME = re.compile(
    _ACK_DIGIT + r"{1,2}\s*[:：]\s*" + _ACK_DIGIT + r"{2}(?:\s*[:：]\s*" + _ACK_DIGIT + r"{2})?"
    r"(?:\s*(?:[aApP]\.?\s?[mM]\.?|مساءً|مساء|صباحًا|صباحا|ص|م)(?![\u0621-\u064A]))?"
)
_ACK_DATE = re.compile(_ACK_DIGIT + r"{1,4}\s*[/\-.]\s*" + _ACK_DIGIT + r"{1,2}(?:\s*[/\-.]\s*" + _ACK_DIGIT + r"{2,4})?")
_ACK_WORDS = re.compile(
    r"(?<![A-Za-z])(?:today|yesterday|just now|now|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|mon(?:day)?|tue(?:s(?:day)?)?"
    r"|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)(?![A-Za-z])"
    r"|اليوم|أمس|امس|الآن|الان|يناير|فبراير|مارس|أبريل|إبريل|ابريل|مايو|يونيو|يوليو|أغسطس|اغسطس|سبتمبر|أكتوبر|اكتوبر"
    r"|نوفمبر|ديسمبر|الأحد|الاحد|الاثنين|الإثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس|الجمعة|السبت",
    re.I,
)
_ACK_SEPARATORS = re.compile(r"[·•|،,✓✔\-–—]+")
_ACK_NUMBER = re.compile(r"(?<!\S)" + _ACK_DIGIT + r"{1,4}(?!\S)")
UNMATCHED_LOG = DATA_DIR / "replies_unmatched.log"

state_lock = threading.Lock()
watch_thread: threading.Thread | None = None
stop_flag = threading.Event()
abort_flag = threading.Event()
state: dict = {
    "auth_token": "",
    "chat_pin": "",
    "groups": [],
    "group": "",
    "group_id": "",
    "groups_wait": 0,
    "watching": False,
    "message": "اختر قروبًا لتبدأ المراقبة.",
    "ok": True,
    "checking": False,
    "waiting": False,
    "wait_seconds": 0,
    "items": [],
    "replies": [],
    "text_watch": False,
    "text_left": 0,
    "boundary_status": "",
    "likes_account_id": "",
    "likes_accounts": [],
    "likes_token": "",
    "likes_tweet": "",
    "likes_status": "",
    "likes_checking": False,
    "likes_message": "",
    "likes_ok": True,
    "likes_results": [],
    "rev": 0,
}


WATCH_JS = r"""
() => {
  if (window.__watch && window.__watch.installed) return true;
  const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","intent","share","web"]);
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const rootsOf = (start) => {
    const roots = [];
    const walk = (root) => {
      if (!root || roots.includes(root)) return;
      roots.push(root);
      const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
    };
    walk(start || document);
    return roots;
  };
  const collectDeep = (root, selector, out) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(selector).forEach((node) => out.push(node));
    root.querySelectorAll("*").forEach((node) => {
      if (node.shadowRoot) collectDeep(node.shadowRoot, selector, out);
    });
  };
  const query = (selector) => {
    for (const root of rootsOf(document)) {
      const found = root.querySelector && root.querySelector(selector);
      if (found) return found;
    }
    return null;
  };
  const climb = (node) => {
    const chain = [];
    let el = node;
    while (el && chain.length < 40) {
      chain.push(el);
      if (el.parentElement) el = el.parentElement;
      else if (el.parentNode && el.parentNode.nodeType === 11) el = el.parentNode.host;
      else break;
    }
    return chain;
  };
  const statusOf = (href) => {
    const match = (href || "").match(/status\/(\d{6,25})/);
    return match ? match[1] : "";
  };
  const insideQuote = (node) => climb(node).some((el) => /quote/i.test((el.getAttribute && el.getAttribute("data-testid")) || ""));
  const insideCard = (node) => climb(node).some((el) => {
    const test = (el.getAttribute && el.getAttribute("data-testid")) || "";
    return test === "tweet" || test === "tweetText" || /card\.wrapper|tweetPhoto|quote/i.test(test);
  });
  const isBubble = (el) => {
    const id = (el && el.getAttribute && el.getAttribute("data-testid")) || "";
    return id.startsWith("message-") && !id.startsWith("message-text-");
  };
  const containsOtherBubble = (el, self) => {
    if (!el || el === self) return false;
    if (isBubble(el)) return true;
    const nested = [];
    collectDeep(el, '[data-testid^="message-"]', nested);
    return nested.some((node) => node !== self && isBubble(node));
  };
  const handleFrom = (node) => {
    if (!node || !node.querySelectorAll) return "";
    const links = [];
    collectDeep(node, "a[href]", links);
    for (const link of links) {
      if (insideCard(link)) continue;
      const href = link.getAttribute("href") || "";
      const match = href.match(/(?:^|https?:\/\/(?:www\.)?(?:x|twitter)\.com)\/([A-Za-z0-9_]{1,15})\/?(?:\?.*)?$/i);
      if (match && !reserved.has(match[1].toLowerCase())) return match[1];
    }
    const at = clean(node.innerText || "").match(/@([A-Za-z0-9_]{1,15})/);
    return at && !insideCard(node) ? at[1] : "";
  };
  const nameFrom = (node) => {
    if (!node || insideCard(node)) return "";
    const labelled = clean((node.getAttribute && (node.getAttribute("aria-label") || node.getAttribute("alt"))) || "");
    const text = labelled || clean(node.innerText || "");
    if (!text || text.length > 40 || /status\/\d|https?:/i.test(text)) return "";
    if (/^(now|الآن|\d{1,2}:\d{2}|\d+\s*(m|h|d|s|د|س|ث))$/i.test(text)) return "";
    const line = text.split(" ").slice(0, 4).join(" ");
    if (line.length < 2 || line.length > 40) return "";
    return line.replace(/^@/, "");
  };
  const senderFor = (bubble) => {
    const ownHandle = handleFrom(bubble);
    if (ownHandle) return {handle: ownHandle, name: ""};
    let node = bubble;
    for (let depth = 0; depth < 6 && node; depth += 1) {
      let prev = node.previousElementSibling;
      while (prev) {
        if (containsOtherBubble(prev, bubble)) return {handle: "", name: ""};
        const handle = handleFrom(prev);
        const name = nameFrom(prev);
        if (handle || name) return {handle, name};
        prev = prev.previousElementSibling;
      }
      const parent = node.parentElement || (node.parentNode && node.parentNode.host);
      if (!parent || parent === node) break;
      node = parent;
    }
    return {handle: "", name: ""};
  };
  const listRoot = () => query('[data-testid="dm-message-list"]')
    || query('[data-testid="dm-conversation-content"]')
    || query('[data-testid="dm-message-scroller"]');
  const bubbles = () => {
    const list = listRoot();
    if (!list) return [];
    const found = [];
    collectDeep(list, '[data-testid^="message-"]', found);
    return found.filter((el) => {
      const id = el.getAttribute("data-testid") || "";
      if (!id.startsWith("message-") || id.startsWith("message-text-")) return false;
      const nested = [];
      collectDeep(el, '[data-testid^="message-"]', nested);
      return !nested.some((node) => node !== el && (node.getAttribute("data-testid") || "").startsWith("message-") && !(node.getAttribute("data-testid") || "").startsWith("message-text-"));
    });
  };
  const readRow = (bubble) => {
    const links = [];
    collectDeep(bubble, '[href*="status/"]', links);
    const candidates = [];
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      const statusId = statusOf(href);
      if (!statusId || insideQuote(link)) continue;
      const nested = climb(link).some((el) => {
        if (el === link || !el.getAttribute) return false;
        const parentId = statusOf(el.getAttribute("href") || "");
        return parentId && parentId !== statusId;
      });
      if (nested) continue;
      candidates.push({statusId, href, depth: climb(link).length});
    }
    if (!candidates.length) return null;
    candidates.sort((a, b) => a.depth - b.depth);
    const outer = candidates[0];
    const texts = [];
    collectDeep(bubble, '[data-testid="tweetText"]', texts);
    const own = texts.find((node) => !insideQuote(node));
    const near = senderFor(bubble);
    const messageId = bubble.getAttribute("data-testid") || "";
    return {
      key: messageId + ":" + outer.statusId,
      messageId,
      statusId: outer.statusId,
      url: outer.href && outer.href.startsWith("http") ? outer.href : ("https://x.com" + (outer.href || ("/i/web/status/" + outer.statusId))),
      text: clean((own && own.innerText) || "").slice(0, 240),
      handle: near.handle,
      name: near.name,
      top: Math.round(bubble.getBoundingClientRect().top || 0),
      parentKey: bubble.parentElement ? (bubble.parentElement.getAttribute("data-testid") || "") : ""
    };
  };
  const absorb = (logIt) => {
    const list = listRoot();
    if (!list) return false;
    window.__watch.missAt = window.__watch.missAt || Object.create(null);
    window.__watch.nameless = window.__watch.nameless || Object.create(null);
    const pairs = bubbles().map((bubble) => ({bubble, row: readRow(bubble)})).filter((pair) => pair.row);
    pairs.sort((a, b) => a.row.top - b.row.top);
    let lastParent = null;
    let lastSender = {handle: "", name: ""};
    for (const pair of pairs) {
      const row = pair.row;
      if (row.handle || row.name) {
        lastSender = {handle: row.handle, name: row.name};
        lastParent = pair.bubble.parentElement;
      } else if (pair.bubble.parentElement && pair.bubble.parentElement === lastParent) {
        row.handle = lastSender.handle;
        row.name = lastSender.name;
      } else {
        lastParent = pair.bubble.parentElement;
        lastSender = {handle: "", name: ""};
      }
      if ((row.messageId && window.__watch.oldIds[row.messageId]) || window.__watch.oldStatus[row.statusId]) {
        window.__watch.seen[row.key] = 1;
        continue;
      }
      if (!row.handle && !row.name) {
        if (!window.__watch.missAt[row.key]) window.__watch.missAt[row.key] = Date.now();
        if (Date.now() - window.__watch.missAt[row.key] < 2500) continue;
      }
      if (window.__watch.seen[row.key]) {
        if ((row.handle || row.name) && window.__watch.nameless[row.key]) {
          delete window.__watch.nameless[row.key];
          if (logIt) window.__watch.log.push(row);
        }
        continue;
      }
      window.__watch.seen[row.key] = 1;
      if (!row.handle && !row.name) window.__watch.nameless[row.key] = 1;
      if (logIt) window.__watch.log.push(row);
    }
    return true;
  };
  window.__watch = {
    seen: Object.create(null), log: [], miss: Object.create(null), missAt: Object.create(null), nameless: Object.create(null),
    oldIds: Object.create(null), oldStatus: Object.create(null), armed: false, installed: true
  };
  const stampOld = () => {
    bubbles().forEach((bubble) => {
      const id = bubble.getAttribute("data-testid") || "";
      if (id) window.__watch.oldIds[id] = 1;
    });
    bubbles().map(readRow).filter(Boolean).forEach((row) => {
      if (row.messageId) window.__watch.oldIds[row.messageId] = 1;
      if (row.statusId) window.__watch.oldStatus[row.statusId] = 1;
      window.__watch.seen[row.key] = 1;
    });
  };
  window.__watchReset = () => {
    window.__watch.seen = Object.create(null);
    window.__watch.log = [];
    window.__watch.miss = Object.create(null);
    window.__watch.missAt = Object.create(null);
    window.__watch.nameless = Object.create(null);
    window.__watch.oldIds = Object.create(null);
    window.__watch.oldStatus = Object.create(null);
    window.__watch.armed = false;
  };
  window.__watchBaseline = () => {
    window.__watch.armed = false;
    window.__watch.log = [];
    stampOld();
    if (!window.__watchTimer) {
      window.__watchTimer = setInterval(() => {
        try {
          if (window.__watch.armed) absorb(true);
          else stampOld();
        } catch (error) {}
      }, 40);
    }
    return true;
  };
  window.__watchArm = () => {
    stampOld();
    window.__watch.log = [];
    window.__watch.armed = true;
    return true;
  };
  window.__watchDrain = () => {
    const alive = absorb(true);
    const rows = window.__watch.log.splice(0);
    return {alive, rows};
  };
  window.__watchAnchor = () => bubbles().map((el) => el.getAttribute("data-testid") || "").filter(Boolean);
  window.__watchExport = () => ({
    oldIds: Object.keys((window.__watch && window.__watch.oldIds) || {}),
    oldStatus: Object.keys((window.__watch && window.__watch.oldStatus) || {})
  });
  window.__watchResume = (saved) => {
    const watch = window.__watch;
    if (!watch) return false;
    (saved.oldIds || []).forEach((id) => { if (id) watch.oldIds[id] = 1; });
    (saved.oldStatus || []).forEach((id) => { if (id) watch.oldStatus[id] = 1; });
    watch.armed = true;
    watch.log = [];
    if (!window.__watchTimer) {
      window.__watchTimer = setInterval(() => {
        try { if (window.__watch && window.__watch.armed) absorb(true); } catch (error) {}
      }, 40);
    }
    return true;
  };
  window.__watchVisible = () => {
    const nodes = bubbles();
    const rows = nodes.map(readRow).filter(Boolean);
    return {
      messages: nodes.map((el) => el.getAttribute("data-testid") || "").filter(Boolean),
      ids: rows.map((row) => row.statusId)
    };
  };
  return true;
}
"""


TEXT_SCAN_JS = r"""
() => {
  const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","intent","share","web"]);
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const rootsOf = (start) => {
    const roots = [];
    const walk = (root) => {
      if (!root || roots.includes(root)) return;
      roots.push(root);
      const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
    };
    walk(start || document);
    return roots;
  };
  const collectDeep = (root, selector, out) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(selector).forEach((node) => out.push(node));
    root.querySelectorAll("*").forEach((node) => {
      if (node.shadowRoot) collectDeep(node.shadowRoot, selector, out);
    });
  };
  const query = (selector) => {
    for (const root of rootsOf(document)) {
      const found = root.querySelector && root.querySelector(selector);
      if (found) return found;
    }
    return null;
  };
  const climb = (node) => {
    const chain = [];
    let el = node;
    while (el && chain.length < 40) {
      chain.push(el);
      if (el.parentElement) el = el.parentElement;
      else if (el.parentNode && el.parentNode.nodeType === 11) el = el.parentNode.host;
      else break;
    }
    return chain;
  };
  const insideCard = (node) => climb(node).some((el) => {
    const test = (el.getAttribute && el.getAttribute("data-testid")) || "";
    return test === "tweet" || test === "tweetText" || /card\.wrapper|tweetPhoto|quote/i.test(test);
  });
  const isBubble = (el) => {
    const id = (el && el.getAttribute && el.getAttribute("data-testid")) || "";
    return id.startsWith("message-") && !id.startsWith("message-text-");
  };
  const containsOtherBubble = (el, self) => {
    if (!el || el === self) return false;
    if (isBubble(el)) return true;
    const nested = [];
    collectDeep(el, '[data-testid^="message-"]', nested);
    return nested.some((node) => node !== self && isBubble(node));
  };
  const handleFrom = (node) => {
    if (!node || !node.querySelectorAll) return "";
    const links = [];
    collectDeep(node, "a[href]", links);
    for (const link of links) {
      if (insideCard(link)) continue;
      const href = link.getAttribute("href") || "";
      const match = href.match(/(?:^|https?:\/\/(?:www\.)?(?:x|twitter)\.com)\/([A-Za-z0-9_]{1,15})\/?(?:\?.*)?$/i);
      if (match && !reserved.has(match[1].toLowerCase())) return match[1];
    }
    const at = clean(node.innerText || "").match(/@([A-Za-z0-9_]{1,15})/);
    return at && !insideCard(node) ? at[1] : "";
  };
  const nameFrom = (node) => {
    if (!node || insideCard(node)) return "";
    const labelled = clean((node.getAttribute && (node.getAttribute("aria-label") || node.getAttribute("alt"))) || "");
    const text = labelled || clean(node.innerText || "");
    if (!text || text.length > 40 || /status\/\d|https?:/i.test(text)) return "";
    if (/^(now|الآن|\d{1,2}:\d{2}|\d+\s*(m|h|d|s|د|س|ث))$/i.test(text)) return "";
    const line = text.split(" ").slice(0, 4).join(" ");
    if (line.length < 2 || line.length > 40) return "";
    return line.replace(/^@/, "");
  };
  const senderFor = (bubble) => {
    const ownHandle = handleFrom(bubble);
    if (ownHandle) return {handle: ownHandle, name: ""};
    let node = bubble;
    for (let depth = 0; depth < 6 && node; depth += 1) {
      let prev = node.previousElementSibling;
      while (prev) {
        if (containsOtherBubble(prev, bubble)) return {handle: "", name: ""};
        const handle = handleFrom(prev);
        const name = nameFrom(prev);
        if (handle || name) return {handle, name};
        prev = prev.previousElementSibling;
      }
      const parent = node.parentElement || (node.parentNode && node.parentNode.host);
      if (!parent || parent === node) break;
      node = parent;
    }
    return {handle: "", name: ""};
  };
  const listRoot = () => query('[data-testid="dm-message-list"]')
    || query('[data-testid="dm-conversation-content"]')
    || query('[data-testid="dm-message-scroller"]');
  const bubbles = () => {
    const list = listRoot();
    if (!list) return [];
    const found = [];
    collectDeep(list, '[data-testid^="message-"]', found);
    return found.filter((el) => {
      const id = el.getAttribute("data-testid") || "";
      if (!id.startsWith("message-") || id.startsWith("message-text-")) return false;
      const nested = [];
      collectDeep(el, '[data-testid^="message-"]', nested);
      return !nested.some((node) => node !== el && (node.getAttribute("data-testid") || "").startsWith("message-") && !(node.getAttribute("data-testid") || "").startsWith("message-text-"));
    });
  };
  const timeLike = /^(now|الآن|\d{1,2}:\d{2}(?:\s*[ap]m)?|\d+\s*[smhdw]|\d+\s*(?:د|دقائق|دقيقة|س|ساعة|ساعات|ي|يوم|أيام|ث|أسبوع|أسابيع))\.?$/i;
  // تم / f / F, also with repeated letters (تتتممم, ff) and the short forms ت and تك.
  const ackLine = (value) => /^(ت+م*|ت+ك+|f+|F+)[.。!！?؟]*$/.test(clean(value).replace(/[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF\u0640]/g, ""));
  // The send time and date shown in the bubble ("10:21 PM", "١٠:٢١ م", "Oct 20", "أمس"), removed from a line.
  const D = String.raw`[0-9\u0660-\u0669\u06F0-\u06F9]`;
  const stamps = [
    new RegExp(D + String.raw`{1,2}\s*[:：]\s*` + D + String.raw`{2}(?:\s*[:：]\s*` + D + String.raw`{2})?(?:\s*(?:[aApP]\.?\s?[mM]\.?|مساءً|مساء|صباحًا|صباحا|ص|م)(?![\u0621-\u064A]))?`, "g"),
    new RegExp(D + String.raw`{1,4}\s*[/\-.]\s*` + D + String.raw`{1,2}(?:\s*[/\-.]\s*` + D + String.raw`{2,4})?`, "g"),
    /(?<![A-Za-z])(?:today|yesterday|just now|now|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)(?![A-Za-z])/gi,
    /اليوم|أمس|امس|الآن|الان|يناير|فبراير|مارس|أبريل|إبريل|ابريل|مايو|يونيو|يوليو|أغسطس|اغسطس|سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر|الأحد|الاحد|الاثنين|الإثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس|الجمعة|السبت/g,
    /[·•|،,✓✔\-–—]+/g,
    new RegExp(String.raw`(^|\s)` + D + String.raw`{1,4}(?=\s|$)`, "g")
  ];
  const stripStamp = (value) => stamps.reduce((text, re) => text.replace(re, " "), String(value || ""));
  const textOf = (bubble) => {
    const nodes = [];
    collectDeep(bubble, '[data-testid^="message-text-"]', nodes);
    const fromNodes = clean(nodes.map((node) => node.innerText || "").join(" "));
    if (fromNodes) return fromNodes.slice(0, 240);
    const lines = (bubble.innerText || "").split(/\n+/).map(clean).filter(Boolean)
      .filter((line) => !timeLike.test(line) && clean(stripStamp(line)));
    const hit = lines.find((line) => ackLine(stripStamp(line)));
    if (hit && lines.length <= 4) return clean(stripStamp(hit)).slice(0, 240);
    return clean(lines.join(" ")).slice(0, 240);
  };
  const attachment = (bubble) => {
    const nodes = [];
    collectDeep(bubble, "a[href], [data-testid], video, audio, img", nodes);
    for (const node of nodes) {
      const href = (node.getAttribute && node.getAttribute("href")) || "";
      if (/status\/\d{6,25}/.test(href)) return true;
      const test = (node.getAttribute && node.getAttribute("data-testid")) || "";
      if (/tweetPhoto|tweetText|^tweet$|video|media|card\.wrapper|image|audio|gif/i.test(test)) return true;
      const tag = (node.tagName || "").toLowerCase();
      if (tag === "video" || tag === "audio") return true;
      if (tag === "img") {
        const rect = node.getBoundingClientRect();
        if (rect.width > 72 && rect.height > 72) return true;
      }
    }
    return false;
  };
  const pairs = bubbles().map((bubble) => {
    const near = senderFor(bubble);
    return {
      bubble,
      row: {
        id: bubble.getAttribute("data-testid") || "",
        text: textOf(bubble),
        handle: near.handle,
        name: near.name,
        top: Math.round(bubble.getBoundingClientRect().top || 0),
        textOnly: !attachment(bubble)
      }
    };
  }).filter((pair) => pair.row.id);
  pairs.sort((a, b) => a.row.top - b.row.top);
  let lastParent = null;
  let lastSender = {handle: "", name: ""};
  const rows = [];
  for (const pair of pairs) {
    const row = pair.row;
    if (row.handle || row.name) {
      lastSender = {handle: row.handle, name: row.name};
      lastParent = pair.bubble.parentElement;
    } else if (pair.bubble.parentElement && pair.bubble.parentElement === lastParent) {
      row.handle = lastSender.handle;
      row.name = lastSender.name;
    } else {
      lastParent = pair.bubble.parentElement;
      lastSender = {handle: "", name: ""};
    }
    rows.push(row);
  }
  return rows;
}
"""


FOCUS_MESSAGE_JS = r"""
(messageId) => {
  if (!messageId || !window.CSS || !CSS.escape) return false;
  const selector = '[data-testid="' + CSS.escape(messageId) + '"]';
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  for (const root of roots) {
    const found = root.querySelector && root.querySelector(selector);
    if (found && found.scrollIntoView) {
      found.scrollIntoView({block: "center", inline: "nearest"});
      return true;
    }
  }
  return false;
}
"""


LIKES_READ_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","intent","share","status","likes"]);
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const cells = [];
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll('[data-testid="UserCell"]').forEach((node) => cells.push(node));
  }
  const people = [];
  const seen = new Set();
  for (const cell of cells) {
    let handle = "";
    const links = cell.querySelectorAll("a[href]");
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      const match = href.match(/^\/([A-Za-z0-9_]{1,15})\/?$/);
      if (match && !reserved.has(match[1].toLowerCase())) {
        handle = match[1];
        break;
      }
    }
    if (!handle || seen.has(handle.toLowerCase())) continue;
    seen.add(handle.toLowerCase());
    let name = "";
    const block = cell.querySelector('[data-testid="User-Name"]');
    if (block) {
      const parts = [...block.querySelectorAll("span")].map((node) => clean(node.textContent || "")).filter(Boolean);
      name = parts.find((part) => part.replace(/^@/, "").toLowerCase() !== handle.toLowerCase() && !part.startsWith("@")) || "";
    }
    people.push({handle, name});
  }
  return people;
}
"""


LIKES_SCROLL_JS = r"""
() => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let cell = null;
  for (const root of roots) {
    cell = root.querySelector && root.querySelector('[data-testid="UserCell"]');
    if (cell) break;
  }
  let node = cell ? cell.parentElement : null;
  while (node && node !== document.body) {
    const style = getComputedStyle(node);
    if (/(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 40) {
      const before = node.scrollTop;
      node.scrollTop = before + Math.max(480, Math.round(node.clientHeight * 0.8));
      const atEnd = node.scrollTop + node.clientHeight >= node.scrollHeight - 80;
      return {moved: node.scrollTop > before, atEnd};
    }
    node = node.parentElement;
  }
  const scroller = document.scrollingElement || document.documentElement;
  const before = scroller.scrollTop;
  scroller.scrollTop = before + Math.max(520, Math.round(window.innerHeight * 0.8));
  const atEnd = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 80;
  return {moved: scroller.scrollTop > before, atEnd};
}
"""


def account_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def remember_likes_account(token: str, handle: str = "") -> str:
    key = account_key(token)
    handle = str(handle or "").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        handle = ""
    with state_lock:
        accounts = state.setdefault("likes_accounts", [])
        found = next((item for item in accounts if item.get("id") == key), None)
        if found is None:
            accounts.append({
                "id": key,
                "token": token,
                "handle": handle,
                "label": f"@{handle}" if handle else f"حساب {len(accounts) + 1}",
            })
            if len(accounts) > 30:
                del accounts[:-30]
        elif handle:
            found["handle"] = handle
            found["label"] = f"@{handle}"
        state["likes_account_id"] = key
    return key


def token_for_account(account_id: str) -> str:
    with state_lock:
        for item in state.get("likes_accounts") or []:
            if item.get("id") == account_id:
                return str(item.get("token") or "")
    return ""


def load_state() -> None:
    try:
        loaded = json.loads(WATCH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(loaded, dict):
        return
    with state_lock:
        state["auth_token"] = str(loaded.get("auth_token") or "")
        state["chat_pin"] = str(loaded.get("chat_pin") or "")
        state["group"] = str(loaded.get("group") or "")
        group_id = str(loaded.get("group_id") or "")
        state["group_id"] = group_id if valid_group_id(group_id) else ""
        state["groups_wait"] = parse_seconds(loaded.get("groups_wait"), MAX_GROUPS_WAIT_SECONDS) or 0
        state["groups"] = []
        if "wait_seconds" in loaded:
            state["wait_seconds"] = parse_seconds(loaded.get("wait_seconds"), MAX_WATCH_WAIT_SECONDS) or 0
        else:
            # Saved by the older version, which kept this wait in minutes.
            try:
                minutes = float(loaded.get("wait_minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0.0
            state["wait_seconds"] = int(min(MAX_WATCH_WAIT_SECONDS, max(0.0, minutes * 60)))
        items = loaded.get("items")
        if isinstance(items, list):
            state["items"] = [item for item in items if isinstance(item, dict)][-MAX_ITEMS:]
        replies = loaded.get("replies")
        if isinstance(replies, list):
            state["replies"] = [item for item in replies if isinstance(item, dict)][-MAX_ITEMS:]
        state["likes_token"] = str(loaded.get("likes_token") or "")
        state["likes_tweet"] = str(loaded.get("likes_tweet") or "")
        likes_results = loaded.get("likes_results")
        if isinstance(likes_results, list):
            state["likes_results"] = [item for item in likes_results if isinstance(item, dict)][-MAX_ITEMS:]
        state["likes_message"] = str(loaded.get("likes_message") or "")
        state["likes_ok"] = bool(loaded.get("likes_ok", True))
        state["likes_status"] = str(loaded.get("likes_status") or "")
        accounts = []
        seen_ids = set()
        raw_accounts = loaded.get("likes_accounts")
        if isinstance(raw_accounts, list):
            for item in raw_accounts:
                if not isinstance(item, dict):
                    continue
                token = str(item.get("token") or "").strip()
                if not valid_auth_token(token):
                    continue
                key = account_key(token)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                handle = str(item.get("handle") or "").lstrip("@")
                if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                    handle = ""
                accounts.append({
                    "id": key,
                    "token": token,
                    "handle": handle,
                    "label": f"@{handle}" if handle else str(item.get("label") or f"حساب {len(accounts) + 1}"),
                })
        legacy = state["likes_token"]
        if valid_auth_token(legacy) and account_key(legacy) not in seen_ids:
            accounts.append({
                "id": account_key(legacy),
                "token": legacy,
                "handle": "",
                "label": f"حساب {len(accounts) + 1}",
            })
        state["likes_accounts"] = accounts[-30:]
        saved_id = str(loaded.get("likes_account_id") or "")
        if any(item["id"] == saved_id for item in state["likes_accounts"]):
            state["likes_account_id"] = saved_id
        elif state["likes_accounts"]:
            state["likes_account_id"] = state["likes_accounts"][-1]["id"]


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with state_lock:
        payload = {
            "auth_token": state["auth_token"],
            "chat_pin": state["chat_pin"],
            "group": state["group"],
            "group_id": state["group_id"],
            "groups_wait": state["groups_wait"],
            "groups": list(state["groups"]),
            "wait_seconds": state["wait_seconds"],
            "items": list(state["items"])[-MAX_ITEMS:],
            "replies": list(state["replies"])[-MAX_ITEMS:],
            "likes_token": state.get("likes_token") or "",
            "likes_tweet": state.get("likes_tweet") or "",
            "likes_status": state.get("likes_status") or "",
            "likes_message": state.get("likes_message") or "",
            "likes_ok": bool(state.get("likes_ok", True)),
            "likes_account_id": state.get("likes_account_id") or "",
            "likes_accounts": [
                {"id": item.get("id"), "token": item.get("token"), "handle": item.get("handle") or "", "label": item.get("label") or ""}
                for item in (state.get("likes_accounts") or [])
                if isinstance(item, dict)
            ],
            "likes_results": list(state.get("likes_results") or [])[-MAX_ITEMS:],
        }
        temporary = WATCH_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(WATCH_PATH)


def public_state() -> dict:
    with state_lock:
        return {
            "success": True,
            "auth_token": state["auth_token"],
            "chat_pin": state["chat_pin"],
            "groups": list(state["groups"]),
            "group": state["group"],
            "group_id": state["group_id"],
            "groups_wait": state["groups_wait"],
            "watching": state["watching"],
            "message": state["message"],
            "rev": int(state.get("rev") or 0),
            "ok": state["ok"],
            "checking": state.get("checking") or False,
            "waiting": bool(state.get("waiting")),
            "wait_seconds": state["wait_seconds"],
            "count": len(state["items"]),
            "items": list(reversed(state["items"][-200:])),
            "text_watch": bool(state.get("text_watch")),
            "text_left": int(state.get("text_left") or 0),
            "boundary_status": str(state.get("boundary_status") or ""),
            "replies": list(reversed(state["replies"][-200:])),
            "reply_count": member_total(state["replies"]),
            "likes_tweet": state.get("likes_tweet") or "",
            "likes_status": state.get("likes_status") or "",
            "likes_checking": bool(state.get("likes_checking")),
            "likes_message": state.get("likes_message") or "",
            "likes_ok": bool(state.get("likes_ok", True)),
            "likes_account_id": state.get("likes_account_id") or "",
            "likes_accounts": [
                {
                    "id": item.get("id") or "",
                    "handle": item.get("handle") or "",
                    "label": item.get("label") or "حساب",
                }
                for item in (state.get("likes_accounts") or [])
                if isinstance(item, dict) and item.get("id")
            ],
            "likes_results": list(state.get("likes_results") or []),
        }


def write_message(message: str, ok: bool | None = None) -> None:
    state["message"] = message
    if ok is not None:
        state["ok"] = ok
    state["rev"] = int(state.get("rev") or 0) + 1


def set_status(message: str, ok: bool = True, watching: bool | None = None) -> None:
    with state_lock:
        write_message(message, ok)
        if watching is not None:
            state["watching"] = watching


def add_items(rows: list[dict], run_id: str = "") -> int:
    added = 0
    with state_lock:
        seen = {str(item.get("key") or "") for item in state["items"]}
        for row in rows:
            key = str(row.get("key") or "")
            status_id = str(row.get("statusId") or "")
            if not key or not status_id:
                continue
            handle = str(row.get("handle") or "").lstrip("@")
            if handle and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                handle = ""
            name = str(row.get("name") or "")[:80]
            if key in seen:
                current = next((item for item in state["items"] if item.get("key") == key), None)
                if current and (handle or name) and not (current.get("handle") or current.get("name")):
                    current["handle"] = handle
                    current["name"] = name
                    added += 1
                continue
            seen.add(key)
            url = str(row.get("url") or "")
            if status_id and f"/status/{status_id}" not in url:
                url = f"https://x.com/i/web/status/{status_id}"
            if url.startswith("/"):
                url = "https://x.com" + url
            state["items"].append({
                "key": key,
                "message_id": str(row.get("messageId") or ""),
                "status_id": status_id,
                "url": url,
                "text": str(row.get("text") or "")[:240],
                "handle": handle,
                "name": str(row.get("name") or "")[:80],
                "at": time.time(),
                "run_id": run_id,
                "present": None,
            })
            added += 1
        if len(state["items"]) > MAX_ITEMS:
            del state["items"][:-MAX_ITEMS]
    if added:
        save_state()
    return added


def read_chat_view(page) -> dict:
    try:
        return page.evaluate("() => window.__watchVisible ? window.__watchVisible() : {messages: [], ids: []}") or {}
    except Exception:
        return {}


def nudge_chat(page, older: bool) -> None:
    try:
        if older:
            page.evaluate(GROUP_CHAT_SCROLL_JS, "older")
        else:
            page.evaluate(
                """() => {
                  const roots = [];
                  const walk = (root) => {
                    if (!root || roots.includes(root)) return;
                    roots.push(root);
                    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
                    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
                  };
                  walk(document);
                  for (const root of roots) {
                    const scroller = root.querySelector && root.querySelector('[data-testid="dm-message-scroller"]');
                    if (scroller) { scroller.scrollTop += 520; return; }
                  }
                }"""
            )
    except Exception:
        pass
    try:
        box = page.locator('[data-testid="dm-message-scroller"]').first.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] * 0.45, box["y"] + min(box["height"] * 0.45, 520))
            page.mouse.wheel(0, -680 if older else 680)
    except Exception:
        pass
    page.wait_for_timeout(420)


def verify_captured(page, anchor_ids: set[str], run_id: str, reopen=None, hold: bool = False) -> None:
    with state_lock:
        state["checking"] = True
        targets = [
            str(item.get("status_id") or "")
            for item in state["items"]
            if item.get("run_id") == run_id and item.get("status_id")
        ]
    wanted = set(targets)
    set_status("توقفت المراقبة. جارٍ فحص التغريدات للأعلى…", True, True)
    if not wanted:
        if hold:
            set_status("توقفت المراقبة. لا توجد تغريدات جديدة للتحقق. يبدأ تسجيل الردود لمدة 30 د.", True, True)
        else:
            set_status("توقفت المراقبة. لا توجد تغريدات جديدة للتحقق.", True, False)
        with state_lock:
            state["checking"] = False
        return
    found: set[str] = set()
    older = True
    flipped = False
    last_messages = ""
    stall = 0
    reached = False
    for step in range(100):
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
            with state_lock:
                state["checking"] = False
            return
        if not page_alive(page):
            if reopen is None:
                break
            page = reopen()
            continue
        view = read_chat_view(page)
        messages = [str(item) for item in (view.get("messages") or [])]
        for status_id in view.get("ids") or []:
            if str(status_id) in wanted:
                found.add(str(status_id))
        if anchor_ids and set(messages) & anchor_ids and step > 0:
            reached = True
            break
        fingerprint = "|".join(messages)
        if fingerprint == last_messages:
            stall += 1
            if stall == 3 and not flipped:
                older = not older
                flipped = True
                stall = 0
            elif stall >= 4:
                break
        else:
            stall = 0
        last_messages = fingerprint
        if step % 4 == 0:
            set_status(f"يفحص للأعلى… وُجد {len(found)} من {len(wanted)}.", True, True)
        nudge_chat(page, older)
    missing = wanted - found
    with state_lock:
        for item in state["items"]:
            if item.get("run_id") != run_id:
                continue
            status_id = str(item.get("status_id") or "")
            item["present"] = status_id in found
        state["checking"] = False
    save_state()
    if anchor_ids and not reached:
        for _ in range(12):
            view = read_chat_view(page)
            messages = {str(item) for item in (view.get("messages") or [])}
            if messages & anchor_ids:
                reached = True
                break
            nudge_chat(page, not older)
    if missing:
        text = f"التحقق انتهى. {len(found)} موجودة و{len(missing)} حُذفت من القروب."
    else:
        text = f"التحقق انتهى. كل التغريدات ({len(found)}) ما زالت في القروب."
    if hold:
        text += " يبدأ تسجيل الردود من آخر تغريدة لمدة 30 د."
    elif reached:
        text += " الصفحة عادت لنقطة بداية المراقبة."
    set_status(text, True, True if hold else False)


def csrf_ok(data: dict) -> bool:
    expected = session.get("csrf_token", "")
    given = str(data.get("csrf_token") or "")
    return bool(expected) and hmac.compare_digest(given, expected)


def ensure_csrf() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(24)
        session["csrf_token"] = token
    return token


def settle_existing(page) -> None:
    """Mark tweets already in the group before live capture starts."""
    previous = None
    stable = 0
    started = time.monotonic()
    while time.monotonic() - started < 20:
        if stop_flag.is_set() or abort_flag.is_set():
            return
        try:
            page.evaluate(GROUP_CHAT_SCROLL_JS, "latest")
        except Exception:
            pass
        page.evaluate("() => window.__watchBaseline && window.__watchBaseline()")
        info = page.evaluate(
            """() => ({
              bubbles: (window.__watchAnchor ? window.__watchAnchor() : []).length,
              statuses: Object.keys((window.__watch && window.__watch.oldStatus) || {}).length
            })"""
        ) or {}
        signature = (int(info.get("bubbles") or 0), int(info.get("statuses") or 0))
        if signature == previous and signature[0] > 0:
            stable += 1
        else:
            stable = 0
        previous = signature
        waited = time.monotonic() - started
        if stable >= 6 and (signature[1] > 0 or waited >= 9):
            return
        page.wait_for_timeout(400)


def page_alive(page) -> bool:
    try:
        if page is None or page.is_closed():
            return False
        browser = page.context.browser
        return bool(browser and browser.is_connected())
    except Exception:
        return False


def browser_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(word in text for word in ("closed", "disconnected", "driver", "target", "connection", "crash", "browser", "socket"))


def remember_watch(page, saved: dict) -> None:
    try:
        exported = page.evaluate("() => window.__watchExport ? window.__watchExport() : null") or {}
    except Exception:
        return
    saved["ids"].update(str(item) for item in (exported.get("oldIds") or []) if item)
    saved["status"].update(str(item) for item in (exported.get("oldStatus") or []) if item)


def captured_status_ids(run_id: str) -> list[str]:
    with state_lock:
        return [
            str(item.get("status_id") or "")
            for item in state["items"]
            if item.get("run_id") == run_id and item.get("status_id")
        ]


# Short forms members send for تم: ت alone, and تك (ك sits next to م on the Arabic keyboard).
_ACK_TAM_FORMS = {"تم", "ت", "تك"}


def ack_token(value: str) -> str:
    """Text-only reply: تم or f or F, ignoring marks, one layer of edge punctuation,
    and the send time/date that X shows in the same bubble (e.g. "تم ١٠:٢١ م", "f Oct 20, 10:21 PM").
    Repeated letters count once (تتتممم = تم, ff = f), and ت / تك are read as تم."""
    text = _ACK_MARKS.sub("", str(value or "")).replace("ـ", "")
    for pattern in (_ACK_TIME, _ACK_DATE, _ACK_WORDS, _ACK_SEPARATORS, _ACK_NUMBER):
        text = pattern.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(_ACK_EDGE)
    text = re.sub(r"(.)\1+", r"\1", text)
    if text in _ACK_TAM_FORMS:
        return "تم"
    if text in {"f", "F"}:
        return text
    return ""


def note_unmatched(text: str) -> None:
    """Save a short text message that was not read as تم / f / F (shown with any hidden characters)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if UNMATCHED_LOG.exists() and UNMATCHED_LOG.stat().st_size > 100_000:
            UNMATCHED_LOG.unlink()
        with UNMATCHED_LOG.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + ascii(text) + "  " + text + "\n")
    except OSError:
        pass


def member_key(item: dict) -> str:
    handle = str(item.get("handle") or "").lstrip("@").lower()
    if handle:
        return "@" + handle
    name = str(item.get("name") or "").strip()
    if name:
        return "n:" + name
    return "id:" + str(item.get("key") or "")


def member_total(rows: list[dict]) -> int:
    return len({member_key(item) for item in rows if isinstance(item, dict)})


def status_id_of(value: str) -> str:
    text = str(value or "").strip()
    match = re.search(r"status/(\d{6,25})", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{6,25}", text):
        return text
    return ""


def tweet_handle_of(value: str) -> str:
    """The account name in a tweet link (x.com/<user>/status/<id>), or "" when the link has none."""
    match = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/\d", str(value or ""), re.I)
    if not match or match.group(1).lower() in {"i", "web"}:
        return ""
    return match.group(1)


def norm_person_name(value: str) -> str:
    text = _ACK_MARKS.sub("", str(value or "")).replace("ـ", "")
    return re.sub(r"\s+", " ", text).strip().casefold()


def members_for_likes(replies: list[dict]) -> list[dict]:
    """One row per member who sent تم or f or F, in first-seen order."""
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for item in replies:
        if not isinstance(item, dict):
            continue
        key = member_key(item)
        token = ack_token(str(item.get("text") or ""))
        slot = grouped.get(key)
        if slot is None:
            grouped[key] = {
                "key": key,
                "handle": str(item.get("handle") or "").lstrip("@"),
                "name": str(item.get("name") or ""),
                "texts": [token] if token else [],
            }
            order.append(key)
            continue
        if not slot["handle"] and item.get("handle"):
            slot["handle"] = str(item.get("handle") or "").lstrip("@")
        if not slot["name"] and item.get("name"):
            slot["name"] = str(item.get("name") or "")
        if token and token not in slot["texts"]:
            slot["texts"].append(token)
    return [grouped[key] for key in order]


def judge_likes(members: list[dict], likers: list[dict], complete: bool) -> list[dict]:
    by_handle: dict[str, dict] = {}
    by_name: dict[str, list[str]] = {}
    for person in likers:
        if not isinstance(person, dict):
            continue
        handle = str(person.get("handle") or "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            continue
        by_handle[handle.lower()] = person
        name = norm_person_name(str(person.get("name") or ""))
        if name:
            by_name.setdefault(name, []).append(handle.lower())
    results = []
    for member in members:
        handle = str(member.get("handle") or "").lstrip("@")
        name = str(member.get("name") or "")
        texts = [str(item) for item in (member.get("texts") or []) if item]
        row = {
            "key": str(member.get("key") or ""),
            "handle": handle if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) else "",
            "name": name[:80],
            "text": " ".join(texts)[:40],
            "liked": None,
            "note": "",
        }
        if row["handle"]:
            if row["handle"].lower() in by_handle:
                row["liked"] = True
                row["note"] = "موجود في الإعجابات"
            elif complete:
                row["liked"] = False
                row["note"] = "غير موجود في الإعجابات"
            else:
                row["liked"] = None
                row["note"] = "لم يظهر في الجزء الذي فُحص"
        else:
            hits = by_name.get(norm_person_name(name), []) if name else []
            if name and len(set(hits)) == 1:
                row["liked"] = True
                row["note"] = "طُابق الاسم في الإعجابات"
            elif name and len(set(hits)) > 1:
                row["liked"] = None
                row["note"] = "الاسم مكرر، لم يُحسم"
            else:
                row["liked"] = None
                row["note"] = "بدون معرف، لم يُطابق"
        results.append(row)
    return results


def likes_summary(results: list[dict], complete: bool) -> tuple[str, bool]:
    liked = sum(1 for row in results if row.get("liked") is True)
    missing = sum(1 for row in results if row.get("liked") is False)
    unsure = sum(1 for row in results if row.get("liked") is None)
    total = len(results)
    if total == 0:
        return "لا يوجد أعضاء أرسلوا تم أو f أو F.", False
    if missing == 0 and unsure == 0:
        return f"كل الأعضاء ({liked}) موجودون في إعجابات التغريدة.", True
    parts = [f"{liked} موجودون"]
    if missing:
        parts.append(f"{missing} غير موجودين")
    if unsure:
        parts.append(f"{unsure} لم يُحسم")
    text = "من " + str(total) + ": " + "، ".join(parts) + "."
    if not complete:
        text += " لم تُستكمل قائمة الإعجابات."
    return text, missing == 0 and unsure == 0 and complete


def reply_member_count(run_id: str) -> int:
    with state_lock:
        rows = [item for item in state["replies"] if item.get("run_id") == run_id]
    return member_total(rows)


def format_clock(seconds: float) -> str:
    whole = max(0, int(math.ceil(seconds)))
    minutes, secs = divmod(whole, 60)
    return f"{minutes}:{secs:02d}"


def add_replies(rows: list[dict], run_id: str) -> int:
    added = 0
    with state_lock:
        by_key = {str(item.get("key") or ""): item for item in state["replies"]}
        for row in rows:
            key = str(row.get("key") or "").strip()
            token = ack_token(str(row.get("text") or ""))
            if not key or not token:
                continue
            handle = str(row.get("handle") or "").lstrip("@")
            if handle and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                handle = ""
            name = str(row.get("name") or "")[:80]
            current = by_key.get(key)
            if current is not None:
                if (handle or name) and not (current.get("handle") or current.get("name")):
                    current["handle"] = handle
                    current["name"] = name
                    added += 1
                continue
            item = {
                "key": key,
                "handle": handle,
                "name": name,
                "text": token,
                "at": time.time(),
                "run_id": run_id,
            }
            state["replies"].append(item)
            by_key[key] = item
            added += 1
        if len(state["replies"]) > MAX_ITEMS:
            del state["replies"][:-MAX_ITEMS]
    if added:
        save_state()
    return added


def upgrade_reply_senders(rows: list[dict]) -> None:
    """Fill a sender discovered after the text reply was already recorded. Never creates one."""
    changed = False
    with state_lock:
        by_key = {str(item.get("key") or ""): item for item in state["replies"]}
        for row in rows:
            current = by_key.get(str(row.get("key") or "").strip())
            if current is None or current.get("handle") or current.get("name"):
                continue
            handle = str(row.get("handle") or "").lstrip("@")
            if handle and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                handle = ""
            name = str(row.get("name") or "")[:80]
            if not handle and not name:
                continue
            current["handle"] = handle
            current["name"] = name
            changed = True
    if changed:
        save_state()


def boundary_of(run_id: str) -> tuple[str, str]:
    """Message element id and status id of the last tweet saved in this run."""
    with state_lock:
        items = list(state["items"])
    for item in reversed(items):
        if item.get("run_id") != run_id:
            continue
        status_id = str(item.get("status_id") or "")
        message_id = str(item.get("message_id") or "")
        if not message_id and status_id:
            key = str(item.get("key") or "")
            suffix = ":" + status_id
            if key.endswith(suffix):
                message_id = key[: -len(suffix)]
        if message_id:
            return message_id, status_id
    return "", ""


def scan_text_rows(page) -> list[dict] | None:
    try:
        payload = page.evaluate(TEXT_SCAN_JS)
    except Exception:
        return None
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict) and str(row.get("id") or "").strip()]


def scroll_chat(page, action: str) -> dict:
    try:
        payload = page.evaluate(GROUP_CHAT_SCROLL_JS, action) or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def focus_message(page, message_id: str) -> None:
    if not message_id:
        return
    try:
        page.evaluate(FOCUS_MESSAGE_JS, message_id)
    except Exception:
        return


def consider_replies(rows: list[dict], seen: set[str], pending: dict[str, float], run_id: str) -> None:
    now = time.monotonic()
    fresh: list[dict] = []
    upgrades: list[dict] = []
    for row in rows:
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        text = str(row.get("text") or "")
        matched = bool(row.get("textOnly")) and bool(ack_token(text))
        if mid in seen:
            if matched and (row.get("handle") or row.get("name")):
                upgrades.append({
                    "key": mid,
                    "handle": row.get("handle") or "",
                    "name": row.get("name") or "",
                })
            continue
        if not text.strip():
            started = pending.get(mid)
            if started is None:
                pending[mid] = now
                continue
            if now - started < 3:
                continue
            pending.pop(mid, None)
            seen.add(mid)
            continue
        pending.pop(mid, None)
        seen.add(mid)
        if not matched:
            if row.get("textOnly") and len(text) <= 60:
                note_unmatched(text)
            continue
        fresh.append({
            "key": mid,
            "text": text,
            "handle": row.get("handle") or "",
            "name": row.get("name") or "",
        })
    if fresh:
        add_replies(fresh, run_id)
    if upgrades:
        upgrade_reply_senders(upgrades)


def walk_to_boundary(page, boundary_id: str) -> tuple[set[str], list[dict], bool]:
    """Seen ids at or before the boundary, rows after it, and whether that bubble was found."""
    seen: set[str] = set()
    scroll_chat(page, "latest")
    try:
        page.wait_for_timeout(450)
    except Exception:
        return seen, [], False
    if not boundary_id:
        stalled = 0
        for _ in range(8):
            if abort_flag.is_set():
                break
            for row in scan_text_rows(page) or []:
                seen.add(str(row.get("id")))
            info = scroll_chat(page, "older")
            try:
                page.wait_for_timeout(250)
            except Exception:
                break
            if not info.get("moved"):
                stalled += 1
            else:
                stalled = 0
            if stalled >= 2:
                break
        scroll_chat(page, "latest")
        return seen, [], False

    held: list[dict] = []
    held_ids: set[str] = set()
    found = False
    stalled = 0
    after: list[dict] = []
    for _ in range(80):
        if abort_flag.is_set():
            break
        rows = scan_text_rows(page)
        if rows is None:
            stalled += 1
            try:
                page.wait_for_timeout(300)
            except Exception:
                break
            if stalled >= 4:
                break
            continue
        boundary = next((row for row in rows if str(row.get("id") or "") == boundary_id), None)
        if boundary is not None:
            found = True
            limit = float(boundary.get("top") or 0)
            seen.add(boundary_id)
            for row in rows:
                mid = str(row.get("id") or "")
                if not mid:
                    continue
                if float(row.get("top") or 0) <= limit + 8:
                    seen.add(mid)
                elif mid not in held_ids:
                    after.append(row)
            break
        for row in rows:
            mid = str(row.get("id") or "")
            if mid and mid not in held_ids:
                held_ids.add(mid)
                held.append(row)
        info = scroll_chat(page, "older")
        try:
            page.wait_for_timeout(280)
        except Exception:
            break
        if not info.get("moved"):
            stalled += 1
            if stalled >= 3:
                break
        else:
            stalled = 0
    if not found:
        seen.update(held_ids)
        seen.add(boundary_id)
        scroll_chat(page, "latest")
        try:
            page.wait_for_timeout(400)
        except Exception:
            return seen, [], False
        for _ in range(4):
            visible = scan_text_rows(page) or []
            if visible:
                for row in visible:
                    seen.add(str(row.get("id")))
                break
            try:
                page.wait_for_timeout(300)
            except Exception:
                break
        return seen, [], False
    for row in held:
        mid = str(row.get("id") or "")
        if mid and mid not in seen:
            after.append(row)
    focus_message(page, boundary_id)
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    return seen, after, True


def watch_text_replies(holder: dict, reopen, run_id: str) -> None:
    """After the deletion check, sit on the last saved tweet and record تم / f / F for 30 minutes."""
    message_id, status_id = boundary_of(run_id)
    with state_lock:
        state["text_watch"] = True
        state["checking"] = False
        state["watching"] = True
        state["text_left"] = TEXT_WATCH_SECONDS
        state["boundary_status"] = status_id
    try:
        if message_id:
            set_status("يعود إلى آخر تغريدة محفوظة، ثم يسجّل تم أو f أو F لمدة 30 د…", True, True)
        else:
            set_status("لا تغريدة محفوظة هذه الجولة. سيسجّل الرسائل النصية الجديدة لمدة 30 د.", True, True)
        page = holder.get("page")
        if not page_alive(page):
            page = reopen()
            holder["page"] = page
        seen, pending_rows, found = walk_to_boundary(page, message_id)
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
            return
        if message_id and not found:
            set_status("لم تُرَ آخر تغريدة في القائمة. سيسجّل فقط ما يصل بعد الآن لمدة 30 د.", True, True)
        pending: dict[str, float] = {}
        consider_replies(pending_rows, seen, pending, run_id)
        scroll_chat(page, "latest")
        deadline = time.monotonic() + TEXT_WATCH_SECONDS
        shown = ""
        misses = 0
        while not abort_flag.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if not page_alive(page):
                page = reopen()
                holder["page"] = page
                scroll_chat(page, "latest")
            rows = scan_text_rows(page)
            if rows is None:
                misses += 1
                if misses >= 3:
                    try:
                        page = reopen()
                        holder["page"] = page
                        scroll_chat(page, "latest")
                    except Exception:
                        if abort_flag.is_set():
                            break
                    misses = 0
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                continue
            misses = 0
            consider_replies(rows, seen, pending, run_id)
            scroll_chat(page, "latest")
            label = format_clock(left)
            if label != shown:
                shown = label
                count = reply_member_count(run_id)
                where = f"من آخر تغريدة {status_id}" if status_id else "من حد آخر تغريدة"
                set_status(f"{where}. تسجيل الردود متبقي {label}. الأعضاء: {count}.", True, True)
                with state_lock:
                    state["text_left"] = max(0, int(math.ceil(left)))
            try:
                page.wait_for_timeout(400)
            except Exception:
                try:
                    page = reopen()
                    holder["page"] = page
                except Exception:
                    if abort_flag.is_set():
                        break
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
            return
        count = reply_member_count(run_id)
        set_status(f"انتهى انتظار 30 د. سُجّل {count} من الأعضاء أرسلوا تم أو f أو F.", True, False)
    except Exception:
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
        else:
            set_status("انقطع تسجيل الردود قبل اكتمال 30 د. ما سُجّل بقي محفوظًا.", False, False)
    finally:
        with state_lock:
            state["text_watch"] = False
            state["text_left"] = 0


def watch_loop(auth_token: str, pin: str, group_id: str, group_name: str, wait_seconds: float = 0.0) -> None:
    run_id = secrets.token_hex(4)
    anchor_ids: set[str] = set()
    current = {"name": group_name}

    def open_watched_group(page) -> None:
        """Open the group by its id. If it was renamed, keep and show the new name."""
        width = (page.viewport_size or {}).get("width") or 1280
        page.set_viewport_size({"width": width, "height": WATCH_VIEWPORT_HEIGHT})
        name = open_group(page, group_id, current["name"])
        if name and name != current["name"]:
            current["name"] = name
            with state_lock:
                state["group"] = name
            save_state()

    def work(page):
        holder = {"page": page}
        saved = {"ids": set(), "status": set()}

        def reopen():
            set_status("انقطع المتصفح. تتم متابعة المهمة…", True, True)
            while not abort_flag.is_set():
                try:
                    browser_hub._reset_driver()
                    fresh = browser_hub.page_for(auth_token)
                    if not ensure_logged_in(fresh):
                        raise RuntimeError("session")
                    err = open_chat(fresh, pin)
                    if err:
                        raise RuntimeError(err)
                    show_groups_only(fresh)
                    open_watched_group(fresh)
                    holder["page"] = fresh
                    return fresh
                except Exception:
                    if abort_flag.is_set():
                        raise
                    time.sleep(2)
            raise RuntimeError("aborted")

        def rearm(fresh):
            fresh.evaluate(WATCH_JS)
            statuses = list(saved["status"])
            statuses.extend(captured_status_ids(run_id))
            fresh.evaluate(
                "([ids, statuses]) => window.__watchResume && window.__watchResume({oldIds: ids, oldStatus: statuses})",
                [list(saved["ids"]), statuses],
            )
            holder["page"] = fresh
            return fresh

        def resume_watch():
            return rearm(reopen())

        page = holder["page"]
        if not ensure_logged_in(page):
            set_status("الجلسة غير مسجّلة الدخول.", False, False)
            return
        error = open_chat(page, pin)
        if error:
            set_status(error, False, False)
            return
        show_groups_only(page)
        try:
            open_watched_group(page)
        except Exception as exc:
            if not browser_failure(exc) and page_alive(page):
                set_status("تعذر فتح القروب.", False, False)
                return
            page = reopen()
            open_watched_group(page)
        if wait_seconds > 0:
            deadline = time.monotonic() + wait_seconds
            shown = ""
            with state_lock:
                state["waiting"] = True
            try:
                while not stop_flag.is_set() and not abort_flag.is_set():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    label = str(math.ceil(left))
                    if label != shown:
                        shown = label
                        set_status(f"تم فتح القروب. ينتظر {label} ث قبل التقاط البداية…", True, True)
                    try:
                        page.wait_for_timeout(max(50, (left % 1 or 1.0) * 1000))
                    except Exception:
                        if abort_flag.is_set():
                            break
                        page = reopen()
                        shown = ""
            finally:
                with state_lock:
                    state["waiting"] = False
            if abort_flag.is_set():
                set_status("تم إيقاف المهمة.", True, False)
                return
            if stop_flag.is_set():
                set_status(STOPPED_DURING_WAIT, True, False)
                return
        try:
            page.evaluate(GROUP_CHAT_SCROLL_JS, "latest")
        except Exception:
            pass
        page.wait_for_timeout(600)
        try:
            page.evaluate(WATCH_JS)
            page.evaluate("() => window.__watchReset && window.__watchReset()")
            set_status("يتم تجاهل التغريدات الموجودة قبل المراقبة…", True, True)
            settle_existing(page)
            if abort_flag.is_set():
                set_status("تم إيقاف المهمة.", True, False)
                return
            if not stop_flag.is_set():
                anchor = page.evaluate("() => window.__watchAnchor ? window.__watchAnchor() : []") or []
                anchor_ids.update(str(item) for item in anchor if item)
                page.evaluate("() => window.__watchArm && window.__watchArm()")
                remember_watch(page, saved)
            else:
                verify_captured(page, anchor_ids, run_id, reopen)
                return
        except Exception:
            if abort_flag.is_set():
                set_status("تم إيقاف المهمة.", True, False)
                return
            page = reopen()
            holder["page"] = page
            page.evaluate(WATCH_JS)
            page.evaluate("() => window.__watchReset && window.__watchReset()")
            settle_existing(page)
            if abort_flag.is_set():
                set_status("تم إيقاف المهمة.", True, False)
                return
            if stop_flag.is_set():
                verify_captured(page, anchor_ids, run_id, reopen)
                return
            anchor = page.evaluate("() => window.__watchAnchor ? window.__watchAnchor() : []") or []
            anchor_ids.update(str(item) for item in anchor if item)
            page.evaluate("() => window.__watchArm && window.__watchArm()")
            remember_watch(page, saved)
        set_status(f"يراقب «{current['name']}» ويلتقط التغريدات الجديدة.", True, True)
        missed = 0
        while not stop_flag.is_set() and not abort_flag.is_set():
            page = holder["page"]
            try:
                if not page_alive(page):
                    page = resume_watch()
                    continue
                if not page.evaluate("() => !!(window.__watch && window.__watch.armed)"):
                    page = rearm(page)
                    continue
                payload = page.evaluate("() => window.__watchDrain()") or {}
            except Exception:
                missed += 1
                if missed >= 3:
                    try:
                        page = resume_watch()
                    except Exception:
                        if abort_flag.is_set():
                            set_status("تم إيقاف المهمة.", True, False)
                            return
                    missed = 0
                    continue
                try:
                    page.wait_for_timeout(200)
                except Exception:
                    try:
                        page = resume_watch()
                    except Exception:
                        if abort_flag.is_set():
                            set_status("تم إيقاف المهمة.", True, False)
                            return
                    missed = 0
                continue
            missed = 0
            if not payload.get("alive"):
                try:
                    page = resume_watch()
                except Exception:
                    if abort_flag.is_set():
                        set_status("تم إيقاف المهمة.", True, False)
                        return
                continue
            add_items(payload.get("rows") or [], run_id)
            remember_watch(page, saved)
            try:
                page.evaluate(GROUP_CHAT_SCROLL_JS, "latest")
            except Exception:
                pass
            try:
                page.wait_for_timeout(180)
            except Exception:
                try:
                    page = resume_watch()
                except Exception:
                    if abort_flag.is_set():
                        set_status("تم إيقاف المهمة.", True, False)
                        return
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
            return
        while not abort_flag.is_set():
            page = holder["page"]
            try:
                if not page_alive(page):
                    page = reopen()
                verify_captured(page, anchor_ids, run_id, reopen, hold=True)
                if abort_flag.is_set():
                    set_status("تم إيقاف المهمة.", True, False)
                    return
                watch_text_replies(holder, reopen, run_id)
                return
            except Exception:
                if abort_flag.is_set():
                    set_status("تم إيقاف المهمة.", True, False)
                    return
                try:
                    holder["page"] = reopen()
                except Exception:
                    if abort_flag.is_set():
                        set_status("تم إيقاف المهمة.", True, False)
                        return

    def work_then_close(page):
        try:
            return work(page)
        finally:
            # The watch is over (finished, stopped, or failed): close the browser. Left open,
            # it would stay inside the group and X would mark every new message as seen.
            browser_hub._reset_driver()

    try:
        run_on_browser(auth_token, work_then_close)
    except Exception as exc:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with (DATA_DIR / "browser.log").open("a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + repr(exc) + "\n")
        except Exception:
            pass
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
        else:
            set_status("انقطع المتصفح. أعد اختيار القروب.", False, False)
    finally:
        abort_flag.clear()
        with state_lock:
            state["watching"] = False
            state["checking"] = False
            state["waiting"] = False
            state["text_watch"] = False
            state["text_left"] = 0


def stop_watch(timeout: float = 120.0) -> bool:
    global watch_thread
    stop_flag.set()
    thread = watch_thread
    if thread and thread.is_alive():
        thread.join(timeout)
        if thread.is_alive():
            return False
    stop_flag.clear()
    return True


PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>مراقبة القروب</title>
  <style>
    :root { --paper:#f3efe6; --sheet:#fffdf8; --ink:#1c1915; --muted:#6e675e; --line:#e4dcd0; --pine:#0f6b50; --on:#f4fbf7; --bad:#8d3a32; --ok:#e7f5ee; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--paper); color:var(--ink); font:15px/1.5 "Segoe UI", Tahoma, sans-serif; }
    .page { width:min(520px,100%); margin:0 auto; padding:12px; }
    .sheet { background:var(--sheet); border-radius:16px; padding:14px; box-shadow:inset 0 0 0 1px rgba(28,25,21,.06); }
    h1 { margin:0 0 4px; font-size:22px; }
    .sub { margin:0 0 12px; color:var(--muted); font-size:13px; }
    label { display:block; margin:10px 0 4px; font-size:12px; color:var(--muted); }
    input { width:100%; height:36px; border:0; border-radius:10px; padding:0 10px; background:var(--paper); color:var(--ink); font:inherit; box-shadow:inset 0 0 0 1px var(--line); }
    input[readonly] { color:var(--muted); }
    .row { display:flex; gap:6px; align-items:center; }
    .row input { flex:1; }
    button { height:36px; border:0; border-radius:10px; padding:0 12px; background:var(--ink); color:var(--sheet); font:inherit; font-size:13px; font-weight:600; cursor:pointer; }
    button.ghost { background:var(--sheet); color:var(--ink); box-shadow:inset 0 0 0 1px var(--line); }
    button.pine { background:var(--pine); color:var(--on); }
    button:disabled { opacity:.5; cursor:wait; }
    #groups { display:flex; gap:6px; overflow-x:auto; margin-top:8px; padding-bottom:2px; }
    #box { margin-top:10px; padding:8px 10px 10px; border-radius:14px; background:var(--paper); box-shadow:inset 0 0 0 1px var(--line); }
    #groups button { flex:0 0 auto; height:30px; border-radius:999px; background:var(--sheet); color:var(--ink); box-shadow:inset 0 0 0 1px var(--line); font-weight:600; white-space:nowrap; }
    #groups button.on { background:var(--ok); box-shadow:inset 0 0 0 1.5px var(--pine); }
    #likes-accounts { display:flex; gap:6px; overflow-x:auto; margin-top:6px; padding-bottom:2px; }
    #likes-accounts:empty { display:none; }
    #likes-accounts button { flex:0 0 auto; height:30px; border-radius:999px; background:var(--sheet); color:var(--ink); box-shadow:inset 0 0 0 1px var(--line); font-weight:600; white-space:nowrap; }
    #likes-accounts button.on { background:var(--ok); box-shadow:inset 0 0 0 1.5px var(--pine); }
    #box input { background:var(--sheet); }
    .actions { display:flex; gap:8px; margin-top:8px; }
    .actions button { flex:1; height:36px; border:0; border-radius:10px; background:var(--ink); color:#fff; font:inherit; font-weight:700; }
    .actions button.ghost { background:var(--sheet); color:var(--ink); box-shadow:inset 0 0 0 1px var(--line); }
    .notice { margin:10px 0 0; font-size:13px; }
    .notice.bad { color:var(--bad); }
    .notice.ok { color:var(--pine); }
    .feed { margin-top:12px; display:flex; flex-direction:column; gap:6px; max-height:320px; overflow-y:auto; padding:6px; border-radius:12px; box-shadow:inset 0 0 0 1px var(--line); }
    .item { padding:8px 10px; border-radius:12px; background:var(--paper); }
    .item.gone { box-shadow: inset 0 0 0 1px var(--bad); }
    .item.kept { box-shadow: inset 0 0 0 1px var(--pine); }
    .badge { font-size: 11px; font-weight: 700; }
    .badge.gone { color: var(--bad); }
    .badge.kept { color: var(--pine); }
    .badge.two { color: #8a5a12; }
    .badge.unsure { color: #8a5a12; }
    .token { min-width:28px; text-align:center; font-weight:800; color:var(--pine); direction:ltr; unicode-bidi:isolate; }
    .doubles { display:flex; flex-direction:column; gap:6px; margin-top:8px; }
    .double { display:flex; justify-content:space-between; gap:8px; align-items:center; padding:8px 10px; border-radius:12px; background:#fbf6ea; box-shadow:inset 0 0 0 1px #e6d3a4; }
    .double b { font-size:13px; }
    button.mini { height:26px; padding:0 10px; border-radius:999px; font-size:12px; }
    .pop { position:fixed; inset:0; z-index:5; display:flex; align-items:center; justify-content:center; padding:16px; background:rgba(28,25,21,.35); }
    .pop[hidden] { display:none; }
    .pop-card { width:min(420px,100%); max-height:70vh; overflow:auto; background:var(--sheet); border-radius:16px; padding:14px; }
    .pop-list { display:flex; flex-direction:column; gap:8px; margin-top:10px; }
    .pop-list a { color:var(--pine); direction:ltr; unicode-bidi:isolate; font-size:13px; }
    .pop-list p { margin:2px 0 0; color:var(--muted); font-size:12px; }
    .item b { font-size:13px; }
    .item a { color:var(--pine); font-size:12px; direction:ltr; unicode-bidi:isolate; }
    .item p { margin:4px 0 0; font-size:13px; }
    .meta { color:var(--muted); font-size:11px; }
    .top { display:flex; justify-content:space-between; gap:8px; align-items:center; }
  </style>
</head>
<body>
  <main class="page">
    <section class="sheet">
      <h1>مراقبة القروب</h1>
      <p class="sub">يفتح القروب ويبقى داخله. أي تغريدة جديدة تُحفظ مع الحساب الذي أرسلها. عند الإيقاف يُفحص المحذوف، ثم من آخر تغريدة ينتظر 30 د ويسجّل من يرسل تم أو f أو F.</p>
      <label for="token">رمز الجلسة</label>
      <div class="row">
        <input id="token" type="password" maxlength="512" autocomplete="off" spellcheck="false" readonly placeholder="الصق الرمز">
        <button id="edit" class="ghost" type="button">تحرير</button>
      </div>
      <label for="pin">رمز الدردشة</label>
      <div class="row">
        <input id="pin" type="password" inputmode="numeric" maxlength="8" autocomplete="off" placeholder="٤ أرقام">
        <button id="load" class="ghost" type="button">عرض القروبات</button>
      </div>
      <label for="groups-wait">الانتظار قبل التقاط القروبات (بالثواني)</label>
      <input id="groups-wait" type="text" inputmode="decimal" maxlength="5" autocomplete="off" spellcheck="false" placeholder="0 = يلتقط فورًا">
      <button id="stop" class="ghost" type="button" style="width:100%;margin-top:8px" disabled>إيقاف المراقبة</button>
      <button id="abort" class="ghost" type="button" style="width:100%;margin-top:8px" disabled>إيقاف المهمة</button>
      <div id="box" hidden>
        <div class="meta">حدد القروب، ثم اضغط مراقبة</div>
        <div id="groups"></div>
        <label for="wait">الانتظار بعد دخول القروب قبل التقاط البداية (بالثواني)</label>
        <input id="wait" type="text" inputmode="decimal" maxlength="5" autocomplete="off" spellcheck="false" placeholder="0 = يلتقط فورًا">
        <div class="actions">
          <button id="watch" type="button">مراقبة</button>
        </div>
      </div>
      <p id="notice" class="notice" hidden></p>
    </section>
    <section class="sheet" style="margin-top:10px">
      <div class="top">
        <h1 style="font-size:18px">الملتقط</h1>
        <b id="count">0</b>
      </div>
      <div id="doubles" class="doubles" hidden></div>
      <div id="feed" class="feed"></div>
    </section>
    <section class="sheet" style="margin-top:10px">
      <div class="top">
        <h1 style="font-size:18px">ردود نصية</h1>
        <b id="reply-count">0</b>
      </div>
      <p id="reply-meta" class="meta">بعد الإيقاف يعود لآخر تغريدة محفوظة وينتظر 30 دقيقة. يُسجَّل من يرسل تم أو f أو F فقط.</p>
      <div id="replies" class="feed"></div>
    </section>
    <section class="sheet" style="margin-top:10px">
      <div class="top">
        <h1 style="font-size:18px">فحص الإعجابات</h1>
        <b id="likes-count">0</b>
      </div>
      <p class="sub">متصفح مستقل عن المراقبة. اختر حسابًا محفوظًا أو الصق توكنًا جديدًا، ثم افحص تغريدة واحدة. تحديث الصفحة لا يوقف الفحص.</p>
      <p id="likes-accounts-label" class="meta" hidden>الحسابات المحفوظة — انقر للاختيار</p>
      <div id="likes-accounts"></div>
      <label for="likes-token">توكن جديد</label>
      <input id="likes-token" type="password" maxlength="512" autocomplete="off" spellcheck="false" placeholder="يُحفظ بعد أول فحص">
      <label for="likes-tweet">تغريدة الحساب</label>
      <input id="likes-tweet" type="text" maxlength="300" autocomplete="off" spellcheck="false" placeholder="رابط التغريدة أو رقمها">
      <button id="likes-check" class="pine" type="button" style="width:100%;margin-top:8px">فحص</button>
      <p id="likes-notice" class="notice" hidden></p>
      <div id="likes-feed" class="feed"></div>
    </section>
    <div id="pop" class="pop" hidden>
      <div class="pop-card">
        <div class="top">
          <b id="pop-title"></b>
          <button id="pop-close" class="ghost mini" type="button">إغلاق</button>
        </div>
        <div id="pop-body" class="pop-list"></div>
      </div>
    </div>
  </main>
  <script>
    const csrf = {{ csrf|tojson }};
    const token = document.getElementById("token");
    const pin = document.getElementById("pin");
    const edit = document.getElementById("edit");
    const load = document.getElementById("load");
    const stop = document.getElementById("stop");
    const abortTask = document.getElementById("abort");
    const watch = document.getElementById("watch");
    const wait = document.getElementById("wait");
    const groupsWait = document.getElementById("groups-wait");
    const box = document.getElementById("box");
    const groups = document.getElementById("groups");
    const notice = document.getElementById("notice");
    const feed = document.getElementById("feed");
    const doubles = document.getElementById("doubles");
    const replies = document.getElementById("replies");
    const replyCount = document.getElementById("reply-count");
    const replyMeta = document.getElementById("reply-meta");
    const likesToken = document.getElementById("likes-token");
    const likesAccounts = document.getElementById("likes-accounts");
    const likesAccountsLabel = document.getElementById("likes-accounts-label");
    const likesTweet = document.getElementById("likes-tweet");
    const likesCheck = document.getElementById("likes-check");
    const likesNotice = document.getElementById("likes-notice");
    const likesFeed = document.getElementById("likes-feed");
    const likesCount = document.getElementById("likes-count");
    const pop = document.getElementById("pop");
    const popTitle = document.getElementById("pop-title");
    const popBody = document.getElementById("pop-body");
    const count = document.getElementById("count");
    let picked = "";
    let starting = false;
    let busy = false;
    let groupsReady = false;
    let waitLoaded = false;
    let groupsWaitLoaded = false;
    let groupList = [];
    let seenRev = -1;
    let noticeHold = 0;
    let likesFieldsLoaded = false;
    let likesAccountId = "";
    let likesPickLoaded = false;

    function lock(locked) {
      token.readOnly = locked;
      edit.textContent = locked ? "تحرير" : "تم";
    }
    lock(true);
    edit.addEventListener("click", () => {
      if (token.readOnly) { lock(false); token.focus(); token.select(); return; }
      lock(true);
    });

    function show(message, ok) {
      notice.hidden = !message;
      notice.textContent = message || "";
      notice.className = "notice " + (ok ? "ok" : "bad");
    }

    async function post(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({...payload, csrf_token: csrf})
      });
      const data = await response.json();
      if (!response.ok && !data.message) throw new Error("تعذر الاتصال");
      return data;
    }

    document.getElementById("pop-close").addEventListener("click", () => { pop.hidden = true; });
    pop.addEventListener("click", (event) => { if (event.target === pop) pop.hidden = true; });

    function openPop(title, rows) {
      popTitle.textContent = title;
      popBody.replaceChildren();
      rows.forEach((row) => {
        const link = document.createElement("a");
        link.href = row.href;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = row.label;
        if (!row.text) {
          popBody.appendChild(link);
          return;
        }
        const block = document.createElement("div");
        block.appendChild(link);
        const text = document.createElement("p");
        text.textContent = row.text;
        block.appendChild(text);
        popBody.appendChild(block);
      });
      pop.hidden = false;
    }

    // Both wait boxes take seconds: empty = 0, Arabic digits allowed, null = not a valid number.
    function readSeconds(field, max) {
      const text = field.value.trim()
        .replace(/[٠-٩]/g, (digit) => String(digit.charCodeAt(0) - 0x660))
        .replace(/[۰-۹]/g, (digit) => String(digit.charCodeAt(0) - 0x6F0))
        .replace(/[٫,]/g, ".");
      if (!text) return 0;
      if (!/^\d+(\.\d+)?$/.test(text)) return null;
      const seconds = Number(text);
      return seconds <= max ? Math.round(seconds) : null;
    }

    function render(data) {
      busy = !!(data.watching || data.checking || data.text_watch);
      if (document.activeElement !== token && data.auth_token && token.readOnly) token.value = data.auth_token;
      if (document.activeElement !== pin && data.chat_pin) pin.value = data.chat_pin;
      if (!waitLoaded) {
        wait.value = data.wait_seconds ? String(data.wait_seconds) : "";
        waitLoaded = true;
      }
      wait.disabled = busy;
      groupsWait.disabled = busy;
      if (!groupsWaitLoaded) {
        groupsWait.value = data.groups_wait ? String(data.groups_wait) : "";
        groupsWaitLoaded = true;
      }
      // Groups are picked by id, so a renamed group is still the same group.
      groupList = groupsReady ? (data.groups || []).filter((group) => group && group.id && group.name) : [];
      if (picked && groupsReady && !groupList.some((group) => group.id === picked)) picked = "";
      if (!picked && data.group_id && groupList.some((group) => group.id === data.group_id)) picked = data.group_id;
      box.hidden = groupList.length === 0;
      groups.replaceChildren();
      groupList.forEach((group) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = group.name;
        button.disabled = busy;
        if (group.id === picked) button.className = "on";
        button.addEventListener("click", () => {
          if (busy || starting) { show("المهمة تعمل الآن. انتظر حتى تنتهي.", false); return; }
          picked = group.id;
          groups.querySelectorAll("button").forEach((item) => item.classList.toggle("on", item === button));
        });
        groups.appendChild(button);
      });
      if (watch) watch.disabled = busy || !groupList.length;
      count.textContent = String(data.count || 0);
      const bySender = new Map();
      (data.items || []).forEach((item) => {
        const key = item.handle ? "@" + item.handle : (item.name || "");
        if (!key) return;
        if (!bySender.has(key)) bySender.set(key, []);
        bySender.get(key).push(item);
      });
      const twice = [...bySender.entries()].filter(([, rows]) => rows.length === 2);
      doubles.hidden = twice.length === 0;
      doubles.replaceChildren();
      twice.forEach(([who, rows]) => {
        const box = document.createElement("article");
        box.className = "double";
        const title = document.createElement("b");
        title.textContent = who + " أرسل تغريدتين";
        const view = document.createElement("button");
        view.type = "button";
        view.className = "ghost mini";
        view.textContent = "عرض";
        view.addEventListener("click", () => openPop(who, rows.map((item) => ({
          href: item.url,
          label: item.status_id,
          text: item.text || ""
        }))));
        box.append(title, view);
        doubles.appendChild(box);
      });
      feed.replaceChildren();
      (data.items || []).forEach((item) => {
        const row = document.createElement("article");
        row.className = "item" + (item.present === false ? " gone" : item.present === true ? " kept" : "");
        const who = document.createElement("b");
        who.textContent = item.handle ? "@" + item.handle : (item.name || "حساب غير معروف");
        const link = document.createElement("a");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = item.status_id;
        const head = document.createElement("div");
        head.className = "top";
        head.append(who, link);
        if (item.present === true || item.present === false) {
          const badge = document.createElement("span");
          badge.className = "badge " + (item.present ? "kept" : "gone");
          badge.textContent = item.present ? "موجودة" : "محذوفة";
          head.appendChild(badge);
        }
        const senderKey = item.handle ? "@" + item.handle : (item.name || "");
        if (senderKey && bySender.get(senderKey) && bySender.get(senderKey).length === 2) {
          const two = document.createElement("span");
          two.className = "badge two";
          two.textContent = "تغريدتان";
          head.appendChild(two);
        }
        row.appendChild(head);
        if (item.text) {
          const text = document.createElement("p");
          text.textContent = item.text;
          row.appendChild(text);
        }
        feed.appendChild(row);
      });
      const replyRows = data.replies || [];
      replyCount.textContent = String(data.reply_count || 0);
      if (data.text_watch) {
        const left = Math.max(0, Number(data.text_left) || 0);
        const minutes = Math.floor(left / 60);
        const seconds = left % 60;
        const edge = data.boundary_status ? ("من التغريدة " + data.boundary_status + ". ") : "";
        replyMeta.textContent = edge + "يسجّل تم أو f أو F. متبقي " + minutes + ":" + String(seconds).padStart(2, "0");
      } else if (replyRows.length) {
        replyMeta.textContent = "الأعضاء الذين أرسلوا رسالة نصية فقط: تم أو f أو F.";
      } else {
        replyMeta.textContent = "بعد الإيقاف يعود لآخر تغريدة محفوظة وينتظر 30 دقيقة. يُسجَّل من يرسل تم أو f أو F فقط.";
      }
      replies.replaceChildren();
      replyRows.forEach((item) => {
        const row = document.createElement("article");
        row.className = "item";
        const who = document.createElement("b");
        who.textContent = item.handle ? "@" + item.handle : (item.name || "حساب غير معروف");
        const token = document.createElement("span");
        token.className = "token";
        token.textContent = item.text || "";
        const head = document.createElement("div");
        head.className = "top";
        head.append(who, token);
        row.appendChild(head);
        if (item.at) {
          const when = document.createElement("p");
          when.className = "meta";
          when.textContent = new Date(item.at * 1000).toLocaleTimeString("ar", {hour: "2-digit", minute: "2-digit"});
          row.appendChild(when);
        }
        replies.appendChild(row);
      });
      if (!likesFieldsLoaded) {
        if (data.likes_tweet && document.activeElement !== likesTweet) likesTweet.value = data.likes_tweet;
        likesFieldsLoaded = true;
      }
      if (!likesPickLoaded) {
        likesAccountId = data.likes_account_id || "";
        likesPickLoaded = true;
      }
      const accounts = data.likes_accounts || [];
      likesAccountsLabel.hidden = accounts.length === 0;
      likesAccounts.replaceChildren();
      accounts.forEach((account) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = account.label || (account.handle ? "@" + account.handle : "حساب");
        if (account.id === likesAccountId && !likesToken.value.trim()) button.className = "on";
        button.addEventListener("click", () => {
          likesAccountId = account.id;
          likesToken.value = "";
          likesAccounts.querySelectorAll("button").forEach((item) => item.classList.toggle("on", item === button));
        });
        likesAccounts.appendChild(button);
      });
      const likeRows = data.likes_results || [];
      const likedTotal = likeRows.filter((item) => item.liked === true).length;
      likesCount.textContent = likeRows.length ? (likedTotal + "/" + likeRows.length) : "0";
      likesCheck.disabled = !!data.likes_checking;
      likesCheck.textContent = data.likes_checking ? "جارٍ الفحص…" : "فحص";
      if (data.likes_checking || data.likes_message) {
        likesNotice.hidden = !data.likes_message;
        likesNotice.textContent = data.likes_message || "";
        likesNotice.className = "notice " + (data.likes_ok ? "ok" : "bad");
      }
      likesFeed.replaceChildren();
      likeRows.forEach((item) => {
        const row = document.createElement("article");
        row.className = "item" + (item.liked === true ? " kept" : item.liked === false ? " gone" : "");
        const who = document.createElement("b");
        who.textContent = item.handle ? "@" + item.handle : (item.name || "حساب غير معروف");
        const badge = document.createElement("span");
        if (item.liked === true) {
          badge.className = "badge kept";
          badge.textContent = "أعجب";
        } else if (item.liked === false) {
          badge.className = "badge gone";
          badge.textContent = "لم يُعجب";
        } else {
          badge.className = "badge unsure";
          badge.textContent = "لم يُحسم";
        }
        const head = document.createElement("div");
        head.className = "top";
        head.append(who, badge);
        row.appendChild(head);
        const note = document.createElement("p");
        note.className = "meta";
        note.textContent = (item.text ? item.text + " — " : "") + (item.note || "");
        row.appendChild(note);
        likesFeed.appendChild(row);
      });
      if (!noticeHold && Number(data.rev || 0) > seenRev && data.message) {
        seenRev = Number(data.rev || 0);
        show(data.message, !!data.ok);
      }
      stop.disabled = !data.watching || !!data.checking || !!data.text_watch;
      stop.textContent = data.checking ? "جارٍ التحقق…" : (data.text_watch ? "جارٍ تسجيل الردود…" : "إيقاف المراقبة");
      abortTask.disabled = !busy;
    }

    async function refresh() {
      try {
        const response = await fetch("/state", {cache: "no-store"});
        const data = await response.json();
        if (response.ok) render(data);
      } catch (_) {}
    }

    async function start(groupId) {
      if (busy || starting) {
        show("المهمة تعمل الآن. انتظر حتى تنتهي.", false);
        return;
      }
      const auth = token.value.trim();
      const chat = pin.value.trim();
      if (!auth) { show("أدخل رمز الجلسة.", false); token.focus(); return; }
      if (!/^\d{4,8}$/.test(chat)) { show("أدخل رمز الدردشة.", false); pin.focus(); return; }
      const group = groupList.find((item) => item.id === groupId);
      if (!group) { show("حدد القروب ثم اضغط مراقبة.", false); return; }
      const name = group.name;
      const waitSeconds = readSeconds(wait, 86400);
      if (waitSeconds === null) { show("الانتظار بعد دخول القروب بالثواني من 0 إلى 86400.", false); wait.focus(); return; }
      starting = true;
      picked = group.id;
      noticeHold += 1;
      show("جارٍ فتح " + name + "…", true);
      try {
        const data = await post("/watch/start", {auth_token: auth, chat_pin: chat, group: name, group_id: group.id, wait_seconds: waitSeconds});
        show(data.message || "بدأت المراقبة.", !!data.success);
      } catch (_) {
        show("تعذر بدء المراقبة.", false);
      } finally {
        noticeHold = Math.max(0, noticeHold - 1);
        starting = false;
        await refresh();
      }
    }

    load.addEventListener("click", async () => {
      const auth = token.value.trim();
      const chat = pin.value.trim();
      if (!auth) { show("أدخل رمز الجلسة.", false); return; }
      if (!/^\d{4,8}$/.test(chat)) { show("أدخل رمز الدردشة.", false); return; }
      const seconds = readSeconds(groupsWait, 300);
      if (seconds === null) { show("الانتظار قبل التقاط القروبات بالثواني من 0 إلى 300.", false); groupsWait.focus(); return; }
      load.disabled = true;
      noticeHold += 1;
      show(seconds ? "جارٍ عرض القروبات… سينتظر " + seconds + " ث قبل التقاطها." : "جارٍ عرض القروبات…", true);
      try {
        const data = await post("/groups", {auth_token: auth, chat_pin: chat, groups_wait: seconds});
        if (data.success) groupsReady = true;
        show(data.message || (data.success ? "اختر قروبًا." : "تعذر العرض."), !!data.success);
      } catch (_) {
        show("تعذر الاتصال.", false);
      } finally {
        noticeHold = Math.max(0, noticeHold - 1);
        load.disabled = false;
        await refresh();
      }
    });

    stop.addEventListener("click", async () => {
      stop.disabled = true;
      try { await post("/watch/stop", {}); } catch (_) {}
      refresh();
    });

    abortTask.addEventListener("click", async () => {
      abortTask.disabled = true;
      try { await post("/watch/abort", {}); } catch (_) {}
      refresh();
    });

    function likesShow(message, ok) {
      likesNotice.hidden = !message;
      likesNotice.textContent = message || "";
      likesNotice.className = "notice " + (ok ? "ok" : "bad");
    }

    likesToken.addEventListener("input", () => {
      if (likesToken.value.trim()) {
        likesAccountId = "";
        likesAccounts.querySelectorAll("button").forEach((item) => item.classList.remove("on"));
      }
    });

    likesCheck.addEventListener("click", async () => {
      if (likesCheck.disabled) return;
      const auth = likesToken.value.trim();
      const tweet = likesTweet.value.trim();
      if (!auth && !likesAccountId) { likesShow("اختر حسابًا محفوظًا أو أدخل توكنًا.", false); likesToken.focus(); return; }
      if (auth && auth.length < 20) { likesShow("توكن الجلسة غير صالح.", false); likesToken.focus(); return; }
      if (!/\d{6,25}/.test(tweet)) { likesShow("أدخل رابط التغريدة أو رقمها.", false); likesTweet.focus(); return; }
      likesCheck.disabled = true;
      likesShow("جارٍ بدء الفحص في متصفح مستقل…", true);
      const payload = {tweet};
      if (auth) payload.auth_token = auth;
      else payload.account_id = likesAccountId;
      try {
        const data = await post("/likes/check", payload);
        likesShow(data.message || (data.success ? "بدأ الفحص." : "تعذر الفحص."), !!data.success);
        if (data.account_id) likesAccountId = data.account_id;
        if (data.success) likesToken.value = "";
      } catch (_) {
        likesShow("تعذر الاتصال.", false);
      }
      refresh();
    });

    watch.addEventListener("click", () => start(picked));

    refresh();
    setInterval(refresh, 700);
  </script>
</body>
</html>
"""


@app.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def home():
    return render_template_string(PAGE, csrf=ensure_csrf())


@app.get("/state")
def state_endpoint():
    return jsonify(public_state())


@app.post("/groups")
def groups_endpoint():
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    if state["watching"] or state.get("checking") or state.get("text_watch"):
        return jsonify(success=False, message="أوقف المهمة أولًا."), 409
    token = str(data.get("auth_token") or "").strip()
    pin = str(data.get("chat_pin") or "").strip()
    if not valid_auth_token(token):
        return jsonify(success=False, message="رمز الجلسة غير صالح."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة ٤ إلى ٨ أرقام."), 400
    groups_wait = parse_seconds(data.get("groups_wait"), MAX_GROUPS_WAIT_SECONDS)
    if groups_wait is None:
        return jsonify(success=False, message=f"الانتظار قبل التقاط القروبات بالثواني من 0 إلى {MAX_GROUPS_WAIT_SECONDS}."), 400
    with state_lock:
        state["groups_wait"] = groups_wait
    save_state()
    set_status("جارٍ عرض القروبات…", True)

    def work(page):
        if not ensure_logged_in(page):
            return None, "الجلسة غير مسجّلة الدخول."
        error = open_chat(page, pin)
        if error:
            return None, error
        show_groups_only(page)
        if groups_wait:
            set_status(f"ينتظر {groups_wait} ث قبل التقاط القروبات…", True)
            page.wait_for_timeout(groups_wait * 1000)
        names = collect_groups(page)
        return names, None

    try:
        names, error = run_on_browser(token, work)
    except Exception as exc:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with (DATA_DIR / "browser.log").open("a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + repr(exc) + "\n")
        except Exception:
            pass
        set_status("انقطع المتصفح. أعد المحاولة.", False)
        return jsonify(success=False, message="انقطع المتصفح. أعد المحاولة."), 502
    if error:
        set_status(error, False)
        return jsonify(success=False, message=error), 502
    with state_lock:
        state["auth_token"] = token
        state["chat_pin"] = pin
        state["groups"] = names or []
    save_state()
    set_status(f"ظهر {len(names or [])} قروب. اختر واحدًا.", True)
    return jsonify(success=True, message=f"ظهر {len(names or [])} قروب. اختر واحدًا.", groups=names or [])


@app.post("/watch/start")
def watch_start():
    global watch_thread
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    token = str(data.get("auth_token") or "").strip()
    pin = str(data.get("chat_pin") or "").strip()
    group_name = re.sub(r"\s+", " ", str(data.get("group") or "")).strip()
    group_id = str(data.get("group_id") or "").strip()
    if not valid_auth_token(token):
        return jsonify(success=False, message="رمز الجلسة غير صالح."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة ٤ إلى ٨ أرقام."), 400
    if not group_name or not valid_group_id(group_id):
        return jsonify(success=False, message="اختر قروبًا."), 400
    wait_seconds = parse_seconds(data.get("wait_seconds"), MAX_WATCH_WAIT_SECONDS)
    if wait_seconds is None:
        return jsonify(success=False, message=f"الانتظار بعد دخول القروب بالثواني من 0 إلى {MAX_WATCH_WAIT_SECONDS}."), 400
    thread = watch_thread
    with state_lock:
        running = bool(state["watching"] or state.get("checking") or state.get("text_watch"))
    if running or (thread and thread.is_alive()):
        return jsonify(success=False, message="المهمة تعمل الآن. انتظر حتى تنتهي."), 409
    abort_flag.clear()
    stop_flag.clear()
    with state_lock:
        state["auth_token"] = token
        state["chat_pin"] = pin
        state["group"] = group_name
        state["group_id"] = group_id
        state["wait_seconds"] = wait_seconds
        state["watching"] = True
        state["waiting"] = False
        state["text_watch"] = False
        state["text_left"] = 0
        state["boundary_status"] = ""
        state["ok"] = True
        state["items"] = []
        state["replies"] = []
        write_message(f"جارٍ فتح «{group_name}»…", True)
    save_state()
    stop_flag.clear()
    watch_thread = threading.Thread(target=watch_loop, args=(token, pin, group_id, group_name, wait_seconds), daemon=True)
    watch_thread.start()
    if wait_seconds:
        return jsonify(success=True, message=f"بدأت مراقبة «{group_name}». سينتظر {wait_seconds} ث بعد دخول القروب ثم يلتقط البداية.")
    return jsonify(success=True, message=f"بدأت مراقبة «{group_name}».")


@app.post("/watch/stop")
def watch_stop():
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    with state_lock:
        if state.get("text_watch"):
            phase = "text"
        elif not state.get("watching"):
            phase = "idle"
        else:
            phase = "stop"
            waiting = bool(state.get("waiting"))
            stop_flag.set()
            if waiting:
                message = STOPPED_DURING_WAIT
            else:
                message = "توقفت المراقبة. يتم فحص التغريدات، ثم تسجيل الردود من آخر تغريدة لمدة 30 د."
                state["checking"] = True
            write_message(message, True)
    if phase == "text":
        return jsonify(success=False, message="جارٍ تسجيل الردود النصية لمدة 30 د. إيقاف المهمة يلغي الانتظار."), 409
    if phase == "idle":
        return jsonify(success=False, message="لا توجد مراقبة تعمل الآن."), 409
    return jsonify(success=True, message=message)


@app.post("/watch/abort")
def watch_abort():
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    thread = watch_thread
    with state_lock:
        running = bool(state["watching"] or state.get("checking") or state.get("text_watch"))
    if not running and not (thread and thread.is_alive()):
        return jsonify(success=False, message="لا توجد مهمة تعمل الآن."), 409
    abort_flag.set()
    stop_flag.set()
    set_status("يتم إيقاف المهمة…", True, True)
    return jsonify(success=True, message="يتم إيقاف المهمة.")


def set_likes_message(message: str, ok: bool = True) -> None:
    with state_lock:
        state["likes_message"] = message
        state["likes_ok"] = ok


def publish_scan(members: list[dict], likers: list[dict]) -> None:
    wanted = {
        str(member.get("handle") or "").lstrip("@").lower()
        for member in members
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", str(member.get("handle") or "").lstrip("@"))
    }
    found = sum(1 for person in likers if str(person.get("handle") or "").lstrip("@").lower() in wanted)
    results = judge_likes(members, likers, False)
    with state_lock:
        state["likes_results"] = results
        state["likes_ok"] = True
        if wanted:
            state["likes_message"] = f"يجمع الإعجابات… وُجد {found} من {len(wanted)}."
        else:
            state["likes_message"] = f"يجمع الإعجابات… {len(likers)}."


ACCOUNT_HANDLE_JS = r"""
() => {
  const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","intent","share","status"]);
  const links = [
    document.querySelector('a[data-testid="AppTabBar_Profile_Link"]'),
    document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]'),
  ];
  for (const link of links) {
    if (!link) continue;
    const href = link.getAttribute("href") || "";
    const match = href.match(/^\/([A-Za-z0-9_]{1,15})\/?$/);
    if (match && !reserved.has(match[1].toLowerCase())) return match[1];
  }
  return "";
}
"""


def logged_in_handle(page) -> str:
    try:
        handle = str(page.evaluate(ACCOUNT_HANDLE_JS) or "").lstrip("@")
    except Exception:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return handle
    return ""


def read_likers(page) -> list[dict] | None:
    try:
        payload = page.evaluate(LIKES_READ_JS)
    except Exception:
        return None
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def scroll_likes(page) -> dict:
    try:
        payload = page.evaluate(LIKES_SCROLL_JS) or {}
    except Exception:
        payload = {}
    try:
        page.mouse.wheel(0, 1400)
    except Exception:
        pass
    try:
        page.keyboard.press("PageDown")
    except Exception:
        pass
    return payload if isinstance(payload, dict) else {}


# Page height while the likes list is open, so most likers show without scrolling.
LIKES_VIEWPORT_HEIGHT = 20000
# Page height while a group is open for watching. More of the chat stays on the page, so a
# burst of new tweets (or the catch-up after a reload) is read before it scrolls out of view.
WATCH_VIEWPORT_HEIGHT = 3000


def open_likes_page(page, status_id: str, handle: str = "") -> str | None:
    """Open the likes list directly at https://x.com/<user>/status/<id>/likes.
    When the pasted link has no account name (only the number), the tweet is opened
    once to learn it, then the likes list is opened the same way."""
    if not ensure_logged_in(page):
        return "الجلسة غير مسجّلة الدخول. تحقق من توكن الجلسة."
    if not handle:
        page.goto(f"https://x.com/i/web/status/{status_id}", wait_until="domcontentloaded", timeout=60_000)
        try:
            page.locator('article[data-testid="tweet"]').first.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError:
            if "login" in (page.url or "") or "/flow/" in (page.url or ""):
                return "تعذر الدخول إلى الحساب. تحقق من توكن الجلسة."
            return "تعذر فتح التغريدة."
        handle = tweet_handle_of(page.url)
        if not handle:
            # The address did not change to /<user>/status/<id>: read the name from the tweet's own link.
            try:
                href = page.evaluate(
                    """(id) => {
                      const link = [...document.querySelectorAll('article[data-testid="tweet"] a[href*="/status/' + id + '"]')]
                        .find((node) => /^\\/[A-Za-z0-9_]{1,15}\\/status\\//.test(node.getAttribute("href") || ""));
                      return link ? link.getAttribute("href") : "";
                    }""",
                    status_id,
                ) or ""
            except Exception:
                href = ""
            handle = tweet_handle_of("https://x.com" + href) if href else ""
        if not handle:
            return "تعذر معرفة صاحب التغريدة. الصق رابط التغريدة كاملًا مع اسم الحساب."
    width = (page.viewport_size or {}).get("width") or 1280
    page.set_viewport_size({"width": width, "height": LIKES_VIEWPORT_HEIGHT})
    page.goto(f"https://x.com/{handle}/status/{status_id}/likes", wait_until="domcontentloaded", timeout=60_000)
    try:
        # Wait for the likers list (or X's empty notice) so a slow page is not read as "nobody liked".
        page.locator('[data-testid="UserCell"], [data-testid="emptyState"]').first.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError:
        if "login" in (page.url or "") or "/flow/" in (page.url or ""):
            return "تعذر الدخول إلى الحساب. تحقق من توكن الجلسة."
        return "تعذر فتح صفحة الإعجابات. تأكد أن الحساب المستخدم هو صاحب التغريدة."
    page.wait_for_timeout(900)
    return None


def collect_likers(page, members: list[dict], wanted: set[str], need_full: bool) -> tuple[list[dict], bool]:
    collected: dict[str, dict] = {}
    stall = 0
    last_size = -1
    for step in range(400):
        batch = read_likers(page)
        if batch is None:
            stall += 1
        else:
            for person in batch:
                handle = str(person.get("handle") or "").lstrip("@")
                if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                    continue
                collected[handle.lower()] = {
                    "handle": handle,
                    "name": str(person.get("name") or "")[:80],
                }
        found = sum(1 for handle in wanted if handle in collected)
        likers = list(collected.values())
        if step % 4 == 0:
            publish_scan(members, likers)
        if wanted and not need_full and found >= len(wanted):
            publish_scan(members, likers)
            return likers, True
        info = scroll_likes(page)
        size = len(collected)
        moved = bool(info.get("moved"))
        at_end = bool(info.get("atEnd"))
        if size == last_size and (at_end or not moved):
            stall += 1
        else:
            stall = 0
        last_size = size
        try:
            page.wait_for_timeout(380)
        except Exception:
            return list(collected.values()), False
        if stall >= 4:
            return list(collected.values()), True
    return list(collected.values()), False


def close_quietly(obj) -> None:
    try:
        if obj is not None:
            obj.close()
    except Exception:
        pass


def likes_job(auth_token: str, status_id: str, members: list[dict], tweet_handle: str = "") -> None:
    playwright = None
    browser = None
    context = None
    try:
        set_likes_message("جارٍ فتح متصفح جديد والدخول إلى الحساب…")
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
        )
        context, page = open_x_context(browser, auth_token)
        set_likes_message("جارٍ فتح صفحة إعجابات التغريدة…")
        error = open_likes_page(page, status_id, tweet_handle)
        if error:
            set_likes_message(error, False)
            return
        handle = logged_in_handle(page)
        if handle:
            remember_likes_account(auth_token, handle)
            save_state()
            set_likes_message(f"تم الدخول @{handle}. جارٍ فتح الإعجابات…")
        wanted = {
            str(member.get("handle") or "").lstrip("@").lower()
            for member in members
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", str(member.get("handle") or "").lstrip("@"))
        }
        need_full = any(
            not re.fullmatch(r"[A-Za-z0-9_]{1,15}", str(member.get("handle") or "").lstrip("@"))
            for member in members
        )
        likers, complete = collect_likers(page, members, wanted, need_full)
        results = judge_likes(members, likers, complete)
        summary, ok = likes_summary(results, complete)
        with state_lock:
            state["likes_results"] = results
            state["likes_message"] = summary
            state["likes_ok"] = ok
        save_state()
    except Exception as exc:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with (DATA_DIR / "browser.log").open("a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%d %H:%M:%S likes ") + repr(exc) + "\n")
        except Exception:
            pass
        set_likes_message("تعذر فحص الإعجابات. أعد المحاولة.", False)
    finally:
        close_quietly(context)
        close_quietly(browser)
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass
        with state_lock:
            state["likes_checking"] = False


@app.post("/likes/check")
def likes_check():
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    token = str(data.get("auth_token") or "").strip()
    account_id = str(data.get("account_id") or "").strip()
    tweet = re.sub(r"\s+", "", str(data.get("tweet") or ""))
    if not token and account_id:
        token = token_for_account(account_id)
    if not valid_auth_token(token):
        return jsonify(success=False, message="اختر حسابًا محفوظًا أو أدخل توكنًا صالحًا."), 400
    status_id = status_id_of(tweet)
    if not status_id:
        return jsonify(success=False, message="أدخل رابط التغريدة أو رقمها."), 400
    account_id = remember_likes_account(token)
    with state_lock:
        if state.get("likes_checking"):
            busy = True
            members = []
        else:
            busy = False
            members = members_for_likes(list(state["replies"]))
            if members:
                state["likes_checking"] = True
                state["likes_token"] = token
                state["likes_account_id"] = account_id
                state["likes_tweet"] = tweet
                state["likes_status"] = status_id
                state["likes_results"] = []
                state["likes_ok"] = True
                state["likes_message"] = "بدأ الفحص في متصفح مستقل. تحديث الصفحة لا يوقفه."
    save_state()
    if busy:
        return jsonify(success=False, message="فحص الإعجابات يعمل الآن. تغريدة واحدة في كل مرة.", account_id=account_id), 409
    if not members:
        return jsonify(success=False, message="لا يوجد أعضاء أرسلوا تم أو f أو F بعد.", account_id=account_id), 400
    threading.Thread(target=likes_job, args=(token, status_id, members, tweet_handle_of(tweet)), daemon=True).start()
    return jsonify(success=True, message="بدأ فحص الإعجابات. تحديث الصفحة يُظهر التقدّم من حيث هو.", account_id=account_id)


load_state()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5100"))
    app.run(host=os.getenv("BIND_HOST", "0.0.0.0"), port=port, threaded=True)
