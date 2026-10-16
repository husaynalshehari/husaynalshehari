#!/usr/bin/env python3
"""Watch one X group chat and record every new tweet with the account that sent it."""
from __future__ import annotations

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
    for _ in range(6):
        try:
            payload = page.evaluate(READ_GROUPS_JS, wanted) or {}
        except Exception:
            payload = {}
        if payload.get("clicked"):
            current = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip() or wanted["name"]
            deadline = time.monotonic() + 12
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
        page.wait_for_timeout(400)
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


def verify_captured(page, anchor_ids: set[str], run_id: str, reopen=None) -> None:
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
        set_status(
            f"التحقق انتهى. {len(found)} موجودة و{len(missing)} حُذفت من القروب."
            + (" الصفحة عادت لنقطة بداية المراقبة." if reached else ""),
            True,
            False,
        )
    else:
        set_status(
            f"التحقق انتهى. كل التغريدات ({len(found)}) ما زالت في القروب."
            + (" الصفحة عادت لنقطة بداية المراقبة." if reached else ""),
            True,
            False,
        )


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


def watch_loop(auth_token: str, pin: str, group_id: str, group_name: str, wait_seconds: float = 0.0) -> None:
    run_id = secrets.token_hex(4)
    anchor_ids: set[str] = set()
    current = {"name": group_name}

    def open_watched_group(page) -> None:
        """Open the group by its id. If it was renamed, keep and show the new name."""
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
                verify_captured(page, anchor_ids, run_id, reopen)
                if abort_flag.is_set():
                    set_status("تم إيقاف المهمة.", True, False)
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

    try:
        run_on_browser(auth_token, work)
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
      <p class="sub">يفتح القروب ويبقى داخله. أي تغريدة جديدة تُحفظ مع الحساب الذي أرسلها.</p>
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
      busy = !!(data.watching || data.checking);
      if (document.activeElement !== token && data.auth_token && token.readOnly) token.value = data.auth_token;
      if (document.activeElement !== pin && data.chat_pin) pin.value = data.chat_pin;
      if (!waitLoaded) {
        wait.value = data.wait_seconds ? String(data.wait_seconds) : "";
        waitLoaded = true;
      }
      wait.disabled = busy;
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
      if (!noticeHold && Number(data.rev || 0) > seenRev && data.message) {
        seenRev = Number(data.rev || 0);
        show(data.message, !!data.ok);
      }
      stop.disabled = !data.watching || !!data.checking;
      stop.textContent = data.checking ? "جارٍ التحقق…" : "إيقاف المراقبة";
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
    if state["watching"] or state.get("checking"):
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
        running = bool(state["watching"] or state.get("checking"))
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
        state["ok"] = True
        state["items"] = []
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
    if not state.get("watching"):
        return jsonify(success=False, message="لا توجد مراقبة تعمل الآن."), 409
    with state_lock:
        waiting = bool(state.get("waiting"))
        stop_flag.set()
        if waiting:
            message = STOPPED_DURING_WAIT
        else:
            message = "توقفت المراقبة. يتم فحص التغريدات المحذوفة."
            state["checking"] = True
        write_message(message, True)
    return jsonify(success=True, message=message)


@app.post("/watch/abort")
def watch_abort():
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    thread = watch_thread
    with state_lock:
        running = bool(state["watching"] or state.get("checking"))
    if not running and not (thread and thread.is_alive()):
        return jsonify(success=False, message="لا توجد مهمة تعمل الآن."), 409
    abort_flag.set()
    stop_flag.set()
    set_status("يتم إيقاف المهمة…", True, True)
    return jsonify(success=True, message="يتم إيقاف المهمة.")


load_state()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host=os.getenv("BIND_HOST", "0.0.0.0"), port=port, threaded=True)
