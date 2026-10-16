#!/usr/bin/env python3
"""Watch one X group chat and record every new tweet with the account that sent it."""
from __future__ import annotations

import hmac
import json
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
    const target = clean(wanted);
    for (const row of rows) {
      if (nameOf(row) !== target) continue;
      const link = row.querySelector('a[href*="/i/chat/"], a[href*="/messages/"]');
      if (link) link.click();
      else row.click();
      return {clicked: true, groups: [], rows: rows.length, loading: false};
    }
    return {clicked: false, groups: [], rows: rows.length, loading: false};
  }
  const groups = [];
  const seen = new Set();
  for (const row of rows) {
    const id = row.getAttribute("data-testid") || "";
    const desc = row.getAttribute("aria-description") || "";
    const isGroup = /dm-conversation-item-g\d/.test(id) || /جماعية|group chat/i.test(desc);
    if (!isGroup) continue;
    const name = nameOf(row);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    groups.push(name);
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


def collect_groups(page) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    idle = 0
    for _ in range(18):
        try:
            payload = page.evaluate(READ_GROUPS_JS, "") or {}
        except Exception:
            payload = {}
        added = 0
        for name in payload.get("groups") or []:
            clean = re.sub(r"\s+", " ", str(name)).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            found.append(clean)
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


def open_named_group(page, name: str) -> None:
    wanted = re.sub(r"\s+", " ", name).strip()
    for _ in range(6):
        try:
            payload = page.evaluate(READ_GROUPS_JS, wanted) or {}
        except Exception:
            payload = {}
        if payload.get("clicked"):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    state = page.evaluate(GROUP_CHAT_SCROLL_JS, "state") or {}
                except Exception:
                    state = {}
                if state.get("hasList") and state.get("messages"):
                    page.wait_for_timeout(800)
                    return
                page.wait_for_timeout(300)
        try:
            page.evaluate(SCROLL_INBOX_JS)
        except Exception:
            pass
        page.wait_for_timeout(400)
    raise RuntimeError("تم البحث عن القروب لكن قائمة الرسائل لم تُفتح.")



def valid_auth_token(value: str) -> bool:
    return 20 <= len(value) <= 512

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
    "watching": False,
    "message": "اختر قروبًا لتبدأ المراقبة.",
    "ok": True,
    "checking": False,
    "scanning": False,
    "audits": [],
    "no_fire": [],
    "no_fire_ready": False,
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
        state["groups"] = []
        items = loaded.get("items")
        if isinstance(items, list):
            state["items"] = [item for item in items if isinstance(item, dict)][-MAX_ITEMS:]
        audits = loaded.get("audits")
        if isinstance(audits, list):
            state["audits"] = [row for row in audits if isinstance(row, dict)][-200:]
        no_fire = loaded.get("no_fire")
        if isinstance(no_fire, list):
            state["no_fire"] = [row for row in no_fire if isinstance(row, dict)][-200:]
            state["no_fire_ready"] = bool(loaded.get("no_fire_ready"))


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with state_lock:
        payload = {
            "auth_token": state["auth_token"],
            "chat_pin": state["chat_pin"],
            "group": state["group"],
            "groups": list(state["groups"]),
            "items": list(state["items"])[-MAX_ITEMS:],
            "audits": list(state.get("audits") or [])[-200:],
            "no_fire": list(state.get("no_fire") or [])[-200:],
            "no_fire_ready": bool(state.get("no_fire_ready")),
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
            "watching": state["watching"],
            "message": state["message"],
            "rev": int(state.get("rev") or 0),
            "ok": state["ok"],
            "checking": state.get("checking") or False,
            "scanning": state.get("scanning") or False,
            "count": len(state["items"]),
            "items": list(reversed(state["items"][-200:])),
            "audits": list(state.get("audits") or []),
            "no_fire": list(state.get("no_fire") or []),
            "no_fire_ready": bool(state.get("no_fire_ready")),
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


DONE_MARK = "✅✅✅"
STOP_MARK = "❌❌❌"


def with_mark(name: str, mark: str) -> str:
    clean = re.sub(r"\s+", " ", name).strip()
    if clean.endswith(mark):
        return clean
    if len(clean) <= 3:
        return mark
    return clean[:-3] + mark


FOCUS_NAME_EDITOR_JS = r"""
() => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let best = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll("input, textarea, [contenteditable='true']").forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.width < 40 || rect.left > 780 || rect.top < 360 || rect.top > 980) return;
      const value = el.value || el.innerText || "";
      if (!best || rect.top < best.top) best = {x: rect.x + 24, y: rect.y + rect.height / 2, top: rect.top, value: value.slice(0, 80)};
    });
  }
  return best;
}
"""

FIND_EDIT_BUTTON_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let dialog = null;
  let area = 0;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'], [aria-modal='true']")) {
      const rect = el.getBoundingClientRect();
      const size = rect.width * rect.height;
      if (rect.width > 180 && rect.height > 180 && size > area) { dialog = el; area = size; }
    }
  }
  const inside = (el) => {
    let node = el;
    while (node) {
      if (node === dialog) return true;
      const root = node.getRootNode ? node.getRootNode() : null;
      node = node.parentElement || (root && root.host) || null;
    }
    return false;
  };
  const editRe = /تعديل المجموعة|edit group|^تعديل$|edit group name/i;
  const looseRe = /تعديل|edit/i;
  let fallback = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("button, [role='button']")) {
      const label = clean((el.getAttribute("aria-label") || "") + " " + (el.innerText || ""));
      const icon = (el.querySelector("svg") && el.querySelector("svg").getAttribute("data-icon")) || "";
      const rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8) continue;
      const inDialog = inside(el);
      const exact = editRe.test(label) || /icon-edit|pencil|pen/i.test(icon);
      if (inDialog && exact) return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, label: label.slice(0, 80)};
      if (!fallback && inDialog && looseRe.test(label) && rect.top >= 0) {
        fallback = {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, label: label.slice(0, 80)};
      }
    }
  }
  return fallback;
}
"""

CLICK_SAVE_OR_BACK_JS = r"""
(action) => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const saveRe = /حفظ|تم|حفظ الاسم|save|done/i;
  const backRe = /رجوع|إغلاق|close|back/i;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("button")) {
      const label = ((el.getAttribute("aria-label") || "") + " " + (el.innerText || "")).replace(/\s+/g, " ").trim();
      const rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8) continue;
      if (action === "save" && saveRe.test(label)) { el.click(); return label; }
      if (action === "back" && backRe.test(label) && rect.top > 300 && rect.top < 700) { el.click(); return label; }
    }
  }
  return "";
}
"""

def close_group_info(page) -> None:
    try:
        if not page.evaluate(CLICK_SAVE_OR_BACK_JS, "back"):
            page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    page.wait_for_timeout(400)


def rename_group_mark(page, group_name: str, mark: str = DONE_MARK) -> tuple[str, str]:
    new_name = with_mark(group_name, mark)
    already = new_name == re.sub(r"\s+", " ", group_name).strip()
    page.locator('[data-testid="dm-conversation-more-button"]').first.click(timeout=5000)
    page.wait_for_timeout(700)
    edit = None
    for _ in range(4):
        edit = page.evaluate(FIND_EDIT_BUTTON_JS)
        if edit:
            break
        page.wait_for_timeout(300)
    if not edit:
        close_group_info(page)
        return group_name, "skipped"
    if already:
        close_group_info(page)
        return group_name, "same"
    page.mouse.click(edit["x"], edit["y"])
    page.wait_for_timeout(500)
    editor = page.evaluate(FOCUS_NAME_EDITOR_JS)
    if not editor:
        close_group_info(page)
        return group_name, "failed"
    page.mouse.click(editor["x"], editor["y"])
    page.keyboard.press("Control+A")
    page.keyboard.insert_text(new_name)
    page.wait_for_timeout(250)
    if not page.evaluate(CLICK_SAVE_OR_BACK_JS, "save"):
        page.keyboard.press("Enter")
    page.wait_for_timeout(900)
    close_group_info(page)
    return new_name, "renamed"


def rename_group_twice(page, group_name: str, mark: str = DONE_MARK) -> tuple[str, str]:
    last_name, last_reason = group_name, "failed"
    for attempt in range(2):
        if abort_flag.is_set():
            return group_name, "aborted"
        try:
            last_name, last_reason = rename_group_mark(page, group_name, mark)
        except Exception:
            last_name, last_reason = group_name, "failed"
        if last_reason in {"renamed", "same"}:
            return last_name, last_reason
        if attempt == 0:
            set_status("لم تنجح محاولة تغيير الاسم. محاولة ثانية…", True, True)
            page.wait_for_timeout(700)
    return last_name, last_reason


def participant_count(run_id: str) -> int:
    with state_lock:
        people = set()
        for item in state["items"]:
            if item.get("run_id") != run_id:
                continue
            handle = str(item.get("handle") or "").strip().lower()
            name = str(item.get("name") or "").strip()
            key = handle or name
            if key:
                people.add(key)
        return len(people)


def participants_without_fire(run_id: str, reacted: list[dict]) -> list[dict]:
    reacted_handles = {str(person.get("handle") or "").strip().lower() for person in reacted}
    reacted_handles.discard("")
    reacted_names = {str(person.get("name") or "").strip().casefold() for person in reacted}
    reacted_names.discard("")
    with state_lock:
        people: dict[str, dict] = {}
        for item in state["items"]:
            if item.get("run_id") != run_id:
                continue
            handle = str(item.get("handle") or "").strip().lstrip("@")
            name = str(item.get("name") or "").strip()
            key = handle.lower() if handle else name.casefold()
            if not key or key in people:
                continue
            people[key] = {"handle": handle, "name": name}
    missing = []
    for person in people.values():
        handle = person["handle"].lower()
        name = person["name"].casefold()
        if handle and handle in reacted_handles:
            continue
        if name and name in reacted_names:
            continue
        missing.append(person)
    return missing


def remember_renamed_group(old_name: str, new_name: str) -> None:
    with state_lock:
        state["group"] = new_name
        state["groups"] = [new_name if name == old_name else name for name in state["groups"]]
    save_state()


def send_group_message(page, text: str) -> None:
    if "/info" in (page.url or ""):
        close_group_info(page)
    try:
        page.evaluate(GROUP_CHAT_SCROLL_JS, "latest")
    except Exception:
        pass
    box = page.locator('[data-testid="dm-composer-textarea"]').first
    box.click(timeout=5000)
    page.keyboard.insert_text(text)
    page.wait_for_timeout(250)
    page.locator('[data-testid="dm-composer-send-button"]').first.click(timeout=5000)
    page.wait_for_timeout(500)


def close_monitoring(page, group_name: str, run_id: str) -> str:
    if abort_flag.is_set():
        return group_name
    set_status("يتم تغيير آخر ٣ حروف إلى ❌❌❌…", True, True)
    renamed, reason = rename_group_twice(page, group_name, STOP_MARK)
    if reason == "renamed" and renamed != group_name:
        remember_renamed_group(group_name, renamed)
        group_name = renamed
    count = participant_count(run_id)
    message = f"اشوف التم\n\nشراره({count})"
    set_status(f"يتم إرسال الرسالة: شراره({count})…", True, True)
    try:
        send_group_message(page, message)
    except Exception:
        set_status("تعذر إرسال رسالة الإيقاف. سيبدأ فحص المحذوف.", False, True)
    return group_name


FIRE_WINDOW_SECONDS = 30 * 60
REPOST_LIMIT = 100

CLICK_FIRE_PILL_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const bubbles = [];
  const buttons = [];
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll('[data-testid^="message-"]').forEach((el) => {
      const id = el.getAttribute("data-testid") || "";
      if (!/^message-[0-9a-f-]{16,}$/i.test(id)) return;
      const text = clean(el.innerText || "");
      if (!text.includes("اشوف التم") || !/شراره\s*\(/.test(text)) return;
      const rect = el.getBoundingClientRect();
      if (rect.width < 20 || rect.left > 780 || rect.bottom < 0 || rect.top > 1800) return;
      bubbles.push({top: rect.top, bottom: rect.bottom});
    });
    root.querySelectorAll("button").forEach((el) => {
      const label = clean((el.getAttribute("aria-label") || "") + " " + (el.innerText || ""));
      if (!label.includes("🔥") || !/تفاعل/.test(label)) return;
      const rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.left > 780) return;
      buttons.push({el, y: rect.y + rect.height / 2, top: rect.top, bottom: rect.bottom});
    });
  }
  if (!bubbles.length) return {ok: false, reason: "message"};
  bubbles.sort((a, b) => b.bottom - a.bottom);
  const bubble = bubbles[0];
  let best = null;
  let bestDist = 1e9;
  for (const button of buttons) {
    if (button.bottom < bubble.top - 12 || button.top > bubble.bottom + 64) continue;
    const dist = Math.abs(button.y - bubble.bottom);
    if (dist < bestDist) {
      best = button;
      bestDist = dist;
    }
  }
  if (!best) return {ok: false, reason: "pill"};
  best.el.click();
  return {ok: true};
}
"""

