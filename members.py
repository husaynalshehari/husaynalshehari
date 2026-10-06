#!/usr/bin/env python3
"""Members collector for X group chats.

Logs in with auth_token, opens the DM inbox with the chat PIN, opens a group,
climbs up to the chosen tweet, then goes down to the newest message and lists
the members who sent tweets (with how many each sent). Nothing is liked,
reposted, bookmarked or commented.

Its data (sessions, group ids, browser profiles) lives in members_data/ beside
this file, separate from the main script.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template_string, request, session
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


sessions_lock = threading.Lock()


DATA_DIR = Path(__file__).resolve().with_name("members_data")


SESSIONS_PATH = DATA_DIR / "sessions.json"


saved_sessions: list[dict] = []


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
    const target = clean(typeof wanted === "object" ? (wanted.name || "") : wanted);
    const wantedId = typeof wanted === "object" ? String(wanted.testid || "") : "";
    const open = (row, how) => {
      const link = row.querySelector('a[href*="/i/chat/"], a[href*="/messages/"]');
      if (link) link.click();
      else row.click();
      return {clicked: true, how, matched: nameOf(row), groups: [], rows: rows.length, loading: false};
    };
    // 1) The group's own id: stays the same when the group is renamed.
    if (wantedId) {
      for (const row of rows) if ((row.getAttribute("data-testid") || "") === wantedId) return open(row, "id");
    }
    // 2) The exact name.
    for (const row of rows) if (nameOf(row) === target) return open(row, "name");
    // 3) A very similar name: only a few characters changed (usually near the end).
    const chars = (text) => Array.from(text || "");
    const distance = (a, b) => {
      let prev = Array.from({length: b.length + 1}, (_, j) => j);
      for (let i = 1; i <= a.length; i++) {
        const cur = [i];
        for (let j = 1; j <= b.length; j++) {
          cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        }
        prev = cur;
      }
      return prev[b.length];
    };
    const shared = (a, b) => { let n = 0; while (n < a.length && n < b.length && a[n] === b[n]) n++; return n; };
    const targetChars = chars(target);
    const allowed = Math.min(6, Math.max(2, Math.floor(targetChars.length * 0.35)));
    let best = null, bestDistance = Infinity, tie = false;
    for (const row of rows) {
      const id = row.getAttribute("data-testid") || "";
      const desc = row.getAttribute("aria-description") || "";
      if (!(/dm-conversation-item-g\d/.test(id) || /جماعية|group chat/i.test(desc))) continue;
      const nameChars = chars(nameOf(row));
      if (shared(targetChars, nameChars) < Math.min(4, targetChars.length)) continue;
      const d = distance(targetChars, nameChars);
      if (d > allowed) continue;
      if (d < bestDistance) { best = row; bestDistance = d; tie = false; }
      else if (d === bestDistance) tie = true;
    }
    if (best && !tie) return open(best, "similar");
    return {clicked: false, groups: [], rows: rows.length, loading: false};
  }
  const groups = [];
  const ids = [];
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
    ids.push(id);
  }
  const loading = queryAll('[data-testid="dm-conversation-list-loading-footer"]').some((el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
  return {clicked: false, groups, ids, rows: rows.length, loading};
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


GROUP_READ_TWEETS_JS = r"""
(arg) => {
  let stopId = "";
  let dir = 0;
  if (arg && typeof arg === "object") {
    stopId = String(arg.stopId || "");
    dir = arg.dir === 1 || arg.dir === -1 ? arg.dir : 0;
  } else {
    stopId = String(arg || "");
  }
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
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
  const collectDeep = (root, selector, out) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(selector).forEach((node) => out.push(node));
    root.querySelectorAll("*").forEach((node) => {
      if (node.shadowRoot) collectDeep(node.shadowRoot, selector, out);
    });
  };
  const list = queryAll('[data-testid="dm-message-list"]')[0]
    || queryAll('[data-testid="dm-conversation-content"]')[0]
    || queryAll('[data-testid="dm-message-scroller"]')[0];
  if (!list) return {tweets: [], pending: 0};
  const bubbles = [];
  collectDeep(list, '[data-testid^="message-"]', bubbles);
  const isBubble = (el) => {
    const id = el.getAttribute("data-testid") || "";
    return id.startsWith("message-") && !id.startsWith("message-text-");
  };
  const messages = bubbles.filter((el) => {
    if (!isBubble(el)) return false;
    const nested = [];
    collectDeep(el, '[data-testid^="message-"]', nested);
    return !nested.some((node) => node !== el && isBubble(node));
  });
  const targets = messages.length ? messages : [list];
  // Who sent each message: the avatar / profile link next to the bubble (not inside the
  // shared tweet card). Messages without one belong to the same run as the next message
  // below that has one; messages hugging the right edge are this account's own.
  const reserved = new Set(["home", "explore", "messages", "notifications", "i", "settings", "search", "compose", "jobs", "premium"]);
  const upOne = (node) => node.parentElement || (node.parentNode && node.parentNode.nodeType === 11 ? node.parentNode.host : null);
  const isCardPart = (el, stopAt) => {
    let node = el;
    while (node && node !== stopAt) {
      const tag = node.tagName || "";
      const test = (node.getAttribute && node.getAttribute("data-testid")) || "";
      const href = (node.getAttribute && node.getAttribute("href")) || "";
      if (tag === "ARTICLE" || /tweet|card|quote|attachment|media|photo/i.test(test) || /status\//.test(href)) return true;
      node = upOne(node);
    }
    return false;
  };
  const rowOf = (bubble) => {
    let row = bubble;
    for (let level = 0; level < 10; level += 1) {
      const parent = upOne(row);
      if (!parent || parent === list) break;
      if (messages.some((other) => other !== bubble && parent.contains(other))) break;
      row = parent;
    }
    return row;
  };
  const handleIn = (row) => {
    const avatars = [];
    collectDeep(row, '[data-testid^="UserAvatar-Container-"]', avatars);
    for (const el of avatars) {
      const name = (el.getAttribute("data-testid") || "").slice("UserAvatar-Container-".length);
      if (/^[A-Za-z0-9_]{1,15}$/.test(name) && !isCardPart(el, row)) return name;
    }
    const links = [];
    collectDeep(row, 'a[href^="/"]', links);
    for (const el of links) {
      const match = (el.getAttribute("href") || "").match(/^\/([A-Za-z0-9_]{1,15})\/?$/);
      if (match && !reserved.has(match[1].toLowerCase()) && !isCardPart(el, row)) return match[1];
    }
    return "";
  };
  const listRect = list.getBoundingClientRect();
  const senders = new Map();
  let carry = "";
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const bubble = messages[index];
    const row = rowOf(bubble);
    const handle = handleIn(row);
    const rect = bubble.getBoundingClientRect();
    const own = !handle && rect.width > 0 && (listRect.right - rect.right) + 40 < (rect.left - listRect.left);
    if (own) {
      carry = "";
      senders.set(bubble, {sender: "", own: true});
      continue;
    }
    if (handle) carry = handle;
    senders.set(bubble, {sender: handle || carry, own: false});
  }
  const statusRe = /(?:https?:\/\/)?(?:www\.)?(?:x|twitter)\.com\/(?:i\/web\/status\/|i\/status\/)?([A-Za-z0-9_]{1,15}\/)?status\/(\d{6,25})/gi;
  const bareRe = /status\/(\d{6,25})/;
  const tweets = [];
  const seen = new Set();
  let pending = 0;
  const climb = (node) => {
    const chain = [];
    let el = node;
    while (el && chain.length < 50) {
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
  const insideQuote = (node) => climb(node).some((el) => {
    const test = (el.getAttribute && el.getAttribute("data-testid")) || "";
    return /quote/i.test(test);
  });
  const pushId = (statusId, href, bubble, text, hit) => {
    if (!statusId) return;
    const who = senders.get(bubble) || {sender: "", own: false};
    if (seen.has(statusId)) {
      const existing = tweets.find((row) => row.statusId === statusId);
      if (existing && hit) existing.hit = true;
      if (existing && !existing.sender && who.sender) existing.sender = who.sender;
      return;
    }
    seen.add(statusId);
    const rect = bubble.getBoundingClientRect();
    tweets.push({
      sender: who.sender,
      own: who.own,
      statusId,
      url: href || ("https://x.com/i/web/status/" + statusId),
      text: (text || "تغريدة").slice(0, 500),
      top: Math.round(rect.top || 0),
      hit: !!hit
    });
  };
  for (const bubble of targets) {
    const links = [];
    collectDeep(bubble, '[href*="status/"]', links);
    const candidates = [];
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      const statusId = statusOf(href);
      if (!statusId || insideQuote(link)) continue;
      const nestedUnderOther = climb(link).some((el) => {
        if (el === link || !el.getAttribute) return false;
        const parentId = statusOf(el.getAttribute("href") || "");
        return parentId && parentId !== statusId;
      });
      if (nestedUnderOther) continue;
      candidates.push({statusId, href, link, depth: climb(link).length});
    }
    let foundHere = 0;
    if (candidates.length) {
      candidates.sort((a, b) => a.depth - b.depth);
      const outer = candidates[0];
      const textNodes = [];
      collectDeep(bubble, '[data-testid="tweetText"]', textNodes);
      const ownText = textNodes.find((node) => !insideQuote(node));
      const text = clean((ownText && ownText.innerText) || "");
      pushId(outer.statusId, outer.href, bubble, text, !!(stopId && outer.statusId === stopId));
      foundHere = 1;
    }
    const blob = clean(bubble.innerText || "");
    if (!foundHere) {
      statusRe.lastIndex = 0;
      const match = statusRe.exec(blob);
      if (match) {
        pushId(match[2], match[0], bubble, blob.slice(0, 280), !!(stopId && match[2] === stopId));
        foundHere = 1;
      }
    }
    if (!foundHere) {
      const match = bareRe.exec(blob);
      if (match) {
        pushId(match[1], "https://x.com/i/web/status/" + match[1], bubble, blob.slice(0, 280), !!(stopId && match[1] === stopId));
        foundHere = 1;
      }
    }
    const cardish = [];
    collectDeep(bubble, '[data-testid="tweetText"], article, [data-testid="card.wrapper"], [data-testid="tweetPhoto"]', cardish);
    if (!foundHere && cardish.length) pending += 1;
  }
  const scroller = queryAll('[data-testid="dm-message-scroller"]')[0] || list;
  const ids = messages.map((el) => el.getAttribute("data-testid") || "");
  const fingerprint = ids.slice(0, 8).concat(ids.slice(-8)).join("|");
  let loading = false;
  const spinner = list.querySelector('[data-testid="dm-message-list-spinner-slot"], [data-testid="dm-message-list-spinner"], [data-testid*="spinner"], [role="progressbar"]');
  if (spinner) {
    const rect = spinner.getBoundingClientRect();
    loading = rect.width > 8 && rect.height > 8;
  }
  let moved = 0;
  let atOlderEdge = false;
  if (scroller) {
    const beforeTop = scroller.scrollTop || 0;
    atOlderEdge = beforeTop <= 48;
    const client = scroller.clientHeight || 800;
    const step = dir > 0
      ? Math.max(560, Math.min(1200, Math.round(client * 0.75)))
      : Math.max(400, Math.min(800, Math.round(client * 0.5)));
    if (dir === -1 || dir === 1) {
      if (dir < 0 && beforeTop <= 48) {
        scroller.scrollTop = 0;
        try { scroller.dispatchEvent(new WheelEvent("wheel", {deltaY: -640, bubbles: true, cancelable: true})); } catch (error) {}
      } else if (dir > 0 && beforeTop + client >= scroller.scrollHeight - 80) {
        scroller.scrollTop = scroller.scrollHeight;
      } else {
        const next = dir < 0 ? Math.max(0, beforeTop - step) : Math.min(scroller.scrollHeight, beforeTop + step);
        scroller.scrollTop = next;
      }
      try { scroller.dispatchEvent(new Event("scroll", {bubbles: true})); } catch (error) {}
      moved = Math.abs((scroller.scrollTop || 0) - beforeTop);
    }
  }
  return {
    tweets,
    pending,
    messages: messages.length,
    fingerprint,
    loading,
    atOlderEdge,
    moved: Math.round(moved),
    scrollHeight: scroller ? Math.round(scroller.scrollHeight) : 0,
    scrollTop: scroller ? Math.round(scroller.scrollTop) : 0,
    clientHeight: scroller ? Math.round(scroller.clientHeight) : 0
  };
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
  const spinner = list && (list.querySelector('[data-testid="dm-message-list-spinner-slot"], [data-testid="dm-message-list-spinner"], [data-testid*="spinner"]') || list.querySelector('[role="progressbar"]'));
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


CLOSE_POPUPS_JS = r"""
() => {
  const words = /^(close|dismiss|not now|maybe later|got it|skip|no thanks|cancel|إغلاق|اغلاق|ليس الآن|لاحقًا|لاحقا|فهمت|تخطي|تخطّي|لا شكرًا|لا شكرا|إلغاء)$/i;
  const chat = '[data-testid="dm-message-list"], [data-testid="dm-message-scroller"], [data-testid="dm-conversation-content"], [data-testid="dm-inbox-panel"]';
  const layers = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"], [data-testid="sheetDialog"], [data-testid="confirmationSheetDialog"]')]
    .filter((el) => el.getClientRects().length && !el.closest(chat) && !el.querySelector(chat));
  const closed = [];
  for (const layer of layers) {
    const buttons = [...layer.querySelectorAll('button, [role="button"]')].filter((node) => node.getClientRects().length);
    const hit = buttons.find((node) => {
      const label = (node.getAttribute("aria-label") || "").trim();
      const text = (node.innerText || "").replace(/\s+/g, " ").trim();
      const testid = node.getAttribute("data-testid") || "";
      return words.test(label) || words.test(text) || testid === "app-bar-close" || testid === "confirmationSheetCancel";
    });
    if (hit) {
      hit.click();
      closed.push(layer.getAttribute("data-testid") || layer.getAttribute("aria-label") || "dialog");
    }
  }
  return closed;
}
"""


def close_popups(page) -> int:
    """Close pop-up dialogs that cover the page. The chat itself is never touched."""
    try:
        closed = page.evaluate(CLOSE_POPUPS_JS) or []
    except Exception:
        return 0
    if closed:
        if os.environ.get("COLLECT_DEBUG"):
            print(f"closed popups: {closed}", flush=True)
        page.wait_for_timeout(400)
    return len(closed)


BLOCK_MEDIA = True


BLOCKED_RESOURCE_TYPES = {"image", "media"}


def block_media(context) -> None:
    if not BLOCK_MEDIA:
        return
    state = {"allow": False}
    context._media_state = state

    def handle(route):
        request = route.request
        try:
            if not state["allow"] and (request.resource_type in BLOCKED_RESOURCE_TYPES or "video.twimg.com" in request.url):
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass

    try:
        context.route("**/*", handle)
    except Exception:
        pass


NORMAL_VIEWPORT_HEIGHT = 1800


profile_locks: dict[str, threading.Lock] = {}


profile_locks_guard = threading.Lock()


def profile_key(auth_token: str) -> str:
    """Folder name for an account's browser profile. Derived from the token, never the token itself."""
    return hashlib.sha256((auth_token or "").encode("utf-8")).hexdigest()[:24]


def profile_lock(key: str) -> threading.Lock:
    with profile_locks_guard:
        lock = profile_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            profile_locks[key] = lock
        return lock


def open_x_profile(playwright, auth_token: str, profile_dir: Path):
    """Open the account's permanent browser profile (created on first use)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
        viewport={"width": 1280, "height": NORMAL_VIEWPORT_HEIGHT},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    block_media(context)
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
    page = context.pages[0] if context.pages else context.new_page()
    for extra in context.pages[1:]:
        try:
            extra.close()
        except Exception:
            pass
    page.set_default_timeout(20_000)
    page.set_default_navigation_timeout(60_000)
    return context, page


def set_page_height(page, height: int) -> None:
    try:
        page.set_viewport_size({"width": 1280, "height": int(height)})
        page.wait_for_timeout(500)
    except Exception:
        pass


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


def read_session_username(page) -> str:
    """Read the logged-in handle from the home sidebar, not the target profile."""
    try:
        handle = page.evaluate(
            """() => {
              const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose"]);
              const profile = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
              if (profile) {
                const href = profile.getAttribute("href") || "";
                const match = href.match(/^\\/([A-Za-z0-9_]{1,15})\\/?$/);
                if (match && !reserved.has(match[1].toLowerCase())) return match[1];
              }
              const nodes = document.querySelectorAll('[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="UserName"]');
              for (const node of nodes) {
                const match = ((node.innerText || node.textContent || "")).match(/@([A-Za-z0-9_]{1,15})/);
                if (match) return match[1];
              }
              return "";
            }"""
        ) or ""
    except Exception:
        handle = ""
    handle = str(handle).strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return handle
    return ""


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
    deadline = time.monotonic() + 55
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
        page.wait_for_timeout(700)
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


GROUP_IDS_PATH = DATA_DIR / "group_ids.json"


group_ids_lock = threading.Lock()


group_ids: dict[str, str] = {}


def load_group_ids() -> None:
    loaded = read_private_json(GROUP_IDS_PATH) or {}
    rows = loaded.get("groups")
    if isinstance(rows, dict):
        with group_ids_lock:
            group_ids.update({str(k): str(v) for k, v in rows.items() if k and v})


def remember_group_id(name: str, testid: str) -> None:
    """Remember each group's id (it does not change when the group is renamed)."""
    with group_ids_lock:
        if group_ids.get(name) == testid:
            return
        group_ids[name] = testid
        try:
            write_private_json(GROUP_IDS_PATH, {"version": 1, "groups": dict(group_ids)})
        except Exception:
            pass


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
        ids = payload.get("ids") or []
        for index, name in enumerate(payload.get("groups") or []):
            clean = re.sub(r"\s+", " ", str(name)).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            found.append(clean)
            if index < len(ids) and ids[index]:
                remember_group_id(clean, str(ids[index]))
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


def open_named_group(page, name: str) -> str:
    """Find the group in the inbox (waiting up to 10 seconds for it), click it once,
    then wait for its messages. No retries: on failure the task stops with a message.

    The group is matched by its saved id first (renaming does not change it), then by
    exact name, then by a name that starts the same and differs only in the last few
    characters. Returns the name the group has now."""
    wanted = re.sub(r"\s+", " ", name).strip()
    with group_ids_lock:
        saved_id = group_ids.get(wanted, "")
    started = time.monotonic()
    clicked = False
    current_name = wanted
    while time.monotonic() - started < 10:
        try:
            payload = page.evaluate(READ_GROUPS_JS, {"name": wanted, "testid": saved_id}) or {}
        except Exception:
            payload = {}
        if payload.get("clicked"):
            clicked = True
            current_name = re.sub(r"\s+", " ", str(payload.get("matched") or wanted)).strip() or wanted
            if os.environ.get("COLLECT_DEBUG"):
                print(f"group opened by {payload.get('how')}: wanted={wanted!r} now={current_name!r}", flush=True)
            break
        if time.monotonic() - started > 3:
            try:
                page.evaluate(SCROLL_INBOX_JS)
            except Exception:
                pass
        page.wait_for_timeout(700)
    if not clicked:
        raise RuntimeError(f"لم يظهر القروب «{wanted}» في الدردشة خلال ١٠ ثوانٍ. توقفت المهمة.")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            state = page.evaluate(GROUP_CHAT_SCROLL_JS, "state") or {}
        except Exception:
            state = {}
        if state.get("hasList") and state.get("messages"):
            page.wait_for_timeout(800)
            return current_name
        page.wait_for_timeout(300)
    raise RuntimeError("فُتح القروب لكن رسائله لم تظهر. توقفت المهمة.")


def group_chat_state(page) -> dict:
    try:
        return page.evaluate(GROUP_CHAT_SCROLL_JS, "state") or {}
    except Exception:
        return {}


def focus_group_scroller(page) -> None:
    locator = page.locator('[data-testid="dm-message-scroller"]').first
    try:
        box = locator.bounding_box()
    except Exception:
        box = None
    if box and box.get("height", 0) >= 40:
        page.mouse.move(box["x"] + box["width"] * 0.45, box["y"] + min(box["height"] * 0.42, 640))


def tweet_url(raw_url: str, status_id: str) -> str:
    raw = (raw_url or "").strip()
    for match in re.finditer(rf"(?<![A-Za-z0-9_])/([A-Za-z0-9_]{{1,15}})/status/{status_id}(?!\d)", raw):
        owner = match.group(1)
        if owner.lower() in {"i", "web", "intent", "share"}:
            continue
        return f"https://x.com/{owner}/status/{status_id}"
    return f"https://x.com/i/web/status/{status_id}"


SCAN_STOP_JS = r"""
(arg) => {
  let wanted = "";
  let dir = 0;
  if (arg && typeof arg === "object") {
    wanted = String(arg.stopId || "");
    dir = arg.dir === 1 || arg.dir === -1 ? arg.dir : 0;
  } else {
    wanted = String(arg || "");
  }
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
  const list = query('[data-testid="dm-message-list"]') || query('[data-testid="dm-conversation-content"]') || query('[data-testid="dm-message-scroller"]');
  if (!list) return {hit: false, pending: 0, oldest: "", count: 0, hitTop: 0};
  const all = [...list.querySelectorAll('[data-testid^="message-"]')].filter((el) => {
    const id = el.getAttribute("data-testid") || "";
    return id.startsWith("message-") && !id.startsWith("message-text-");
  });
  const messages = all.filter((el) => ![...el.querySelectorAll('[data-testid^="message-"]')].some((node) => {
    const id = node.getAttribute("data-testid") || "";
    return node !== el && id.startsWith("message-") && !id.startsWith("message-text-");
  }));
  let hit = false;
  let hitTop = 0;
  let pending = 0;
  for (const bubble of messages) {
    const links = [...bubble.querySelectorAll('a[href*="status/"]')].map((node) => node.getAttribute("href") || "");
    const blob = (bubble.innerText || "") + "\n" + links.join("\n");
    if (wanted && blob.includes(wanted)) {
      hit = true;
      hitTop = Math.round(bubble.getBoundingClientRect().top || 0);
      break;
    }
    const card = bubble.querySelector('[data-testid="tweetText"], article, [data-testid="card.wrapper"], [data-testid="tweetPhoto"]');
    if (card && !/status\/\d{6,25}/.test(blob)) pending += 1;
  }
  const oldest = messages.length ? (messages[0].getAttribute("data-testid") || "") : "";
  const scroller = query('[data-testid="dm-message-scroller"]') || list;
  let loading = false;
  const spinner = list.querySelector('[data-testid="dm-message-list-spinner-slot"], [data-testid="dm-message-list-spinner"], [data-testid*="spinner"], [role="progressbar"]');
  if (spinner) {
    const rect = spinner.getBoundingClientRect();
    loading = rect.width > 8 && rect.height > 8;
  }
  let moved = 0;
  if (scroller && !hit && (dir === -1 || dir === 1)) {
    const beforeTop = scroller.scrollTop || 0;
    const client = scroller.clientHeight || 800;
    const step = Math.max(400, Math.min(800, Math.round(client * 0.5)));
    if (dir < 0 && beforeTop <= 48) {
      scroller.scrollTop = 0;
      try { scroller.dispatchEvent(new WheelEvent("wheel", {deltaY: -640, bubbles: true, cancelable: true})); } catch (error) {}
    } else if (dir > 0 && beforeTop + client >= scroller.scrollHeight - 80) {
      scroller.scrollTop = scroller.scrollHeight;
    } else {
      const next = dir < 0 ? Math.max(0, beforeTop - step) : Math.min(scroller.scrollHeight, beforeTop + step);
      scroller.scrollTop = next;
    }
    try { scroller.dispatchEvent(new Event("scroll", {bubbles: true})); } catch (error) {}
    moved = Math.abs((scroller.scrollTop || 0) - beforeTop);
  }
  return {
    hit,
    pending,
    oldest,
    count: messages.length,
    hitTop,
    loading,
    moved: Math.round(moved),
    scrollHeight: scroller ? Math.round(scroller.scrollHeight) : 0,
    scrollTop: scroller ? Math.round(scroller.scrollTop) : 0,
    clientHeight: scroller ? Math.round(scroller.clientHeight) : 0
  };
}
"""


STEP_OLDER_JS = r"""
(pixels) => {
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
  const scroller = query('[data-testid="dm-message-scroller"]') || query('[data-testid="dm-message-list"]');
  if (!scroller) return {ok: false};
  const before = scroller.scrollTop;
  const atTop = before <= 48;
  if (!atTop) scroller.scrollTop = Math.max(0, before - Math.max(80, pixels));
  else scroller.scrollTop = 0;
  scroller.dispatchEvent(new Event("scroll", {bubbles: true}));
  return {ok: true, before: Math.round(before), after: Math.round(scroller.scrollTop), atTop, height: Math.round(scroller.scrollHeight)};
}
"""


def collect_group_tweets(page, limit: int, on_progress=None, stop_id: str = "", should_stop=None) -> tuple[list[dict], bool]:
    found: list[dict] = []
    seen: set[str] = set()
    by_id: dict[str, dict] = {}
    reached = False

    def fill_sender(status_id: str, row: dict) -> None:
        entry = by_id.get(status_id)
        if entry is not None and not entry["sender"] and not entry["own"]:
            entry["sender"] = str(row.get("sender") or "")
            entry["own"] = bool(row.get("own"))

    def pull() -> tuple[int, int, list[str]]:
        nonlocal reached
        try:
            payload = page.evaluate(GROUP_READ_TWEETS_JS, "") or {}
        except Exception:
            payload = {}
        if isinstance(payload, list):
            rows, pending = payload, 0
        else:
            rows, pending = payload.get("tweets") or [], int(payload.get("pending") or 0)
        rows = [row for row in rows if isinstance(row, dict)]
        rows.sort(key=lambda row: float(row.get("top") or 0), reverse=True)
        added = 0
        visible: list[str] = []
        for row in rows:
            if reached or (not stop_id and len(found) >= limit):
                break
            status_id = re.sub(r"\D", "", str(row.get("statusId") or ""))
            if not status_id:
                continue
            visible.append(status_id)
            fill_sender(status_id, row)
            if status_id in seen:
                if stop_id and status_id == stop_id:
                    reached = True
                    break
                continue
            seen.add(status_id)
            found.append({
                "url": tweet_url(str(row.get("url") or ""), status_id),
                "text": re.sub(r"\s+", " ", str(row.get("text") or "")).strip()[:280] or "تغريدة",
                "sender": str(row.get("sender") or ""),
                "own": bool(row.get("own")),
            })
            by_id[status_id] = found[-1]
            added += 1
            if stop_id and status_id == stop_id:
                reached = True
                break
        return added, pending, visible

    def settle(tries: int = 7) -> tuple[int, list[str]]:
        added_total = 0
        pending = 0
        visible: list[str] = []
        for _ in range(tries):
            added, pending, visible = pull()
            added_total += added
            if on_progress:
                on_progress(len(found), 0 if stop_id else limit, "collect")
            if reached or (not stop_id and len(found) >= limit):
                return added_total, visible
            if pending:
                page.wait_for_timeout(100)
                continue
            page.wait_for_timeout(40)
            added, pending, visible = pull()
            added_total += added
            if reached or (not stop_id and len(found) >= limit):
                return added_total, visible
            if not pending:
                return added_total, visible
        return added_total, visible

    def read_rows() -> tuple[list[dict], int]:
        try:
            payload = page.evaluate(GROUP_READ_TWEETS_JS, "") or {}
        except Exception:
            payload = {}
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)], 0
        rows = payload.get("tweets") or []
        return [row for row in rows if isinstance(row, dict)], int(payload.get("pending") or 0)

    def row_id(row: dict) -> str:
        return re.sub(r"\D", "", str(row.get("statusId") or ""))

    def add_tweet(status_id: str, row: dict) -> bool:
        if not status_id:
            return False
        if status_id in seen:
            fill_sender(status_id, row)
            return False
        seen.add(status_id)
        found.append({
            "url": tweet_url(str(row.get("url") or ""), status_id),
            "text": re.sub(r"\s+", " ", str(row.get("text") or "")).strip()[:280] or "تغريدة",
            "sender": str(row.get("sender") or ""),
            "own": bool(row.get("own")),
        })
        by_id[status_id] = found[-1]
        return True

    def at_latest(state: dict) -> bool:
        top = int(state.get("scrollTop") or 0)
        height = int(state.get("scrollHeight") or 0)
        client = int(state.get("clientHeight") or 0) or 700
        return height > 0 and top + client >= height - 80

    if stop_id:
        spotted = False
        quiet = 0
        rounds = 0
        hit_top = None
        last_oldest = ""
        last_height = 0
        max_seek = 8000

        def scan_stop(direction: int) -> dict:
            try:
                payload = page.evaluate(SCAN_STOP_JS, {"stopId": stop_id, "dir": direction}) or {}
            except Exception:
                payload = {}
            return payload if isinstance(payload, dict) else {}

        # Climb with real mouse-wheel input (what X's chat listens to for loading
        # older messages). Progress = the oldest mounted message changes, the
        # scroll position moves, or the list grows. Unfinished tweet cards no
        # longer keep the loop alive forever, and the climb has a time limit.
        last_top = -1
        deadline = time.monotonic() + 360
        while not spotted and rounds < max_seek and quiet < 8 and time.monotonic() < deadline:
            if should_stop and should_stop():
                raise JobStopped
            close_popups(page)
            scan = scan_stop(0)
            if scan.get("hit"):
                spotted = True
                hit_top = float(scan.get("hitTop") or 0)
                break
            client = int(scan.get("clientHeight") or 0) or 800
            step = max(250, min(500, int(client * 0.2)))
            before_top = int(scan.get("scrollTop") or 0)
            focus_group_scroller(page)
            try:
                page.mouse.wheel(0, -step)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            state = scan_stop(0)
            if state.get("hit"):
                spotted = True
                hit_top = float(state.get("hitTop") or 0)
                break
            top_now = int(state.get("scrollTop") or 0)
            if abs(top_now - before_top) <= 8 and top_now > 48:
                try:
                    page.evaluate(STEP_OLDER_JS, step)
                except Exception:
                    pass
                page.wait_for_timeout(300)
                state = scan_stop(0)
                top_now = int(state.get("scrollTop") or 0)
            oldest = str(state.get("oldest") or "")
            height = int(state.get("scrollHeight") or 0)
            moved = last_top >= 0 and abs(top_now - last_top) > 8
            grew = height > last_height + 24 or bool(oldest and last_oldest and oldest != last_oldest)
            if moved or grew:
                quiet = 0
            else:
                quiet += 1
            if oldest:
                last_oldest = oldest
            last_height = max(last_height, height)
            last_top = top_now
            rounds += 1
            if on_progress and rounds % 3 == 0:
                on_progress(rounds, 0, "seek")
            if os.environ.get("COLLECT_DEBUG"):
                print(
                    f"seek round={rounds} quiet={quiet} top={top_now} h={height} oldest={oldest} msgs={state.get('count')} pending={state.get('pending')}",
                    flush=True,
                )
        if not spotted:
            return [], False
        rows, _pending = read_rows()
        target_top = hit_top
        for row in rows:
            if row_id(row) == stop_id:
                target_top = float(row.get("top") or 0)
                break
        older: set[str] = set()
        for row in rows:
            status_id = row_id(row)
            top = float(row.get("top") or 0)
            if target_top is not None and top < target_top - 12:
                if status_id and status_id != stop_id:
                    older.add(status_id)
                continue
            add_tweet(status_id, row)
        if stop_id not in {row_id(row) for row in rows}:
            add_tweet(stop_id, {"url": f"https://x.com/i/web/status/{stop_id}", "text": "تغريدة"})
        if on_progress:
            on_progress(len(found), 0, "collect")
        stall = 0
        rounds = 0
        last_fp = ""
        while rounds < max_seek and stall < 5:
            if should_stop and should_stop():
                raise JobStopped
            close_popups(page)
            try:
                payload = page.evaluate(GROUP_READ_TWEETS_JS, {"dir": 1, "stopId": stop_id}) or {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            down_rows = [row for row in payload.get("tweets") or [] if isinstance(row, dict)]
            stop_row = next((row for row in down_rows if row_id(row) == stop_id), None)
            stop_top = float(stop_row.get("top") or 0) if stop_row else None
            for row in down_rows:
                status_id = row_id(row)
                if not status_id or status_id in older:
                    continue
                if stop_top is not None and status_id != stop_id and float(row.get("top") or 0) < stop_top - 12:
                    older.add(status_id)
                    continue
                add_tweet(status_id, row)
            if on_progress and found:
                on_progress(len(found), 0, "collect")
            fp = str(payload.get("fingerprint") or "")
            height = int(payload.get("scrollHeight") or 0)
            top = int(payload.get("scrollTop") or 0)
            client = int(payload.get("clientHeight") or 0) or 700
            latest = height > 0 and top + client >= height - 80
            pending = int(payload.get("pending") or 0)
            moved = bool(fp and last_fp and fp != last_fp) or int(payload.get("moved") or 0) > 8
            if pending or moved:
                stall = 0
                page.wait_for_timeout(1000)
            else:
                stall += 1
                page.wait_for_timeout(1000)
            last_fp = fp or last_fp
            rounds += 1
            if os.environ.get("COLLECT_DEBUG"):
                print(f"down round={rounds} found={len(found)} stall={stall} top={top} h={height} latest={latest}", flush=True)
        return found, True

    for _ in range(8):
        state = group_chat_state(page)
        if state.get("messages"):
            break
        page.wait_for_timeout(100)
    settle(1)
    stall = 0
    rounds = 0
    stall_limit = 12
    max_rounds = min(5000, max(400, (limit or 400) * 8))
    last_fp = ""
    last_h = 0

    while not reached and len(found) < limit and rounds < max_rounds and stall < stall_limit:
        if should_stop and should_stop():
            raise JobStopped
        before_count = len(found)
        close_popups(page)
        try:
            payload = page.evaluate(GROUP_READ_TWEETS_JS, {"dir": -1, "stopId": ""}) or {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        rows = payload.get("tweets") or []
        rows = [row for row in rows if isinstance(row, dict)]
        rows.sort(key=lambda row: float(row.get("top") or 0), reverse=True)
        for row in rows:
            if len(found) >= limit:
                break
            status_id = row_id(row)
            if not status_id or status_id in seen:
                continue
            add_tweet(status_id, row)
        if on_progress and len(found) != before_count:
            on_progress(len(found), limit, "collect")
        fp = str(payload.get("fingerprint") or "")
        height = int(payload.get("scrollHeight") or 0)
        at_edge = int(payload.get("scrollTop") or 0) <= 48 or bool(payload.get("atOlderEdge"))
        loading = bool(payload.get("loading")) or int(payload.get("pending") or 0) > 0
        grew = height > last_h + 24
        new_tweets = len(found) > before_count
        moved = bool(fp and last_fp and fp != last_fp) or int(payload.get("moved") or 0) > 8 or grew
        if new_tweets or loading or moved:
            stall = 0
        else:
            stall += 1
        if len(found) >= limit:
            break
        if loading:
            page.wait_for_timeout(2000)
        elif at_edge and not moved:
            page.wait_for_timeout(2000)
        else:
            page.wait_for_timeout(2000)
        last_fp = fp or last_fp
        last_h = max(last_h, height)
        rounds += 1
        if os.environ.get("COLLECT_DEBUG"):
            print(
                f"round={rounds} found={len(found)} stall={stall} top={payload.get('scrollTop')} h={height} load={loading} edge={at_edge}",
                flush=True,
            )
    return found[:limit], reached


class JobStopped(Exception):
    pass


def valid_auth_token(value: str) -> bool:
    return 20 <= len(value) <= 512


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


def read_private_json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        if path.exists():
            broken = path.with_suffix(path.suffix + ".corrupt")
            path.replace(broken)
        return None
    return loaded if isinstance(loaded, dict) else None


def persist_sessions_unlocked() -> None:
    write_private_json(SESSIONS_PATH, {"version": 1, "sessions": saved_sessions})


def load_sessions() -> None:
    global saved_sessions
    loaded = read_private_json(SESSIONS_PATH)
    rows = loaded.get("sessions") if loaded else None
    if not isinstance(rows, list):
        saved_sessions = []
        return
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = str(row.get("token") or "")
        username = str(row.get("username") or "").lstrip("@")
        if not valid_auth_token(token):
            continue
        if username and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
            username = ""
        cleaned.append({
            "id": str(row.get("id") or secrets.token_urlsafe(12)),
            "digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "token": token,
            "username": username,
            "chat_pin": str(row.get("chat_pin") or "") if valid_chat_pin(str(row.get("chat_pin") or "")) else "",
            "verified_at": float(row.get("verified_at") or 0),
        })
    cleaned.sort(key=lambda item: item["verified_at"], reverse=True)
    saved_sessions = cleaned[:50]


def remember_session(auth_token: str, username: str, chat_pin: str = "") -> None:
    token = (auth_token or "").strip()
    if not valid_auth_token(token):
        return
    username = (username or "").strip().lstrip("@")
    if username and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        username = ""
    pin = (chat_pin or "").strip()
    if pin and not valid_chat_pin(pin):
        pin = ""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with sessions_lock:
        found = next((row for row in saved_sessions if hmac.compare_digest(row["digest"], digest)), None)
        if found:
            if username:
                found["username"] = username
            if pin:
                found["chat_pin"] = pin
            found["token"] = token
            found["verified_at"] = now
        else:
            saved_sessions.append({
                "id": secrets.token_urlsafe(12),
                "digest": digest,
                "token": token,
                "username": username,
                "chat_pin": pin,
                "verified_at": now,
            })
        saved_sessions.sort(key=lambda item: item["verified_at"], reverse=True)
        del saved_sessions[50:]
        persist_sessions_unlocked()


# ---------------------------------------------------------------------------
# Members collector: open the group, climb to the chosen tweet, then go down
# and record who sent each tweet. Nothing is liked, reposted or commented.
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)
ACCESS_PATH = os.getenv("X_ACCESS_PATH", "members")
PROFILES_DIR = DATA_DIR / "profiles"
COLLECT_VIEWPORT_HEIGHT = int(os.getenv("COLLECT_VIEWPORT_HEIGHT", "30000"))
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def run_on_browser(auth_token: str, work):
    """Open this account's browser profile for one task, then close it."""
    key = profile_key(auth_token)
    with profile_lock(key):
        playwright = sync_playwright().start()
        context = None
        try:
            context, page = open_x_profile(playwright, auth_token, PROFILES_DIR / key)
            return work(page)
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass


def extract_status_id(value: str) -> str:
    value = (value or "").strip()
    match = re.search(r"status/(\d{6,25})", value)
    if match:
        return match.group(1)
    return value if re.fullmatch(r"\d{6,25}", value) else ""


def summarize_members(tweets: list[dict], own_handle: str) -> list[dict]:
    """Group the collected tweets by the member who sent them, in order of first appearance."""
    members: dict[str, dict] = {}
    for tweet in tweets:
        if tweet.get("own"):
            handle = own_handle or "أنت"
        else:
            handle = str(tweet.get("sender") or "").strip() or "غير معروف"
        row = members.setdefault(handle, {"handle": handle, "count": 0, "tweets": []})
        row["count"] += 1
        row["tweets"].append(tweet.get("url") or "")
    return list(members.values())


def publish(job_id: str, **fields) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job.update(fields)


def run_members_job(job_id: str, auth_token: str, pin: str, group_name: str, stop_id: str, group_wait_s: int) -> None:
    def on_progress(done: int, requested: int, phase: str) -> None:
        if (jobs.get(job_id) or {}).get("cancel"):
            raise JobStopped
        if phase == "seek":
            publish(job_id, message=f"يصعد إلى التغريدة المحددة… الخطوة {done}")
        elif phase == "collect":
            publish(job_id, message=f"ينزل ويجمع… {done} تغريدة حتى الآن")

    def work(page):
        publish(job_id, message="التحقق من الجلسة…")
        if not ensure_logged_in(page):
            return publish(job_id, status="done", success=False, message="الجلسة غير مسجّلة الدخول.")
        handle = read_session_username(page)
        if handle:
            remember_session(auth_token, handle, pin)
        publish(job_id, message="إدخال رمز الدردشة…")
        set_page_height(page, COLLECT_VIEWPORT_HEIGHT)
        error = open_chat(page, pin)
        if error:
            return publish(job_id, status="done", success=False, message=error)
        show_groups_only(page)
        publish(job_id, message=f"فتح القروب: {group_name}")
        open_named_group(page, group_name)
        if group_wait_s > 0:
            publish(job_id, message=f"انتظار {group_wait_s} ث داخل القروب…")
            page.wait_for_timeout(group_wait_s * 1000)
        tweets, reached = collect_group_tweets(
            page, 100000, on_progress, stop_id, lambda: bool((jobs.get(job_id) or {}).get("cancel")),
        )
        if not reached:
            return publish(job_id, status="done", success=False, message="وصلت نهاية السجل ولم تظهر التغريدة المحددة.")
        members = summarize_members(tweets, handle or "")
        publish(
            job_id, status="done", success=True, tweets=len(tweets), members=members,
            message=f"تم: {len(members)} عضو أرسلوا {len(tweets)} تغريدة.",
        )

    try:
        run_on_browser(auth_token, work)
    except JobStopped:
        publish(job_id, status="done", success=False, message="تم الإيقاف.")
    except Exception as exc:
        publish(job_id, status="done", success=False, message=str(exc)[:300] or "حدث خطأ.")


def fetch_groups(auth_token: str, pin: str) -> tuple[list[str], str | None]:
    def work(page):
        if not ensure_logged_in(page):
            return [], "الجلسة غير مسجّلة الدخول."
        handle = read_session_username(page)
        if handle:
            remember_session(auth_token, handle, pin)
        error = open_chat(page, pin)
        if error:
            return [], error
        show_groups_only(page)
        page.wait_for_timeout(5000)
        names = collect_groups(page)
        return (names, None) if names else ([], "لم تظهر قروبات.")

    try:
        return run_on_browser(auth_token, work)
    except Exception as exc:
        return [], str(exc)[:300] or "تعذّر جلب القروبات."


PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>جمع الأعضاء</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#2a2f3a; --text:#e8eaf0; --muted:#9aa3b2; --accent:#1d9bf0; --bad:#f4212e; --good:#00ba7c; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6 system-ui, "Segoe UI", Tahoma, sans-serif; }
  main { max-width:720px; margin:0 auto; padding:16px; }
  h1 { font-size:20px; margin:8px 0 16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; margin-bottom:12px; }
  label { display:block; color:var(--muted); font-size:13px; margin:8px 0 4px; }
  input[type=text], input[type=password], input[type=number] { width:100%; padding:10px; border-radius:8px; border:1px solid var(--line); background:#0c0e12; color:var(--text); font-size:15px; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  button { padding:10px 16px; border-radius:999px; border:0; background:var(--accent); color:#fff; font-weight:600; font-size:15px; cursor:pointer; }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--text); }
  button:disabled { opacity:.5; cursor:default; }
  #groups { list-style:none; padding:0; margin:8px 0 0; display:flex; flex-wrap:wrap; gap:6px; }
  #groups button { background:#0c0e12; border:1px solid var(--line); color:var(--text); font-weight:500; }
  #groups button.on { border-color:var(--accent); background:#10273a; }
  #status { color:var(--muted); min-height:1.6em; }
  #status.bad { color:var(--bad); } #status.good { color:var(--good); }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:right; padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:500; font-size:13px; }
  td a { color:var(--accent); text-decoration:none; }
  details summary { cursor:pointer; color:var(--muted); font-size:13px; }
  details a { display:block; font-size:13px; direction:ltr; text-align:left; word-break:break-all; }
  .wait { width:80px !important; }
</style>
</head>
<body>
<main>
  <h1>جمع الأعضاء الذين أرسلوا تغريدات في القروب</h1>
  <div class="card">
    <label for="token">auth_token</label>
    <input id="token" type="password" autocomplete="off" spellcheck="false">
    <label for="pin">رمز الدردشة</label>
    <input id="pin" type="password" inputmode="numeric" maxlength="8" autocomplete="off">
    <div class="row" style="margin-top:12px"><button id="show-groups" class="ghost" type="button">عرض القروبات</button></div>
    <ul id="groups"></ul>
  </div>
  <div class="card">
    <label for="stop">رابط التغريدة المحددة (يبدأ الجمع منها إلى آخر القروب)</label>
    <input id="stop" type="text" dir="ltr" spellcheck="false" placeholder="https://x.com/user/status/123...">
    <label for="wait">انتظار داخل القروب قبل التمرير (ثوانٍ)</label>
    <input id="wait" class="wait" type="number" min="0" max="300" value="8">
    <div class="row" style="margin-top:12px">
      <button id="start" type="button">جمع الأعضاء</button>
      <button id="stop-job" class="ghost" type="button" hidden>إيقاف</button>
    </div>
    <p id="status"></p>
  </div>
  <div class="card" id="result" hidden>
    <div class="row" style="justify-content:space-between">
      <b id="summary"></b>
      <button id="copy" class="ghost" type="button">نسخ الأعضاء</button>
    </div>
    <table>
      <thead><tr><th>#</th><th>العضو</th><th>عدد التغريدات</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</main>
<script>
(() => {
  const api = {{ api|tojson }};
  const csrf = {{ csrf|tojson }};
  const $ = (id) => document.getElementById(id);
  let group = "";
  let jobId = "";
  let members = [];
  const remember = () => {
    try { localStorage.setItem("members_form", JSON.stringify({token: $("token").value, pin: $("pin").value, stop: $("stop").value, wait: $("wait").value, group})); } catch (_) {}
  };
  try {
    const saved = JSON.parse(localStorage.getItem("members_form") || "null");
    if (saved) { $("token").value = saved.token || ""; $("pin").value = saved.pin || ""; $("stop").value = saved.stop || ""; $("wait").value = saved.wait || "8"; group = saved.group || ""; }
  } catch (_) {}
  ["token", "pin", "stop", "wait"].forEach((id) => $(id).addEventListener("input", remember));
  const say = (text, kind) => { $("status").textContent = text || ""; $("status").className = kind || ""; };
  const post = async (path, body) => {
    const response = await fetch(api + path, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify(body)});
    return response.json();
  };
  const renderGroups = (names) => {
    $("groups").replaceChildren();
    names.forEach((name) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = name;
      if (name === group) button.className = "on";
      button.addEventListener("click", () => { group = name; remember(); renderGroups(names); say("القروب المختار: " + name); });
      li.appendChild(button);
      $("groups").appendChild(li);
    });
  };
  if (group) renderGroups([group]);
  $("show-groups").addEventListener("click", async () => {
    $("show-groups").disabled = true;
    say("جلب القروبات… قد يأخذ دقيقة.");
    try {
      const data = await post("/groups", {auth_token: $("token").value.trim(), chat_pin: $("pin").value.trim()});
      if (!data.success) { say(data.message || "تعذّر جلب القروبات.", "bad"); return; }
      renderGroups(data.groups);
      say("اختر قروبًا.", "good");
    } catch (_) { say("تعذّر الاتصال.", "bad"); }
    finally { $("show-groups").disabled = false; }
  });
  const renderMembers = (data) => {
    members = data.members || [];
    $("result").hidden = false;
    $("summary").textContent = members.length + " عضو · " + (data.tweets || 0) + " تغريدة";
    $("rows").replaceChildren();
    members.forEach((member, index) => {
      const tr = document.createElement("tr");
      const num = document.createElement("td");
      num.textContent = String(index + 1);
      const who = document.createElement("td");
      if (/^[A-Za-z0-9_]{1,15}$/.test(member.handle)) {
        const link = document.createElement("a");
        link.href = "https://x.com/" + member.handle;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "@" + member.handle;
        who.appendChild(link);
      } else {
        who.textContent = member.handle;
      }
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "التغريدات";
      details.appendChild(summary);
      (member.tweets || []).forEach((url) => {
        const a = document.createElement("a");
        a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = url;
        details.appendChild(a);
      });
      who.appendChild(details);
      const count = document.createElement("td");
      count.textContent = String(member.count);
      tr.append(num, who, count);
      $("rows").appendChild(tr);
    });
  };
  const poll = async () => {
    for (;;) {
      const response = await fetch(api + "/job/" + encodeURIComponent(jobId), {headers: {"X-CSRF-Token": csrf}, cache: "no-store"});
      const data = await response.json();
      if (!response.ok) { say(data.message || "انقطعت المتابعة.", "bad"); break; }
      if (data.status === "done") {
        say(data.message, data.success ? "good" : "bad");
        if (data.members) renderMembers(data);
        break;
      }
      say(data.message);
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    $("start").disabled = false;
    $("stop-job").hidden = true;
  };
  $("start").addEventListener("click", async () => {
    if (!group) { say("اعرض القروبات واختر قروبًا.", "bad"); return; }
    $("start").disabled = true;
    $("result").hidden = true;
    say("بدء…");
    try {
      const data = await post("/start", {auth_token: $("token").value.trim(), chat_pin: $("pin").value.trim(), group, stop_tweet: $("stop").value.trim(), group_wait: Number.parseInt($("wait").value, 10) || 0});
      if (!data.success) { say(data.message || "تعذّر البدء.", "bad"); $("start").disabled = false; return; }
      jobId = data.job_id;
      $("stop-job").hidden = false;
      poll();
    } catch (_) { say("تعذّر الاتصال.", "bad"); $("start").disabled = false; }
  });
  $("stop-job").addEventListener("click", () => { if (jobId) post("/stop", {job_id: jobId}); say("جارٍ الإيقاف…"); });
  $("copy").addEventListener("click", async () => {
    const text = members.map((member) => /^[A-Za-z0-9_]{1,15}$/.test(member.handle) ? "@" + member.handle : member.handle).join("\n");
    try { await navigator.clipboard.writeText(text); say("نُسخت قائمة الأعضاء.", "good"); } catch (_) { say("تعذّر النسخ.", "bad"); }
  });
})();
</script>
</body>
</html>
"""


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


def csrf_ok() -> bool:
    expected = session.get("csrf") or ""
    return bool(expected) and hmac.compare_digest(expected, request.headers.get("X-CSRF-Token", ""))


@app.get("/")
def root():
    return redirect(f"/{ACCESS_PATH}")


@app.get("/<access_path>")
def index(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    return render_template_string(PAGE, api=f"/{ACCESS_PATH}", csrf=csrf_token())


@app.post("/<access_path>/groups")
def groups_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH) or not csrf_ok():
        abort(404)
    data = request.get_json(silent=True) or {}
    token = str(data.get("auth_token") or "").strip()
    pin = str(data.get("chat_pin") or "").strip()
    if not valid_auth_token(token):
        return jsonify(success=False, message="أدخل auth_token صالحًا.")
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة من ٤ إلى ٨ أرقام.")
    names, error = fetch_groups(token, pin)
    if error:
        return jsonify(success=False, message=error)
    return jsonify(success=True, groups=names)


@app.post("/<access_path>/start")
def start_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH) or not csrf_ok():
        abort(404)
    data = request.get_json(silent=True) or {}
    token = str(data.get("auth_token") or "").strip()
    pin = str(data.get("chat_pin") or "").strip()
    group_name = re.sub(r"\s+", " ", str(data.get("group") or "")).strip()
    stop_id = extract_status_id(str(data.get("stop_tweet") or ""))
    try:
        group_wait_s = max(0, min(300, int(data.get("group_wait") or 0)))
    except (TypeError, ValueError):
        group_wait_s = 0
    if not valid_auth_token(token):
        return jsonify(success=False, message="أدخل auth_token صالحًا.")
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة من ٤ إلى ٨ أرقام.")
    if not group_name:
        return jsonify(success=False, message="اختر قروبًا.")
    if not stop_id:
        return jsonify(success=False, message="الصق رابط التغريدة المحددة أو رقمها.")
    job_id = secrets.token_urlsafe(12)
    with jobs_lock:
        jobs[job_id] = {"status": "running", "message": "بدء…", "owner": session.get("csrf")}
    threading.Thread(target=run_members_job, args=(job_id, token, pin, group_name, stop_id, group_wait_s), daemon=True).start()
    return jsonify(success=True, job_id=job_id)


@app.get("/<access_path>/job/<job_id>")
def job_endpoint(access_path: str, job_id: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH) or not csrf_ok():
        abort(404)
    with jobs_lock:
        job = dict(jobs.get(job_id) or {})
    if not job or job.get("owner") != session.get("csrf"):
        return jsonify(success=False, message="لا توجد مهمة بهذا المعرّف."), 404
    job.pop("owner", None)
    job.pop("cancel", None)
    return jsonify(job)


@app.post("/<access_path>/stop")
def stop_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH) or not csrf_ok():
        abort(404)
    data = request.get_json(silent=True) or {}
    with jobs_lock:
        job = jobs.get(str(data.get("job_id") or ""))
        if job and job.get("owner") == session.get("csrf"):
            job["cancel"] = True
    return jsonify(success=True)


load_sessions()
load_group_ids()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    bind_host = os.getenv("BIND_HOST", "0.0.0.0")
    print(f"Members collector: http://127.0.0.1:{port}/{ACCESS_PATH}", flush=True)
    app.run(host=bind_host, port=port, debug=False, threaded=True)