READ_FIRE_REACTORS_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","intent","share","web","tos","privacy"]);
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const handleOf = (href) => {
    const abs = (href || "").match(/https?:\/\/(?:www\.)?(?:x|twitter)\.com\/([A-Za-z0-9_]{1,15})\/?$/i);
    const rel = (href || "").match(/^\/([A-Za-z0-9_]{1,15})\/?$/);
    return (abs && abs[1]) || (rel && rel[1]) || "";
  };
  const overlay = (el) => {
    let node = el;
    for (let depth = 0; depth < 10 && node; depth += 1) {
      const role = node.getAttribute && node.getAttribute("role");
      if (role === "dialog" || (node.getAttribute && node.getAttribute("aria-modal") === "true")) return true;
      const style = node.nodeType === 1 ? getComputedStyle(node) : null;
      if (style && (style.position === "fixed" || style.position === "absolute")) return true;
      node = node.parentElement || (node.parentNode && node.parentNode.host);
    }
    return false;
  };
  const people = [];
  const seen = new Set();
  const take = (el) => {
    if (!el.querySelectorAll) return;
    el.querySelectorAll("a[href]").forEach((link) => {
      const handle = handleOf(link.getAttribute("href") || "");
      if (!handle || reserved.has(handle.toLowerCase()) || seen.has(handle.toLowerCase())) return;
      const rect = link.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8) return;
      seen.add(handle.toLowerCase());
      people.push({handle, name: clean(link.innerText || "").slice(0, 60)});
    });
  };
  let dialog = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll("[role='dialog'], [aria-modal='true']").forEach((el) => {
      const rect = el.getBoundingClientRect();
      const text = el.innerText || "";
      if (rect.width < 80 || rect.height < 40 || rect.width > 760) return;
      if (!/تفاعل|🔥/.test(text)) return;
      if (!dialog || rect.height < dialog.getBoundingClientRect().height) dialog = el;
    });
  }
  if (dialog) take(dialog);
  if (!people.length) {
    for (const root of roots) {
      if (!root.querySelectorAll) continue;
      root.querySelectorAll("a[href]").forEach((link) => {
        if (overlay(link)) take(link.parentElement || link);
      });
    }
  }
  return people;
}
"""

SCROLL_REACTOR_DIALOG_JS = r"""
() => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'], [aria-modal='true']")) {
      const text = el.innerText || "";
      if (!/تفاعل|🔥/.test(text)) continue;
      const boxes = [el, ...el.querySelectorAll("*")];
      for (const box of boxes) {
        if (box.scrollHeight > box.clientHeight + 24) {
          box.scrollTop += 240;
          return true;
        }
      }
    }
  }
  return false;
}
"""

COLLECT_REPOSTS_JS = r"""
() => {
  const ids = [];
  const seen = new Set();
  document.querySelectorAll("article[data-testid='tweet']").forEach((article) => {
    const time = article.querySelector("time");
    const link = time && time.closest("a");
    const href = (link && link.getAttribute("href")) || "";
    const match = href.match(/status\/(\d{6,25})/);
    if (!match || seen.has(match[1])) return;
    seen.add(match[1]);
    ids.push(match[1]);
  });
  return ids;
}
"""


def run_status_ids(run_id: str) -> list[str]:
    with state_lock:
        return [
            str(item.get("status_id") or "")
            for item in state["items"]
            if item.get("run_id") == run_id and item.get("status_id")
        ]


def scroll_latest(page) -> None:
    try:
        page.evaluate(GROUP_CHAT_SCROLL_JS, "latest")
    except Exception:
        pass
    page.wait_for_timeout(350)


def return_to_group(page, group_url: str, group_name: str) -> None:
    current = page.url or ""
    if group_url and "i/chat/" in group_url and group_url.split("?")[0] not in current:
        page.goto(group_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(700)
    if "i/chat/g" not in (page.url or ""):
        try:
            open_named_group(page, group_name)
        except Exception:
            pass
    scroll_latest(page)


def read_fire_reactors(page) -> list[dict]:
    scroll_latest(page)
    try:
        opened = page.evaluate(CLICK_FIRE_PILL_JS) or {}
    except Exception:
        return []
    if not opened.get("ok"):
        return []
    page.wait_for_timeout(700)
    people: dict[str, dict] = {}
    for _ in range(15):
        try:
            batch = page.evaluate(READ_FIRE_REACTORS_JS) or []
        except Exception:
            batch = []
        for person in batch:
            handle = str(person.get("handle") or "").lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                continue
            people[handle.lower()] = {"handle": handle, "name": str(person.get("name") or "")[:60]}
        try:
            moved = page.evaluate(SCROLL_REACTOR_DIALOG_JS)
        except Exception:
            moved = False
        if not moved:
            break
        page.wait_for_timeout(250)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(200)
    return list(people.values())


SCROLL_REPOSTS_JS = r"""
() => {
  const article = document.querySelector("article[data-testid='tweet']");
  let node = article && article.parentElement;
  while (node && node !== document.body) {
    if (node.scrollHeight > node.clientHeight + 80) {
      const before = node.scrollTop;
      node.scrollTop += 780;
      if (node.scrollTop > before + 40) return "inner";
    }
    node = node.parentElement;
  }
  const before = window.scrollY;
  window.scrollBy(0, 820);
  return window.scrollY > before + 40 ? "window" : "stuck";
}
"""

REPOSTS_LOADING_JS = r"""
() => !!document.querySelector('[role="progressbar"], [data-testid="cellInnerDiv"] [role="progressbar"]')
"""


def collect_repost_ids(page, handle: str, limit: int = REPOST_LIMIT, on_progress=None) -> list[str]:
    budget = time.monotonic() + 90
    page.goto(f"https://x.com/{handle}/reposts", wait_until="domcontentloaded", timeout=45_000)
    found: list[str] = []
    seen: set[str] = set()

    def report() -> None:
        if on_progress:
            on_progress(len(found))

    def absorb() -> int:
        if abort_flag.is_set() or time.monotonic() >= budget:
            return 0
        try:
            batch = page.evaluate(COLLECT_REPOSTS_JS) or []
        except Exception as exc:
            if browser_failure(exc) or not page_alive(page):
                raise
            return 0
        added = 0
        for status_id in batch:
            status_id = str(status_id)
            if not status_id or status_id in seen:
                continue
            seen.add(status_id)
            found.append(status_id)
            added += 1
            if len(found) >= limit:
                break
        return added

    opened = time.monotonic() + 8
    while time.monotonic() < opened and time.monotonic() < budget and not absorb():
        if abort_flag.is_set():
            return found[:limit]
        page.wait_for_timeout(400)
        report()
    empty_waits = 0
    while len(found) < limit and empty_waits < 4 and time.monotonic() < budget:
        if abort_flag.is_set():
            return found[:limit]
        before = len(found)
        try:
            moved = page.evaluate(SCROLL_REPOSTS_JS)
        except Exception as exc:
            if browser_failure(exc) or not page_alive(page):
                raise
            moved = "stuck"
        if moved == "stuck":
            empty_waits += 1
            page.wait_for_timeout(400)
            report()
            continue
        wait_until = min(time.monotonic() + 1.6, budget)
        while time.monotonic() < wait_until and len(found) < limit:
            try:
                loading = page.evaluate(REPOSTS_LOADING_JS)
            except Exception:
                loading = False
            absorb()
            report()
            if len(found) > before and not loading:
                break
            page.wait_for_timeout(250)
        if len(found) > before:
            empty_waits = 0
        else:
            empty_waits += 1
    return found[:limit]


def visible_messages(page) -> list[dict]:
    try:
        rows = page.evaluate(
            r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const rows = [];
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll('[data-testid^="message-"]').forEach((el) => {
      const id = el.getAttribute("data-testid") || "";
      if (!/^message-[0-9a-f-]{16,}$/i.test(id)) return;
      const rect = el.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 12 || rect.left > 780) return;
      rows.push({id, x: rect.x + Math.min(48, rect.width / 3), y: rect.y + 16, text: clean(el.innerText || "").slice(0, 80)});
    });
  }
  return rows;
}
"""
        ) or []
    except Exception:
        return []
    return rows


def delete_message_for_everyone(page, message_id: str, x: float, y: float) -> str:
    page.mouse.move(x, y)
    page.wait_for_timeout(250)
    try:
        opened = page.evaluate(
            r"""
(id) => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const suffix = String(id || "").replace(/^message-/, "");
  for (const root of roots) {
    const button = root.querySelector && root.querySelector('[data-testid="message-overflow-button-' + suffix + '"]');
    if (button) { button.click(); return true; }
  }
  return false;
}
""",
            message_id,
        )
    except Exception:
        opened = False
    if not opened:
        return "miss"
    page.wait_for_timeout(350)
    try:
        clicked = page.evaluate(
            r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const everyone = /حذف\s*لدى\s*الجميع|حذف\s*للجميع|delete for everyone/i;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("button, [role='menuitem']")) {
      const label = clean((el.innerText || "") + " " + (el.getAttribute("aria-label") || ""));
      if (!everyone.test(label) || /لدي|for you|for me/i.test(label)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 10) continue;
      el.click();
      return label.slice(0, 40);
    }
  }
  return "";
}
"""
        )
    except Exception:
        clicked = ""
    if not clicked:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return "unavailable"
    page.wait_for_timeout(400)
    try:
        page.evaluate(
            r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const confirm = /^(حذف لدى الجميع|حذف للجميع|حذف|تأكيد|نعم، حذف)$/i;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'] button, [aria-modal='true'] button")) {
      const label = clean(el.innerText || el.getAttribute("aria-label") || "");
      if (!confirm.test(label) || /لدي|إلغاء|cancel/i.test(label)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 20 || rect.top < 80) continue;
      el.click();
      return label;
    }
  }
  return "";
}
"""
        )
    except Exception:
        pass
    page.wait_for_timeout(500)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return "deleted"


def purge_arrived_messages(page, baseline: set[str], skipped: set[str]) -> int:
    deleted = 0
    for _ in range(8):
        rows = [
            row for row in visible_messages(page)
            if row["id"] not in baseline and row["id"] not in skipped
            and not ("اشوف التم" in row.get("text", "") and "شراره" in row.get("text", ""))
        ]
        if not rows:
            break
        row = rows[-1]
        result = delete_message_for_everyone(page, row["id"], row["x"], row["y"])
        if result == "deleted":
            baseline.add(row["id"])
            deleted += 1
            continue
        if result == "unavailable":
            skipped.add(row["id"])
            continue
        break
    return deleted


def record_audit(row: dict) -> None:
    with state_lock:
        audits = state.setdefault("audits", [])
        audits.append(row)
        if len(audits) > 200:
            del audits[:-200]
    save_state()


def audit_fire_reposts(page, run_id: str, group_name: str, reopen=None):
    group_url = page.url or ""
    if "i/chat/g" not in group_url:
        group_url = ""
    wanted = run_status_ids(run_id)
    wanted_set = set(wanted)
    with state_lock:
        state["checking"] = True
        state["audits"] = []
        state["no_fire"] = []
        state["no_fire_ready"] = False
    save_state()
    deadline = time.monotonic() + FIRE_WINDOW_SECONDS
    seen: set[str] = set()
    reacted: list[dict] = []
    pending: list[dict] = []
    checked = 0
    short = 0
    removed = 0
    final_scan = False
    baseline: set[str] = set()
    skipped: set[str] = set()
    try:
        return_to_group(page, group_url, group_name)
        baseline.update(row["id"] for row in visible_messages(page))
    except Exception:
        pass
    while (pending or not final_scan) and not abort_flag.is_set():
        if not page_alive(page):
            if reopen is None:
                break
            page = reopen()
            continue
        if not final_scan:
            try:
                return_to_group(page, group_url, group_name)
                removed += purge_arrived_messages(page, baseline, skipped)
                reactors = read_fire_reactors(page)
            except Exception as exc:
                if browser_failure(exc) or not page_alive(page):
                    if reopen is None:
                        break
                    page = reopen()
                    continue
                reactors = []
            if not group_url and "i/chat/g" in (page.url or ""):
                group_url = page.url
            for person in reactors:
                key = person["handle"].lower()
                if key in seen:
                    continue
                seen.add(key)
                reacted.append(person)
                pending.append(person)
            if time.monotonic() >= deadline:
                final_scan = True
        if not pending:
            if final_scan:
                break
            left = max(0, int(deadline - time.monotonic()))
            note = f" حُذفت {removed} رسالة." if removed else ""
            set_status(f"بانتظار تفاعل 🔥… تبقى {left // 60} د. فُحص {checked}.{note}", True, True)
            page.wait_for_timeout(2000)
            if abort_flag.is_set():
                break
            try:
                removed += purge_arrived_messages(page, baseline, skipped)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            continue
        if abort_flag.is_set():
            break
        person = pending.pop(0)
        handle = person["handle"]
        left = max(0, int(deadline - time.monotonic()))
        set_status(f"يفتح إعادات @{handle}… تبقى {left // 60} د. في الانتظار {len(pending)}.", True, True)
        missing: list[str] = []
        error = ""
        try:
            def on_progress(count: int) -> None:
                remain = max(0, int(deadline - time.monotonic()))
                set_status(
                    f"يفحص إعادات @{handle}… {count}/100. تبقى {remain // 60} د. في الانتظار {len(pending)}.",
                    True,
                    True,
                )
            reposts = collect_repost_ids(page, handle, on_progress=on_progress)
            repost_set = set(reposts)
            missing = [status_id for status_id in wanted if status_id not in repost_set]
        except Exception as exc:
            if browser_failure(exc) or not page_alive(page):
                attempt = int(person.get("tries") or 0) + 1
                if attempt < 2 and reopen is not None:
                    person["tries"] = attempt
                    pending.insert(0, person)
                    page = reopen()
                    continue
            error = "تعذر فتح المنشورات المُعاد نشرها"
            missing = list(wanted)
        if abort_flag.is_set():
            break
        checked += 1
        if missing:
            short += 1
        record_audit({
            "handle": handle,
            "name": person.get("name") or "",
            "wanted": len(wanted),
            "missing": missing[:100],
            "error": error,
        })
    if abort_flag.is_set():
        with state_lock:
            state["checking"] = False
        set_status("تم إيقاف المهمة.", True, False)
        return
    absent = participants_without_fire(run_id, reacted)
    with state_lock:
        state["checking"] = False
        state["no_fire"] = absent
        state["no_fire_ready"] = True
    save_state()
    if checked == 0:
        extra = f" حُذفت {removed} رسالة وصلت أثناء الانتظار." if removed else ""
        set_status(f"انتهت مهلة ٣٠ دقيقة. {len(absent)} لم يضعوا الشعلة." + extra, True, False)
    else:
        extra = f" وحُذفت {removed} رسالة وصلت أثناء الانتظار." if removed else ""
        set_status(
            f"انتهى فحص الشعلة. فُحص {checked} حسابًا، و{short} لديهم نقص. {len(absent)} لم يضعوا الشعلة.{extra}",
            True,
            False,
        )


RESET_INBOX_JS = r"""
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
      node.scrollTop = 0;
      return true;
    }
    node = node.parentElement;
  }
  return false;
}
"""

READ_MEMBERS_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","intent","share","web","tos","privacy"]);
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let dialog = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll("[role='dialog'], [aria-modal='true']").forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.width > 200 && rect.height > 200) dialog = el;
    });
  }
  if (!dialog) return {people: [], expected: 0, admin: false, syncing: false};
  const text = clean(dialog.innerText || "");
  const count = text.match(/(\d+)\s*من الأعضاء/) || text.match(/(\d+)\s*عضو/);
  let viewer = "";
  const profile = document.querySelector('a[data-testid="AppTabBar_Profile_Link"], a[data-testid="SideNav_AccountSwitcher_Button"]');
  const profileHref = (profile && profile.getAttribute("href")) || "";
  const profileMatch = profileHref.match(/\/([A-Za-z0-9_]{1,15})\/?$/);
  if (profileMatch) viewer = profileMatch[1].toLowerCase();
  let admin = false;
  const adminRe = /تعديل المجموعة|edit group|^تعديل$|edit group name/i;
  const badgeRe = /(^|\s)(مسؤول|admin)(\s|$)/i;
  const consider = (el) => {
    const label = clean((el.getAttribute("aria-label") || "") + " " + (el.innerText || ""));
    const icon = (el.querySelector("svg") && el.querySelector("svg").getAttribute("data-icon")) || "";
    if (adminRe.test(label) || /icon-edit/i.test(icon)) admin = true;
  };
  const walkDialog = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("button, [role='button']").forEach(consider);
    root.querySelectorAll("*").forEach((node) => { if (node.shadowRoot) walkDialog(node.shadowRoot); });
  };
  walkDialog(dialog);
  const people = [];
  const seen = new Set();
  const reservedHit = (handle) => !handle || reserved.has(String(handle).toLowerCase());
  const handleOf = (href) => {
    const value = href || "";
    const abs = value.match(/https?:\/\/(?:www\.)?(?:x|twitter)\.com\/([A-Za-z0-9_]{1,15})\/?$/i);
    const rel = value.match(/^\/([A-Za-z0-9_]{1,15})\/?$/i);
    const nested = value.match(/(?:x\.com|^)\/([A-Za-z0-9_]{1,15})\/?$/i);
    const handle = (abs && abs[1]) || (rel && rel[1]) || (nested && nested[1]) || "";
    return reservedHit(handle) ? "" : handle;
  };
  const idOf = (href) => {
    const match = (href || "").match(/\/i\/user\/(\d{4,22})(?:\/|\?|$)/i);
    return match ? match[1] : "";
  };
  const atOf = (text) => {
    const match = clean(text).match(/@([A-Za-z0-9_]{1,15})\b/);
    return match && !reservedHit(match[1]) ? match[1] : "";
  };
  const suspendedRe = /حساب موقوف|الحساب موقوف|تم إيقاف الحساب|تم ايقاف الحساب|تم إيقاف|تم ايقاف|account suspended|suspended account|this account has been suspended/i;
  const isMore = (el) => {
    const label = (el.getAttribute && el.getAttribute("aria-label")) || "";
    return label.startsWith("المزيد من الخيارات") || /^more options/i.test(label);
  };
  const collectDeep = (root, selector, into) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(selector).forEach((node) => into.push(node));
    root.querySelectorAll("*").forEach((node) => { if (node.shadowRoot) collectDeep(node.shadowRoot, selector, into); });
  };
  const links = [];
  const buttons = [];
  collectDeep(dialog, "a[href]", links);
  collectDeep(dialog, "button, [role='button']", buttons);
  const describe = (node, labelText) => {
    let handle = atOf(labelText || "");
    let userId = "";
    let name = "";
    let suspended = suspendedRe.test(labelText || "");
    let current = node;
    for (let depth = 0; depth < 7 && current && current !== dialog; depth += 1) {
      const text = clean(current.innerText || "");
      if (text.length > 260) break;
      if (!handle) handle = atOf(text);
      const bits = [text];
      if (current.querySelectorAll) {
        current.querySelectorAll("[aria-label], [title], img[alt]").forEach((el) => {
          bits.push(el.getAttribute("aria-label") || "", el.getAttribute("title") || "", el.getAttribute("alt") || "");
        });
      }
      if (text.length <= 160 && suspendedRe.test(clean(bits.join(" ")))) suspended = true;
      const inner = [];
      collectDeep(current, "a[href]", inner);
      let profiles = 0;
      for (const link of inner) {
        const href = link.getAttribute("href") || "";
        const foundHandle = handleOf(href);
        const foundId = idOf(href);
        if (foundHandle || foundId) profiles += 1;
        if (!handle && foundHandle) handle = foundHandle;
        if (!userId && foundId) userId = foundId;
        if (!name) name = clean(link.innerText || "");
      }
      if (profiles > 1) break;
      if (handle || userId) break;
      current = current.parentElement || (current.getRootNode && current.getRootNode().host) || null;
    }
    return {handle, userId, name, suspended};
  };
  const pushPerson = (handle, userId, name, removable, suspended) => {
    handle = reservedHit(handle) ? "" : String(handle || "");
    userId = /^\d{4,22}$/.test(String(userId || "")) ? String(userId) : "";
    if (!handle && !userId) return;
    const existing = people.find((item) =>
      (handle && item.handle && item.handle.toLowerCase() === handle.toLowerCase()) ||
      (userId && item.userId === userId)
    );
    if (existing) {
      existing.removable = existing.removable || !!removable;
      existing.suspended = existing.suspended || !!suspended;
      if (!existing.userId && userId) existing.userId = userId;
      if (!existing.handle && handle) existing.handle = handle;
      if (!existing.name && name) existing.name = clean(name).slice(0, 60);
      seen.add((existing.handle || "").toLowerCase() || ("id:" + existing.userId));
      return;
    }
    const key = handle.toLowerCase() || ("id:" + userId);
    if (seen.has(key)) return;
    seen.add(key);
    people.push({
      handle,
      userId,
      name: clean(name || "").slice(0, 60),
      removable: !!removable,
      suspended: !!suspended
    });
  };
  buttons.filter(isMore).forEach((button) => {
    const info = describe(button, button.getAttribute("aria-label") || "");
    if (viewer && info.handle && info.handle.toLowerCase() === viewer && badgeRe.test(clean(button.parentElement && button.parentElement.innerText || ""))) admin = true;
    pushPerson(info.handle, info.userId, info.name, true, info.suspended);
  });
  links.forEach((link) => {
    const href = link.getAttribute("href") || "";
    const handle = handleOf(href);
    const userId = idOf(href);
    if (!handle && !userId) return;
    if (viewer && handle && handle.toLowerCase() === viewer) {
      let node = link;
      for (let depth = 0; depth < 4 && node; depth += 1) {
        const rowText = clean(node.innerText || "");
        if (rowText && rowText.length < 80 && badgeRe.test(rowText)) admin = true;
        node = node.parentElement;
      }
    }
    const info = describe(link, link.innerText || "");
    pushPerson(handle || info.handle, userId || info.userId, info.name || link.innerText, false, info.suspended || suspendedRe.test(link.innerText || ""));
  });
  const unnamedSuspended = buttons.filter(isMore).filter((button) => {
    const info = describe(button, button.getAttribute("aria-label") || "");
    return info.suspended && !info.handle && !info.userId;
  }).length;
  return {people, expected: count ? Number(count[1]) : 0, admin, syncing: text.includes("قيد التقدم"), unnamedSuspended};
}
"""

SCROLL_MEMBERS_JS = r"""
(action) => {
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let dialog = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'], [aria-modal='true']")) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 180 && rect.height > 180) dialog = el;
    }
  }
  if (!dialog) return false;
  const boxes = [];
  const take = (box) => {
    if (!box || boxes.includes(box) || box.clientHeight < 80) return;
    const span = box.scrollHeight - box.clientHeight;
    if (span > 24) boxes.push(box);
  };
  const gather = (root) => {
    if (!root || !root.querySelectorAll) return;
    take(root);
    root.querySelectorAll("*").forEach((node) => {
      take(node);
      if (node.shadowRoot) gather(node.shadowRoot);
    });
  };
  gather(dialog);
  let best = null;
  let bestHeight = 1e15;
  for (const box of boxes) {
    const links = box.querySelectorAll ? box.querySelectorAll("a[href]").length : 0;
    if (links < 2) continue;
    if (box.scrollHeight < bestHeight) {
      best = box;
      bestHeight = box.scrollHeight;
    }
  }
  if (!best) best = boxes[0];
  if (!best) return false;
  if (action === "top") {
    best.scrollTop = 0;
    return true;
  }
  const before = best.scrollTop;
  const step = Math.max(72, Math.min(150, Math.round(best.clientHeight * 0.34)));
  best.scrollTop = before + step;
  return best.scrollTop > before + 4;
}
"""

CLICK_ALL_MEMBERS_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let dialog = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'], [aria-modal='true']")) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 180 && rect.height > 180) dialog = el;
    }
  }
  const scope = dialog || document;
  for (const el of scope.querySelectorAll("button, [role='button'], a")) {
    const label = clean(el.innerText || el.getAttribute("aria-label") || "");
    if (label === "عرض الكل" || label === "مشاهدة الكل" || /عرض كل|كل الأعضاء|see all|show all|view all|all members/i.test(label)) {
      el.click();
      return label;
    }
  }
  return "";
}
"""

OPEN_UNNAMED_SUSPENDED_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const suspendedRe = /حساب موقوف|الحساب موقوف|تم إيقاف الحساب|تم ايقاف الحساب|تم إيقاف|تم ايقاف|account suspended|suspended account|this account has been suspended/i;
  const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","intent","share","web","tos","privacy"]);
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let dialog = null;
  let area = 0;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'], [aria-modal='true']")) {
      const rect = el.getBoundingClientRect();
      const size = rect.width * rect.height;
      if (rect.width > 180 && rect.height > 180 && size > area) { dialog = el; area = size; }
    }
  }
  if (!dialog) return false;
  const buttons = [];
  const collect = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("button, [role='button']").forEach((el) => buttons.push(el));
    root.querySelectorAll("*").forEach((node) => { if (node.shadowRoot) collect(node.shadowRoot); });
  };
  collect(dialog);
  for (const button of buttons) {
    const label = button.getAttribute("aria-label") || "";
    if (!(label.startsWith("المزيد من الخيارات") || /^more options/i.test(label))) continue;
    let node = button;
    let suspended = false;
    let identified = false;
    for (let depth = 0; depth < 6 && node && node !== dialog; depth += 1) {
      const text = clean(node.innerText || "");
      if (text.length > 160) break;
      const bits = [text, label];
      if (node.querySelectorAll) {
        node.querySelectorAll("[aria-label], [title], img[alt]").forEach((el) => {
          bits.push(el.getAttribute("aria-label") || "", el.getAttribute("title") || "", el.getAttribute("alt") || "");
        });
      }
      if (suspendedRe.test(clean(bits.join(" ")))) suspended = true;
      if (/@[A-Za-z0-9_]{1,15}\b/.test(text) || /\/i\/user\/\d{4,22}/.test(text)) identified = true;
      const links = node.querySelectorAll ? [...node.querySelectorAll("a[href]")] : [];
      for (const link of links) {
        const href = link.getAttribute("href") || "";
        if (/\/i\/user\/\d{4,22}(?:\/|\?|$)/.test(href)) identified = true;
        const handle = (href.match(/^\/([A-Za-z0-9_]{1,15})\/?$/) || [])[1] || "";
        if (handle && !reserved.has(handle.toLowerCase())) identified = true;
      }
      if (links.length > 3) break;
      node = node.parentElement;
    }
    if (!suspended || identified) continue;
    button.scrollIntoView({block: "center", inline: "nearest"});
    button.click();
    return true;
  }
  return false;
}
"""

CLICK_MEMBER_MENU_JS = r"""
(key) => {
  const raw = String(key || "");
  const byId = raw.toLowerCase().startsWith("id:");
  const wantedId = byId ? raw.slice(3) : "";
  const wanted = byId ? "" : ("/" + raw.toLowerCase());
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let dialog = null;
  let area = 0;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'], [aria-modal='true']")) {
      const rect = el.getBoundingClientRect();
      const size = rect.width * rect.height;
      if (rect.width > 200 && rect.height > 200 && size > area) { dialog = el; area = size; }
    }
  }
  if (!dialog) return null;
  const inside = (el) => {
    let node = el;
    while (node) {
      if (node === dialog) return true;
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
    }
    return false;
  };
  let link = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const item of root.querySelectorAll("a[href]")) {
      if (!inside(item)) continue;
      const href = (item.getAttribute("href") || "").toLowerCase();
      const tail = wantedId ? ("/i/user/" + wantedId) : "";
      const hit = wantedId
        ? (href.endsWith(tail) || href.includes(tail + "?") || href.includes(tail + "/"))
        : (href.endsWith(wanted) || href.includes(wanted + "?"));
      if (hit) { link = item; break; }
    }
    if (link) break;
  }
  if (!link) return null;
  let button = null;
  let row = link.parentElement;
  for (let depth = 0; depth < 8 && row && inside(row); depth += 1) {
    const profiles = [...row.querySelectorAll("a[href]")].filter((item) => /\/[A-Za-z0-9_]{1,15}\/?$/i.test(item.getAttribute("href") || ""));
    if (profiles.length > 1) break;
    button = [...row.querySelectorAll("button")].find((item) => {
      const label = item.getAttribute("aria-label") || "";
      return label.startsWith("المزيد من الخيارات") || /^more options/i.test(label);
    });
    if (button) break;
    row = row.parentElement;
  }
  if (!button) return null;
  let scroller = button.parentElement;
  for (let hops = 0; scroller && hops < 14; hops += 1) {
    const style = getComputedStyle(scroller);
    if (/(auto|scroll|overlay)/.test(style.overflowY) && scroller.scrollHeight > scroller.clientHeight + 20) break;
    scroller = scroller.parentElement;
  }
  const place = () => {
    const b = button.getBoundingClientRect();
    if (!scroller || scroller === document.body || scroller === document.documentElement) {
      button.scrollIntoView({block: "center", inline: "nearest"});
      return;
    }
    const sbox = scroller.getBoundingClientRect();
    const viewTop = Math.max(sbox.top, 8);
    const viewBottom = Math.min(sbox.bottom, window.innerHeight - 8);
    if (b.height < 8 || b.top < viewTop + 4 || b.bottom > viewBottom - 4) {
      scroller.scrollTop += b.top - viewTop - 48;
    }
  };
  place();
  place();
  const box = button.getBoundingClientRect();
  button.click();
  return {label: button.getAttribute("aria-label") || "", x: box.x + box.width / 2, y: box.y + box.height / 2};
}
"""

CLICK_REMOVE_MENU_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const wanted = /إزالة من المجموعة|remove from group|remove from conversation/i;
  const reject = /إلغاء|cancel|مراسلة|message this/i;
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let best = null;
  let bestArea = 1e9;
  let bestLabel = "";
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("button, [role='button'], [role='menuitem'], div, span")) {
      const rect = el.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 14 || rect.height > 80) continue;
      if (rect.top < 0 || rect.bottom > window.innerHeight + 4 || rect.left < 0) continue;
      const label = clean(el.innerText || el.getAttribute("aria-label") || "");
      if (!wanted.test(label) || reject.test(label)) continue;
      const area = rect.width * rect.height;
      if (area < bestArea) {
        bestArea = area;
        best = el;
        bestLabel = label;
      }
    }
  }
  if (!best) return null;
  let target = best;
  let node = best;
  for (let depth = 0; depth < 5 && node; depth += 1) {
    const role = (node.getAttribute && node.getAttribute("role")) || "";
    if (node.tagName === "BUTTON" || role === "menuitem" || role === "button") {
      target = node;
      break;
    }
    node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
  }
  window.__grokHit = target;
  const rect = target.getBoundingClientRect();
  return {label: bestLabel, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
}
"""

MEMBER_STILL_THERE_JS = r"""
(handle) => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const raw = String(handle || "");
  const byId = raw.toLowerCase().startsWith("id:");
  const wantedId = byId ? raw.slice(3) : "";
  const wanted = byId ? "" : raw.toLowerCase();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const dialogs = [];
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll("[role='dialog'], [role='alertdialog'], [aria-modal='true']").forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.width > 80 && rect.height > 40) dialogs.push(el);
    });
  }
  if (!dialogs.length) return "closed";
  const linksOf = (dialog) => {
    const links = [];
    const collect = (root) => {
      if (!root || !root.querySelectorAll) return;
      root.querySelectorAll("a[href]").forEach((link) => links.push(link));
      root.querySelectorAll("*").forEach((node) => { if (node.shadowRoot) collect(node.shadowRoot); });
    };
    collect(dialog);
    return links;
  };
  const matches = (link) => {
    const href = (link.getAttribute("href") || "").toLowerCase();
    if (wantedId) {
      const tail = "/i/user/" + wantedId;
      return href.endsWith(tail) || href.includes(tail + "?") || href.includes(tail + "/");
    }
    return href.endsWith("/" + wanted) || href.includes("/" + wanted + "?");
  };
  let sawMembers = false;
  let inList = false;
  for (const dialog of dialogs) {
    const text = clean(dialog.innerText || "");
    const links = linksOf(dialog);
    const memberList = /من الأعضاء|عرض الكل|\bmembers\b/i.test(text) || links.filter((link) => /\/[A-Za-z0-9_]{1,15}\/?(\?|$)/.test(link.getAttribute("href") || "")).length > 2;
    if (!memberList) continue;
    sawMembers = true;
    if (links.some(matches)) inList = true;
  }
  if (inList) return "yes";
  if (sawMembers) return "no";
  return "confirm";
}
"""

INFO_SYNCING_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  let dialog = null;
  let area = 0;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    for (const el of root.querySelectorAll("[role='dialog'], [aria-modal='true']")) {
      const rect = el.getBoundingClientRect();
      const size = rect.width * rect.height;
      if (size > area) { dialog = el; area = size; }
    }
  }
  if (!dialog) return true;
  return clean(dialog.innerText || "").includes("قيد التقدم");
}
"""

CLICK_REMOVE_CONFIRM_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const roots = [];
  const walk = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(document);
  const parentOf = (node) => node && (node.parentElement || (node.getRootNode && node.getRootNode().host) || null);
  const inMenu = (el) => {
    let node = el;
    for (let depth = 0; depth < 10 && node; depth += 1) {
      const role = node.getAttribute && node.getAttribute("role");
      if (role === "menu" || role === "menuitem") return true;
      const text = clean((node.innerText || "").slice(0, 200));
      if (/مراسلة هذا المستخدم|message this user/i.test(text) && /إزالة من المجموعة|remove from group/i.test(text)) return true;
      node = parentOf(node);
    }
    return false;
  };
  const inOverlay = (el) => {
    let node = el;
    for (let depth = 0; depth < 12 && node; depth += 1) {
      const role = (node.getAttribute && node.getAttribute("role")) || "";
      const test = (node.getAttribute && node.getAttribute("data-testid")) || "";
      if (role === "dialog" || role === "alertdialog" || node.getAttribute && node.getAttribute("aria-modal") === "true") return true;
      if (/sheet|confirm/i.test(test)) return true;
      const style = node.nodeType === 1 ? getComputedStyle(node) : null;
      const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : null;
      if (style && style.position === "fixed" && rect && rect.height > 36 && rect.height < 520 && rect.width > 80) return true;
      node = parentOf(node);
    }
    return false;
  };
  const noRe = /إلغاء|cancel|رجوع|تراجع|ليس الآن|not now|مراسلة|message this/i;
  const shortYes = /^(إزالة|تأكيد|تأكيد الإزالة|نعم|موافق|حسناً|حسنا|yes|remove|confirm|ok)$/i;
  const longYes = /إزالة من المجموعة|remove from group|remove from conversation|remove member/i;
  const visible = [];
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    root.querySelectorAll("button, [role='button'], [data-testid='confirmationSheetConfirm']").forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8 || rect.bottom < 0 || rect.top > window.innerHeight) return;
      const test = el.getAttribute("data-testid") || "";
      const label = clean(el.innerText || el.getAttribute("aria-label") || "");
      if (noRe.test(label) && test !== "confirmationSheetConfirm") return;
      visible.push({el, test, label});
    });
  }
  const pointOf = (item) => {
    window.__grokHit = item.el;
    const rect = item.el.getBoundingClientRect();
    return {label: (item.label || item.test || "confirm").slice(0, 60), x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
  };
  const byTest = visible.find((item) => item.test === "confirmationSheetConfirm");
  if (byTest && !inMenu(byTest.el)) return pointOf(byTest);
  const choiceSheet = (el) => {
    let node = el;
    for (let depth = 0; depth < 8 && node; depth += 1) {
      const text = clean(node.innerText || "");
      if (text.length > 12 && text.length < 480 && /إلغاء|cancel/i.test(text) && /إزالة|remove|تأكيد|confirm/i.test(text) && !/مراسلة هذا المستخدم|message this user/i.test(text)) return true;
      node = parentOf(node);
    }
    return false;
  };
  const short = visible.find((item) => shortYes.test(item.label) && !inMenu(item.el) && (choiceSheet(item.el) || inOverlay(item.el)));
  if (short) return pointOf(short);
  const long = visible.find((item) => longYes.test(item.label) && !inMenu(item.el) && choiceSheet(item.el));
  if (long) return pointOf(long);
  return null;
}
"""

LOOKUP_MANY_JS = r"""
async (handles) => {
  const bearer = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
  const ct0 = (document.cookie.split("; ").find((item) => item.startsWith("ct0=")) || "").slice(4);
  if (!ct0) return {status: 0, found: [], codes: [], why: "no-ct0"};
  const headers = {
    authorization: bearer,
    "x-csrf-token": decodeURIComponent(ct0),
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes",
    "content-type": "application/x-www-form-urlencoded"
  };
  try {
    const response = await fetch("https://x.com/i/api/1.1/users/lookup.json", {
      method: "POST",
      headers,
      credentials: "include",
      body: "screen_name=" + encodeURIComponent((handles || []).join(","))
    });
    let body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    const codes = ((body && body.errors) || []).map((item) => item && item.code).filter((code) => code !== undefined && code !== null);
    const found = Array.isArray(body) ? body.map((user) => String((user && user.screen_name) || "").toLowerCase()).filter(Boolean) : [];
    return {status: response.status, found, codes, why: Array.isArray(body) ? "lookup" : "lookup-error"};
  } catch (error) {
    return {status: 0, found: [], codes: [], why: "network"};
  }
}
"""

ACCOUNT_LOOKUP_JS = r"""
async (handle) => {
  const bearer = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
  const ct0 = (document.cookie.split("; ").find((item) => item.startsWith("ct0=")) || "").slice(4);
  if (!ct0) return {status: 0, codes: [], hasUser: false, why: "no-ct0"};
  const headers = {
    authorization: bearer,
    "x-csrf-token": decodeURIComponent(ct0),
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes"
  };
  const codesOf = (body) => ((body && body.errors) || []).map((item) => item && item.code).filter((code) => code !== undefined && code !== null);
  const queryId = async () => {
    if (window.__userQueryId) return window.__userQueryId;
    const urls = [];
    const add = (src) => {
      if (src && src.includes("/client-web/") && !urls.includes(src)) urls.push(src);
    };
    document.querySelectorAll("script[src]").forEach((node) => add(node.src));
    try {
      performance.getEntriesByType("resource").forEach((entry) => add(entry.name));
    } catch (error) {}
    urls.sort((a, b) => Number(b.includes("main.")) - Number(a.includes("main.")));
    for (const url of urls.slice(0, 60)) {
      let text = "";
      try { text = await (await fetch(url)).text(); } catch (error) { continue; }
      const at = text.indexOf('operationName:"UserByScreenName"');
      if (at < 0) continue;
      const match = text.slice(Math.max(0, at - 200), at + 80).match(/queryId:"([A-Za-z0-9_-]{12,})"/);
      if (match) {
        window.__userQueryId = match[1];
        return match[1];
      }
    }
    window.__userQueryId = "KybxDj9RrADIITXlGG8kpw";
    return window.__userQueryId;
  };
  const readGraph = async () => {
    const id = await queryId();
    if (!id) return {status: 0, codes: [], hasUser: false, why: "no-endpoint"};
    const variables = {screen_name: handle, withSafetyModeUserFields: true};
    const features = {
      hidden_profile_subscriptions_enabled: true,
      responsive_web_graphql_exclude_directive_enabled: true,
      verified_phone_label_enabled: false,
      responsive_web_graphql_skip_user_profile_image_extensions_enabled: false,
      responsive_web_graphql_timeline_navigation_enabled: true
    };
    const url = "https://x.com/i/api/graphql/" + id + "/UserByScreenName?variables=" + encodeURIComponent(JSON.stringify(variables)) + "&features=" + encodeURIComponent(JSON.stringify(features));
    const response = await fetch(url, {headers, credentials: "include"});
    let body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    const result = body && body.data && body.data.user && body.data.user.result;
    const reason = String((result && (result.reason || result.unavailable_reason)) || "");
    const legacy = result && result.legacy;
    const legacySuspended = !!(legacy && (legacy.suspended === true || /suspend/i.test(String(legacy.profile_interstitial_type || ""))));
    const codes = codesOf(body);
    if (/suspend/i.test(reason) || legacySuspended) codes.push(63);
    const hasUser = !!(result && result.__typename === "User" && !legacySuspended);
    return {
      status: response.status,
      codes,
      hasUser,
      notSuspended: hasUser,
      why: reason || (hasUser ? "user" : "missing")
    };
  };
  try {
    return await readGraph();
  } catch (error) {
    return {status: 0, codes: [], hasUser: false, why: "network"};
  }
}
"""


SHOW_USER_JS = r"""
async (handle) => {
  const bearer = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
  const ct0 = (document.cookie.split("; ").find((item) => item.startsWith("ct0=")) || "").slice(4);
  if (!ct0) return {status: 0, codes: [], hasUser: false, why: "no-ct0"};
  const headers = {
    authorization: bearer,
    "x-csrf-token": decodeURIComponent(ct0),
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes"
  };
  try {
    const response = await fetch("https://x.com/i/api/1.1/users/show.json?screen_name=" + encodeURIComponent(handle), {headers, credentials: "include"});
    let body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    const codes = ((body && body.errors) || []).map((item) => item && item.code).filter((code) => code !== undefined && code !== null);
    if (body && body.suspended) codes.push(63);
    const message = String((body && body.errors && body.errors[0] && body.errors[0].message) || "");
    const hasUser = !!(body && body.screen_name && !body.suspended);
    return {
      status: response.status,
      codes,
      hasUser,
      notSuspended: hasUser,
      why: (body && body.suspended) ? "Suspended" : (message || (hasUser ? "show" : "show-missing"))
    };
  } catch (error) {
    return {status: 0, codes: [], hasUser: false, why: "network"};
  }
}
"""


def verdict_from_lookup(info: dict) -> str:
    if not isinstance(info, dict):
        return "unknown"
    codes: list[int] = []
    for code in info.get("codes") or []:
        try:
            codes.append(int(code))
        except (TypeError, ValueError):
            continue
    why = str(info.get("why") or "").lower()
    if 64 in codes:
        return "viewer"
    if 63 in codes or why == "suspended" or "suspend" in why:
        return "suspended"
    if info.get("hasUser") or info.get("notSuspended"):
        return "clear"
    if 50 in codes or 17 in codes:
        return "clear"
    return "unknown"


def lookup_codes(info: dict) -> set[int]:
    codes: set[int] = set()
    for code in (info or {}).get("codes") or []:
        try:
            codes.add(int(code))
        except (TypeError, ValueError):
            continue
    return codes


def lookup_is_limited(info: dict) -> bool:
    if not isinstance(info, dict):
        return False
    if 88 in lookup_codes(info):
        return True
    try:
        status = int(info.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    return status == 429


_limit_not_before = 0.0


def wait_for_limit(page, attempt: int = 0) -> None:
    global _limit_not_before
    now = time.monotonic()
    if now < _limit_not_before:
        return
    seconds = 12
    _limit_not_before = now + seconds + 20
    for left in range(seconds, 0, -1):
        if abort_flag.is_set():
            return
        if left == seconds or left == 6:
            with state_lock:
                write_message(f"طلبات الفحص مزدحمة. انتظار {left} ث ثم المتابعة…")
        page.wait_for_timeout(1000)


def account_lookup(page, handle: str) -> tuple[str, str]:
    handle = str(handle or "").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return "unknown", "bad-handle"
    verdict, why = "unknown", "unconfirmed"
    for attempt in range(2):
        try:
            info = page.evaluate(SHOW_USER_JS, handle) or {}
        except Exception:
            info = {"status": 0, "why": "network"}
        if not isinstance(info, dict):
            info = {"status": 0, "why": "network"}
        verdict = verdict_from_lookup(info)
        why = str(info.get("why") or "")
        if verdict in {"suspended", "clear", "viewer"}:
            return verdict, why
        if lookup_is_limited(info) and attempt == 0:
            wait_for_limit(page, attempt)
            continue
        if why == "network" and attempt == 0:
            page.wait_for_timeout(700)
            continue
        break
    return verdict, why or "unconfirmed"


def account_verdict(page, handle: str) -> str:
    verdict, _why = account_lookup(page, handle)
    return verdict


def visible_handles(page, handles: list[str]) -> tuple[set[str], list[str], bool]:
    active: set[str] = set()
    pending: list[str] = []
    blocked = False
    chunks = [handles[index:index + 80] for index in range(0, len(handles), 80)]
    for index, chunk in enumerate(chunks, start=1):
        if abort_flag.is_set():
            pending.extend(handles[(index - 1) * 80:])
            break
        with state_lock:
            write_message(f"يفحص الدفعة {index} من {len(chunks)}")
        info: dict = {}
        for attempt in range(2):
            try:
                info = page.evaluate(LOOKUP_MANY_JS, chunk) or {}
            except Exception:
                info = {"status": 0, "why": "network", "found": []}
            if str(info.get("why") or "") == "no-ct0":
                return set(), list(handles), False
            if lookup_is_limited(info) and not info.get("found"):
                if attempt == 0:
                    wait_for_limit(page, attempt)
                    continue
                blocked = True
            break
        found = {str(name).lower() for name in (info.get("found") or []) if name}
        status = 0
        try:
            status = int(info.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if index == 1 and not found and status == 404 and not lookup_codes(info) and str(info.get("why") or "") == "lookup-error":
            return set(), list(handles), False
        if blocked and not found:
            pending.extend(chunk)
        else:
            active.update(found)
            pending.extend(handle for handle in chunk if handle.lower() not in found)
        page.wait_for_timeout(500)
    return active, pending, blocked


def open_info_panel(page) -> None:
    page.locator('[data-testid="dm-conversation-more-button"]').first.click(timeout=8000)
    page.wait_for_timeout(700)


def read_member_panel(page) -> dict:
    try:
        payload = page.evaluate(READ_MEMBERS_JS) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {"people": [], "expected": 0, "admin": False, "syncing": False}
    return payload


def collect_group_members(page) -> tuple[list[dict], bool]:
    open_info_panel(page)
    people: dict[str, dict] = {}
    admin = False
    expected = 0

    def absorb(panel: dict) -> None:
        nonlocal admin, expected
        admin = admin or bool(panel.get("admin"))
        expected = max(expected, int(panel.get("expected") or 0))
        for person in panel.get("people") or []:
            handle = str(person.get("handle") or "").lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                continue
            key = handle.lower()
            current = people.get(key) or {}
            people[key] = {
                "handle": handle,
                "name": str(person.get("name") or current.get("name") or "")[:60],
                "removable": bool(person.get("removable") or current.get("removable")),
            }

    opened = ""
    started = time.monotonic()
    while time.monotonic() - started < 25 and not opened:
        if abort_flag.is_set():
            break
        panel = read_member_panel(page)
        absorb(panel)
        elapsed = time.monotonic() - started
        if admin or elapsed > 6:
            try:
                opened = page.evaluate(CLICK_ALL_MEMBERS_JS) or ""
            except Exception:
                opened = ""
            if opened:
                page.wait_for_timeout(800)
                break
            if expected and len(people) >= expected and elapsed > 5:
                break
        with state_lock:
            write_message("بانتظار قائمة كل الأعضاء…", True)
        page.wait_for_timeout(400)

    quiet = 0
    phase = time.monotonic()
    for _ in range(900):
        if abort_flag.is_set():
            break
        before = len(people)
        panel = read_member_panel(page)
        absorb(panel)
        if not opened and time.monotonic() - phase < 12:
            try:
                opened = page.evaluate(CLICK_ALL_MEMBERS_JS) or ""
            except Exception:
                opened = ""
            if opened:
                page.wait_for_timeout(800)
                quiet = 0
                continue
        if expected and len(people) >= expected and (opened or len(people) >= expected):
            with state_lock:
                write_message(f"قراءة الأعضاء {len(people)} من {expected}", True)
            break
        try:
            moved = bool(page.evaluate(SCROLL_MEMBERS_JS))
        except Exception:
            moved = False
        page.wait_for_timeout(180)
        with state_lock:
            write_message(f"قراءة الأعضاء {len(people)}" + (f" من {expected}" if expected else ""), True)
        if len(people) == before and not moved:
            quiet += 1
            settled = opened or (expected and len(people) >= expected)
            if quiet >= 4 and people and settled:
                break
            if quiet >= 4 and people and not opened and time.monotonic() - phase > 12:
                break
        else:
            quiet = 0
    return list(people.values()), admin


def account_is_suspended(page, handle: str) -> bool:
    return account_verdict(page, handle) == "suspended"


MEMBER_ID_JS = r"""
(handle) => {
  const wanted = String(handle || "").toLowerCase();
  const roots = [];
  const walkRoot = (root) => {
    if (!root || roots.includes(root)) return;
    roots.push(root);
    const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const node of nodes) if (node.shadowRoot) walkRoot(node.shadowRoot);
  };
  walkRoot(document);
  let link = null;
  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    link = [...root.querySelectorAll("a[href]")].find((item) => (item.getAttribute("href") || "").toLowerCase().endsWith("/" + wanted));
    if (link) break;
  }
  if (!link) return "";
  const fiberKey = Object.keys(link).find((name) => name.startsWith("__reactFiber"));
  if (!fiberKey) return "";
  const seen = new Set();
  let found = "";
  const walk = (value, depth) => {
    if (found || !value || typeof value !== "object" || depth > 7 || seen.has(value)) return;
    seen.add(value);
    let matched = false;
    const ids = [];
    for (const key of Object.keys(value).slice(0, 50)) {
      const item = value[key];
      if (typeof item !== "string") continue;
      if (item.toLowerCase() === wanted) matched = true;
      if (/^\d{8,22}$/.test(item)) ids.push(item);
    }
    if (matched && ids.length) {
      found = ids[0];
      return;
    }
    for (const key of Object.keys(value).slice(0, 40)) walk(value[key], depth + 1);
  };
  let fiber = link[fiberKey];
  for (let i = 0; fiber && i < 40 && !found; i += 1) {
    walk(fiber.memoizedProps, 0);
    fiber = fiber.return;
  }
  return found;
}
"""

REMOVE_MEMBER_JS = r"""
async ({conversationId, userId}) => {
  const bearer = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
  const ct0 = (document.cookie.split("; ").find((item) => item.startsWith("ct0=")) || "").slice(4);
  if (!ct0) return {ok: false, error: "no-ct0"};
  const headers = {
    authorization: bearer,
    "content-type": "application/json",
    "x-csrf-token": decodeURIComponent(ct0),
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-active-user": "yes"
  };
  const query = "mutation RemoveFromGroupMutation($conversation_id: String!, $user_ids: [NumericString!]!, $action_signatures: [ActionSignatureInput!], $group_key_rotation_data: XChatGroupKeyRotationDataInput) { xchat_remove_group_members(member_ids: $user_ids, conversation_id: $conversation_id, safety_level: XChat, action_signatures: $action_signatures, group_key_rotation_data: $group_key_rotation_data) { __typename ... on RemoveGroupMembersResponse { error_code conversation_key_change_sequence_id } } }";
  try {
    const response = await fetch("https://x.com/i/api/graphql/Lnm2b8oz4dnV8V-RtP1SYA/RemoveFromGroupMutation", {
      method: "POST",
      headers,
      credentials: "include",
      body: JSON.stringify({query, variables: {conversation_id: conversationId, user_ids: [String(userId)]}})
    });
    let body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    const payload = body && body.data && body.data.xchat_remove_group_members;
    const error = (payload && payload.error_code) || (body && body.errors && body.errors[0] && body.errors[0].message) || "";
    return {ok: response.status === 200 && payload && payload.__typename === "RemoveGroupMembersResponse" && !error, error: String(error || response.status)};
  } catch (error) {
    return {ok: false, error: "network"};
  }
}
"""


def member_list_syncing(page) -> bool:
    try:
        return bool(page.evaluate(r"""
        () => {
          const roots = [];
          const walk = (root) => {
            if (!root || roots.includes(root)) return;
            roots.push(root);
            const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
            for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
          };
          walk(document);
          for (const root of roots) {
            if (!root.querySelectorAll) continue;
            for (const el of root.querySelectorAll("div, span, p")) {
              const text = (el.innerText || "").replace(/\s+/g, " ").trim();
              if (!text.includes("قيد التقدم") || text.length > 80) continue;
              const rect = el.getBoundingClientRect();
              if (rect.width > 20 && rect.height > 8 && rect.bottom > 0 && rect.top < innerHeight) return true;
            }
          }
          return false;
        }
        """))
    except Exception:
        return False


def close_member_menu(page) -> None:
    try:
        open_menu = page.evaluate(r"""
        () => {
          const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
          const roots = [];
          const walk = (root) => {
            if (!root || roots.includes(root)) return;
            roots.push(root);
            const nodes = root.querySelectorAll ? root.querySelectorAll("*") : [];
            for (const node of nodes) if (node.shadowRoot) walk(node.shadowRoot);
          };
          walk(document);
          for (const root of roots) {
            if (!root.querySelectorAll) continue;
            for (const el of root.querySelectorAll("div, span, button")) {
              const label = clean(el.innerText || "");
              if (label === "مراسلة هذا المستخدم" || label === "إزالة من المجموعة" || /^message this user$/i.test(label)) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 20 && rect.height > 10 && rect.top >= 0 && rect.bottom <= innerHeight + 4) return true;
              }
            }
          }
          return false;
        }
        """)
    except Exception:
        open_menu = False
    if not open_menu:
        return
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def trusted_click(page, point) -> bool:
    if not isinstance(point, dict):
        return False
    try:
        x = float(point.get("x") or 0)
        y = float(point.get("y") or 0)
    except (TypeError, ValueError):
        return False
    if x < 1 or y < 1:
        return False
    try:
        page.mouse.click(x, y, delay=40)
        return True
    except Exception:
        return False


def click_marked(page) -> None:
    try:
        page.evaluate("() => { const el = window.__grokHit; if (el && el.click) el.click(); }")
    except Exception:
        pass


def member_presence(page, handle: str) -> str:
    try:
        return str(page.evaluate(MEMBER_STILL_THERE_JS, handle) or "yes")
    except Exception:
        return "yes"


def confirm_remove_click(page) -> str:
    for _ in range(12):
        try:
            point = page.evaluate(CLICK_REMOVE_CONFIRM_JS)
        except Exception:
            point = None
        if isinstance(point, dict) and point.get("x"):
            trusted_click(page, point)
            page.wait_for_timeout(280)
            try:
                still = page.evaluate(CLICK_REMOVE_CONFIRM_JS)
            except Exception:
                still = None
            if isinstance(still, dict) and still.get("x"):
                click_marked(page)
                page.wait_for_timeout(280)
            return str(point.get("label") or "confirm")
        page.wait_for_timeout(180)
    return ""


def wait_member_removed(page, handle: str) -> str:
    present_streak = 0
    for _ in range(18):
        if abort_flag.is_set():
            return "aborted"
        try:
            syncing = member_list_syncing(page)
        except Exception:
            syncing = False
        presence = member_presence(page, handle)
        if presence == "no":
            return "removed"
        if presence == "closed":
            return "closed"
        if syncing or presence == "confirm":
            present_streak = 0
        else:
            present_streak += 1
            if present_streak >= 8:
                return "present"
        page.wait_for_timeout(280)
    return "present"


def remove_member_via_api(page, handle: str) -> bool:
    raw = str(handle or "")
    user_id = ""
    if raw.lower().startswith("id:"):
        user_id = raw[3:]
        raw = ""
    match = re.search(r"/i/chat/((?:g)?\d+)", page.url or "")
    if not match:
        return False
    if not re.fullmatch(r"\d{4,22}", user_id):
        if not raw:
            return False
        try:
            user_id = str(page.evaluate(MEMBER_ID_JS, raw) or "")
        except Exception:
            return False
    if not re.fullmatch(r"\d{4,22}", user_id):
        return False
    try:
        result = page.evaluate(
            REMOVE_MEMBER_JS,
            {"conversationId": match.group(1), "userId": user_id},
        ) or {}
    except Exception:
        return False
    return bool(isinstance(result, dict) and result.get("ok"))


def remove_from_open_menu(page) -> str:
    page.wait_for_timeout(350)
    item = None
    for _ in range(5):
        try:
            item = page.evaluate(CLICK_REMOVE_MENU_JS)
        except Exception:
            item = None
        if item:
            break
        page.wait_for_timeout(140)
    if not item:
        close_member_menu(page)
        return "no-button"
    trusted_click(page, item)
    page.wait_for_timeout(300)
    label = confirm_remove_click(page)
    if not label:
        click_marked(page)
        page.wait_for_timeout(300)
        label = confirm_remove_click(page)
    page.wait_for_timeout(450)
    if not label:
        close_member_menu(page)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    if member_presence(page, "___") == "closed":
        try:
            open_info_panel(page)
            page.wait_for_timeout(500)
        except Exception:
            pass
    return "removed" if label else "no-confirm"


def remove_group_member(page, handle: str) -> str:
    for _ in range(20):
        if not member_list_syncing(page):
            break
        page.wait_for_timeout(400)
    last = "not-removed"
    for attempt in range(3):
        if member_presence(page, handle) == "closed":
            try:
                open_info_panel(page)
                page.wait_for_timeout(700)
            except Exception:
                return "not-removed"
        opened = None
        for _ in range(6):
            try:
                opened = page.evaluate(CLICK_MEMBER_MENU_JS, handle)
            except Exception:
                opened = None
            if opened:
                break
            try:
                page.evaluate(SCROLL_MEMBERS_JS)
            except Exception:
                pass
            page.wait_for_timeout(220)
        if not opened:
            last = "missing"
            continue
        page.wait_for_timeout(400)
        item = None
        for _ in range(5):
            try:
                item = page.evaluate(CLICK_REMOVE_MENU_JS)
            except Exception:
                item = None
            if item:
                break
            page.wait_for_timeout(160)
        if not item:
            close_member_menu(page)
            page.wait_for_timeout(250)
            last = "no-button"
            continue
        trusted_click(page, item)
        page.wait_for_timeout(350)
        label = confirm_remove_click(page)
        if not label:
            click_marked(page)
            page.wait_for_timeout(350)
            label = confirm_remove_click(page)
        outcome = wait_member_removed(page, handle)
        if outcome == "removed":
            return "removed"
        if outcome == "aborted":
            return "aborted"
        if outcome == "closed":
            try:
                open_info_panel(page)
                page.wait_for_timeout(700)
            except Exception:
                return "not-removed"
            if member_presence(page, handle) == "no":
                return "removed"
        last = "no-confirm" if not label else "not-removed"
        for _ in range(2):
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(160)
        if member_presence(page, handle) == "closed":
            try:
                open_info_panel(page)
                page.wait_for_timeout(600)
            except Exception:
                pass
    if remove_member_via_api(page, handle):
        page.wait_for_timeout(700)
        if member_presence(page, handle) == "closed":
            try:
                open_info_panel(page)
                page.wait_for_timeout(700)
            except Exception:
                return "removed"
        if member_presence(page, handle) == "no":
            return "removed"
    return last


def scan_suspended_members(auth_token: str, pin: str, group_name: str) -> None:
    def work(page):
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
            return
        with state_lock:
            state["scanning"] = True
            write_message(f"جارٍ فتح «{group_name}» لفحص الحسابات…", True)
        error = open_chat(page, pin) if ensure_logged_in(page) else "الجلسة غير مسجّلة الدخول."
        if error:
            set_status(error, False, False)
            return
        show_groups_only(page)
        try:
            page.evaluate(RESET_INBOX_JS)
        except Exception:
            pass
        try:
            open_named_group(page, group_name)
        except Exception as exc:
            set_status(str(exc) or "تعذر فتح القروب.", False, False)
            return
        group_url = page.url or ""
        with state_lock:
            write_message("جارٍ فتح قائمة الأعضاء…")
        try:
            page.evaluate(GROUP_CHAT_SCROLL_JS, "latest")
        except Exception:
            pass
        page.wait_for_timeout(1500)
        try:
            open_info_panel(page)
        except Exception:
            set_status("تعذر فتح معلومات القروب.", False, False)
            return

        def panel_says_admin() -> bool:
            panel_now = read_member_panel(page)
            return bool(panel_now.get("admin"))

        admin = False
        for _ in range(12):
            if abort_flag.is_set():
                set_status("تم إيقاف المهمة.", True, False)
                return
            if panel_says_admin():
                admin = True
                break
            page.wait_for_timeout(400)
        if not admin:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(600)
                open_info_panel(page)
            except Exception:
                pass
            for _ in range(12):
                if abort_flag.is_set():
                    set_status("تم إيقاف المهمة.", True, False)
                    return
                if panel_says_admin():
                    admin = True
                    break
                page.wait_for_timeout(400)
        if not admin:
            close_group_info(page)
            set_status("الحساب ليس مشرف القروب. توقف الفحص ولم تُزل أي حسابات.", False, False)
            return
        for _ in range(16):
            if abort_flag.is_set():
                set_status("تم إيقاف المهمة.", True, False)
                return
            if not member_list_syncing(page):
                break
            page.wait_for_timeout(400)
        try:
            page.evaluate(CLICK_ALL_MEMBERS_JS)
        except Exception:
            pass
        page.wait_for_timeout(600)
        removed: list[str] = []
        failed: list[str] = []
        roster: dict[str, dict] = {}
        expected = 0

        def absorb(panel: dict) -> None:
            nonlocal expected
            expected = max(expected, int(panel.get("expected") or 0))
            for person in panel.get("people") or []:
                handle = str(person.get("handle") or "").lstrip("@")
                user_id = str(person.get("userId") or "")
                if handle and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                    handle = ""
                if not re.fullmatch(r"\d{4,22}", user_id):
                    user_id = ""
                if not handle and not user_id:
                    continue
                key = handle.lower() if handle else f"id:{user_id}"
                current = roster.get(key) or {}
                roster[key] = {
                    "key": key,
                    "handle": handle or str(current.get("handle") or ""),
                    "user_id": user_id or str(current.get("user_id") or ""),
                    "name": str(person.get("name") or current.get("name") or "")[:60],
                    "suspended": bool(person.get("suspended") or current.get("suspended")),
                }

        def sweep_unnamed() -> None:
            for _ in range(6):
                if abort_flag.is_set():
                    return
                try:
                    opened = page.evaluate(OPEN_UNNAMED_SUSPENDED_JS)
                except Exception:
                    opened = False
                if not opened:
                    return
                outcome = remove_from_open_menu(page)
                if outcome == "removed":
                    removed.append("بدون-اسم")
                else:
                    failed.append(f"بدون-اسم ({outcome})")
                    return

        def harvest(limit: int) -> None:
            nonlocal expected
            try:
                page.evaluate(SCROLL_MEMBERS_JS, "top")
            except Exception:
                pass
            page.wait_for_timeout(280)
            stall = 0
            last_total = -1
            for _ in range(limit):
                if abort_flag.is_set():
                    return
                panel = read_member_panel(page)
                absorb(panel)
                sweep_unnamed()
                total = len(roster) + sum(1 for name in removed if name == "بدون-اسم")
                with state_lock:
                    write_message("جمع الأعضاء " + str(len(roster)) + (f" من {expected}" if expected else ""))
                if total == last_total:
                    stall += 1
                else:
                    stall = 0
                    last_total = total
                try:
                    moved = bool(page.evaluate(SCROLL_MEMBERS_JS, "down"))
                except Exception:
                    moved = False
                if not moved:
                    stall += 1
                if expected and total >= expected and stall >= 2:
                    return
                if stall >= 8:
                    return
                page.wait_for_timeout(180)

        harvest(500)
        if expected and len(roster) + sum(1 for name in removed if name == "بدون-اسم") < expected:
            harvest(500)

        for person in list(roster.values()):
            if abort_flag.is_set():
                set_status("تم إيقاف المهمة.", True, False)
                return
            if not person.get("suspended"):
                continue
            handle = person["handle"]
            label = f"@{handle}" if handle else person["key"]
            with state_lock:
                write_message(f"{label} ظاهر كموقوف. تتم إزالته من القائمة…")
            if "/info" not in (page.url or ""):
                try:
                    page.goto(group_url, wait_until="domcontentloaded", timeout=60_000)
                    open_info_panel(page)
                    page.evaluate(CLICK_ALL_MEMBERS_JS)
                except Exception:
                    failed.append(f"{label} (failed)")
                    continue
            target = handle or person["key"]
            try:
                outcome = remove_group_member(page, target)
            except Exception:
                outcome = "failed"
            if outcome == "missing" and person["user_id"] and target != "id:" + person["user_id"]:
                try:
                    outcome = remove_group_member(page, "id:" + person["user_id"])
                except Exception:
                    outcome = "failed"
            if outcome == "removed":
                removed.append(handle or person["key"])
            else:
                failed.append(f"{label} ({outcome})")
                if "/info" not in (page.url or ""):
                    try:
                        open_info_panel(page)
                    except Exception:
                        pass
        seen = len(roster) + sum(1 for name in removed if name == "بدون-اسم")
        gap = f" ظهرت {seen} من {expected}." if expected and seen < expected else ""
        if removed and not failed:
            set_status(f"انتهى الفحص من القائمة بلا طلبات. أُزيل {len(removed)} حساب موقوف: " + "، ".join("@" + name for name in removed) + gap, True, False)
        elif removed:
            set_status(
                f"أُزيل {len(removed)} وتعذر إزالة {len(failed)}. " + " | ".join(failed[:4]) + gap,
                False,
                False,
            )
        elif failed:
            set_status("ظهرت حسابات موقوفة في القائمة لكن تأكيد الإزالة لم يكتمل. " + " | ".join(failed[:4]) + gap, False, False)
        else:
            set_status(f"انتهى المرور على القائمة بلا طلبات. لا يوجد عضو مكتوب عليه أنه موقوف.{gap}", True, False)

    try:
        run_on_browser(auth_token, work)
    except Exception:
        if abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
        else:
            set_status("انقطع المتصفح أثناء فحص الحسابات.", False, False)
    finally:
        abort_flag.clear()
        with state_lock:
            state["scanning"] = False
            state["watching"] = False
            state["checking"] = False


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


def watch_loop(auth_token: str, pin: str, group_name: str) -> None:
    run_id = secrets.token_hex(4)
    anchor_ids: set[str] = set()

    def work(page):
        nonlocal group_name
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
                    open_named_group(fresh, group_name)
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
            open_named_group(page, group_name)
        except Exception as exc:
            if not browser_failure(exc) and page_alive(page):
                set_status("تعذر فتح القروب.", False, False)
                return
            page = reopen()
            open_named_group(page, group_name)
        note = ""
        set_status("يتم البحث عن زر التعديل…", True, True)
        renamed, reason = rename_group_twice(page, group_name)
        if reason == "aborted" or abort_flag.is_set():
            set_status("تم إيقاف المهمة.", True, False)
            return
        if reason == "renamed" and renamed != group_name:
            old_name = group_name
            group_name = renamed
            with state_lock:
                state["group"] = renamed
                state["groups"] = [renamed if name == old_name else name for name in state["groups"]]
            save_state()
        elif reason == "skipped":
            set_status("الحساب ليس مشرف القروب. توقف ولم يتم تنفيذ أي شيء.", False, False)
            return
        elif reason == "failed":
            note = " تعذر تغيير الاسم."
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
                close_monitoring(page, group_name, run_id)
                verify_captured(page, anchor_ids, run_id, reopen)
                audit_fire_reposts(page, run_id, group_name, reopen)
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
                close_monitoring(page, group_name, run_id)
                verify_captured(page, anchor_ids, run_id, reopen)
                audit_fire_reposts(page, run_id, group_name, reopen)
                return
            anchor = page.evaluate("() => window.__watchAnchor ? window.__watchAnchor() : []") or []
            anchor_ids.update(str(item) for item in anchor if item)
            page.evaluate("() => window.__watchArm && window.__watchArm()")
            remember_watch(page, saved)
        set_status(f"يراقب «{group_name}» ويلتقط التغريدات الجديدة.{note}", reason != "failed", True)
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
                close_monitoring(page, group_name, run_id)
                if abort_flag.is_set():
                    set_status("تم إيقاف المهمة.", True, False)
                    return
                verify_captured(page, anchor_ids, run_id, reopen)
                if abort_flag.is_set():
                    set_status("تم إيقاف المهمة.", True, False)
                    return
                audit_fire_reposts(holder["page"], run_id, group_name, reopen)
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
    .audits { display:flex; flex-direction:column; gap:6px; margin-top:8px; }
    #audits { max-height:320px; overflow-y:auto; padding:6px; border-radius:12px; box-shadow:inset 0 0 0 1px var(--line); }
    .audit { display:flex; justify-content:space-between; gap:8px; align-items:center; padding:8px 10px; border-radius:12px; background:var(--paper); }
    .audit.bad { box-shadow:inset 0 0 0 1px var(--bad); }
    .audit.ok { box-shadow:inset 0 0 0 1px var(--pine); }
    .audit b { font-size:13px; }
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
      <button id="stop" class="ghost" type="button" style="width:100%;margin-top:8px" disabled>إيقاف المراقبة</button>
      <button id="abort" class="ghost" type="button" style="width:100%;margin-top:8px" disabled>إيقاف المهمة</button>
      <div id="box" hidden>
        <div class="meta">حدد القروب، ثم اضغط مراقبة أو فحص الحسابات الموقوفة</div>
        <div id="groups"></div>
        <div class="actions">
          <button id="watch" type="button">مراقبة</button>
          <button id="scan" class="ghost" type="button">فحص الحسابات الموقوفة</button>
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
    <section class="sheet" id="audit-sheet" style="margin-top:10px" hidden>
      <div class="top">
        <h1 style="font-size:18px">فحص إعادة النشر</h1>
        <b id="audit-count">0</b>
      </div>
      <div id="audits" class="audits"></div>
    </section>
    <section class="sheet" id="nofire-sheet" style="margin-top:10px" hidden>
      <div class="top">
        <h1 style="font-size:18px">لم يضعوا الشعلة</h1>
        <b id="nofire-count">0</b>
      </div>
      <div id="nofire" class="audits"></div>
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
    const scan = document.getElementById("scan");
    const watch = document.getElementById("watch");
    const box = document.getElementById("box");
    const groups = document.getElementById("groups");
    const notice = document.getElementById("notice");
    const feed = document.getElementById("feed");
    const doubles = document.getElementById("doubles");
    const audits = document.getElementById("audits");
    const auditSheet = document.getElementById("audit-sheet");
    const auditCount = document.getElementById("audit-count");
    const nofireSheet = document.getElementById("nofire-sheet");
    const nofireCount = document.getElementById("nofire-count");
    const nofire = document.getElementById("nofire");
    const pop = document.getElementById("pop");
    const popTitle = document.getElementById("pop-title");
    const popBody = document.getElementById("pop-body");
    const count = document.getElementById("count");
    let picked = "";
    let starting = false;
    let busy = false;
    let groupsReady = false;
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

    function render(data) {
      busy = !!(data.watching || data.checking || data.scanning);
      if (document.activeElement !== token && data.auth_token && token.readOnly) token.value = data.auth_token;
      if (document.activeElement !== pin && data.chat_pin) pin.value = data.chat_pin;
      if (picked && !(data.groups || []).includes(picked) && groupsReady) picked = "";
      if (!picked && data.group) picked = data.group;
      const names = groupsReady ? (data.groups || []) : [];
      box.hidden = names.length === 0;
      groups.replaceChildren();
      names.forEach((name) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = name;
        button.disabled = busy;
        if (name === picked) button.className = "on";
        button.addEventListener("click", () => {
          if (busy || starting) { show("المهمة تعمل الآن. انتظر حتى تنتهي.", false); return; }
          picked = name;
          groups.querySelectorAll("button").forEach((item) => item.classList.toggle("on", item === button));
        });
        groups.appendChild(button);
      });
      if (watch) watch.disabled = busy || !names.length;
      if (scan) scan.disabled = busy || !names.length;
      count.textContent = String(data.count || 0);
      const auditRows = data.audits || [];
      auditSheet.hidden = auditRows.length === 0;
      auditCount.textContent = String(auditRows.length);
      audits.replaceChildren();
      auditRows.forEach((row) => {
        const card = document.createElement("article");
        const missing = row.missing || [];
        card.className = "audit " + (missing.length || row.error ? "bad" : "ok");
        const who = row.handle ? "@" + row.handle : (row.name || "حساب");
        const title = document.createElement("b");
        title.textContent = row.error
          ? who + " " + row.error
          : (missing.length ? who + " ناقص " + missing.length + " من " + (row.wanted || 0) : who + " أعاد نشر الكل");
        card.appendChild(title);
        if (missing.length) {
          const view = document.createElement("button");
          view.type = "button";
          view.className = "ghost mini";
          view.textContent = "عرض";
          view.addEventListener("click", () => openPop(who, missing.map((id) => ({
            href: "https://x.com/i/web/status/" + id,
            label: id
          }))));
          card.appendChild(view);
        }
        audits.appendChild(card);
      });
      const absent = data.no_fire_ready ? (data.no_fire || []) : [];
      nofireSheet.hidden = !data.no_fire_ready;
      nofireCount.textContent = String(absent.length);
      nofire.replaceChildren();
      if (data.no_fire_ready && absent.length === 0) {
        const empty = document.createElement("p");
        empty.className = "meta";
        empty.textContent = "كل المشاركين وضعوا الشعلة.";
        nofire.appendChild(empty);
      }
      absent.forEach((row) => {
        const card = document.createElement("article");
        card.className = "audit";
        const title = document.createElement("b");
        title.textContent = row.handle ? "@" + row.handle : (row.name || "حساب غير معروف");
        card.appendChild(title);
        nofire.appendChild(card);
      });
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
      if (scan) scan.disabled = busy;
    }

    async function refresh() {
      try {
        const response = await fetch("/state", {cache: "no-store"});
        const data = await response.json();
        if (response.ok) render(data);
      } catch (_) {}
    }

    async function start(name) {
      if (busy || starting) {
        show("المهمة تعمل الآن. انتظر حتى تنتهي.", false);
        return;
      }
      const auth = token.value.trim();
      const chat = pin.value.trim();
      if (!auth) { show("أدخل رمز الجلسة.", false); token.focus(); return; }
      if (!/^\d{4,8}$/.test(chat)) { show("أدخل رمز الدردشة.", false); pin.focus(); return; }
      if (!name) { show("حدد القروب ثم اضغط مراقبة.", false); return; }
      starting = true;
      picked = name;
      noticeHold += 1;
      show("جارٍ فتح " + name + "…", true);
      try {
        const data = await post("/watch/start", {auth_token: auth, chat_pin: chat, group: name});
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
      load.disabled = true;
      noticeHold += 1;
      show("جارٍ عرض القروبات…", true);
      try {
        const data = await post("/groups", {auth_token: auth, chat_pin: chat});
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

    scan.addEventListener("click", async () => {
      if (busy || starting) {
        show("المهمة تعمل الآن. انتظر حتى تنتهي.", false);
        return;
      }
      const auth = token.value.trim();
      const chat = pin.value.trim();
      const name = (picked || "").trim();
      if (!auth) { show("أدخل رمز الجلسة.", false); return; }
      if (!/^\d{4,8}$/.test(chat)) { show("أدخل رمز الدردشة.", false); return; }
      if (!name) { show("حدد القروب ثم اضغط فحص الحسابات الموقوفة.", false); return; }
      scan.disabled = true;
      noticeHold += 1;
      show("جارٍ فحص حسابات " + name + "…", true);
      try {
        const data = await post("/members/scan", {auth_token: auth, chat_pin: chat, group: name});
        show(data.message || "بدأ الفحص.", !!data.success);
      } catch (_) {
        show("تعذر بدء الفحص.", false);
      } finally {
        noticeHold = Math.max(0, noticeHold - 1);
        await refresh();
      }
    });

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
    if state["watching"] or state.get("checking") or state.get("scanning"):
        return jsonify(success=False, message="أوقف المهمة أولًا."), 409
    token = str(data.get("auth_token") or "").strip()
    pin = str(data.get("chat_pin") or "").strip()
    if not valid_auth_token(token):
        return jsonify(success=False, message="رمز الجلسة غير صالح."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة ٤ إلى ٨ أرقام."), 400
    set_status("جارٍ عرض القروبات…", True)

    def work(page):
        if not ensure_logged_in(page):
            return None, "الجلسة غير مسجّلة الدخول."
        error = open_chat(page, pin)
        if error:
            return None, error
        show_groups_only(page)
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
    if not valid_auth_token(token):
        return jsonify(success=False, message="رمز الجلسة غير صالح."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة ٤ إلى ٨ أرقام."), 400
    if not group_name:
        return jsonify(success=False, message="اختر قروبًا."), 400
    thread = watch_thread
    with state_lock:
        running = bool(state["watching"] or state.get("checking") or state.get("scanning"))
    if running or (thread and thread.is_alive()):
        return jsonify(success=False, message="المهمة تعمل الآن. انتظر حتى تنتهي."), 409
    abort_flag.clear()
    stop_flag.clear()
    with state_lock:
        state["auth_token"] = token
        state["chat_pin"] = pin
        state["group"] = group_name
        state["watching"] = True
        state["ok"] = True
        state["items"] = []
        state["audits"] = []
        state["no_fire"] = []
        state["no_fire_ready"] = False
        write_message(f"جارٍ فتح «{group_name}»…", True)
    save_state()
    stop_flag.clear()
    watch_thread = threading.Thread(target=watch_loop, args=(token, pin, group_name), daemon=True)
    watch_thread.start()
    return jsonify(success=True, message=f"بدأت مراقبة «{group_name}».")


@app.post("/members/scan")
def members_scan():
    global watch_thread
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    token = str(data.get("auth_token") or "").strip()
    pin = str(data.get("chat_pin") or "").strip()
    group_name = re.sub(r"\s+", " ", str(data.get("group") or "")).strip()
    if not valid_auth_token(token):
        return jsonify(success=False, message="رمز الجلسة غير صالح."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة ٤ إلى ٨ أرقام."), 400
    if not group_name:
        return jsonify(success=False, message="اختر قروبًا."), 400
    thread = watch_thread
    with state_lock:
        running = bool(state["watching"] or state.get("checking") or state.get("scanning"))
    if running or (thread and thread.is_alive()):
        return jsonify(success=False, message="المهمة تعمل الآن. انتظر حتى تنتهي."), 409
    abort_flag.clear()
    stop_flag.clear()
    with state_lock:
        state["auth_token"] = token
        state["chat_pin"] = pin
        state["group"] = group_name
        state["scanning"] = True
        state["watching"] = False
        state["checking"] = False
        state["ok"] = True
        write_message(f"جارٍ فحص حسابات «{group_name}»…", True)
    save_state()
    watch_thread = threading.Thread(target=scan_suspended_members, args=(token, pin, group_name), daemon=True)
    watch_thread.start()
    return jsonify(success=True, message=f"بدأ فحص حسابات «{group_name}».")


@app.post("/watch/stop")
def watch_stop():
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    if not state.get("watching"):
        return jsonify(success=False, message="لا توجد مراقبة تعمل الآن."), 409
    stop_flag.set()
    with state_lock:
        state["checking"] = True
    set_status("توقفت المراقبة. يتم تغيير الاسم ثم الرسالة ثم فحص المحذوف ثم الشعلة.", True, True)
    return jsonify(success=True, message="يتم تغيير الاسم وإرسال الرسالة ثم فحص الشعلة لمدة ٣٠ دقيقة.")


@app.post("/watch/abort")
def watch_abort():
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="أعد تحميل الصفحة."), 400
    thread = watch_thread
    with state_lock:
        running = bool(state["watching"] or state.get("checking") or state.get("scanning"))
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
