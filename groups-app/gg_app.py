#!/usr/bin/env python3
"""One-file Flask page to verify an X session and act on a chat group's tweets.

Saved sessions (auth token + the account username) are written beside this
script and survive a restart. Job progress is kept too, so leaving the browser
and coming back shows whether the work is still running, where it stopped,
or that it finished. The token is not returned except to this same page.
"""
from __future__ import annotations

import atexit
import base64
import gzip
import hashlib
import hmac
import http.client
import concurrent.futures
import importlib.util
import itertools
import json
import logging
import math
import os
import queue
import random
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import traceback
import unicodedata
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from flask import Flask, abort, jsonify, make_response, redirect, render_template_string, request, session
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Page, sync_playwright
from werkzeug.serving import make_server

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("X_APP_SECURE_COOKIE", "0") == "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)
# Every request stays under 8 KB, except starting a task from «روابط التغريدات» (up to 1000 links).
SMALL_REQUEST_BYTES = 8 * 1024

ACCESS_PATH = os.getenv("X_ACCESS_PATH", "desk")
# This copy's version. In users' copies it is the version the owner published from «توزيع البرنامج»
# (g.py writes it into the file it hands out); in the owner's copy it does not change.
APP_VERSION = "1.0"
# In a copy given to a user: the owner's address to take updates from, and that user's key.
# The owner's copy fills both in when it hands the program out; both stay empty in the owner's copy.
UPDATE_SOURCE = ""
UPDATE_KEY = ""
# In a copy given to a user: the owner's content.py («إنشاء محتوى»), packed in by g.py when it hands the
# program out (gzip, then base64), so the user's copy has the page with no file of its own. Empty in the
# owner's copy, which uses content.py beside it.
CONTENT_EMBEDDED = ""
# On users' copies, the posts «إنشاء محتوى» may make in one day (Saudi time). The owner's copy has no limit.
USER_POSTS_PER_DAY = 1
ATTEMPT_WINDOW_SECONDS = 300
MAX_ATTEMPTS_PER_CLIENT = 5
MAX_REPOSTS_REQUEST = 1000
jobs: dict[str, dict] = {}
attempts: dict[str, deque[float]] = defaultdict(deque)
attempts_lock = threading.Lock()
jobs_lock = threading.Lock()
sessions_lock = threading.Lock()
gemini_lock = threading.Lock()
JOB_TTL_SECONDS = 86400
DATA_DIR = Path(__file__).resolve().with_name("data")
# The app has one log, «السجل» (the app keeps everything printed here in a file, with the time of each line). A
# line that tells of something that failed starts with ✗.
try:
    (DATA_DIR / "click-log.txt").unlink()          # (the separate click log of earlier versions)
except OSError:
    pass


def trace_click(message: str) -> None:
    """One line in the app's log about a press, so a failed one can be read afterwards."""
    text = str(message).replace("\n", " ")[:500]
    failed = any(word in text for word in ("تعذر", "تعذّر", "فشل", "استثناء", "لم تُصب", "غير موجود", "no-handler", "'ok': False", "error"))
    print(("✗ " if failed else "") + "نقر: " + text, flush=True)


def trace_error(what: str) -> None:
    """The error being handled now, with where in the script it came from, in the app's log."""
    print("✗ " + what + ":\n" + traceback.format_exc().strip(), flush=True)


_job_said: dict[str, str] = {}


def note_job(job_id: str, fields: dict) -> None:
    """Each new message of a task is a line in the app's log; a task that ended badly is marked."""
    message = str(fields.get("message") or "")
    if not message or _job_said.get(job_id) == message:
        return
    if len(_job_said) > 200:
        _job_said.clear()
    _job_said[job_id] = message
    failed = fields.get("status") == "done" and fields.get("success") is False
    print(("✗ " if failed else "") + f"مهمة {job_id[:4]}: {message}", flush=True)


class LoggedResults(list):
    """A task's results: each tweet's outcome is also a line in the app's log."""

    def append(self, result) -> None:
        super().append(result)
        try:
            outcomes = result.get("actions") or {}
            fine = ("تم التنفيذ", "تم التجاوز", "تم التخطي")
            failed = any(not any(word in str(value) for word in fine) for value in outcomes.values())
            where = str(result.get("url") or "")[-48:]
            print(("✗ " if failed else "") + f"تغريدة {where}: " + "، ".join(f"{key}: {value}" for key, value in outcomes.items()), flush=True)
        except Exception:
            pass


def trace_page(page, why: str) -> None:
    try:
        snap = page.evaluate(
            """() => {
              const tests = [...document.querySelectorAll("[data-testid]")].slice(0, 40)
                .map((el) => el.getAttribute("data-testid"));
              return {url: location.href, w: innerWidth, h: innerHeight, dpr: window.devicePixelRatio || 1, tests};
            }"""
        )
    except Exception as exc:
        snap = type(exc).__name__ + ": " + str(exc)[:160]
    trace_click(why + " | " + str(snap)[:450])
SESSIONS_PATH = DATA_DIR / "sessions.json"
JOBS_PATH = DATA_DIR / "jobs.json"
GEMINI_PATH = DATA_DIR / "gemini.json"
saved_sessions: list[dict] = []
saved_gemini = {"api_key": "", "model": "gemini-3.5-flash"}
JOB_FIELDS = (
    "owner", "client_id", "created_at", "updated_at", "status", "phase", "kind",
    "collected", "done", "requested", "message", "success",
    "username", "actions", "items", "results", "key_hash",
)
logging.getLogger("werkzeug").disabled = True

def load_page() -> str:
    external = Path(__file__).with_name("templates").joinpath("index.html")
    if external.is_file():
        return external.read_text(encoding="utf-8")
    return EMBEDDED_PAGE


EMBEDDED_PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>مراجعة إعادات النشر</title>
  <style>
    @font-face {
      font-family: "IBM Plex Sans Arabic";
      font-style: normal;
      font-weight: 400;
      font-display: swap;
      src: url("/static/fonts/plex-arabic-400.woff2") format("woff2");
      unicode-range: U+0600-06FF, U+0750-077F, U+0870-088E, U+0890-0891, U+0897-08E1, U+08E3-08FF, U+200C-200E, U+2010-2011, U+204F, U+2E41, U+FB50-FDFF, U+FE70-FE74, U+FE76-FEFC;
    }
    @font-face {
      font-family: "IBM Plex Sans Arabic";
      font-style: normal;
      font-weight: 500;
      font-display: swap;
      src: url("/static/fonts/plex-arabic-500.woff2") format("woff2");
      unicode-range: U+0600-06FF, U+0750-077F, U+0870-088E, U+0890-0891, U+0897-08E1, U+08E3-08FF, U+200C-200E, U+2010-2011, U+204F, U+2E41, U+FB50-FDFF, U+FE70-FE74, U+FE76-FEFC;
    }
    @font-face {
      font-family: "IBM Plex Sans Arabic";
      font-style: normal;
      font-weight: 600;
      font-display: swap;
      src: url("/static/fonts/plex-arabic-600.woff2") format("woff2");
      unicode-range: U+0600-06FF, U+0750-077F, U+0870-088E, U+0890-0891, U+0897-08E1, U+08E3-08FF, U+200C-200E, U+2010-2011, U+204F, U+2E41, U+FB50-FDFF, U+FE70-FE74, U+FE76-FEFC;
    }
    @font-face {
      font-family: "IBM Plex Sans Arabic";
      font-style: normal;
      font-weight: 400;
      font-display: swap;
      src: url("/static/fonts/plex-latin-400.woff2") format("woff2");
      unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;
    }
    @font-face {
      font-family: "IBM Plex Sans Arabic";
      font-style: normal;
      font-weight: 500;
      font-display: swap;
      src: url("/static/fonts/plex-latin-500.woff2") format("woff2");
      unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;
    }
    @font-face {
      font-family: "IBM Plex Sans Arabic";
      font-style: normal;
      font-weight: 600;
      font-display: swap;
      src: url("/static/fonts/plex-latin-600.woff2") format("woff2");
      unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;
    }

    :root {
      --paper: #f3efe6;
      --sheet: #fffdf8;
      --ink: #1c1915;
      --muted: #6e675e;
      --line: #e4dcd0;
      --pine: #0f6b50;
      --pine-deep: #0b5340;
      --on-pine: #f4fbf7;
      --clay: #8d3a32;
      --clay-bg: #fbf1ee;
      --ok-bg: #e7f5ee;
      --ok-ink: #0d5a40;
      --shadow: 0 0 0 1px rgba(28, 25, 21, 0.06), 0 1px 2px rgba(28, 25, 21, 0.04), 0 16px 40px rgba(28, 25, 21, 0.05);
      --sans: "IBM Plex Sans Arabic", "Segoe UI", Tahoma, sans-serif;
      color-scheme: light;
    }

    * { box-sizing: border-box; }
    html {
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
      font-size: 100%;
    }
    body {
      margin: 0;
      min-height: 100vh;
      min-height: 100dvh;
      background: var(--paper);
      color: var(--ink);
      font-family: var(--sans);
      font-size: 15px;
      line-height: 1.55;
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0 0 auto 0;
      height: 3px;
      background: var(--ink);
    }
    [hidden] { display: none !important; }
    /* Invisible spot in the top-left corner: asks for the password that resets the comment cost. */
    .cost-reset {
      position: absolute;
      top: 0;
      left: 0;
      z-index: 5;
      width: 40px;
      height: 40px;
      padding: 0;
      border: 0;
      background: transparent;
      opacity: 0;
      cursor: default;
      -webkit-tap-highlight-color: transparent;
    }
    button, [role="button"] { cursor: pointer; }
    button:disabled { cursor: wait; }
    :focus-visible { outline: 2px solid rgba(15, 107, 80, 0.4); outline-offset: 2px; }

    .page {
      width: min(480px, 100%);
      margin: 0 auto;
      padding: calc(8px + env(safe-area-inset-top)) 12px calc(12px + env(safe-area-inset-bottom));
    }
    .mast {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 8px;
    }
    .brand { display: flex; gap: 12px; align-items: center; min-width: 0; }
    .mast-links { margin-inline-start: auto; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
    .mast-links a { display: inline-flex; align-items: center; text-decoration: none; white-space: nowrap; }
    .mark {
      flex: 0 0 auto;
      width: 22px;
      height: 28px;
      border-radius: 3px 7px 7px 3px;
      background: var(--ink);
      position: relative;
    }
    .mark::before {
      content: "";
      position: absolute;
      inset-inline-start: 5px;
      top: 6px;
      bottom: 6px;
      width: 3px;
      border-radius: 2px;
      background: var(--pine);
    }
    .eyebrow {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
    }
    h1 {
      margin: 0;
      font-size: 17px;
      line-height: 1.2;
      font-weight: 600;
    }
    .counts {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .counts li {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 2px;
      min-width: 0;
      padding: 5px 2px;
      border-radius: 8px;
      background: var(--sheet);
      box-shadow: inset 0 0 0 1px var(--line);
      color: var(--muted);
      font-size: 11px;
      font-weight: 500;
      text-align: center;
      line-height: 1.2;
    }
    .counts b { color: var(--ink); font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }

    .lede, .block-head p, .choice-copy, .gemini-fields .hint, .footnote { display: none; }
    .sheet {
      margin-top: 8px;
      padding: 2px;
      background: var(--sheet);
      border-radius: 14px;
      box-shadow: var(--shadow);
    }
    .sheet-inner { padding: 8px 10px 10px; }
    .block + .block { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line); }
    .sheet h2, .block-head h2 { margin: 0; font-size: 13px; font-weight: 600; }
    label.field-label, .field-label {
      display: block;
      margin: 6px 0 3px;
      font-size: 11px;
      font-weight: 600;
    }
    .hint { margin: 5px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .gemini-usage { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 10px 0; padding: 8px 10px; border-radius: 10px;
      background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); color: var(--muted); font-size: 12px; line-height: 1.5; }
    .gemini-usage b { color: var(--ink); font-size: 12px; }
    /* Brown at $0; the page turns it redder as the cost grows, fully red from $10. */
    .gemini-cost { padding: 1px 8px; border-radius: 999px; background: rgb(139, 69, 19); color: #fff; font-weight: 600; }
    input[type="password"], input[type="text"], input[type="number"] {
      width: 100%;
      min-height: 32px;
      height: 32px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--sheet);
      color: var(--ink);
      font: inherit;
      font-size: 16px;
    }
    input:focus { outline: none; border-color: var(--pine); box-shadow: 0 0 0 3px rgba(15, 107, 80, 0.16); }
    #auth_token, #stop-tweet, #gemini-model, #claude-model { direction: ltr; text-align: start; }
    #auth_token[readonly], #gemini-key[readonly], #gemini-model[readonly] { color: var(--muted); }
    #account-name { font-weight: 600; }
    .token-row { display: flex; align-items: stretch; gap: 6px; }
    .token-row input { flex: 1; min-width: 0; }
    .btn {
      min-height: 32px;
      height: 32px;
      border: 0;
      border-radius: 8px;
      padding: 0 8px;
      background: var(--ink);
      color: var(--sheet);
      font: inherit;
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
    }
    .btn:disabled { opacity: 0.5; }
    .btn-pine { background: var(--pine); color: var(--on-pine); }
    .btn-pine:hover:not(:disabled) { background: var(--pine-deep); }
    .btn-ghost {
      background: var(--sheet);
      color: var(--ink);
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .btn-ghost:hover:not(:disabled) { background: var(--paper); }
    .wait-groups {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 32px;
      padding: 0 8px;
      white-space: nowrap;
      font-size: 12px;
      font-weight: 600;
    }
    .wait-groups input[type="checkbox"] { width: 15px; height: 15px; accent-color: var(--pine); }
    input.wait-sec {
      width: 52px;
      min-height: 28px;
      height: 28px;
      padding: 0 4px;
      text-align: center;
      direction: ltr;
      font-size: 14px;
    }
    /* The three options on one line; on a screen too narrow for it, an option moves down whole. */
    .options {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 12px;
      margin: 8px 0;
      padding: 8px;
      border-radius: 12px;
      background: var(--paper);
      box-shadow: inset 0 0 0 1px var(--line);
      font-size: 12px;
      font-weight: 600;
    }
    .options input[type="checkbox"] { width: 15px; height: 15px; margin: 0; accent-color: var(--pine); }
    .options label { cursor: pointer; white-space: nowrap; }
    .opt { display: inline-flex; align-items: center; gap: 3px; white-space: nowrap; }
    .opt input.wait-sec { width: 36px; }
    .opt #account-profiles { width: 28px; }
    .opt #phone-height { width: 54px; }
    /* No up/down arrows in these small boxes: they would cover the number. */
    .opt input[type="number"] { -moz-appearance: textfield; appearance: textfield; }
    .opt input[type="number"]::-webkit-inner-spin-button,
    .opt input[type="number"]::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
    .btn-wide { width: 100%; margin-top: 8px; min-height: 34px; height: 34px; }
    .stop-row { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; align-items: center; margin-top: 6px; }
    /* «روابط التغريدات»: at the other end of the groups row; on (pine) while the links box shows. */
    .links-button { margin-inline-start: auto; }
    .groups-row { flex-wrap: wrap; row-gap: 6px; }     /* a very narrow screen: the button goes under, not off the screen */
    .links-button[aria-pressed="true"] { background: var(--pine); color: var(--on-pine); box-shadow: none; }
    .links-button[aria-pressed="true"]:hover:not(:disabled) { background: var(--pine-deep); }
    .links-box { margin-top: 10px; }
    #tweet-links {
      display: block;
      width: 100%;
      min-height: 120px;
      max-height: 50vh;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--sheet);
      color: var(--ink);
      font: inherit;
      font-size: 16px;
      line-height: 1.5;
      resize: vertical;
      direction: ltr;
      text-align: left;
    }
    #tweet-links:placeholder-shown { direction: rtl; text-align: right; }
    #tweet-links:focus { outline: none; border-color: var(--pine); box-shadow: 0 0 0 3px rgba(15, 107, 80, 0.16); }
    .links-foot { display: flex; align-items: center; gap: 8px; margin-top: 6px; color: var(--muted); font-size: 12px; font-weight: 600; }
    .links-foot .btn { margin-inline-start: auto; min-height: 28px; height: 28px; }
    #links-count.is-bad { color: var(--clay); }
    #group-box {
      margin-top: 10px;
      margin-bottom: 14px;
      padding: 8px 10px 10px;
      border-radius: 14px;
      background: var(--paper);
      box-shadow: inset 0 0 0 1px var(--line);
    }
    /* Three names on top of each other, filling column after column, scrolled sideways. */
    #group-list {
      display: grid;
      grid-auto-flow: column;
      grid-template-rows: repeat(3, auto);
      grid-auto-columns: max-content;
      justify-content: start;
      overflow-x: auto;
      overflow-y: hidden;
      gap: 6px;
      margin-top: 6px;
      padding-bottom: 2px;
    }
    #group-list .pick {
      flex: none;
      min-height: 30px;
      height: 30px;
      border-radius: 999px;
      padding: 0 10px;
      font-size: 12px;
      white-space: nowrap;
      background: var(--sheet);
    }
    #group-list .pick .who { max-width: none; overflow: visible; }
    /* Each name as wide as its column, so the columns line up. */
    #group-list li { display: flex; }
    #group-list .pick { width: 100%; align-items: center; }
    .choices { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-top: 6px; }
    .gemini-fields {
      display: grid;
      grid-template-columns: 1.3fr 0.7fr;
      gap: 6px;
      margin-top: 6px;
      padding: 0;
      background: transparent;
    }
    .gemini-fields[hidden] { display: none !important; }
    .reply-voice { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin-top: 6px; }
    .reply-voice[hidden] { display: none !important; }
    .engine-note { flex-basis: 100%; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .engine-note[hidden] { display: none !important; }
    .voice-label { font-size: 12px; font-weight: 600; color: var(--muted); }
    .voices { display: flex; gap: 4px; }
    /* Smaller than the actions above them. */
    .voices .choice { min-height: 26px; padding: 0 7px; gap: 4px; border-radius: 7px; }
    .voices .choice input { width: 13px; height: 13px; }
    .voices .choice-title { font-size: 12px; }
    #show-gemini, #show-claude { height: 26px; min-height: 26px; padding: 0 7px; }
    #claude-fields { grid-template-columns: 1fr 1fr; }   /* (room for the whole model name, on a phone too) */
    .claude-actions { grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
    .claude-now { color: var(--muted); font-size: 12px; line-height: 1.5; }
    .gemini-fields .field-label { margin-top: 0; }
    .gemini-edit { height: 32px; min-height: 32px; padding: 0 8px; font-size: 12px; }
    .gemini-owner { grid-column: 1 / -1; margin: 0; color: var(--muted); font-size: 12px; }
    .choice {
      display: flex;
      gap: 6px;
      align-items: center;
      margin: 0;
      padding: 0 8px;
      min-height: 32px;
      border-radius: 8px;
      background: var(--sheet);
      box-shadow: inset 0 0 0 1px var(--line);
      cursor: pointer;
    }
    .choice:has(input:focus-visible) { outline: 2px solid rgba(15, 107, 80, 0.4); outline-offset: 2px; }
    .choice input { width: 16px; height: 16px; margin: 0; flex: 0 0 auto; accent-color: var(--pine); }
    .choice-title { display: block; font-weight: 600; font-size: 13px; line-height: 1.2; }
    .choice:has(input:checked) {
      background: var(--ok-bg);
      box-shadow: inset 0 0 0 1.5px var(--pine);
    }
    .notice {
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 12px;
      font-size: 13px;
      line-height: 1.55;
    }
    .notice.is-ok { color: var(--ok-ink); background: var(--ok-bg); }
    .notice.is-bad { color: var(--clay); background: var(--clay-bg); }
    .task-card { margin-top: 8px; padding: 8px; border: 1px solid var(--line); border-radius: 12px; background: var(--sheet-2, transparent); }
    .task-card h3 { margin: 0; font-size: 13px; font-weight: 600; }
    .task-card p { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
    .task-card .meter { margin-top: 8px; }
    .task-actions { display: flex; flex-wrap: wrap; column-gap: 6px; }
    .task-actions .btn { margin-top: 6px; }
    .task-view { display: block; width: 100%; height: auto; margin-top: 8px; border: 1px solid var(--line); border-radius: 8px; }
    .progress-top h2 { margin: 0; font-size: 16px; font-weight: 600; text-wrap: balance; }
    .progress-top p { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
    .progress-figure { text-align: end; flex: 0 0 auto; }
    .figure {
      font-size: 28px;
      font-weight: 600;
      letter-spacing: -0.04em;
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }
    .denom { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
    .meter {
      height: 4px;
      margin-top: 12px;
      border-radius: 99px;
      background: var(--line);
      overflow: hidden;
    }
    .meter > span {
      display: block;
      height: 100%;
      width: 0;
      background: var(--pine);
      transform-origin: 100% 50%;
      transition: width 240ms ease-out;
    }
    .meter.is-indeterminate > span { width: 100%; animation: meter-pulse 1.15s ease-in-out infinite; }
    @keyframes meter-pulse {
      0%, 100% { transform: scaleX(0.18); opacity: 0.55; }
      50% { transform: scaleX(1); opacity: 1; }
    }
    .progress-note { margin: 8px 0 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 12px; }
    .stat { padding: 10px 6px; border-radius: 12px; background: var(--paper); text-align: center; }
    .stat b {
      display: block;
      font-size: 20px;
      font-weight: 600;
      letter-spacing: -0.03em;
      font-variant-numeric: tabular-nums;
      line-height: 1.1;
    }
    .stat span { display: block; margin-top: 2px; color: var(--muted); font-size: 11px; line-height: 1.3; }
    .post-list {
      max-height: min(52vh, 460px);
      margin: 12px 0 0;
      padding: 0;
      overflow: auto;
      list-style: none;
      border-block: 1px solid var(--line);
      -webkit-overflow-scrolling: touch;
    }
    .post-list li { padding: 10px 0; border-bottom: 1px solid var(--line); }
    .post-list li:last-child { border-bottom: 0; }
    .post-top { display: flex; align-items: flex-start; gap: 8px; }
    .idx {
      flex: 0 0 auto;
      min-width: 1.2rem;
      padding-top: 1px;
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }
    .post-list a {
      color: var(--pine-deep);
      font-weight: 500;
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .excerpt { margin: 6px 0 0; color: var(--ink); font-size: 13px; line-height: 1.5; }
    .pills { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
    .pill {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      min-height: 22px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 500;
      line-height: 1.35;
      white-space: normal;
      overflow-wrap: anywhere;
      text-align: start;
    }
    .pill.is-will { color: var(--ok-ink); background: var(--ok-bg); }
    .pill.is-skip { color: var(--muted); background: var(--paper); }
    .pill.is-fail { color: var(--clay); background: var(--clay-bg); }
    .session-account { margin: 8px 0 0; font-weight: 600; font-size: 14px; color: var(--ok-ink); }
    .saved-sessions { margin-top: 12px; }
    .saved-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
    .saved-session { display: flex; gap: 6px; align-items: stretch; }
    .pick {
      flex: 1;
      min-width: 0;
      min-height: 40px;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      justify-content: center;
      gap: 1px;
      padding: 6px 10px;
      border: 0;
      border-radius: 10px;
      background: var(--paper);
      color: var(--ink);
      font: inherit;
      font-size: 13px;
      text-align: start;
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .pick.is-on { background: var(--ok-bg); box-shadow: inset 0 0 0 1.5px var(--pine); }
    .pick .who { font-weight: 600; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pick .meta { color: var(--muted); font-size: 11px; direction: ltr; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
    .drop {
      flex: 0 0 auto;
      min-height: 40px;
      padding: 0 10px;
      border: 0;
      border-radius: 10px;
      background: transparent;
      color: var(--clay);
      font: inherit;
      font-size: 12px;
      font-weight: 600;
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .update-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 10px;
      padding: 8px 10px;
      border-radius: 12px;
      color: var(--ok-ink);
      background: var(--ok-bg);
      font-size: 13px;
      font-weight: 600;
    }
    .update-bar .btn { flex: 0 0 auto; }
    .version-line { display: flex; flex-wrap: wrap; justify-content: center; gap: 4px 10px; margin: 14px 0 4px; color: var(--muted); font-size: 12px; }
    .link-button { border: 0; padding: 0; background: none; color: var(--pine); font: inherit; font-weight: 600; text-decoration: underline; cursor: pointer; }

    @media (min-width: 760px) {
      body { font-size: 15px; }
      .page {
        width: min(760px, calc(100% - 48px));
        padding-top: 36px;
      }
      .mast { flex-direction: row; justify-content: space-between; align-items: flex-end; gap: 24px; }
      .counts { min-width: 340px; }
      h1 { font-size: 28px; }
      .lede { font-size: 14px; max-width: 62ch; }
      .sheet { border-radius: 18px; padding: 6px; }
      .sheet-inner { padding: 18px 18px 20px; }
      .choices { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .figure { font-size: 40px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation: none !important;
        transition: none !important;
      }
    }
  </style>
</head>
<body>
  {% if owner_copy %}<button id="cost-reset" class="cost-reset" type="button" tabindex="-1" aria-hidden="true"></button>{% endif %}
  <main class="page">
    <header class="mast">
      <div class="brand">
        <span class="mark" aria-hidden="true"></span>
        <div>
          <p class="eyebrow">من قروب الدردشة</p>
          <h1>تنفيذ على تغريدات القروب</h1>
        </div>
        <div class="mast-links">
          {% if not tweets_in_content %}<a class="btn btn-ghost" href="{{ api_root }}/tweets">عرض التغريدات</a>{% endif %}
          {% if content_ready %}<a class="btn btn-ghost" href="{{ api_root }}/content">إنشاء محتوى</a>{% endif %}
        </div>
      </div>
      <ol class="counts" aria-label="الإجراءات الناجحة">
        <li>الرتويت <b id="count-repost">0</b></li>
        <li>الإعجاب <b id="count-like">0</b></li>
        <li>المرجعية <b id="count-bookmark">0</b></li>
        <li>التعليقات <b id="count-comment">0</b></li>
      </ol>
    </header>
    <p class="lede">يُفتح تبويب إعادات النشر ويُمرَّر حتى يكتمل العدد أو ينتهي الخط. ترى حالة كل إجراء، ثم تقرر. لا يُمس الحساب قبل «تأكيد التنفيذ».</p>

    <div id="update-bar" class="update-bar" role="status" hidden>
      <span id="update-text"></span>
      <button id="update-button" class="btn btn-pine" type="button">تحديث</button>
    </div>

    <div id="notice" class="notice" role="status" hidden></div>

    <form id="repost-form" class="sheet" autocomplete="off" novalidate>
      <div class="sheet-inner">
        <input id="csrf_token" type="hidden" value="{{ csrf_token }}">
        <section class="block">
          <div class="block-head">
            <h2>الجلسة</h2>
            <p>سجّل الدخول من المتصفح، ثم اضغط تحقق. تُحفظ الجلسة مع اسم الحساب على هذا الجهاز.</p>
          </div>
          <div class="token-row">
            <input id="account-name" type="text" readonly tabindex="-1" dir="auto" placeholder="اسم المستخدم" aria-label="الحساب">
            <button id="quick-verify" class="btn btn-ghost" type="button">تحقق</button>
            {% if in_app %}<button id="browser-login" class="btn btn-ghost" type="button">تسجيل الدخول</button>{% endif %}
            <button id="account-settings" class="btn btn-ghost" type="button" aria-expanded="false" aria-controls="account-panel">إعداد الحساب</button>
          </div>
          <div id="account-panel" hidden>
            <input id="auth_token" name="auth_token" type="hidden" value="">
            <p id="session-account" class="session-account" hidden></p>
            <div id="saved-sessions" class="saved-sessions" hidden>
              <p class="field-label">جلسات محفوظة</p>
              <ul id="saved-session-list" class="saved-list"></ul>
            </div>
            <label class="field-label" for="chat-pin">رمز الدردشة</label>
            <input id="chat-pin" name="chat_pin" type="password" inputmode="numeric" maxlength="8" required
                   autocomplete="off" spellcheck="false" placeholder="٤ أرقام" aria-label="رمز الدردشة">
          </div>
        </section>

        <section class="block">
          <div class="block-head">
            <h2>القروب</h2>
            <p>يُفتح صندوق الدردشة، وتُعرض القروبات فقط. اختر واحدًا ثم العدد.</p>
          </div>
          <div class="token-row groups-row">
            <button id="groups-button" class="btn btn-ghost" type="button">عرض القروبات</button>
            <label id="wait-groups-label" class="wait-groups" for="wait-groups">
              <input id="wait-groups" type="checkbox" checked>
              انتظار
              <input id="wait-groups-sec" class="wait-sec" type="number" min="1" max="120" step="1" value="30" inputmode="numeric" aria-label="ثواني الانتظار قبل التقاط القروبات">
              ث
            </label>
            <button id="links-button" class="btn btn-ghost links-button" type="button" aria-pressed="false" aria-controls="links-box" title="روابط التغريدات" aria-label="روابط التغريدات">الروابط</button>
          </div>
          <div id="group-box" class="saved-sessions" hidden>
            <p class="field-label">اختر قروبًا</p>
            <ul id="group-list" class="saved-list"></ul>
          </div>
          <div id="links-box" class="links-box" hidden>
            <textarea id="tweet-links" rows="5" maxlength="200000" autocomplete="off" autocapitalize="off" spellcheck="false"
                      placeholder="الصق روابط التغريدات هنا، كل رابط في سطر" aria-label="روابط التغريدات"></textarea>
            <div class="links-foot">
              <span id="links-count" aria-live="polite"></span>
              <button id="links-clear" class="btn btn-ghost" type="button">مسح</button>
            </div>
          </div>
          <div class="options">
            <span id="group-wait-opt" class="opt">
              <input id="group-wait-on" type="checkbox" checked>
              <label for="group-wait-on" title="انتظار داخل القروب قبل التمرير والجمع">قبل الجمع</label>
              <input id="group-wait-sec" class="wait-sec" type="number" min="0" max="300" step="1" value="30" inputmode="numeric" aria-label="ثواني الانتظار داخل القروب">
              ث
            </span>
            <span class="opt">
              <input id="account-profiles-on" type="checkbox">
              <label for="account-profiles-on" title="عدد المهام في نفس الوقت لكل حساب" aria-label="مهام لكل حساب">مهام</label>
              <input id="account-profiles" class="wait-sec" type="number" min="0" max="5" step="1" value="0" inputmode="numeric" aria-label="عدد المهام لكل حساب">
            </span>
            <span class="opt">
              <input id="phone-screen" type="checkbox" checked>
              <label for="phone-screen" title="شاشة جوال، وطولها بالبكسل أثناء الجمع داخل القروب" aria-label="شاشة جوال">جوال</label>
              <input id="phone-height" class="wait-sec" type="number" min="600" max="25000" step="1" value="10000" inputmode="numeric" aria-label="طول شاشة الجوال أثناء الجمع بالبكسل">
            </span>
          </div>
          <div id="stop-row" class="stop-row">
            <input id="stop-tweet" name="stop_tweet" type="text" maxlength="300" autocomplete="off" spellcheck="false"
                   placeholder="رابط التغريدة المحددة" aria-label="التغريدة المحددة">
          </div>
        </section>

        <section class="block">
          <div class="block-head">
            <h2>الإجراءات</h2>
            <p>تُطبَّق مباشرة على التغريدات المستخرجة من القروب.</p>
          </div>
          <div class="choices">
            <label class="choice">
              <input id="action-repost" type="checkbox" checked>
              <span>
                <span class="choice-title">إعادة النشر</span>
                <span class="choice-copy">فقط إذا لم تكن الإعادة مفعّلة.</span>
              </span>
            </label>
            <label class="choice">
              <input id="action-like" type="checkbox" checked>
              <span>
                <span class="choice-title">إعجاب</span>
                <span class="choice-copy">فقط إذا لم يكن الإعجاب موجودًا.</span>
              </span>
            </label>
            <label class="choice">
              <input id="action-bookmark" type="checkbox" checked>
              <span>
                <span class="choice-title">المرجعية</span>
                <span class="choice-copy">فقط إذا لم يكن المنشور محفوظًا.</span>
              </span>
            </label>
            <label class="choice">
              <input id="action-comment" type="checkbox">
              <span>
                <span class="choice-title">التعليقات</span>
                <span class="choice-copy">يُنشر رد على التغريدة المستخرجة.</span>
              </span>
            </label>
          </div>
          <div id="reply-voice" class="reply-voice" hidden>
            <span class="voice-label">صيغة الرد</span>
            <div class="voices" role="radiogroup" aria-label="صيغة الرد">
              <label class="choice"><input type="radio" name="reply-voice" value="male" checked><span class="choice-title">مذكر</span></label>
              <label class="choice"><input type="radio" name="reply-voice" value="female"><span class="choice-title">مؤنث</span></label>
              <label class="choice"><input type="radio" name="reply-voice" value="neutral"><span class="choice-title">عام</span></label>
            </div>
            {% if owner_copy %}<button id="show-gemini" class="btn btn-ghost gemini-edit" type="button" aria-expanded="false">مفتاح Gemini</button>
            {% if in_app %}<button id="show-server" class="btn btn-ghost gemini-edit" type="button" aria-expanded="false">سيرفر التعليقات</button>
            <span id="comments-by-grok" class="engine-note" hidden>التعليقات يكتبها سيرفرك، بالرابط المحفوظ في «سيرفر التعليقات».</span>
            {% elif claude_here %}<button id="show-claude" class="btn btn-ghost gemini-edit" type="button" aria-expanded="false">مفتاح Claude</button>
            <span id="comments-by-grok" class="engine-note" hidden>التعليقات بواسطة Claude، بالمفتاح المحفوظ في «مفتاح Claude».</span>
            {% else %}<span id="comments-by-grok" class="engine-note" data-text="التعليقات بواسطة {engine}، كما اخترت للمستخدمين في «توزيع البرنامج»." hidden>التعليقات بواسطة Grok، كما اخترت للمستخدمين في «توزيع البرنامج».</span>{% endif %}{% endif %}
          </div>
          <div id="gemini-fields" class="gemini-fields" hidden>
            {% if not owner_copy %}<p class="gemini-owner">يضبط صاحب البرنامج مفتاح التعليقات ونموذجها.</p>{% endif %}
            <div>
              <label class="field-label" for="gemini-key">مفتاح Gemini</label>
              <input id="gemini-key" type="password" maxlength="256" autocomplete="off" spellcheck="false" readonly
                     placeholder="المفتاح" aria-label="مفتاح Gemini API">
            </div>
            <div>
              <label class="field-label" for="gemini-model">النموذج</label>
              <div class="token-row">
                <input id="gemini-model" type="text" maxlength="80" autocomplete="off" spellcheck="false" readonly
                       value="gemini-3.5-flash" placeholder="gemini-3.5-flash" aria-label="نموذج Gemini">
                <button id="edit-gemini" class="btn btn-ghost gemini-edit" type="button" aria-pressed="false">تحرير</button>
              </div>
            </div>
            <p class="hint">يُحفظ المفتاح واسم النموذج على هذا الخادم بعد أول إدخال، ويظهران تلقائيًا حتى بعد إغلاق الصفحة أو إعادة تشغيل السكربت.</p>
          </div>
          {% if in_app %}<div id="server-fields" class="gemini-fields" hidden>
            <div style="grid-column: 1 / -1">
              <label class="field-label" for="server-link">رابط سيرفر التعليقات</label>
              <input id="server-link" type="password" maxlength="400" autocomplete="off" autocapitalize="off" spellcheck="false" dir="ltr"
                     placeholder="https://…" aria-label="رابط سيرفر التعليقات">
            </div>
            <div class="claude-actions" style="grid-column: 1 / -1">
              <button id="save-server" class="btn btn-ghost gemini-edit" type="button">حفظ واتصال</button>
              <button id="delete-server" class="btn btn-ghost gemini-edit" type="button" hidden>حذف</button>
              <span id="server-now" class="claude-now">لم يُحفظ رابط بعد: التعليقات لا تُكتب بدونه.</span>
            </div>
            <p class="hint" style="grid-column: 1 / -1">هو رابط تطبيق «تعليقات» نفسه، يطبعه السيرفر بالأمر: <span dir="ltr">python3 comments_server.py --link</span>. مفتاح النموذج يبقى في السيرفر، ولا يُحفظ في الجوال إلا هذا الرابط.</p>
          </div>{% elif claude_here %}<div id="claude-fields" class="gemini-fields" hidden>
            <div>
              <label class="field-label" for="claude-key">مفتاح Claude</label>
              <input id="claude-key" type="password" maxlength="260" autocomplete="off" spellcheck="false" placeholder="المفتاح" aria-label="مفتاح Claude API">
            </div>
            <div>
              <label class="field-label" for="claude-model">النموذج</label>
              <input id="claude-model" type="text" maxlength="60" autocomplete="off" spellcheck="false" value="{{ claude_default }}" placeholder="{{ claude_default }}" aria-label="نموذج Claude">
            </div>
            <div class="claude-actions">
              <button id="save-claude" class="btn btn-ghost gemini-edit" type="button">حفظ</button>
              <button id="delete-claude" class="btn btn-ghost gemini-edit" type="button" hidden>حذف</button>
              <span id="claude-now" class="claude-now">بدون مفتاح Claude تُكتب التعليقات بواسطة Gemini.</span>
            </div>
          </div>{% endif %}
          <div id="gemini-usage" class="gemini-usage" aria-live="polite">
            <span id="gemini-usage-text">التعليقات (0)</span>
            <span id="gemini-cost" class="gemini-cost">التكلفة (<bdi id="gemini-cost-value" dir="ltr">$0.000</bdi>)</span>
          </div>
          <button id="reposts-button" class="btn btn-pine btn-wide" type="submit">تنفيذ</button>
        </section>
      </div>
    </form>

    <section id="task-board" class="sheet" hidden>
      <div class="sheet-inner">
        <h2>المهام</h2>
        <div id="task-list"></div>
      </div>
    </section>

    <section id="progress-panel" class="sheet" hidden>
      <div class="sheet-inner">
        <div class="progress-top">
          <div>
            <h2 id="progress-title">الجمع</h2>
            <p id="progress-message">جارٍ العمل…</p>
          </div>
          <div class="progress-figure" aria-hidden="true">
            <span id="progress-count" class="figure">0</span>
            <span id="progress-denom" class="denom"></span>
          </div>
        </div>
        <div id="meter" class="meter is-indeterminate" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="تقدم العملية">
          <span id="meter-bar"></span>
        </div>
        <p id="progress-note" class="progress-note">لا يُنفَّذ أي إجراء خلال الجمع.</p>
      </div>
    </section>

    <section id="repost-results" class="sheet" hidden>
      <div class="sheet-inner">
        <h2 id="repost-heading">نتيجة التنفيذ</h2>
        <div class="stats" id="result-stats"></div>
        <ol id="repost-list" class="post-list"></ol>
        <p id="no-reposts" class="hint" hidden>لم تُرجع العملية أي صفوف.</p>
      </div>
    </section>

    <p class="version-line">
      <span>الإصدار {{ app_version }}</span>
      <span id="update-state"></span>
      <a id="dist-link" class="link-button" target="_blank" rel="noopener" hidden>توزيع البرنامج</a>
    </p>

    <p class="footnote">الأعداد الكبيرة تأخذ وقتًا لأن كل دفعة تُنتظر حتى تظهر. إذا أغلقت المتصفح أثناء العملية فافتح الصفحة نفسها لاحقًا: يظهر التقدم من حيث بلغ، أو النتيجة إن اكتملت. الجلسات المحفوظة تبقى بعد إعادة تشغيل السكربت.</p>
  </main>
<script>
(() => {
  const apiRoot = {{ api_root|tojson }};
  const LABELS = {repost: "إعادة النشر", like: "إعجاب", bookmark: "المرجعية", comment: "تعليق"};
  const tokenField = document.getElementById("auth_token");
  function lockToken() {}
  const stopField = document.getElementById("stop-tweet");
  const pinField = document.getElementById("chat-pin");
  const groupsButton = document.getElementById("groups-button");
  const waitGroups = document.getElementById("wait-groups");
  const waitGroupsSec = document.getElementById("wait-groups-sec");
  const groupWaitOn = document.getElementById("group-wait-on");
  const groupWaitSec = document.getElementById("group-wait-sec");
  const accountProfilesOn = document.getElementById("account-profiles-on");
  const phoneScreen = document.getElementById("phone-screen");
  const phoneHeight = document.getElementById("phone-height");
  const accountProfiles = document.getElementById("account-profiles");
  function rememberWaits() {
    try {
      localStorage.setItem("x_wait_settings_v4", JSON.stringify({
        groupsOn: waitGroups.checked, groupsSec: waitGroupsSec.value,
        groupOn: groupWaitOn.checked, groupSec: groupWaitSec.value,
        profilesOn: accountProfilesOn.checked, profiles: accountProfiles.value,
        phone: phoneScreen.checked, phoneHeight: phoneHeight.value
      }));
    } catch (_) {}
  }
  try {
    // "_v2": choices saved before the current defaults (30 s waits ticked) are not restored.
    // "_v3": from v2 everything is kept except the phone screen, which takes its new default (ticked).
    // "_v4": from v3 everything is kept except the phone's collecting height, which takes its new default (10000).
    const latest = localStorage.getItem("x_wait_settings_v4");
    const before = latest || localStorage.getItem("x_wait_settings_v3");
    const savedWaits = JSON.parse(before || localStorage.getItem("x_wait_settings_v2") || "null");
    if (savedWaits) {
      waitGroups.checked = !!savedWaits.groupsOn;
      if (savedWaits.groupsSec) waitGroupsSec.value = savedWaits.groupsSec;
      groupWaitOn.checked = !!savedWaits.groupOn;
      if (savedWaits.groupSec !== undefined && savedWaits.groupSec !== "") groupWaitSec.value = savedWaits.groupSec;
      accountProfilesOn.checked = !!savedWaits.profilesOn;
      if (before) phoneScreen.checked = !!savedWaits.phone;
      if (latest && savedWaits.phoneHeight) phoneHeight.value = savedWaits.phoneHeight;
      if (savedWaits.profiles) accountProfiles.value = savedWaits.profiles;
    }
  } catch (_) {}
  [waitGroups, waitGroupsSec, groupWaitOn, groupWaitSec, accountProfilesOn, accountProfiles, phoneScreen, phoneHeight].forEach((node) => node.addEventListener("change", rememberWaits));
  function secondsFrom(field, min, max, fallback) {
    const value = Number.parseInt(field.value, 10);
    if (!Number.isInteger(value)) return fallback;
    return Math.max(min, Math.min(max, value));
  }
  const groupBox = document.getElementById("group-box");
  const groupList = document.getElementById("group-list");
  // «روابط التغريدات»: instead of a group, the task runs on the tweets pasted in this box. Meanwhile the
  // groups, the group's tweet and the waits hide, and «عرض القروبات» reads «القروبات»: it brings back
  // the groups already shown, without opening the browser and the chat again.
  const linksButton = document.getElementById("links-button");
  const linksBox = document.getElementById("links-box");
  const linksField = document.getElementById("tweet-links");
  const linksCount = document.getElementById("links-count");
  const stopRow = document.getElementById("stop-row");
  const groupWaitOpt = document.getElementById("group-wait-opt");
  const waitGroupsLabel = document.getElementById("wait-groups-label");
  const MAX_LINKS = {{ max_links|tojson }};
  let linksMode = false;
  // Each tweet in the pasted text once, in order: "handle/id", or "id" for a link without the
  // author's name (x.com/i/web/status/…) or a bare tweet number.
  function pastedTweets(text) {
    const seen = new Set();
    const found = [];
    const add = (handle, id) => {
      if (seen.has(id)) return;
      seen.add(id);
      found.push(handle && !/^(i|web)$/i.test(handle) ? handle + "/" + id : id);
    };
    for (const token of String(text || "").split(/[\s,،]+/)) {
      const pattern = /(?:x|twitter)\.com\/([A-Za-z0-9_]{1,15})(?:\/web)?\/status(?:es)?\/(\d{6,25})/gi;
      let match;
      let any = false;
      while ((match = pattern.exec(token))) {
        any = true;
        add(match[1], match[2]);
      }
      if (!any && /^\d{15,25}$/.test(token)) add("", token);
    }
    return found;
  }
  function showLinksCount() {
    const count = pastedTweets(linksField.value).length;
    linksCount.textContent = count ? count + " تغريدة" + (count > MAX_LINKS ? " · الحد الأقصى " + MAX_LINKS : "") : "لم يُلصق أي رابط بعد";
    linksCount.classList.toggle("is-bad", count > MAX_LINKS);
  }
  function setLinksMode(on) {
    linksMode = on;
    linksButton.setAttribute("aria-pressed", on ? "true" : "false");
    linksBox.hidden = !on;
    stopRow.hidden = on;
    groupWaitOpt.hidden = on;
    waitGroupsLabel.hidden = on;
    groupsButton.textContent = on ? "القروبات" : "عرض القروبات";
    groupBox.hidden = on || groupNames.length === 0;
    try {
      localStorage.setItem("x_links_mode", on ? "1" : "0");
    } catch (_) {}
    if (on) showLinksCount();
  }
  linksButton.addEventListener("click", () => {
    setLinksMode(!linksMode);
    if (linksMode && !linksField.value.trim()) linksField.focus();
  });
  linksField.addEventListener("input", () => {
    showLinksCount();
    try {
      localStorage.setItem("x_tweet_links", linksField.value);
    } catch (_) {}
  });
  document.getElementById("links-clear").addEventListener("click", () => {
    linksField.value = "";
    try {
      localStorage.removeItem("x_tweet_links");
    } catch (_) {}
    showLinksCount();
    linksField.focus();
  });
  const commentToggle = document.getElementById("action-comment");
  const geminiFields = document.getElementById("gemini-fields");
  const geminiKey = document.getElementById("gemini-key");
  const geminiModel = document.getElementById("gemini-model");
  const geminiUsageText = document.getElementById("gemini-usage-text");
  const geminiCost = document.getElementById("gemini-cost");
  const geminiCostValue = document.getElementById("gemini-cost-value");
  const editGemini = document.getElementById("edit-gemini");
  const replyVoice = document.getElementById("reply-voice");
  // The owner's copy only: opens the Gemini key and model fields.
  const showGemini = document.getElementById("show-gemini");
  // A user's copy: the owner sets the Gemini key and model; here they are only shown.
  const geminiByOwner = {{ (not owner_copy)|tojson }};
  editGemini.hidden = geminiByOwner;
  // The owner's copy: Grok writes its comments when it is the one chosen in «توزيع البرنامج» (as the script says).
  // (Grok or Claude, chosen in «توزيع البرنامج»: no Gemini key is asked for then.)
  let commentsByOther = false;
  const engineNote = document.getElementById("comments-by-grok");
  const inApp = {{ in_app|tojson }};
  function showCommentsEngine(engine) {
    commentsByOther = engine === "grok" || engine === "claude" || engine === "server";
    if (!engineNote) return;
    engineNote.hidden = !commentsByOther;
    if (commentsByOther && engineNote.dataset.text) {
      engineNote.textContent = engineNote.dataset.text.replace("{engine}", engine === "claude" ? "Claude" : "Grok");
    }
  }
  // In the app the comments are written on the owner's server: the panel keeps that server's link.
  const serverFields = document.getElementById("server-fields");
  const showServer = document.getElementById("show-server");
  function showCommentsServer(data) {
    if (!serverFields) return;
    const saved = data.comments_server || "";
    document.getElementById("server-now").textContent = saved
      ? "محفوظ: " + saved + (data.comments_model ? " · النموذج في السيرفر: " + data.comments_model : "") + " — التعليقات تُكتب فيه."
      : "لم يُحفظ رابط بعد: التعليقات لا تُكتب بدونه.";
    document.getElementById("delete-server").hidden = !saved;
  }
  function openServer(open) {
    if (!serverFields) return;
    serverFields.hidden = !open;
    showServer.setAttribute("aria-expanded", open ? "true" : "false");
  }
  // A copy with no «توزيع البرنامج»: its own «مفتاح Claude», typed here, writes its comments when saved.
  const claudeFields = document.getElementById("claude-fields");
  const showClaude = document.getElementById("show-claude");
  function showOwnClaude(data) {
    if (!claudeFields) return;
    const saved = data.claude_key || "";
    document.getElementById("claude-now").textContent = saved
      ? "محفوظ: " + saved + " · " + (data.claude_model || "") + " — التعليقات بواسطة Claude."
      : "بدون مفتاح Claude تُكتب التعليقات بواسطة Gemini.";
    document.getElementById("delete-claude").hidden = !saved;
    const claudeModel = document.getElementById("claude-model");
    if (data.claude_model && document.activeElement !== claudeModel) claudeModel.value = data.claude_model;
  }
  function lockGemini(locked) {
    geminiKey.readOnly = locked;
    geminiModel.readOnly = locked;
    editGemini.textContent = locked ? "تحرير" : "تم";
    editGemini.setAttribute("aria-pressed", locked ? "false" : "true");
  }
  lockGemini(true);
  const csrf = document.getElementById("csrf_token").value;
  const loginButton = document.getElementById("login-button");
  const accountName = document.getElementById("account-name");
  const quickVerify = document.getElementById("quick-verify");
  const accountSettings = document.getElementById("account-settings");
  const accountPanel = document.getElementById("account-panel");
  const repostButton = document.getElementById("reposts-button");
  const notice = document.getElementById("notice");
  const progressPanel = document.getElementById("progress-panel");
  const taskBoard = document.getElementById("task-board");
  const taskList = document.getElementById("task-list");
  const liveTasks = new Map();
  const progressTitle = document.getElementById("progress-title");
  const progressMessage = document.getElementById("progress-message");
  const progressCount = document.getElementById("progress-count");
  const progressDenom = document.getElementById("progress-denom");
  const progressNote = document.getElementById("progress-note");
  const meter = document.getElementById("meter");
  const meterBar = document.getElementById("meter-bar");
  const repostResults = document.getElementById("repost-results");
  const resultStats = document.getElementById("result-stats");
  const repostList = document.getElementById("repost-list");
  const noReposts = document.getElementById("no-reposts");
  const sessionAccount = document.getElementById("session-account");
  const savedSessions = document.getElementById("saved-sessions");
  const savedSessionList = document.getElementById("saved-session-list");
  let progressStarted = 0;
  let sessionRows = [];
  let groupNames = [];
  let selectedGroup = localStorage.getItem("x_selected_group") || "";
  // The links box as it was left on this browser (its text, and whether it was the one showing).
  try {
    linksField.value = localStorage.getItem("x_tweet_links") || "";
    if (localStorage.getItem("x_links_mode") === "1") setLinksMode(true);
  } catch (_) {}

  function clientId() {
    const key = "x_desk_client";
    let id = localStorage.getItem(key) || "";
    if (!/^[A-Za-z0-9_-]{16,80}$/.test(id)) {
      const bytes = new Uint8Array(18);
      crypto.getRandomValues(bytes);
      id = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
      localStorage.setItem(key, id);
    }
    return id;
  }

  function readTasks() {
    try {
      const parsed = JSON.parse(localStorage.getItem("x_reposts_tasks") || "[]");
      return Array.isArray(parsed) ? parsed.filter((row) => row && row.id && row.key) : [];
    } catch (_) {
      return [];
    }
  }

  function writeTasks(rows) {
    localStorage.setItem("x_reposts_tasks", JSON.stringify(rows.slice(0, 12)));
  }

  // A request with no answer in 15 s is dropped so the page can ask again, instead of waiting on
  // one that was cut while the phone was away or switching networks.
  async function fetchWithTimeout(url, options, ms = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    try {
      return await fetch(url, {...options, signal: controller.signal});
    } finally {
      clearTimeout(timer);
    }
  }

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

  function followTask(task) {
    if (!task || !task.id || liveTasks.has(task.id)) return;
    task.key = task.key || "";
    liveTasks.set(task.id, true);
    if (task.key) {
      const rows = readTasks().filter((row) => row.id !== task.id);
      rows.unshift(task);
      writeTasks(rows);
    }
    taskBoard.hidden = false;
    const card = document.createElement("article");
    card.className = "task-card";
    card.innerHTML = '<div class="progress-top"><div><h3></h3><p class="task-phase"></p><p class="task-msg"></p></div><b class="task-count">0</b></div><div class="meter is-indeterminate"><span class="task-bar"></span></div><div class="task-actions"><button class="btn btn-ghost task-stop" type="button">إيقاف هذه المهمة</button><button class="btn btn-ghost task-watch" type="button" hidden>مراقبة</button><button class="btn btn-ghost task-copy" type="button" hidden>نسخ التغريدات</button><button class="btn task-retry" type="button" hidden>إعادة المحاولة</button></div><p class="task-view-note" hidden>جارٍ تجهيز صورة المتصفح…</p><img class="task-view" alt="متصفح المهمة" hidden>';
    card.querySelector("h3").textContent = task.title || "مهمة";
    const stopButton = card.querySelector(".task-stop");
    const retryButton = card.querySelector(".task-retry");
    const copyButton = card.querySelector(".task-copy");
    const watchButton = card.querySelector(".task-watch");
    const view = card.querySelector(".task-view");
    const viewNote = card.querySelector(".task-view-note");
    let watching = false;
    let watchGen = 0;
    let viewUrl = "";
    // "مراقبة": the task's browser, asked for 4 times a second while open (only new pictures come back).
    const setWatching = async (on) => {
      watching = on;
      const gen = ++watchGen;
      watchButton.textContent = on ? "إخفاء المراقبة" : "مراقبة";
      viewNote.hidden = !on || Boolean(viewUrl);
      view.hidden = !on || !viewUrl;
      if (!on) {
        if (viewUrl) URL.revokeObjectURL(viewUrl);
        viewUrl = "";
        view.removeAttribute("src");
        return;
      }
      let lastFrame = 0;
      while (watching && gen === watchGen) {
        try {
          const response = await fetchWithTimeout(apiRoot + "/jobs/" + encodeURIComponent(task.id) + "/view?after=" + lastFrame, {
            headers: {"X-CSRF-Token": csrf, "X-Job-Key": task.key, "X-Client-Id": clientId()},
            cache: "no-store"
          });
          if (response.status === 200 && watching && gen === watchGen) {
            lastFrame = Number(response.headers.get("X-Frame")) || lastFrame;
            const url = URL.createObjectURL(await response.blob());
            if (viewUrl) URL.revokeObjectURL(viewUrl);
            viewUrl = url;
            view.src = url;
            view.hidden = false;
            viewNote.hidden = true;
          }
        } catch (_) {}
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    };
    watchButton.addEventListener("click", () => setWatching(!watching));
    let links = [];
    let linksWanted = true;
    copyButton.addEventListener("click", async () => {
      if (!links.length) return;
      const count = links.length;
      if (await copyText(links.join("\n"))) {
        copyButton.textContent = "نُسخت " + count + " تغريدة";
        setTimeout(() => { copyButton.textContent = "نسخ التغريدات"; }, 1500);
      } else {
        showNotice("تعذّر النسخ من هذا المتصفح.", false);
      }
    });
    // While the task runs the polls leave out the tweet list, so ask for its links once.
    const loadLinks = async () => {
      linksWanted = false;
      try {
        const response = await fetchWithTimeout(apiRoot + "/jobs/" + encodeURIComponent(task.id) + "?light=1&links=1", {
          headers: {"X-CSRF-Token": csrf, "X-Job-Key": task.key, "X-Client-Id": clientId()},
          cache: "no-store"
        });
        const data = await response.json();
        if (response.ok && Array.isArray(data.links) && data.links.length) {
          links = data.links;
          copyButton.hidden = false;
          return;
        }
      } catch (_) {}
      linksWanted = true;
    };
    stopButton.addEventListener("click", async () => {
      stopButton.disabled = true;
      try {
        await postJson("/stop", {job_id: task.id, job_key: task.key});
      } catch (_) {
        stopButton.disabled = false;
      }
    });
    retryButton.addEventListener("click", async () => {
      retryButton.disabled = true;
      try {
        const data = await postJson("/retry", {job_id: task.id, job_key: task.key});
        if (!data.success) {
          showNotice(data.message || "تعذّرت إعادة المحاولة.", false);
          retryButton.disabled = false;
          return;
        }
        retryButton.hidden = true;
        stopButton.disabled = false;
        showNotice((task.title ? task.title + ": " : "") + (data.message || "بدأت إعادة المحاولة."), true);
        if (!liveTasks.has(task.id)) {
          liveTasks.set(task.id, true);
          const rows = readTasks().filter((row) => row.id !== task.id);
          rows.unshift(task);
          writeTasks(rows);
          watch();
        }
      } catch (_) {
        showNotice("تعذّر الاتصال أثناء إعادة المحاولة.", false);
        retryButton.disabled = false;
      }
    });
    taskList.prepend(card);
    const paint = (data) => {
      const phase = data.phase || "collect";
      const titles = {login: "فتح القروب", prepare: "تجهيز", collect: "جمع", act: "تنفيذ"};
      card.querySelector(".task-phase").textContent = titles[phase] || "جارٍ العمل";
      card.querySelector(".task-msg").textContent = data.message || "";
      const current = phase === "act" ? (data.done || 0) : (data.collected || 0);
      const total = data.requested || 0;
      card.querySelector(".task-count").textContent = total ? current + " / " + total : String(current);
      const meter = card.querySelector(".meter");
      const indeterminate = data.status === "running" && (phase === "login" || phase === "prepare" || !total);
      meter.classList.toggle("is-indeterminate", indeterminate);
      card.querySelector(".task-bar").style.width = indeterminate ? "" : Math.max(0, Math.min(100, total ? Math.round((current / total) * 100) : 100)) + "%";
      stopButton.hidden = data.status !== "running";
      retryButton.hidden = !(data.status !== "running" && data.resumable);
      retryButton.disabled = false;
      if (Array.isArray(data.items) && data.items.length) links = data.items.map((item) => item && item.url).filter(Boolean);
      if (!links.length && linksWanted && phase === "act") loadLinks();
      copyButton.hidden = !links.length;
      watchButton.hidden = data.status !== "running";
      if (data.status !== "running" && watching) setWatching(false);
    };
    const watch = async () => {
      try {
        for (;;) {
          let response;
          let data;
          try {
            response = await fetchWithTimeout(apiRoot + "/jobs/" + encodeURIComponent(task.id) + "?light=1", {
              headers: {"X-CSRF-Token": csrf, "X-Job-Key": task.key, "X-Client-Id": clientId()},
              cache: "no-store"
            });
            data = await response.json();
          } catch (_) {
            // No answer (the phone was away, the network changed): keep trying instead of stopping.
            card.querySelector(".task-msg").textContent = "انقطع الاتصال لحظيًا. جارٍ إعادة الاتصال…";
            await new Promise((resolve) => setTimeout(resolve, 1500));
            continue;
          }
          if (response.status === 404) writeTasks(readTasks().filter((row) => row.id !== task.id));
          if (!response.ok) throw new Error(data.message || "تعذّر متابعة المهمة.");
          paint(data);
          if (data.status !== "running") {
            showNotice((task.title ? task.title + ": " : "") + (data.message || ""), Boolean(data.success));
            if (Array.isArray(data.results) && data.results.length) renderResults(data);
            // A task stopped with a retry button stays in the saved task list so it is still there after a reload.
            if (!data.resumable) writeTasks(readTasks().filter((row) => row.id !== task.id));
            loadActionCounts();
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 700));
        }
      } catch (error) {
        card.querySelector(".task-msg").textContent = error.message || "انقطع متابعة المهمة.";
      } finally {
        liveTasks.delete(task.id);
      }
    };
    watch();
  }

  function showAccount(text) {
    sessionAccount.textContent = text || "";
    sessionAccount.hidden = !text;
  }

  // The one-row view: the account's username; the token, its buttons and the chat PIN sit behind "إعداد الحساب".
  function syncAccountName() {
    const token = tokenField.value.trim();
    const row = sessionRows.find((item) => item.token === token);
    accountName.value = row && row.username ? "@" + row.username : (token ? "حساب غير معروف" : "");
  }

  function setAccountPanel(open) {
    accountPanel.hidden = !open;
    accountSettings.textContent = open ? "إخفاء الإعداد" : "إعداد الحساب";
    accountSettings.setAttribute("aria-expanded", open ? "true" : "false");
  }

  // Focus a field, opening the account settings first when it is inside them.
  function revealField(field) {
    if (accountPanel.contains(field)) setAccountPanel(true);
    field.focus();
  }

  function formatWhen(epoch) {
    if (!epoch) return "";
    const minutes = Math.round(Math.max(0, Date.now() - epoch * 1000) / 60000);
    if (minutes < 1) return "الآن";
    if (minutes < 60) return "قبل " + minutes + " د";
    const hours = Math.round(minutes / 60);
    if (hours < 24) return "قبل " + hours + " س";
    const days = Math.round(hours / 24);
    if (days < 14) return "قبل " + days + " ي";
    return new Date(epoch * 1000).toLocaleDateString("ar");
  }

  function renderSessions() {
    const token = tokenField.value.trim();
    savedSessions.hidden = sessionRows.length === 0;
    savedSessionList.replaceChildren();
    sessionRows.forEach((row) => {
      const item = document.createElement("li");
      item.className = "saved-session";
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "pick" + (row.token === token ? " is-on" : "");
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = row.username ? "@" + row.username : "حساب غير معروف";
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = [row.hint, formatWhen(row.verified_at)].filter(Boolean).join(" · ");
      pick.append(who, meta);
      pick.addEventListener("click", () => {
        tokenField.value = row.token;
        lockToken(true);
        pinField.value = row.chat_pin || "";
        showAccount(row.username ? "الجلسة المحفوظة: @" + row.username : "تم اختيار جلسة محفوظة.");
        showNotice(row.username
          ? (row.chat_pin ? "تم ملء جلسة @" + row.username + " ورمز دردشتها." : "تم ملء جلسة @" + row.username + ". أدخل رمز الدردشة مرة واحدة ليُحفظ.")
          : (row.chat_pin ? "تم ملء الرمز المحفوظ ورمز الدردشة." : "تم ملء الرمز المحفوظ."), true);
        renderSessions();
        pushScreen();
      });
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "drop";
      drop.textContent = "حذف";
      drop.setAttribute("aria-label", "حذف الجلسة" + (row.username ? " @" + row.username : ""));
      drop.addEventListener("click", async () => {
        drop.disabled = true;
        try {
          const data = await postJson("/sessions/delete", {id: row.id});
          if (!data.success) {
            showNotice(data.message || "تعذّر حذف الجلسة.", false);
            return;
          }
          if (tokenField.value.trim() === row.token) {
            tokenField.value = "";
            lockToken(true);
            showAccount("");
            pushScreen();
          }
          showNotice("حُذفت الجلسة من هذا الخادم.", true);
          await loadSessions();
        } catch (_) {
          showNotice("تعذّر حذف الجلسة.", false);
        } finally {
          drop.disabled = false;
        }
      });
      item.append(pick, drop);
      savedSessionList.appendChild(item);
    });
    syncAccountName();
  }

  async function loadSessions() {
    try {
      const response = await fetchWithTimeout(apiRoot + "/sessions", {
        headers: {"X-CSRF-Token": csrf, "X-Client-Id": clientId()},
        cache: "no-store"
      });
      const data = await response.json();
      if (!response.ok) return;
      sessionRows = data.sessions || [];
      renderSessions();
      showCommentsEngine(data.comments_engine);
      showOwnClaude(data);
      showCommentsServer(data);
      if (geminiByOwner) {
        geminiKey.value = data.gemini_api_key || "";
        geminiModel.value = data.gemini_model || "";
      } else {
        if (data.gemini_api_key && document.activeElement !== geminiKey) {
          geminiKey.value = data.gemini_api_key;
          localStorage.setItem("x_gemini_key", data.gemini_api_key);
        }
        if (data.gemini_model && document.activeElement !== geminiModel) {
          geminiModel.value = data.gemini_model;
          localStorage.setItem("x_gemini_model", data.gemini_model);
        }
      }
    } catch (_) {}
  }

  // Who writes the comments now (it may have changed in «توزيع البرنامج» since the page opened); nothing else
  // on the page is touched.
  async function loadCommentsEngine() {
    try {
      const response = await fetchWithTimeout(apiRoot + "/sessions", {
        headers: {"X-CSRF-Token": csrf, "X-Client-Id": clientId()},
        cache: "no-store"
      });
      const data = await response.json();
      if (response.ok) showCommentsEngine(data.comments_engine);
    } catch (_) {}
  }

  // Successful actions of all tasks so far (kept on the server), shown at the top of the page.
  async function loadActionCounts() {
    try {
      const response = await fetchWithTimeout(apiRoot + "/stats", {headers: {"X-CSRF-Token": csrf}, cache: "no-store"});
      const data = await response.json();
      if (!response.ok || !data.success) return;
      ["repost", "like", "bookmark", "comment"].forEach((key) => {
        document.getElementById("count-" + key).textContent = Number(data.counts[key] || 0).toLocaleString("en-US");
      });
    } catch (_) {}
  }

  function reveal(node) {
    node.hidden = false;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({block: "start", behavior: reduce ? "auto" : "smooth"});
  }

  function showNotice(message, success) {
    notice.textContent = message;
    notice.className = "notice " + (success ? "is-ok" : "is-bad");
    notice.hidden = false;
  }

  function selectedActions() {
    return ["repost", "like", "bookmark", "comment"].filter((key) => document.getElementById("action-" + key).checked);
  }

  function syncGemini() {
    replyVoice.hidden = !commentToggle.checked;
    if (!commentToggle.checked) {
      openGemini(false);
      openServer(false);
      if (claudeFields) { claudeFields.hidden = true; showClaude.setAttribute("aria-expanded", "false"); }
    }
  }

  function openGemini(open) {
    geminiFields.hidden = !open;
    if (showGemini) showGemini.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function selectedVoice() {
    return (document.querySelector('input[name="reply-voice"]:checked') || {}).value || "male";
  }

  // Total comments written by the model, and their price, read from the server.
  async function loadGeminiUsage() {
    try {
      const response = await fetchWithTimeout(apiRoot + "/gemini-usage", {headers: {"X-CSRF-Token": csrf}, cache: "no-store"});
      const data = await response.json();
      if (!response.ok || !data.success) return;
      // The comments since the cost was last reset (they start again from 0 with it); the count at
      // the top of the page is all of them and is never reset.
      const comments = Number(data.cost_comments ?? data.comments ?? 0);
      const total = Number(data.total || 0);
      geminiUsageText.textContent = "التعليقات (" + comments.toLocaleString("en-US") + ")";
      geminiCostValue.textContent = "$" + total.toFixed(3);
      // Brown at $0, redder as the cost grows, fully red from $10.
      const brown = [139, 69, 19];
      const red = [211, 47, 47];
      const share = Math.max(0, Math.min(1, total / 10));
      geminiCost.style.backgroundColor = "rgb(" + brown.map((value, i) => Math.round(value + (red[i] - value) * share)).join(", ") + ")";
    } catch (_) {}
  }

  function shortPath(url) {
    try { return new URL(url).pathname; } catch (_) { return url; }
  }

  function stat(value, label) {
    const box = document.createElement("div");
    box.className = "stat";
    const number = document.createElement("b");
    number.textContent = String(value);
    const caption = document.createElement("span");
    caption.textContent = label;
    box.append(number, caption);
    return box;
  }

  function pill(text, kind) {
    const node = document.createElement("span");
    node.className = "pill " + kind;
    node.textContent = text;
    return node;
  }

  async function postJson(path, payload) {
    const response = await fetch(apiRoot + path, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf, "X-Client-Id": clientId()},
      cache: "no-store",
      body: JSON.stringify({...payload, csrf_token: csrf})
    });
    const data = await response.json();
    if (!response.ok && !data.message) throw new Error("تعذّر إكمال الطلب.");
    return data;
  }

  function renderProgress(data) {
    progressPanel.hidden = false;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    progressPanel.scrollIntoView({block: "nearest", behavior: reduceMotion ? "auto" : "smooth"});
    const phase = data.phase || "collect";
    const titles = {login: "فتح القروب", prepare: "تجهيز المهمة", collect: "جمع تغريدات القروب", act: "تنفيذ الإجراءات"};
    progressTitle.textContent = titles[phase] || "جارٍ العمل";
    progressMessage.textContent = data.message || "";
    const current = phase === "act" ? (data.done || 0) : (data.collected || 0);
    const total = data.requested || 0;
    progressCount.textContent = String(current);
    progressDenom.textContent = total ? " / " + total : "";
    const pct = total ? Math.max(0, Math.min(100, Math.round((current / total) * 100))) : 0;
    const indeterminate = phase === "login" || phase === "prepare" || !total;
    meter.classList.toggle("is-indeterminate", indeterminate);
    meterBar.style.width = indeterminate ? "" : pct + "%";
    meter.setAttribute("aria-valuenow", String(pct));
    const elapsed = Math.max(0, Math.round((performance.now() - progressStarted) / 1000));
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    const clock = mins ? mins + " د " + secs + " ث" : secs + " ث";
    progressNote.textContent = phase === "act"
      ? "مضى " + clock + ". الحالات المفعّلة مسبقًا تُتجاوز."
      : "مضى " + clock + ". لا يُنفَّذ أي إجراء خلال هذه المرحلة.";
  }

  function finishTrackedJob(data) {
    if (!data || data.status === "running") return;
    showNotice(data.message || "", Boolean(data.success));
    renderProgress(data);
    if (data.status === "interrupted") {
      progressNote.textContent = "آخر حالة قبل توقف السكربت. لن تُستكمل العملية تلقائيًا.";
    }
    const hasResults = Array.isArray(data.results) && data.results.length > 0;
    if (data.kind === "execute" && hasResults) renderResults(data);
    loadSessions();
  }

  function renderResults(data) {
    const rows = data.results || [];
    let done = 0;
    let skip = 0;
    let fail = 0;
    repostList.replaceChildren();
    rows.forEach((row, index) => {
      const item = document.createElement("li");
      const top = document.createElement("div");
      top.className = "post-top";
      const idx = document.createElement("span");
      idx.className = "idx";
      idx.textContent = String(index + 1);
      const link = document.createElement("a");
      link.href = row.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = shortPath(row.url || "");
      top.append(idx, link);
      item.appendChild(top);
      const pills = document.createElement("div");
      pills.className = "pills";
      Object.entries(row.actions || {}).forEach(([key, value]) => {
        let kind = "is-fail";
        if (value === "تم التنفيذ") { kind = "is-will"; done += 1; }
        else if (String(value).includes("مفعّل") || String(value).includes("تخطي")) { kind = "is-skip"; skip += 1; }
        else fail += 1;
        pills.appendChild(pill((LABELS[key] || key) + " · " + value, kind));
      });
      item.appendChild(pills);
      if (row.comment) {
        const note = document.createElement("p");
        note.className = "excerpt";
        note.textContent = row.comment;
        item.appendChild(note);
      }
      repostList.appendChild(item);
    });
    resultStats.replaceChildren(stat(done, "نُفّذ"), stat(skip, "تُجاوز"), stat(fail, "لم يكتمل"));
    noReposts.hidden = rows.length > 0;
    repostResults.hidden = false;
    reveal(repostResults);
  }

  commentToggle.addEventListener("change", syncGemini);
  if (showGemini) showGemini.addEventListener("click", () => openGemini(geminiFields.hidden));
  if (serverFields) {
    const serverLink = document.getElementById("server-link");
    const saveServer = document.getElementById("save-server");
    showServer.addEventListener("click", () => {
      openServer(serverFields.hidden);
      if (!serverFields.hidden) serverLink.focus();
    });
    saveServer.addEventListener("click", async () => {
      saveServer.disabled = true;
      showNotice("جارٍ الاتصال بسيرفر التعليقات…", true);
      try {
        const data = await postJson("/comments-server", {link: serverLink.value.trim()});
        showNotice(data.message || "تعذّر حفظ رابط السيرفر.", Boolean(data.success));
        if (data.success) {
          serverLink.value = "";
          showCommentsServer(data);
          showCommentsEngine(data.comments_engine);
        }
      } catch (_) {
        showNotice("تعذّر حفظ رابط السيرفر.", false);
      } finally {
        saveServer.disabled = false;
      }
    });
    document.getElementById("delete-server").addEventListener("click", async () => {
      if (!window.confirm("حذف رابط سيرفر التعليقات؟ لا تُكتب التعليقات بدونه.")) return;
      try {
        const data = await postJson("/comments-server/delete", {});
        showNotice(data.message || "تعذّر حذف رابط السيرفر.", Boolean(data.success));
        if (data.success) {
          showCommentsServer(data);
          showCommentsEngine(data.comments_engine);
        }
      } catch (_) {
        showNotice("تعذّر حذف رابط السيرفر.", false);
      }
    });
  }
  if (claudeFields) {
    const claudeKey = document.getElementById("claude-key");
    const claudeModel = document.getElementById("claude-model");
    showClaude.addEventListener("click", () => {
      claudeFields.hidden = !claudeFields.hidden;
      showClaude.setAttribute("aria-expanded", claudeFields.hidden ? "false" : "true");
      if (!claudeFields.hidden) claudeKey.focus();
    });
    document.getElementById("save-claude").addEventListener("click", async () => {
      try {
        const data = await postJson("/claude", {api_key: claudeKey.value.trim(), model: claudeModel.value.trim()});
        showNotice(data.message || "تعذّر حفظ مفتاح Claude.", Boolean(data.success));
        if (data.success) {
          claudeKey.value = "";
          showOwnClaude(data);
          showCommentsEngine(data.comments_engine);
        }
      } catch (_) {
        showNotice("تعذّر حفظ مفتاح Claude.", false);
      }
    });
    document.getElementById("delete-claude").addEventListener("click", async () => {
      if (!window.confirm("حذف مفتاح Claude؟ تعود التعليقات إلى Gemini.")) return;
      try {
        const data = await postJson("/claude/delete", {});
        showNotice(data.message || "تعذّر حذف مفتاح Claude.", Boolean(data.success));
        if (data.success) {
          showOwnClaude(data);
          showCommentsEngine(data.comments_engine);
        }
      } catch (_) {
        showNotice("تعذّر حذف مفتاح Claude.", false);
      }
    });
  }
  // The reply's voice chosen last time, on this browser ("_v2": a choice made before «مذكر» became the
  // default is not restored).
  try {
    const voice = localStorage.getItem("x_reply_voice_v2");
    const box = document.querySelector('input[name="reply-voice"][value="' + voice + '"]');
    if (box) box.checked = true;
  } catch (_) {}
  document.querySelectorAll('input[name="reply-voice"]').forEach((box) => box.addEventListener("change", () => {
    try {
      localStorage.setItem("x_reply_voice_v2", selectedVoice());
    } catch (_) {}
  }));
  const savedModel = geminiByOwner ? "" : localStorage.getItem("x_gemini_model");
  const savedKey = geminiByOwner ? "" : localStorage.getItem("x_gemini_key");
  if (savedModel) geminiModel.value = savedModel;
  if (savedKey) geminiKey.value = savedKey;
  let geminiSaveTimer = 0;
  function rememberGeminiLocal() {
    if (geminiByOwner) return;
    const key = geminiKey.value.trim();
    const model = geminiModel.value.trim();
    if (key) localStorage.setItem("x_gemini_key", key);
    if (model) localStorage.setItem("x_gemini_model", model);
    clearTimeout(geminiSaveTimer);
    geminiSaveTimer = setTimeout(() => {
      if (!key && !model) return;
      postJson("/gemini", {gemini_api_key: key, gemini_model: model}).catch(() => {});
    }, 350);
  }
  geminiKey.addEventListener("change", rememberGeminiLocal);
  geminiModel.addEventListener("change", rememberGeminiLocal);
  geminiKey.addEventListener("blur", rememberGeminiLocal);
  geminiModel.addEventListener("blur", rememberGeminiLocal);
  editGemini.addEventListener("click", () => {
    if (geminiKey.readOnly) {
      lockGemini(false);
      geminiKey.focus();
      geminiKey.select();
      return;
    }
    lockGemini(true);
    rememberGeminiLocal();
  });
  syncGemini();
  loadGeminiUsage();
  setInterval(loadGeminiUsage, 15000);
  loadActionCounts();
  setInterval(loadActionCounts, 5000);
  // The invisible top-left button (the owner's copy only): with the password, the comment cost starts
  // again from 0 (the count stays). Users' costs are reset from «توزيع البرنامج».
  document.getElementById("cost-reset")?.addEventListener("click", async () => {
    const password = window.prompt("كلمة السر");
    if (password === null) return;
    try {
      const data = await postJson("/gemini-usage/reset", {password});
      showNotice(data.message || "تعذّرت إعادة التعيين.", Boolean(data.success));
      if (data.success) loadGeminiUsage();
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء إعادة التعيين.", false);
    }
  });

  accountSettings.addEventListener("click", () => setAccountPanel(accountPanel.hidden));

  async function verifySession() {
    try {
      const pulled = await postJson("/browser-session", {});
      if (pulled.success && pulled.auth_token) tokenField.value = pulled.auth_token;
    } catch (_) {}
    const authToken = tokenField.value.trim();
    if (!authToken) { showNotice("اضغط تسجيل الدخول، ثم ارجع واضغط تحقق.", false); return; }
    if (loginButton) loginButton.disabled = true;
    quickVerify.disabled = true;
    showNotice("جارٍ التحقق من الجلسة…", true);
    try {
      const data = await postJson("/verify-login", {auth_token: authToken});
      showNotice(data.message, data.success);
      if (data.success) {
        showAccount(data.username ? "تم التحقق: هذه الجلسة تخص @" + data.username : "تم التحقق من الدخول، دون قراءة الاسم.");
        await loadSessions();
      }
    } catch (_) {
      showNotice("تعذّر الاتصال لإكمال التحقق.", false);
    } finally {
      if (loginButton) loginButton.disabled = false;
      quickVerify.disabled = false;
    }
  }
  if (loginButton) loginButton.addEventListener("click", verifySession);
  quickVerify.addEventListener("click", verifySession);
  document.getElementById("browser-login")?.addEventListener("click", async () => {
    const button = document.getElementById("browser-login");
    button.disabled = true;
    showNotice("يُفتح المتصفح لتسجيل الدخول…", true);
    try {
      const data = await postJson("/browser-login", {});
      showNotice(data.message, Boolean(data.success));
    } catch (_) {
      showNotice("تعذّر فتح المتصفح.", false);
    } finally {
      button.disabled = false;
    }
  });

  function renderGroups() {
    groupBox.hidden = linksMode || groupNames.length === 0;
    // Three rows, or fewer when there are fewer groups (no empty rows under them).
    groupList.style.gridTemplateRows = "repeat(" + Math.max(1, Math.min(3, groupNames.length)) + ", auto)";
    groupList.replaceChildren();
    groupNames.forEach((name) => {
      const item = document.createElement("li");
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "pick" + (name === selectedGroup ? " is-on" : "");
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = name;
      pick.append(who);
      pick.addEventListener("click", () => {
        selectedGroup = name;
        localStorage.setItem("x_selected_group", name);
        renderGroups();
        showNotice("تم اختيار: " + name, true);
        pushScreen();
      });
      item.append(pick);
      groupList.appendChild(item);
    });
  }

  groupsButton.addEventListener("click", async () => {
    if (linksMode) {
      // «القروبات»: back to the groups shown before, at once (no browser, no chat).
      setLinksMode(false);
      if (!groupNames.length) showNotice("لا توجد قروبات معروضة بعد. اضغط «عرض القروبات» لجلبها.", true);
      return;
    }
    const authToken = tokenField.value.trim();
    const pin = pinField.value.trim();
    if (!authToken) { showNotice("سجّل الدخول ثم اضغط تحقق أولًا.", false); return; }
    if (!/^\d{4,8}$/.test(pin)) { showNotice("رمز الدردشة ٤ إلى ٨ أرقام.", false); revealField(pinField); return; }
    groupsButton.disabled = true;
    const settle = !!waitGroups.checked;
    const settleSeconds = secondsFrom(waitGroupsSec, 1, 120, 30);
    showNotice(settle ? "جارٍ فتح الدردشة ثم الانتظار " + settleSeconds + " ث قبل التقاط القروبات…" : "جارٍ فتح الدردشة وعرض القروبات…", true);
    try {
      const data = await postJson("/groups", {auth_token: authToken, chat_pin: pin, wait: settle, wait_seconds: settleSeconds});
      if (!data.success) {
        showNotice(data.message || "تعذّر عرض القروبات.", false);
        return;
      }
      groupNames = data.groups || [];
      if (selectedGroup && !groupNames.includes(selectedGroup)) selectedGroup = "";
      renderGroups();
      showNotice(groupNames.length ? "ظهر " + groupNames.length + " قروب. اختر واحدًا ثم اضغط تنفيذ." : "لم يظهر أي قروب.", groupNames.length > 0);
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء جلب القروبات.", false);
    } finally {
      groupsButton.disabled = false;
    }
  });

  document.getElementById("repost-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const authToken = tokenField.value.trim();
    const pin = pinField.value.trim();
    const stopRaw = stopField.value.trim();
    const stopMatch = stopRaw.match(/status\/(\d{6,25})/);
    const stopId = stopMatch ? stopMatch[1] : (/^\d{15,25}$/.test(stopRaw) ? stopRaw : "");
    const actions = selectedActions();
    const byLinks = linksMode;
    const links = byLinks ? pastedTweets(linksField.value) : [];
    if (!authToken) { showNotice("سجّل الدخول ثم اضغط تحقق أولًا.", false); return; }
    if (byLinks) {
      // No chat and no group: only the tweets are needed (the chat PIN is not).
      if (!links.length) { showNotice("الصق رابط تغريدة واحدًا على الأقل.", false); linksField.focus(); return; }
      if (links.length > MAX_LINKS) { showNotice("الحد الأقصى " + MAX_LINKS + " رابط في المهمة الواحدة.", false); linksField.focus(); return; }
    } else {
      if (!/^\d{4,8}$/.test(pin)) { showNotice("أدخل رمز الدردشة.", false); revealField(pinField); return; }
      if (!selectedGroup) { showNotice("اعرض القروبات ثم اختر واحدًا.", false); return; }
      if (!stopId) { showNotice("الصق رابط التغريدة المحددة أو رقمها.", false); stopField.focus(); return; }
    }
    if (!actions.length) { showNotice("اختر إجراءً واحدًا على الأقل.", false); return; }
    if (actions.includes("comment") && geminiByOwner) {
      // (Its keys come from the owner when it starts: the script waits for them, and says if they do not come.)
    } else if (actions.includes("comment")) {
      repostButton.disabled = true;          // (not sent twice while it asks)
      await loadCommentsEngine();
      repostButton.disabled = false;
      if (inApp && !commentsByOther) {
        openServer(true);
        showNotice("ضع رابط سيرفر التعليقات أولًا، ثم اضغط «حفظ واتصال».", false);
        document.getElementById("server-link").focus();
        return;
      }
      if (!commentsByOther) {
        if (!geminiKey.value.trim()) { openGemini(true); lockGemini(false); showNotice("أدخل مفتاح Gemini.", false); geminiKey.focus(); return; }
        if (!geminiModel.value.trim()) { openGemini(true); lockGemini(false); showNotice("أدخل اسم النموذج.", false); geminiModel.focus(); return; }
      }
      rememberGeminiLocal();
    }
    repostButton.disabled = true;
    try {
      const task = {
        auth_token: authToken,
        chat_pin: pin,
        actions,
        gemini_api_key: geminiKey.value.trim(),
        gemini_model: geminiModel.value.trim(),
        reply_voice: selectedVoice(),
        account_profiles: accountProfilesOn.checked ? secondsFrom(accountProfiles, 1, 5, 1) : 1,
        phone_screen: phoneScreen.checked,
        phone_height: secondsFrom(phoneHeight, 600, 25000, 10000)
      };
      if (byLinks) task.links = links;
      else Object.assign(task, {group: selectedGroup, stop_tweet: stopRaw, group_wait: groupWaitOn.checked ? secondsFrom(groupWaitSec, 0, 300, 30) : null});
      const started = await postJson("/group-run", task);
      if (!started.success || !started.job_id || !started.job_key) {
        showNotice(started.message || "تعذّر بدء التنفيذ.", false);
        return;
      }
      const account = (sessionAccount.textContent || "").replace("الجلسة المحفوظة: ", "");
      const name = byLinks ? "روابط (" + links.length + ")" : selectedGroup;
      followTask({
        id: started.job_id,
        key: started.job_key,
        title: name + (account ? " · " + account : "") + (phoneScreen.checked ? " · جوال " + secondsFrom(phoneHeight, 600, 25000, 10000) : ""),
        startedAt: Date.now()
      });
      showNotice((byLinks ? "بدأت مهمة على " + links.length + " تغريدة من الروابط." : "بدأت مهمة «" + selectedGroup + "».") + " يمكن بدء مهمة أخرى على نفس الحساب أو حساب مختلف.", true);
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء التنفيذ.", false);
    } finally {
      repostButton.disabled = false;
    }
  });

  tokenField.addEventListener("input", () => {
    const token = tokenField.value.trim();
    const row = sessionRows.find((item) => item.token === token);
    if (row) {
      showAccount(row.username ? "الجلسة المحفوظة: @" + row.username : "تم اختيار جلسة محفوظة.");
      if (document.activeElement !== pinField) pinField.value = row.chat_pin || "";
    } else showAccount("");
    renderSessions();
  });

  let pinSaveTimer = 0;
  function saveAccountPin() {
    const token = tokenField.value.trim();
    const pin = pinField.value.trim();
    if (!token || !/^\d{4,8}$/.test(pin)) return;
    const row = sessionRows.find((item) => item.token === token);
    if (row) row.chat_pin = pin;
    postJson("/sessions/pin", {auth_token: token, chat_pin: pin}).catch(() => {});
  }

  let screenTimer = 0;
  function pushScreen() {
    clearTimeout(screenTimer);
    screenTimer = setTimeout(() => {
      const payload = {
        auth_token: tokenField.value.trim(),
        chat_pin: pinField.value.trim(),
        group: selectedGroup,
        stop_tweet: stopField.value.trim(),
        actions: selectedActions()
      };
      if (groupNames.length) payload.groups = groupNames;
      postJson("/screen", payload).catch(() => {});
    }, 350);
  }

  function applyScreen(data) {
    if (!data) return;
    if (data.auth_token) tokenField.value = data.auth_token;
    lockToken(true);
    if (data.chat_pin) pinField.value = data.chat_pin;
    if (typeof data.stop_tweet === "string") stopField.value = data.stop_tweet;
    if (Array.isArray(data.actions) && data.actions.length) {
      ["repost", "like", "bookmark", "comment"].forEach((key) => {
        const box = document.getElementById("action-" + key);
        if (box) box.checked = data.actions.includes(key);
      });
      syncGemini();
    }
    if (Array.isArray(data.groups) && data.groups.length) {
      groupNames = data.groups;
      if (data.group) selectedGroup = data.group;
      renderGroups();
    } else if (data.group) {
      selectedGroup = data.group;
    }
  }

  async function restoreScreen() {
    // The saved tasks start showing their progress at once, without waiting for the rest.
    readTasks().forEach((task) => followTask(task));
    let runningJobs = [];
    let screenJob = null;
    for (let attempt = 0; attempt < 2 && !screenJob; attempt += 1) {
      try {
        const response = await fetchWithTimeout(apiRoot + "/state", {cache: "no-store"});
        const data = await response.json();
        if (response.ok && data && data.success) {
          applyScreen(data);
          screenJob = data;
          runningJobs = Array.isArray(data.running_jobs) ? data.running_jobs : [];
        }
      } catch (_) {}
    }
    await loadSessions();
    runningJobs.slice().reverse().forEach((job) => {
      const same = screenJob && screenJob.job_id === job.id;
      followTask({
        id: job.id,
        key: same ? (screenJob.job_key || "") : "",
        title: job.username || (screenJob && screenJob.group) || "مهمة"
      });
    });
    if (!runningJobs.length && screenJob && screenJob.job && screenJob.job.status && screenJob.job.status !== "running") {
      finishTrackedJob(screenJob.job);
    }
    if (taskList.childElementCount) {
      taskBoard.hidden = false;
      taskBoard.scrollIntoView({block: "start"});
    }
  }

  tokenField.addEventListener("change", pushScreen);
  tokenField.addEventListener("input", pushScreen);
  pinField.addEventListener("input", () => {
    clearTimeout(pinSaveTimer);
    pinSaveTimer = setTimeout(saveAccountPin, 400);
  });
  pinField.addEventListener("change", () => {
    saveAccountPin();
    pushScreen();
  });
  stopField.addEventListener("change", pushScreen);
  document.querySelectorAll('#repost-form input[type="checkbox"]').forEach((box) => {
    box.addEventListener("change", pushScreen);
  });

  restoreScreen();

  // Updates. A user's copy shows «تحديث» when the owner offers a newer version; the owner's
  // copy shows «توزيع البرنامج», which opens g.py's page.
  const updateBar = document.getElementById("update-bar");
  const updateText = document.getElementById("update-text");
  const updateButton = document.getElementById("update-button");
  const updateState = document.getElementById("update-state");
  const distLink = document.getElementById("dist-link");

  async function checkUpdate() {
    try {
      const response = await fetchWithTimeout(apiRoot + "/update/check", {cache: "no-store"}, 30000);
      const data = await response.json();
      if (data.owner) {
        // g.py's page: this host on its own port, unless CONTROL_URL says where.
        if (data.control_url || data.control_port) {
          distLink.href = data.control_url || location.protocol + "//" + location.hostname + ":" + data.control_port + "/";
          distLink.hidden = false;
        }
        return;
      }
      updateState.textContent = data.message || "";
      if (data.available && !updateButton.disabled) {
        updateText.textContent = "يتوفر إصدار جديد " + data.latest;
        updateButton.textContent = "تحديث إلى " + data.latest;
      }
      updateBar.hidden = !data.available;
    } catch (_) {}
  }

  updateButton.addEventListener("click", async () => {
    updateButton.disabled = true;
    updateText.textContent = "جارٍ تنزيل التحديث…";
    let data;
    try {
      data = await postJson("/update/apply", {});
    } catch (_) {
      data = {message: "تعذّر الاتصال أثناء التحديث."};
    }
    if (!data.success) {
      updateText.textContent = data.message || "تعذّر التحديث.";
      updateButton.disabled = false;
      return;
    }
    updateText.textContent = data.message;
    // Wait until the program runs again with the new version, then open its page.
    const started = Date.now();
    while (Date.now() - started < 90000) {
      await new Promise((done) => setTimeout(done, 1500));
      try {
        const response = await fetchWithTimeout(apiRoot + "/update/check?light=1", {cache: "no-store"}, 4000);
        if ((await response.json()).version === data.version) {
          location.reload();
          return;
        }
      } catch (_) {}
    }
    updateText.textContent = "لم يعد البرنامج للعمل بعد. حدّث الصفحة بعد قليل.";
  });

  checkUpdate();
  setInterval(checkUpdate, 30 * 60 * 1000);

})();
</script>
</body>
</html>

'''

PAGE = load_page()

ACTION_CONTROLS = {
    "repost": ("unretweet", "retweet"),
    "like": ("unlike", "like"),
    "bookmark": ("removeBookmark", "bookmark"),
}

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
      const path = link ? (link.getAttribute("href") || "") : "";
      if (link) link.click();
      else row.click();
      return {clicked: true, how, matched: nameOf(row), path: path, groups: [], rows: rows.length, loading: false};
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
  const paths = [];
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
    const link = row.querySelector('a[href*="/i/chat/"], a[href*="/messages/"]');
    paths.push(link ? (link.getAttribute("href") || "") : "");
  }
  const loading = queryAll('[data-testid="dm-conversation-list-loading-footer"]').some((el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  });
  return {clicked: false, groups, ids, paths, rows: rows.length, loading};
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

# Shared start of the group chat's scripts below: finds elements in the page and its shadow
# roots, and the element that really scrolls the chat.
CHAT_FIND_JS = r"""
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
  // How much of the element shows on the screen, in square pixels.
  const shownArea = (el) => {
    if (el.checkVisibility && !el.checkVisibility({opacityProperty: true, visibilityProperty: true, checkOpacity: true, checkVisibilityCSS: true})) return 0;
    const rect = el.getBoundingClientRect();
    const width = Math.min(rect.right, innerWidth) - Math.max(rect.left, 0);
    const height = Math.min(rect.bottom, innerHeight) - Math.max(rect.top, 0);
    return width > 0 && height > 0 ? width * height : 0;
  };
  // The match that shows most on the screen (the page can also hold a hidden copy of the
  // chat), else the first match.
  const query = (selector) => {
    const all = queryAll(selector);
    let best = null;
    let bestArea = 0;
    for (const el of all) {
      const area = shownArea(el);
      if (area > bestArea) {
        best = el;
        bestArea = area;
      }
    }
    return best || all[0] || null;
  };
  // The element that really scrolls the chat: X's scroller itself, one of its parents, or the
  // page (X's phone layout can scroll one of these instead of the scroller).
  const scrollerOf = (start) => {
    for (let node = start; node && node.nodeType === 1; node = node.parentElement || (node.parentNode && node.parentNode.host) || null) {
      if (/(auto|scroll|overlay)/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight + 10) return node;
    }
    const page = document.scrollingElement;
    if (page && page.scrollHeight > page.clientHeight + 10) return page;
    return start;
  };
"""

GROUP_READ_TWEETS_JS = r"""
(arg) => {
  let stopId = "";
  let dir = 0;
  let factor = 1;
  if (arg && typeof arg === "object") {
    stopId = String(arg.stopId || "");
    dir = arg.dir === 1 || arg.dir === -1 ? arg.dir : 0;
    factor = Number(arg.factor) > 1 ? Number(arg.factor) : 1;
  } else {
    stopId = String(arg || "");
  }
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
""" + CHAT_FIND_JS + r"""
  const collectDeep = (root, selector, out) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(selector).forEach((node) => out.push(node));
    root.querySelectorAll("*").forEach((node) => {
      if (node.shadowRoot) collectDeep(node.shadowRoot, selector, out);
    });
  };
  const list = query('[data-testid="dm-message-list"]')
    || query('[data-testid="dm-conversation-content"]')
    || query('[data-testid="dm-message-scroller"]');
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
    if (seen.has(statusId)) {
      if (hit) {
        const existing = tweets.find((row) => row.statusId === statusId);
        if (existing) existing.hit = true;
      }
      return;
    }
    seen.add(statusId);
    const rect = bubble.getBoundingClientRect();
    tweets.push({
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
  const scroller = scrollerOf(query('[data-testid="dm-message-scroller"]') || list);
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
    const base = dir > 0
      ? Math.max(560, Math.min(1200, Math.round(client * 0.75)))
      : Math.max(400, Math.min(800, Math.round(client * 0.5)));
    // GROUP_SCROLL_FACTOR times the step, but never past what the chat shows at once.
    const step = Math.max(base, Math.min(base * factor, Math.round(client * 0.9)));
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
""" + CHAT_FIND_JS + r"""
  const list = query('[data-testid="dm-message-list"]') || query('[data-testid="dm-conversation-content"]');
  const start = query('[data-testid="dm-message-scroller"]') || list;
  const scroller = start ? scrollerOf(start) : null;
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

ALLOWED_ACTIONS = {"repost", "like", "bookmark", "comment"}


def parse_requested_count(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def normalize_status_path(href: str) -> str | None:
    raw = href or ""
    for match in re.finditer(r"(?:https?://(?:www\.)?(?:x|twitter)\.com)?/([A-Za-z0-9_]{1,15})/status/(\d{6,25})", raw):
        owner, status_id = match.group(1), match.group(2)
        if owner.lower() in {"i", "web", "intent", "share"}:
            continue
        return f"/{owner}/status/{status_id}"
    status_id = re.search(r"status/(\d{6,25})", raw)
    if status_id:
        return f"/i/web/status/{status_id.group(1)}"
    return None


_mouse_at: dict[int, tuple[float, float]] = {}


def _mouse_start(page, target_x: float, target_y: float) -> tuple[float, float]:
    remembered = _mouse_at.get(id(page))
    if remembered:
        return remembered
    size = page.viewport_size or {"width": VIEWPORT_WIDTH, "height": 900}
    angle = random.uniform(0, math.tau)
    distance = random.uniform(140, 420)
    return (
        min(max(target_x + math.cos(angle) * distance, 8), size["width"] - 8),
        min(max(target_y + math.sin(angle) * distance, 8), size["height"] - 8),
    )


def _human_path(start: tuple[float, float], end: tuple[float, float], viewport: dict) -> list[tuple[float, float]]:
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy)
    if dist < 6:
        return [(ex, ey)]
    side_x, side_y = -dy / dist, dx / dist
    bend = max(-220.0, min(220.0, dist * random.uniform(0.18, 0.62))) * random.choice((-1.0, 1.0))
    c1 = (
        sx + dx * random.uniform(0.18, 0.42) + side_x * bend * random.uniform(0.35, 1.05),
        sy + dy * random.uniform(0.18, 0.42) + side_y * bend * random.uniform(0.35, 1.05),
    )
    c2 = (
        sx + dx * random.uniform(0.58, 0.86) + side_x * bend * random.uniform(-0.25, 0.85),
        sy + dy * random.uniform(0.58, 0.86) + side_y * bend * random.uniform(-0.25, 0.85),
    )
    width = float(viewport.get("width") or VIEWPORT_WIDTH)
    height = float(viewport.get("height") or 900)
    sway = random.uniform(2, 6)
    phase = random.uniform(0, math.tau)
    waves = random.uniform(1.0, 1.6)
    ease = random.uniform(1.2, 1.6)
    points: list[tuple[float, float]] = []
    steps = 2
    for index in range(1, steps + 1):
        raw = index / steps
        progress = 1 - (1 - raw) ** ease
        inverse = 1 - progress
        x = (inverse ** 3) * sx + 3 * (inverse ** 2) * progress * c1[0] + 3 * inverse * (progress ** 2) * c2[0] + (progress ** 3) * ex
        y = (inverse ** 3) * sy + 3 * (inverse ** 2) * progress * c1[1] + 3 * inverse * (progress ** 2) * c2[1] + (progress ** 3) * ey
        wobble = math.sin(progress * waves * math.tau + phase) * sway * (1 - progress)
        x += side_x * wobble
        y += side_y * wobble
        if index < steps:
            x = min(max(x, 1), width - 1)
            y = min(max(y, 1), height - 1)
        points.append((x, y))
    points[-1] = (ex, ey)
    return points


# Inside the Android app («قروبات») the page is not scrolled for the script's own sake: a button is clicked where
# it is, once the page has stopped moving, and the aim is checked again just before the press (on a phone X loads
# slower, and scrolling it while it loaded moved the buttons from under the mouse).
IN_APP = os.environ.get("GG_IN_APP") == "1"


def _same_box(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and all(abs(float(a[key]) - float(b[key])) < 1 for key in ("x", "y", "width", "height"))


def _in_view(page, box: dict | None) -> bool:
    if not box:
        return False
    viewport = page.viewport_size or {"width": VIEWPORT_WIDTH, "height": 900}
    middle_y = float(box["y"]) + float(box["height"]) / 2
    middle_x = float(box["x"]) + float(box["width"]) / 2
    return 0 < middle_y < viewport["height"] and 0 < middle_x < viewport["width"]


def _still_box(page, read_box, seconds: float = 5.0, scroll=None):
    """The box once it reads the same twice in a row (the page stopped moving). scroll(): asked once, only when
    the box is outside the page's view."""
    last = None
    scrolled = scroll is None
    deadline = time.monotonic() + seconds
    while True:
        box = read_box()
        if box and _same_box(box, last):
            if _in_view(page, box) or scrolled:
                return box
            try:
                scroll()
            except Exception:
                pass
            scrolled = True
            box = None
        if time.monotonic() >= deadline:
            return box
        last = box
        page.wait_for_timeout(150)


def _press_at(page, read_box, box: dict, local_x: float, local_y: float) -> None:
    """Move to the spot like a hand; if the button moved meanwhile, move again to where it is now; then press."""
    x, y = box["x"] + local_x, box["y"] + local_y
    _move_like_a_hand(page, x, y)
    for _ in range(4):
        try:
            now = read_box()
        except Exception:
            now = None
        if not now:
            break
        nx = now["x"] + min(local_x, max(1.0, now["width"] - 1))
        ny = now["y"] + min(local_y, max(1.0, now["height"] - 1))
        if abs(nx - x) < 1 and abs(ny - y) < 1:
            break
        page.mouse.move(nx, ny, steps=4)
        x, y = nx, ny
        _mouse_at[id(page)] = (x, y)
        page.wait_for_timeout(120)
    page.mouse.down()
    page.wait_for_timeout(100)
    page.mouse.up()


def _move_like_a_hand(page, x: float, y: float) -> None:
    viewport = page.viewport_size or {"width": VIEWPORT_WIDTH, "height": 900}
    start = _mouse_start(page, x, y)
    for px, py in _human_path(start, (x, y), viewport):
        page.mouse.move(px, py)
    _mouse_at[id(page)] = (x, y)


def real_click(locator) -> None:
    """Use the selector only to read the button box, then send a real mouse click."""
    locator.wait_for(state="visible", timeout=8000)
    if IN_APP:
        box = _still_box(locator.page, locator.bounding_box, scroll=lambda: locator.scroll_into_view_if_needed(timeout=4000))
    else:
        try:
            locator.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        box = locator.bounding_box()
    if not box or box["width"] < 2 or box["height"] < 2:
        raise PlaywrightTimeoutError("button box missing")
    width = box["width"]
    height = box["height"]
    inset_x = min(2.0, width * 0.12)
    inset_y = min(2.0, height * 0.12)
    zone = random.choice(("center", "left", "right", "top", "bottom"))
    if zone == "center":
        local_x = width * random.uniform(0.42, 0.58)
        local_y = height * random.uniform(0.42, 0.58)
    elif zone == "left":
        local_x = random.uniform(inset_x, max(inset_x + 0.5, width * 0.22))
        local_y = height * random.uniform(0.28, 0.72)
    elif zone == "right":
        local_x = random.uniform(min(width * 0.78, width - inset_x), width - inset_x)
        local_y = height * random.uniform(0.28, 0.72)
    elif zone == "top":
        local_x = width * random.uniform(0.28, 0.72)
        local_y = random.uniform(inset_y, max(inset_y + 0.5, height * 0.22))
    else:
        local_x = width * random.uniform(0.28, 0.72)
        local_y = random.uniform(min(height * 0.78, height - inset_y), height - inset_y)
    local_x = min(max(local_x, inset_x), width - inset_x)
    local_y = min(max(local_y, inset_y), height - inset_y)
    page = locator.page
    if IN_APP:
        _press_at(page, locator.bounding_box, box, local_x, local_y)
        return
    x = box["x"] + local_x
    y = box["y"] + local_y
    _move_like_a_hand(page, x, y)
    page.mouse.down()
    page.wait_for_timeout(100)
    page.mouse.up()


def article_locator(page, path: str):
    status_id = ""
    match = re.search(r"status/(\d{6,25})", path or "")
    if match:
        status_id = match.group(1)
    articles = page.locator('article[data-testid="tweet"]')
    if not status_id:
        return articles
    return articles.filter(has=page.locator(f'a[href*="/status/{status_id}"]'))


def click_box(page, box: dict) -> None:
    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)
    if width < 2 or height < 2:
        raise PlaywrightTimeoutError("button box missing")
    x = float(box["x"]) + width * random.uniform(0.4, 0.6)
    y = float(box["y"]) + height * random.uniform(0.4, 0.6)
    box_text = {key: round(float(box[key]), 1) for key in ("x", "y", "width", "height")}
    trace_click(f"ضغط على ({x:.0f},{y:.0f}) صندوق {box_text}")
    _move_like_a_hand(page, x, y)
    page.mouse.down()
    page.wait_for_timeout(100)
    page.mouse.up()


def read_action_state(page, status_id: str, actions: list[str]) -> dict | None:
    payload = page.evaluate(
        """({statusId, names, scroll}) => {
          const articles = [...document.querySelectorAll('article[data-testid="tweet"]')];
          const boxOf = (el) => {
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            if (rect.width < 2 || rect.height < 2) return null;
            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
          };
          let card = null;
          if (statusId) {
            card = articles.find((article) => [...article.querySelectorAll('a[href*="/status/"]')].some((node) => (node.getAttribute("href") || "").includes("/status/" + statusId)));
          }
          if (!card) card = articles[0] || null;
          if (!card) return null;
          if (scroll) { try { card.scrollIntoView({block: "center"}); } catch (error) {} }
          const map = {repost: ["unretweet", "retweet"], like: ["unlike", "like"], bookmark: ["removeBookmark", "bookmark"]};
          const labelOf = {
            reply: /reply|رد/i, retweet: /repost|retweet|إعادة النشر|اعادة النشر/i,
            unretweet: /undo repost|unretweet|تم إعادة|إلغاء إعادة/i,
            like: /like|إعجاب|اعجاب/i, unlike: /unlike|liked|أعجبني|معجب/i,
            bookmark: /bookmark|إشارة مرجعية/i, removeBookmark: /remove bookmark|إزالة الإشارة/i
          };
          const scopeOf = (article) => (article && article.closest("[data-testid='cellInnerDiv']")) || article;
          const findBtn = (root, testid) => {
            if (!root) return null;
            const re = labelOf[testid];
            const nodes = [];
            const tagged = root.querySelector('[data-testid="' + testid + '"]');
            if (tagged) nodes.push(tagged);
            if (re) {
              for (const node of root.querySelectorAll("button, [role='button']")) {
                const label = node.getAttribute("aria-label") || "";
                if (!re.test(label)) continue;
                if (testid === "like" && /unlike|liked|أعجبني|معجب/i.test(label)) continue;
                nodes.push(node);
              }
            }
            for (const node of nodes) {
              const btn = node.closest("button, [role='button']") || node;
              if (btn.closest("[data-testid='tweetTextarea_0'], [data-testid='toolBar']")) continue;
              const rect = btn.getBoundingClientRect();
              if (rect.width >= 2 && rect.height >= 2) return btn;
            }
            return null;
          };
          const out = {};
          const root = scopeOf(card);
          for (const name of names) {
            const pair = map[name];
            if (!pair) continue;
            const on = findBtn(root, pair[0]);
            const off = findBtn(root, pair[1]);
            out[name] = {on: !!on, box: on ? null : boxOf(off)};
          }
          return out;
        }""",
        {"statusId": status_id, "names": actions, "scroll": not IN_APP},
    )
    return payload if isinstance(payload, dict) else None


ACTION_BUTTON_PROBE_JS = r"""
({statusId, testid, scroll}) => {
  const labelOf = {
    reply: /reply|رد/i, retweet: /repost|retweet|إعادة النشر|اعادة النشر/i,
    unretweet: /undo repost|unretweet|تم إعادة|إلغاء إعادة/i,
    like: /like|إعجاب|اعجاب/i, unlike: /unlike|liked|أعجبني|معجب/i,
    bookmark: /bookmark|إشارة مرجعية/i, removeBookmark: /remove bookmark|إزالة الإشارة/i
  };
  const articles = [...document.querySelectorAll('article[data-testid="tweet"], [data-testid="tweet"]')];
  let card = null;
  if (statusId) {
    card = articles.find((article) => [...article.querySelectorAll('a[href*="/status/"]')]
      .some((node) => (node.getAttribute("href") || "").includes("/status/" + statusId)));
  }
  if (!card) card = articles[0] || null;
  if (!card) return null;
  const root = card.closest('[data-testid="cellInnerDiv"]') || card;
  const re = labelOf[testid];
  const nodes = [];
  const tagged = root.querySelector('[data-testid="' + testid + '"]');
  if (tagged) nodes.push(tagged);
  if (re) {
    for (const node of root.querySelectorAll("button, [role='button']")) {
      const label = node.getAttribute("aria-label") || "";
      if (!re.test(label)) continue;
      if (testid === "like" && /unlike|liked|أعجبني|معجب/i.test(label)) continue;
      nodes.push(node);
    }
  }
  let button = null;
  for (const node of nodes) {
    const btn = node.closest("button, [role='button']") || node;
    if (btn.closest("[data-testid='tweetTextarea_0'], [data-testid='toolBar']")) continue;
    const rect = btn.getBoundingClientRect();
    if (rect.width >= 2 && rect.height >= 2) { button = btn; break; }
  }
  if (!button) return {box: null};
  if (scroll) {
    try { button.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"}); } catch (error) {}
    return {scrolled: true};
  }
  const rect = button.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return {box: null};
  const x = rect.x + rect.width / 2;
  const y = rect.y + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  const ok = !!hit && (button === hit || button.contains(hit) || hit.closest("button, [role='button']") === button);
  let blocker = "";
  if (!ok && hit) {
    const tag = hit.closest("[data-testid]");
    blocker = (tag && tag.getAttribute("data-testid")) || hit.tagName.toLowerCase();
  }
  return {box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}, ok, blocker,
          inView: y > 0 && y < window.innerHeight};
}
"""

TAP_ACTION_JS = r"""
({statusId, testid}) => {
  const labelOf = {
    reply: /reply|رد/i, retweet: /repost|retweet|إعادة النشر|اعادة النشر/i,
    unretweet: /undo repost|unretweet|تم إعادة|إلغاء إعادة/i,
    like: /like|إعجاب|اعجاب/i, unlike: /unlike|liked|أعجبني|معجب/i,
    bookmark: /bookmark|إشارة مرجعية/i, removeBookmark: /remove bookmark|إزالة الإشارة/i
  };
  const articles = [...document.querySelectorAll('article[data-testid="tweet"], [data-testid="tweet"]')];
  let card = null;
  if (statusId) {
    card = articles.find((article) => [...article.querySelectorAll('a[href*="/status/"]')]
      .some((node) => (node.getAttribute("href") || "").includes("/status/" + statusId)));
  }
  if (!card) card = articles[0] || null;
  if (!card) return "missing";
  const root = card.closest('[data-testid="cellInnerDiv"]') || card;
  const re = labelOf[testid];
  const nodes = [];
  const tagged = root.querySelector('[data-testid="' + testid + '"]');
  if (tagged) nodes.push(tagged);
  if (re) {
    for (const node of root.querySelectorAll("button, [role='button']")) {
      const label = node.getAttribute("aria-label") || "";
      if (!re.test(label)) continue;
      if (testid === "like" && /unlike|liked|أعجبني|معجب/i.test(label)) continue;
      nodes.push(node);
    }
  }
  let button = null;
  for (const node of nodes) {
    const btn = node.closest("button, [role='button']") || node;
    if (btn.closest("[data-testid='tweetTextarea_0'], [data-testid='toolBar']")) continue;
    if (btn.tagName === "A" && /\/status\//.test(btn.getAttribute("href") || "")) continue;
    const rect = btn.getBoundingClientRect();
    if (rect.width >= 2 && rect.height >= 2) { button = btn; break; }
  }
  if (!button) return "missing";
  const rect = button.getBoundingClientRect();
  if (rect.bottom < 0 || rect.top > window.innerHeight) {
    try { button.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"}); } catch (error) {}
  }
  button.addEventListener("click", (event) => event.stopPropagation(), {capture: true, once: true});
  button.click();
  return "clicked";
}
"""


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


ACCEPT_COOKIES_JS = r"""
() => {
  const accept = /^(accept all cookies|قبول جميع ملفات الكوكيز)$/i;
  for (const node of document.querySelectorAll('button, [role="button"]')) {
    if (accept.test((node.textContent || "").replace(/\s+/g, " ").trim()) && node.getClientRects().length) {
      node.click();
      return true;
    }
  }
  return false;
}
"""


def accept_cookies(page) -> bool:
    """Press "قبول جميع ملفات الكوكيز" on X's cookie notice when it shows: it covers the bottom of the page."""
    try:
        clicked = bool(page.evaluate(ACCEPT_COOKIES_JS))
    except Exception:
        return False
    if clicked:
        page.wait_for_timeout(400)
    return clicked


def close_popups(page) -> int:
    """Close pop-up dialogs that cover the page, and accept the cookie notice. The chat itself is never touched."""
    cookies = accept_cookies(page)
    try:
        closed = page.evaluate(CLOSE_POPUPS_JS) or []
    except Exception:
        return int(cookies)
    if closed:
        if os.environ.get("COLLECT_DEBUG"):
            print(f"closed popups: {closed}", flush=True)
        page.wait_for_timeout(400)
    return len(closed) + int(cookies)


# Official mobile actions from X's docs (docs.x.com Web Intents). On a phone the timeline
# button is not the desktop control: a tap opens the post and its reply box. Like, repost and
# reply are documented as these pages, which work in a mobile WebView.
OFFICIAL_INTENTS = {
    "like": "https://x.com/intent/like?tweet_id={id}",
    "repost": "https://x.com/intent/retweet?tweet_id={id}",
}

CONFIRM_INTENT_JS = r"""
() => {
  const body = ((document.body && document.body.innerText) || "").replace(/\s+/g, " ").slice(0, 2500);
  if (/you liked|liked this tweet|تم الإعجاب|أعجبك هذا/i.test(body)) return "done";
  if (/reposted|you reposted|تم إعادة النشر|أعدت نشر/i.test(body)) return "done";
  const words = /^(like|إعجاب|اعجاب|repost|retweet|إعادة النشر|اعادة النشر)$/i;
  const nodes = [...document.querySelectorAll('button, [role="button"], input[type="submit"], [data-testid="confirmationSheetConfirm"]')];
  const hit = nodes.find((node) => {
    if (node.closest("[data-testid='tweetTextarea_0']")) return false;
    const test = node.getAttribute("data-testid") || "";
    const text = (node.innerText || node.value || node.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
    return test === "confirmationSheetConfirm" || words.test(text);
  });
  if (!hit) return "";
  hit.click();
  return "clicked";
}
"""


def _tweet_back_url(path: str, status_id: str) -> str:
    raw = (path or "").strip()
    if raw.startswith("http"):
        return raw
    if raw.startswith("/"):
        return "https://x.com" + raw
    if status_id:
        return f"https://x.com/i/web/status/{status_id}"
    return "https://x.com" + ("/" + raw if raw else "")


def press_official_intent(page, path: str, status_id: str, action: str) -> str:
    """Like or repost through the official mobile intent page, then return to the post."""
    if not status_id or action not in OFFICIAL_INTENTS:
        return "missing"
    back = _tweet_back_url(path, status_id)
    goto_x(page, OFFICIAL_INTENTS[action].format(id=status_id))
    outcome = ""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            outcome = str(page.evaluate(CONFIRM_INTENT_JS) or "")
        except Exception:
            outcome = ""
        if outcome:
            break
        page.wait_for_timeout(400)
    page.wait_for_timeout(900)
    try:
        goto_x(page, back)
        page.wait_for_timeout(800)
    except Exception:
        pass
    return "" if outcome else "missing"


# The phone page does not use the desktop buttons. A click on the card opens the tweet and
# stops there. These are the calls that page itself makes when a button works (the same web
# session, read from the open page). Query ids are the ones documented from X's own client
# and are replaced by whatever that page already loaded when it differs.
WEB_MUTATION_JS = r"""
async ({action, tweetId, text}) => {
  const bearer = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
  const fallback = {
    FavoriteTweet: "lI07N6Otwv1PhnEgXILM7A",
    UnfavoriteTweet: "ZYKSe-w7KEslx3JhSIk5LA",
    CreateRetweet: "mbRO74GrOvSfRcJnlMapnQ",
    DeleteRetweet: "ZyZigVsNiFO6v1dEks1eWg",
    CreateBookmark: "aoDbu3RHznuiSkQ9aNM67Q",
    DeleteBookmark: "Wlmlj2-xzyS1GN3a6cj-mQ",
    CreateTweet: "l4hk8uVGvR-a8kzQ4nOhPA"
  };
  const tweetFeatures = {
    premium_content_api_read_enabled: false,
    communities_web_enable_tweet_community_results_fetch: true,
    c9s_tweet_anatomy_moderator_badge_enabled: true,
    responsive_web_grok_analyze_button_fetch_trends_enabled: false,
    responsive_web_grok_analyze_post_followups_enabled: true,
    rweb_cashtags_composer_attachment_enabled: true,
    responsive_web_jetfuel_frame: true,
    rweb_sports_post_context_enabled: true,
    x_elections_post_context_enabled: true,
    responsive_web_grok_share_attachment_enabled: true,
    responsive_web_grok_annotations_enabled: true,
    responsive_web_edit_tweet_api_enabled: true,
    graphql_is_translatable_rweb_tweet_is_translatable_enabled: true,
    view_counts_everywhere_api_enabled: true,
    longform_notetweets_consumption_enabled: true,
    responsive_web_twitter_article_tweet_consumption_enabled: true,
    content_disclosure_indicator_enabled: true,
    content_disclosure_ai_generated_indicator_enabled: true,
    responsive_web_grok_show_grok_translated_post: true,
    responsive_web_grok_analysis_button_from_backend: true,
    post_ctas_fetch_enabled: true,
    longform_notetweets_rich_text_read_enabled: true,
    longform_notetweets_inline_media_enabled: false,
    profile_label_improvements_pcf_label_in_post_enabled: true,
    responsive_web_profile_redirect_enabled: true,
    rweb_tipjar_consumption_enabled: false,
    verified_phone_label_enabled: false,
    responsive_web_nested_quote_preview_enabled: true,
    articles_preview_enabled: true,
    rweb_cashtags_enabled: true,
    responsive_web_grok_community_note_auto_translation_is_enabled: true,
    freedom_of_speech_not_reach_fetch_enabled: true,
    standardized_nudges_misinfo: true,
    tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled: true,
    responsive_web_grok_image_annotation_enabled: true,
    responsive_web_grok_imagine_annotation_enabled: true,
    responsive_web_graphql_timeline_navigation_enabled: true
  };
  const ct0 = decodeURIComponent((document.cookie.match(/(?:^|; )ct0=([^;]+)/) || [])[1] || "");
  if (!ct0) return {ok: false, error: "لا توجد جلسة دخول في الصفحة"};
  const learn = () => {
    if (window.__ggQLearned) return;
    window.__ggQLearned = true;
    window.__ggQ = Object.assign({}, fallback, window.__ggQ || {});
    const grab = (source) => {
      if (!source) return;
      const first = /queryId:"([A-Za-z0-9_-]{8,80})",operationName:"([A-Za-z0-9_]+)"/g;
      const second = /operationName:"([A-Za-z0-9_]+)",queryId:"([A-Za-z0-9_-]{8,80})"/g;
      let match;
      while ((match = first.exec(source))) window.__ggQ[match[2]] = match[1];
      while ((match = second.exec(source))) window.__ggQ[match[1]] = match[2];
    };
    try {
      const chunks = self.webpackChunk_twitter_responsive_web || [];
      let budget = 0;
      for (const chunk of chunks) {
        const mods = chunk && chunk[1];
        if (!mods) continue;
        for (const fn of Object.values(mods)) {
          let source = "";
          try { source = Function.prototype.toString.call(fn); } catch (error) { continue; }
          if (source.indexOf("queryId") < 0 && source.indexOf("FavoriteTweet") < 0 && source.indexOf("CreateTweet") < 0) continue;
          grab(source);
          budget += source.length;
          if (budget > 2500000) return;
        }
      }
    } catch (error) {}
  };
  const send = async (name, variables, features) => {
    const queryId = (window.__ggQ && window.__ggQ[name]) || fallback[name];
    const response = await fetch("https://x.com/i/api/graphql/" + queryId + "/" + name, {
      method: "POST",
      credentials: "include",
      headers: {
        authorization: "Bearer " + bearer,
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "content-type": "application/json"
      },
      body: JSON.stringify({variables, features: features || {}, queryId})
    });
    const raw = await response.text();
    let data = {};
    try { data = JSON.parse(raw); } catch (error) {}
    const errors = data.errors || [];
    const message = (errors[0] && (errors[0].message || "")) || "";
    const already = /already|duplicate|has already|been already|327|139/i.test(message + " " + raw.slice(0, 300));
    return {status: response.status, ok: (response.status === 200 && errors.length === 0) || already, error: message || ("HTTP " + response.status), raw: raw.slice(0, 180)};
  };
  learn();
  const ops = {like: "FavoriteTweet", repost: "CreateRetweet", bookmark: "CreateBookmark", comment: "CreateTweet"};
  const name = ops[action];
  if (!name) return {ok: false, error: "إجراء غير معروف"};
  let variables = {tweet_id: String(tweetId)};
  let features = {};
  if (action === "repost") variables = {tweet_id: String(tweetId), dark_request: false};
  if (action === "comment") {
    variables = {
      tweet_text: text || "",
      dark_request: false,
      media: {media_entities: [], possibly_sensitive: false},
      semantic_annotation_ids: [],
      reply: {in_reply_to_tweet_id: String(tweetId), exclude_reply_user_ids: []}
    };
    features = tweetFeatures;
  }
  let result = await send(name, variables, features);
  if (!result.ok && result.status === 404) {
    window.__ggQLearned = false;
    learn();
    result = await send(name, variables, features);
  }
  if (!result.ok && action === "like") {
    const response = await fetch("https://x.com/i/api/1.1/favorites/create.json", {
      method: "POST",
      credentials: "include",
      headers: {
        authorization: "Bearer " + bearer,
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "content-type": "application/json"
      },
      body: JSON.stringify({id: String(tweetId)})
    });
    const raw = await response.text();
    if (response.status === 200 || /already|139/i.test(raw)) return {ok: true, via: "rest"};
    result = {ok: false, error: result.error || ("HTTP " + response.status)};
  }
  if (!result.ok && action === "repost") {
    const response = await fetch("https://x.com/i/api/1.1/statuses/retweet/" + encodeURIComponent(tweetId) + ".json", {
      method: "POST",
      credentials: "include",
      headers: {
        authorization: "Bearer " + bearer,
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "content-type": "application/x-www-form-urlencoded"
      },
      body: "id=" + encodeURIComponent(tweetId)
    });
    const raw = await response.text();
    if (response.status === 200 || /already|327/i.test(raw)) return {ok: true, via: "rest"};
  }
  return result.ok ? {ok: true} : {ok: false, error: (result.error || "فشل الطلب").slice(0, 140)};
}
"""


def web_mutate(page, status_id: str, action: str, text: str = "") -> dict:
    """Like, repost, bookmark, or reply through the call the logged-in page already uses."""
    try:
        result = page.evaluate(WEB_MUTATION_JS, {"action": action, "tweetId": status_id, "text": text or ""})
    except Exception as exc:
        return {"ok": False, "error": str(exc).splitlines()[0][:140]}
    return result if isinstance(result, dict) else {"ok": False, "error": "رد غير مفهوم من الصفحة"}


PRESS_PAGE_JS = r"""
async ({statusId, testid}) => {
  const labelOf = {
    reply: /reply|رد/i, retweet: /repost|retweet|إعادة النشر|اعادة النشر/i,
    unretweet: /undo repost|unretweet|تم إعادة|إلغاء إعادة/i,
    like: /like|إعجاب|اعجاب/i, unlike: /unlike|liked|أعجبني|معجب/i,
    bookmark: /bookmark|إشارة مرجعية/i, removeBookmark: /remove bookmark|إزالة الإشارة/i
  };
  const articles = [...document.querySelectorAll('article[data-testid="tweet"], [data-testid="tweet"]')];
  let card = articles.find((article) => statusId && [...article.querySelectorAll('a[href*="/status/"]')]
    .some((node) => (node.getAttribute("href") || "").includes("/status/" + statusId))) || articles[0] || null;
  if (!card) return {how: "missing"};
  const root = card.closest('[data-testid="cellInnerDiv"]') || card;
  const re = labelOf[testid];
  const nodes = [];
  const tagged = root.querySelector('[data-testid="' + testid + '"]');
  if (tagged) nodes.push(tagged);
  if (re) {
    for (const node of root.querySelectorAll("button, [role='button']")) {
      const label = node.getAttribute("aria-label") || "";
      if (!re.test(label)) continue;
      if (testid === "like" && /unlike|liked|أعجبني|معجب/i.test(label)) continue;
      nodes.push(node);
    }
  }
  let button = null;
  for (const node of nodes) {
    const btn = node.closest("button, [role='button']") || node;
    if (btn.closest("[data-testid='tweetTextarea_0'], [data-testid='toolBar']")) continue;
    const rect = btn.getBoundingClientRect();
    if (rect.width >= 2 && rect.height >= 2) { button = btn; break; }
  }
  if (!button) return {how: "missing"};
  const before = button.getAttribute("aria-label") || "";
  const event = (node) => ({
    preventDefault() {}, stopPropagation() {}, persist() {},
    nativeEvent: {isTrusted: true, button: 0},
    isTrusted: true, button: 0, type: "click",
    target: node, currentTarget: node
  });
  const fire = (node) => {
    if (!node) return "";
    const keys = Object.keys(node);
    for (const key of keys) {
      if (!key.startsWith("__reactProps$")) continue;
      const props = node[key];
      if (props && typeof props.onClick === "function") {
        props.onClick(event(node));
        return "react";
      }
    }
    return "";
  };
  let how = fire(button);
  if (!how) {
    for (const child of button.querySelectorAll("*")) {
      how = fire(child);
      if (how) break;
    }
  }
  await new Promise((done) => setTimeout(done, 400));
  const sheet = document.querySelector('[data-testid="confirmationSheetConfirm"], [data-testid="retweetConfirm"]');
  if (sheet) {
    fire(sheet) || sheet.click();
    how = (how || "sheet") + "+تأكيد";
  }
  return {how: how || "no-handler", before, after: button.isConnected ? (button.getAttribute("aria-label") || "") : "gone"};
}
"""


def press_action_button(page, path: str, status_id: str, action: str) -> str:
    """Scroll the button into view, make sure nothing covers it, then click it.

    Returns "" after a click reached the button, "missing" when the button is
    not on the page, or the name of whatever was covering the button.
    """
    testid = ACTION_CONTROLS[action][1]
    blocker = ""

    def probe(scroll: bool = False):
        try:
            return page.evaluate(ACTION_BUTTON_PROBE_JS, {"statusId": status_id, "testid": testid, "scroll": scroll})
        except Exception:
            return None

    for _ in range(3):
        close_popups(page)
        info = probe(False)
        trace_click(f"{action} {status_id} فحص {info}")
        if not info or not info.get("box"):
            trace_page(page, f"{action} الزر غير موجود")
            return "missing"
        if not info.get("inView"):
            probe(True)
            page.wait_for_timeout(400)
            info = probe(False)
        if info and info.get("ok") and info.get("inView") and info.get("box"):
            if IN_APP:
                try:
                    pressed = page.evaluate(PRESS_PAGE_JS, {"statusId": status_id, "testid": testid})
                except Exception as exc:
                    pressed = {"how": "error", "error": str(exc)[:160]}
                trace_click(f"{action} معالج الصفحة {pressed}")
                return ""
            click_box(page, info["box"])
            trace_click(f"{action} أُرسلت النقرة")
            return ""
        blocker = str((info or {}).get("blocker") or "خارج الشاشة")
        trace_click(f"{action} لم تُصب النقرة: {blocker}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(600)
    try:
        button = article_locator(page, path).first.locator(f'[data-testid="{testid}"]').first
        button.click(timeout=4000, delay=100)
        return ""
    except Exception:
        return blocker or "غير معروف"


def apply_actions_on_path(page, path: str, actions: list[str]) -> dict[str, str]:
    """Click only inactive controls. Already-on actions are left untouched."""
    status_id = ""
    match = re.search(r"status/(\d{6,25})", path or "")
    if match:
        status_id = match.group(1)
    try:
        state = read_action_state(page, status_id, actions)
    except Exception as exc:
        if browser_closed_error(exc):
            raise BrowserClosed() from exc
        state = None
    if not state:
        return {key: "تعذر العثور على المنشور" for key in actions}
    outcome: dict[str, str] = {}
    for action in actions:
        row = state.get(action) if isinstance(state.get(action), dict) else {}
        if row.get("on"):
            outcome[action] = "مفعّل مسبقًا — تم التجاوز"
            continue
        try:
            blocker = press_action_button(page, path, status_id, action)
            if blocker == "missing":
                outcome[action] = "زر الإجراء غير متاح — تم التجاوز"
                continue
            if blocker:
                outcome[action] = f"تعذر النقر: الزر مغطى بـ {blocker}"
                continue
            if action == "repost":
                confirm = page.locator('[data-testid="retweetConfirm"]')
                try:
                    confirm.wait_for(state="visible", timeout=1200)
                    real_click(confirm.first)
                except PlaywrightTimeoutError:
                    pass
            confirmed = False
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                again = read_action_state(page, status_id, [action]) or {}
                if (again.get(action) or {}).get("on"):
                    confirmed = True
                    break
                page.wait_for_timeout(60)
            outcome[action] = "تم التنفيذ" if confirmed else "أُرسل الطلب لكن تعذر تأكيد الحالة"
            if not confirmed and IN_APP and status_id:
                done = web_mutate(page, status_id, action)
                trace_click(f"{action} طلب الجلسة {done}")
                if done.get("ok"):
                    outcome[action] = "تم التنفيذ"
            trace_click(f"{action} النتيجة: {outcome[action]}")
        except Exception as exc:
            if browser_closed_error(exc):
                raise BrowserClosed() from exc
            outcome[action] = "تعذر التنفيذ"
            trace_click(action + " استثناء " + traceback.format_exc().replace("\n", " | ")[-400:])
    return outcome


def normalize_gemini_model(value: str) -> str | None:
    model = (value or "").strip()
    if model.lower().startswith("models/"):
        model = model.split("/", 1)[1].strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", model):
        return model
    return None


def valid_gemini_key(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{20,256}", value or ""))


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


def comment_is_complete(text: str) -> bool:
    # An emoji or two at the end are fine: the words before them are what is checked.
    text = re.sub(r"[\s\u200d\ufe0f\u2600-\u27bf\U0001f000-\U0001faff]+$", "", text or "")
    words = [word for word in text.split() if word]
    if len(words) < 6:
        return False
    if re.search(r"[،,؛;:：\-–—/\\*]$", text.strip()):
        return False
    last = re.sub(r"[.!?؟…\"'«»“”]+$", "", words[-1])
    return len(last) > 2


def gemini_thinking_config(model: str) -> dict | None:
    if re.search(r"2\.5", model):
        return {"thinkingBudget": 0}
    if re.search(r"gemini-3|3\.\d", model):
        return {"thinkingLevel": "low"}
    return None


def gemini_is_busy(status: int, detail: str) -> bool:
    if status in (429, 500, 502, 503, 504):
        return True
    return bool(re.search(
        r"high demand|try again later|unavailable|overloaded|resource exhausted|rate limit|quota",
        detail or "",
        re.I,
    ))


GEMINI_USAGE_PATH = DATA_DIR / "gemini_usage.json"
# Price counted for each comment the model writes (shown on the page as the running total).
GEMINI_PRICE_PER_COMMENT = 0.007
# Password asked by the page's hidden button that resets the cost (the comment count is kept). Only in
# the owner's copy: users' costs are reset from «توزيع البرنامج».
COST_RESET_PASSWORD = "a1@"
gemini_usage_lock = threading.Lock()
gemini_usage: dict = {}


def load_gemini_usage() -> None:
    loaded = read_private_json(GEMINI_USAGE_PATH) or {}
    try:
        comments = max(0, int(loaded.get("comments") or 0))
        since = float(loaded.get("since") or 0)
        # Comments counted in the cost since it was last reset (all of them if it never was).
        cost_comments = max(0, int(loaded.get("cost_comments", comments) or 0))
    except (TypeError, ValueError):
        comments, since, cost_comments = 0, 0.0, 0
    with gemini_usage_lock:
        gemini_usage.clear()
        # reset_id: the owner's last cost reset this copy made («توزيع البرنامج»).
        gemini_usage.update({"comments": comments, "since": since, "cost_comments": cost_comments, "reset_id": str(loaded.get("reset_id") or "")})


def record_comment_generated() -> None:
    """Count one comment written by the model."""
    try:
        with gemini_usage_lock:
            if not gemini_usage.get("since"):
                gemini_usage["since"] = time.time()
            gemini_usage["comments"] = int(gemini_usage.get("comments") or 0) + 1
            gemini_usage["cost_comments"] = int(gemini_usage.get("cost_comments") or 0) + 1
            write_private_json(GEMINI_USAGE_PATH, dict(gemini_usage))
    except Exception:
        pass


def reset_gemini_cost(reset_id: str = "") -> None:
    """Start the cost again from 0; the comment count is kept. reset_id: the owner's reset it answers."""
    with gemini_usage_lock:
        gemini_usage["cost_comments"] = 0
        if reset_id:
            gemini_usage["reset_id"] = reset_id
        write_private_json(GEMINI_USAGE_PATH, dict(gemini_usage))


# «صيغة الرد» on the page: how the comment speaks of the one writing it (the account's owner).
REPLY_VOICES = {
    "male": "You write as a man: when you speak of yourself, use masculine forms (in Arabic: مبسوط، متأكد).",
    "female": "You write as a woman: when you speak of yourself, use feminine forms (in Arabic: مبسوطة، متأكدة).",
    "neutral": "The reply must suit a man or a woman: never use words that show the writer's gender "
               "(in Arabic, no forms like مبسوط or مبسوطة, متأكد or متأكدة).",
}
# The voice when none (or an unknown one) is given.
DEFAULT_REPLY_VOICE = "male"
# How a reply takes the tweet: one is drawn at random for each reply, so the group's accounts do not all answer
# one tweet the same way (and one account does not answer every tweet the same way).
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
# How long, in words: drawn at random too, all within REPLY_MAX_CHARS.
REPLY_LENGTHS = ((6, 10), (8, 13), (10, 16))
# The most a posted reply may be, in characters (spaces included).
REPLY_MAX_CHARS = 100
# How much of the tweet's text goes with the screenshot: enough for a long post to be read to its end (a story
# cut at its start was answered wrongly).
REPLY_TWEET_TEXT = 1200
# How an account writes: one manner for each account (drawn from it, so it stays the same), as a real person
# keeps their own.
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
# The last replies written on this copy, by account and by tweet: the next reply is told not to look like them.
REPLIES_REMEMBERED = 8
replies_by_account: dict[str, list[str]] = {}
replies_by_tweet: dict[str, list[str]] = {}
replies_memory_lock = threading.Lock()


def reply_manner_number(account: str) -> int:
    """Which manner is the account's own (always the same one for an account)."""
    if not account:
        return random.randrange(len(REPLY_MANNERS))
    return int(hashlib.sha256(("reply-manner:" + account).encode("utf-8")).hexdigest()[:8], 16) % len(REPLY_MANNERS)


def reply_manner(account: str) -> str:
    """The account's own manner of writing (always the same one for an account)."""
    return REPLY_MANNERS[reply_manner_number(account)]


def remember_reply(account: str, tweet_id: str, text: str) -> None:
    with replies_memory_lock:
        for book, key in ((replies_by_account, account), (replies_by_tweet, tweet_id)):
            if key:
                kept = book.setdefault(key, [])
                kept.append(text)
                del kept[:-REPLIES_REMEMBERED]
        while len(replies_by_tweet) > 500:
            replies_by_tweet.pop(next(iter(replies_by_tweet)))


def reply_prompt(voice: str, account: str = "", tweet_id: str = "") -> str:
    """What the model is asked for one reply: a remark on the tweet's topic, not about the writer (no stories or
    experiences of theirs) and not a compliment, in the account's manner, with an angle and a length drawn for
    this reply, and the replies it must not look like."""
    low, high = random.choice(REPLY_LENGTHS)
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
        f"This time: {random.choice(REPLY_ANGLES)}. Your manner (always the same): {reply_manner(account)}. "
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
    with replies_memory_lock:
        own = list(replies_by_account.get(account) or []) if account else []
        others = [text for text in (replies_by_tweet.get(tweet_id) or []) if text not in own] if tweet_id else []
    if others:
        prompt += ("\n\nReplies other people already wrote to this same tweet. Yours must say something else, "
                   "in another way:\n" + "\n".join("- " + text for text in others))
    if own:
        prompt += ("\n\nYour own last replies to other tweets. This one must not look like them (another opening, "
                   "another shape, other words):\n" + "\n".join("- " + text for text in own))
    return prompt


def gemini_comment(api_key: str, model: str, image: bytes | None, tweet_text: str, voice: str = DEFAULT_REPLY_VOICE,
                   account: str = "", tweet_id: str = "") -> str:
    """One reply to a tweet, from the account (its profile key) to the tweet (its id)."""
    prompt = reply_prompt(voice, account, tweet_id)
    if image:
        prompt = "Look at the tweet in the image. " + prompt
    if tweet_text:
        prompt += "\n\nTweet text:\n" + tweet_text[:REPLY_TWEET_TEXT]
    parts: list[dict] = [{"text": prompt}]
    if image:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode("ascii")}})
    attempts: list[tuple[dict | None, int]] = [(gemini_thinking_config(model), 2048), (None, 4096)]
    busy_waits = (5, 12, 20, 30)
    best = ""
    saw_busy = False
    for thinking, max_tokens in attempts:
        # The model's own temperature (1.0): Google advises it for Gemini 3, and it keeps the replies varied.
        config: dict = {"temperature": 1.0, "maxOutputTokens": max_tokens}
        if thinking:
            config["thinkingConfig"] = thinking
        body = {"contents": [{"parts": parts}], "generationConfig": config}
        payload = None
        thinking_rejected = False
        for try_index in range(len(busy_waits) + 1):
            request = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + quote(model, safe="")
                + ":generateContent",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
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
                if gemini_is_busy(getattr(exc, "code", 0) or 0, detail):
                    saw_busy = True
                    if try_index < len(busy_waits):
                        time.sleep(busy_waits[try_index])
                        continue
                    break
                raise RuntimeError(detail or "رفض Gemini الطلب") from None
            except Exception as exc:
                if try_index < 2:
                    time.sleep(busy_waits[try_index])
                    continue
                raise RuntimeError("تعذر الاتصال بـ Gemini") from exc
        if thinking_rejected:
            continue
        if not payload:
            break
        candidate = (payload.get("candidates") or [{}])[0]
        reply_parts = ((candidate.get("content") or {}).get("parts") or [])
        texts = []
        for part in reply_parts:
            if not isinstance(part, dict) or part.get("thought"):
                continue
            if part.get("text"):
                texts.append(str(part["text"]))
        text = clean_comment(" ".join(texts))
        if comment_is_complete(text):
            record_comment_generated()
            remember_reply(account, tweet_id, text)
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        record_comment_generated()
        remember_reply(account, tweet_id, best)
        return best
    if saw_busy:
        raise RuntimeError("النموذج مشغول مؤقتًا بعد عدة محاولات. غيّر النموذج أو أعد التنفيذ بعد قليل.")
    return ""


# Comments by Grok (xAI), when the owner chooses it in «توزيع البرنامج»: for users' copies, its key and model
# come with each check-in (kept in memory only); the owner's own copy follows the same choice, with the key
# and model saved there. (A copy of the script with no «توزيع البرنامج» has its own «مفتاح Claude» in its
# panel instead: see Claude's part below.) Each comment is asked with the same prompt as Gemini's, through
# xAI's Responses API, with the tweet's picture.
GROK_URL = "https://api.x.ai/v1/responses"
GROK_DEFAULT_MODEL = "grok-4.3"             # (as in g.py: the model when none is set there)
GROK_PATH = DATA_DIR / "grok.json"         # (a Grok key kept from before: removed when the program starts)
# Comments are asked with no reasoning: much faster (and cheaper) than thinking first, and enough for a reply
# this short. A model that does not take the setting is asked again without it (with its own default), and
# is not sent it again.
GROK_REASONING = {"effort": "none"}
grok_reasoning_refused: set[str] = set()
grok_lock = threading.Lock()
saved_grok: dict = {"engine": "gemini", "api_key": "", "model": ""}


def valid_grok_key(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{20,256}", value or ""))


def valid_grok_model(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,60}", value or ""))


def apply_owner_comments(engine: str, key: str, model: str) -> None:
    """Who the owner chose to write this copy's comments (from its check-in): Grok or Claude, with its key and
    model, or Gemini (the other key is then dropped here)."""
    valid = (engine == "grok" and valid_grok_key(key) and valid_grok_model(model)) or \
        (engine == "claude" and valid_claude_key(key) and valid_claude_model(model))
    wanted = {"engine": engine if valid else "gemini", "api_key": key if valid else "", "model": model if valid else ""}
    with grok_lock:
        saved_grok.update(wanted)              # (in memory only)


def distribution_saved() -> dict:
    """What «توزيع البرنامج» saved beside this script (g.py's data/distribution.json), only read here."""
    try:
        loaded = json.loads((CONTROL_FILE.with_name("data") / "distribution.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def owner_comments_choice() -> dict:
    """The owner's copy: who writes the comments, as chosen in «توزيع البرنامج» for users' copies (the same for
    this one) {"engine", "api_key", "model"}: from g.py's page when this program serves it, else from what
    that page saved beside this script."""
    page = distribution_page
    names = ("comments_engine", "grok_key", "grok_model", "claude_key", "claude_model")
    if page is not None:
        with page.store_lock:
            saved = {name: page.store.get(name) for name in names}
        grok_default = str(getattr(page, "GROK_DEFAULT_MODEL", "") or GROK_DEFAULT_MODEL)
        claude_default = str(getattr(page, "CLAUDE_DEFAULT_MODEL", "") or CLAUDE_DEFAULT_MODEL)
    else:
        saved, grok_default, claude_default = distribution_saved(), GROK_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL
    engine = str(saved.get("comments_engine") or "")
    if engine == "claude":
        return {"engine": engine, "api_key": str(saved.get("claude_key") or "").strip(),
                "model": str(saved.get("claude_model") or "").strip().lower() or claude_default}
    return {"engine": engine, "api_key": str(saved.get("grok_key") or "").strip(),
            "model": str(saved.get("grok_model") or "").strip().lower() or grok_default}


def comments_writer() -> tuple[str, str, str]:
    """Who writes this copy's comments: ("grok" | "claude", key, model), or ("gemini", "", "") — the owner's
    choice in «توزيع البرنامج», for users' copies (from their check-ins) and for the owner's own copy alike. In the
    app: ("server", the server's link, "") when the link is saved in the panel, else ("none", "", ""): there the
    comments are written on the owner's server only, never from the phone."""
    if IN_APP:
        link = saved_server_link()
        return ("server", link, "") if link else ("none", "", "")
    if is_owner_copy() and not key_typed_here():
        choice = owner_comments_choice()
    else:
        # A user's copy (from its check-ins), or a copy with no «توزيع البرنامج» (the key typed in its panel).
        with grok_lock:
            choice = dict(saved_grok)
    engine, key, model = choice.get("engine"), str(choice.get("api_key") or ""), str(choice.get("model") or "")
    if engine == "grok" and valid_grok_key(key) and valid_grok_model(model):
        return "grok", key, model
    if engine == "claude" and valid_claude_key(key) and valid_claude_model(model):
        return "claude", key, model
    return "gemini", "", ""


def grok_settings() -> tuple[str, str]:
    """(key, model) when Grok writes this copy's comments, else ("", "")."""
    engine, key, model = comments_writer()
    return (key, model) if engine == "grok" else ("", "")


def grok_comments_on() -> bool:
    """Whether Grok writes this copy's comments (see comments_writer)."""
    return bool(grok_settings()[0])


def gemini_writes_comments() -> bool:
    """Whether Gemini writes this copy's comments (no Grok or Claude chosen)."""
    return comments_writer()[0] == "gemini"


def grok_answer_text(answer: dict) -> str:
    """The text of a Responses API answer: output[] → message → content[] → output_text."""
    parts = []
    for item in answer.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    parts.append(str(content.get("text") or ""))
    return "\n".join(parts) or str(answer.get("output_text") or "")


def grok_comment(api_key: str, model: str, image: bytes | None, tweet_text: str, voice: str = DEFAULT_REPLY_VOICE,
                 account: str = "", tweet_id: str = "") -> str:
    """One reply to a tweet by Grok: the same prompt as Gemini's, the tweet's picture with it, no reasoning. A
    model that does not take the reasoning setting is asked again without it; a busy Grok is asked again a
    few times."""
    prompt = reply_prompt(voice, account, tweet_id)
    if image:
        prompt = "Look at the tweet in the image. " + prompt
    if tweet_text:
        prompt += "\n\nTweet text:\n" + tweet_text[:REPLY_TWEET_TEXT]
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    if image:
        content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")})
    reasoning = model not in grok_reasoning_refused
    busy_waits = (5, 12, 20, 30)
    best = ""
    saw_busy = False
    for _ in range(2):               # a reply that comes back cut off is asked for once more
        payload = None
        for try_index in range(len(busy_waits) + 1):
            body: dict = {"model": model, "input": [{"role": "user", "content": content}]}
            if reasoning:
                body["reasoning"] = dict(GROK_REASONING)
            request = urllib.request.Request(GROK_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    data = json.loads(exc.read(4096).decode("utf-8", "replace"))
                    error = data.get("error") if isinstance(data, dict) else None
                    detail = str((error.get("message") if isinstance(error, dict) else error) or data.get("message") or "")
                except Exception:
                    detail = ""
                detail = re.sub(r"\s+", " ", detail.replace(api_key, "…") if api_key else detail).strip()[:160]
                if reasoning and exc.code == 400 and re.search(r"reasoning", detail, re.I):
                    reasoning = False    # this model takes no reasoning setting: asked again without it,
                    grok_reasoning_refused.add(model)    # and not sent it again
                    continue
                if exc.code in (401, 403):
                    raise RuntimeError("مفتاح Grok غير صالح أو بلا صلاحية. أخبر صاحب البرنامج.") from None
                if re.search(r"credit|spending limit|billing", detail, re.I):
                    raise RuntimeError("نفد رصيد Grok أو بلغ حد الإنفاق. أخبر صاحب البرنامج.") from None
                if exc.code in (429, 500, 502, 503, 504):
                    saw_busy = True
                    if try_index < len(busy_waits):
                        time.sleep(busy_waits[try_index])
                        continue
                    break
                raise RuntimeError(("رفض Grok الطلب: " + detail) if detail else f"تعذر الاتصال بـ Grok (رمز {exc.code}).") from None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if try_index < 2:
                    time.sleep(busy_waits[try_index])
                    continue
                raise RuntimeError("تعذر الاتصال بـ Grok") from exc
        if not isinstance(payload, dict):
            break
        text = clean_comment(grok_answer_text(payload))
        if comment_is_complete(text):
            record_comment_generated()
            remember_reply(account, tweet_id, text)
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        record_comment_generated()
        remember_reply(account, tweet_id, best)
        return best
    if saw_busy:
        raise RuntimeError("Grok مشغول مؤقتًا بعد عدة محاولات. أعد التنفيذ بعد قليل.")
    return ""


# Comments by Claude (Anthropic), when the owner chooses it in «توزيع البرنامج»: the same as Grok's (its key and
# model come with each check-in, in memory only), asked through the Messages API with the same prompt and the
# tweet's picture, at medium effort (it thinks a little before it writes).
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION = "2026-10-21"
CLAUDE_DEFAULT_MODEL = "claude-haiku-5-5"   # (as in g.py: the model when none is set there)
CLAUDE_EFFORT = "medium"
# Models that take no effort setting (older ones), asked again without it and not sent it again.
claude_effort_refused: set[str] = set()


def valid_claude_key(value: str) -> bool:
    return bool(re.fullmatch(r"sk-ant-[A-Za-z0-9_\-]{20,250}", value or ""))


def valid_claude_model(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,60}", value or ""))


# A copy of the script with no «توزيع البرنامج» (no g.py beside it) has its own «مفتاح Claude» in its panel: the
# key typed there, kept sealed in data/claude.json, writes its comments with Claude, at the same effort and with
# the same prompt, and it needs nothing else. (Its panel had «مفتاح Grok» before: a Grok key kept from then is
# removed, and the comments are Gemini's until a Claude key is saved.)
CLAUDE_PATH = DATA_DIR / "claude.json"


def key_typed_here() -> bool:
    """A copy on its own (see on_its_own): its comments are written with the Claude key typed in its own panel,
    when one is saved, and it needs nothing else."""
    return on_its_own()


def load_panel_key() -> None:
    """A user's copy keeps the comments key in memory only, from its check-ins, never on its disk: files from
    before (data/grok.json, which held one) are removed, and the copy starts with Gemini until its first
    check-in. The owner's copy reads back the Claude key typed in its own panel («مفتاح Claude»), kept there
    sealed; a Grok key typed there before («مفتاح Grok», no longer in the panel) is removed, as is any file it
    cannot use."""
    key = model = ""
    if is_owner_copy():
        loaded = read_private_json(CLAUDE_PATH) or {}
        key = open_key(loaded.get("api_key_sealed")).strip() if loaded.get("source") == "panel" else ""
        model = str(loaded.get("model") or "").strip().lower() or CLAUDE_DEFAULT_MODEL
    if not (valid_claude_key(key) and valid_claude_model(model)):
        key = model = ""
    for path in (GROK_PATH,) if key else (GROK_PATH, CLAUDE_PATH):
        try:
            path.unlink()
        except OSError:
            pass
    with grok_lock:
        saved_grok.update(engine="claude" if key else "gemini", api_key=key, model=model)


def remember_own_claude(key: str, model: str) -> None:
    """The Claude key and model typed in this copy's panel (none: back to Gemini), kept sealed."""
    with grok_lock:
        saved_grok.update(engine="claude" if key else "gemini", api_key=key, model=model if key else "")
        if key:
            write_private_json(CLAUDE_PATH, {"version": 1, "source": "panel", "api_key_sealed": seal_key(key), "model": model})
        else:
            try:
                CLAUDE_PATH.unlink()
            except OSError:
                pass


def own_claude_shown() -> dict:
    """What the panel shows of its own Claude key: only its first and last letters."""
    with grok_lock:
        key, model = (saved_grok["api_key"], saved_grok["model"]) if saved_grok["engine"] == "claude" else ("", "")
    return {"claude_key": f"{key[:7]}…{key[-4:]}" if key else "", "claude_model": model or CLAUDE_DEFAULT_MODEL}


# In the app the comments are written on the owner's server (comments_server.py, the server the «تعليقات» app
# uses), not from the phone: the panel keeps only that server's link, as comments_server.py prints it:
#     https://address:port/secret#fingerprint
# The connection is encrypted and the server is known by its certificate's fingerprint, which the link carries.
# The server reads the tweet, asks the model with the key kept there, and answers with the finished reply. No
# model key is on the phone.
COMMENTS_SERVER_PATH = DATA_DIR / "comments_server.json"
SERVER_LINK = re.compile(r"https://(\[[0-9A-Fa-f:]{2,39}\]|[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?)(?::(\d{1,5}))?/([A-Za-z0-9_\-]{16,64})/?#([0-9a-fA-F]{64})")
SERVER_CONNECT_SECONDS = 15
SERVER_COMMENT_SECONDS = 300          # (the server reads the tweet, then the model may think before it writes)
SERVER_RETRY_WAITS = (1.5, 4.0)       # a connection that did not reach the server is tried again, a little later
OWNER_LINK = " (يطبعه السيرفر: python3 comments_server.py --link)"
NO_SERVER_LINK = "ضع رابط سيرفر التعليقات أولًا: إعداد التعليقات ← «سيرفر التعليقات»."
comments_server_lock = threading.Lock()
comments_server = {"link": "", "model": ""}


class ServerNotReached(RuntimeError):
    """The connection did not reach the server: it may be tried again."""


def tidy_server_link(text: str) -> str:
    """The link as pasted, without the spaces and the unseen marks a paste may carry."""
    return "".join(ch for ch in str(text or "") if not ch.isspace() and unicodedata.category(ch) != "Cf")


def parse_server_link(link: str) -> tuple[str, int, str, str] | None:
    """(address, port, secret, fingerprint) of a server link, or None when it is not one."""
    match = SERVER_LINK.fullmatch(link or "")
    if not match:
        return None
    port = int(match.group(2) or 443)
    if not 0 < port < 65536:
        return None
    return match.group(1), port, match.group(3), match.group(4).lower()


def server_shown(link: str) -> str:
    """Where the server is, without its secret."""
    parsed = parse_server_link(link)
    return f"{parsed[0]}:{parsed[1]}" if parsed else ""


def saved_server_link() -> str:
    with comments_server_lock:
        return comments_server["link"]


def load_comments_server() -> None:
    loaded = read_private_json(COMMENTS_SERVER_PATH) or {}
    link = open_key(loaded.get("link_sealed")).strip() if loaded.get("link_sealed") else ""
    with comments_server_lock:
        comments_server.update(link=link if parse_server_link(link) else "", model="")


def remember_comments_server(link: str, model: str = "") -> None:
    """The server's link (none: forgotten), kept sealed as the keys are."""
    with comments_server_lock:
        comments_server.update(link=link, model=model if link else "")
        if link:
            write_private_json(COMMENTS_SERVER_PATH, {"version": 1, "link_sealed": seal_key(link)})
        else:
            try:
                COMMENTS_SERVER_PATH.unlink()
            except OSError:
                pass


def server_exchange(link: str, action: str, body: dict | None = None, seconds: float = 30) -> dict:
    """One request to the comments server. Its certificate is checked against the link's fingerprint before
    anything is sent (the secret is in the request). What it answers, or RuntimeError with words for the user."""
    parsed = parse_server_link(link)
    if not parsed:
        raise RuntimeError("رابط السيرفر غير صحيح. انسخه كاملًا كما هو" + OWNER_LINK + ".")
    host, port, token, pin = parsed
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE              # (the certificate is known by its fingerprint instead, below)
    connection = http.client.HTTPSConnection(host, port, timeout=SERVER_CONNECT_SECONDS, context=context)
    try:
        try:
            connection.connect()
        except ssl.SSLError:
            raise RuntimeError("لم يتم الاتصال الآمن بالسيرفر: على هذا العنوان والمنفذ برنامج آخر. راجع الرابط.") from None
        except OSError:
            raise ServerNotReached("تعذّر الوصول إلى سيرفر التعليقات. تأكد من اتصال الجوال بالإنترنت ومن أن السيرفر يعمل.") from None
        seen = hashlib.sha256(connection.sock.getpeercert(binary_form=True) or b"").hexdigest()
        if not hmac.compare_digest(seen, pin):
            raise RuntimeError("شهادة السيرفر لا تطابق الرابط. خذ الرابط الحالي" + OWNER_LINK + ".")
        connection.sock.settimeout(seconds)
        sent = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Connection": "close"}
        if sent is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            connection.request("POST" if sent is not None else "GET", f"/{token}/{action}", body=sent, headers=headers)
            answer = connection.getresponse()
            status, raw = answer.status, answer.read(1 << 20)
        except (socket.timeout, TimeoutError):
            raise RuntimeError("طال انتظار سيرفر التعليقات ولم يرد. أعد المحاولة.") from None
        except (OSError, http.client.HTTPException):
            raise RuntimeError("انقطع الاتصال بسيرفر التعليقات قبل أن يرد. أعد المحاولة.") from None
    finally:
        connection.close()
    if status == 404:
        raise RuntimeError("رابط السيرفر قديم أو ناقص. خذ الرابط الحالي" + OWNER_LINK + ".")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise RuntimeError("رد غير مفهوم من سيرفر التعليقات." if status == 200 else f"ردّ سيرفر التعليقات برمز {status}. أعد المحاولة.")
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "").strip() or f"ردّ سيرفر التعليقات برمز {status}. أعد المحاولة.")
    return data


def ask_server(link: str, action: str, body: dict | None = None, seconds: float = 30) -> dict:
    """server_exchange, tried again when the connection did not reach the server."""
    for attempt in range(len(SERVER_RETRY_WAITS) + 1):
        try:
            return server_exchange(link, action, body, seconds)
        except ServerNotReached:
            if attempt >= len(SERVER_RETRY_WAITS):
                raise
            time.sleep(SERVER_RETRY_WAITS[attempt])
    raise ServerNotReached("تعذّر الوصول إلى سيرفر التعليقات.")


def server_comment(link: str, tweet_id: str, voice: str, account: str) -> str:
    """One reply to a tweet, written on the comments server: it is told the tweet's number, the voice, this
    account's manner, and this copy's last replies (the new one must not look like them)."""
    if not re.fullmatch(r"\d{1,25}", tweet_id or ""):
        raise RuntimeError("لم يُعرف رقم التغريدة، فلم يُطلب تعليقها من السيرفر.")
    with replies_memory_lock:
        before = list(replies_by_account.get(account, []))[-6:] if account else []
        before += [text for text in replies_by_tweet.get(tweet_id, []) if text not in before][-4:]
    body = {"id": tweet_id, "voice": {"male": 0, "female": 1, "neutral": 2}.get(voice, 0),
            "manner": reply_manner_number(account), "before": before[-8:]}
    text = polish_comment(clean_comment(str(ask_server(link, "comment", body, SERVER_COMMENT_SECONDS).get("comment") or "")))
    if not text:
        raise RuntimeError("لم يصل تعليق من السيرفر. أعد المحاولة.")
    if len(text) > REPLY_MAX_CHARS:
        text = fit_comment(text)
    record_comment_generated()
    remember_reply(account, tweet_id, text)
    return text


def claude_answer_text(answer: dict) -> str:
    """The text of a Messages API answer: its text blocks (it may begin with thinking blocks)."""
    parts = [str(block.get("text") or "") for block in answer.get("content") or []
             if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(parts)


def claude_comment(api_key: str, model: str, image: bytes | None, tweet_text: str, voice: str = DEFAULT_REPLY_VOICE,
                   account: str = "", tweet_id: str = "") -> str:
    """One reply to a tweet by Claude: the same prompt as Gemini's and Grok's, the tweet's picture with it, at
    medium effort. A model that takes no effort setting is asked again without it; a busy Claude is asked again
    a few times."""
    prompt = reply_prompt(voice, account, tweet_id)
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
    best = ""
    saw_busy = False
    for _ in range(2):               # a reply that comes back cut off is asked for once more
        payload = None
        for try_index in range(len(busy_waits) + 1):
            # (Its thinking counts toward max_tokens: room for it, and for the short reply after it.)
            body: dict = {"model": model, "max_tokens": 2048, "messages": [{"role": "user", "content": content}]}
            if effort:
                body["output_config"] = {"effort": CLAUDE_EFFORT}
            request = urllib.request.Request(CLAUDE_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                             headers={"Content-Type": "application/json", "x-api-key": api_key,
                                                      "anthropic-version": CLAUDE_API_VERSION})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
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
                    effort = False       # this model takes no effort setting: asked again without it,
                    claude_effort_refused.add(model)    # and not sent it again
                    continue
                if exc.code in (401, 403):
                    raise RuntimeError("مفتاح Claude غير صالح أو بلا صلاحية. أخبر صاحب البرنامج.") from None
                if code == "enforced_spend_limit_reached" or re.search(r"usage limits|credit balance|billing", detail, re.I):
                    raise RuntimeError("بلغ حساب Claude حد الصرف أو نفد رصيده. أخبر صاحب البرنامج.") from None
                if exc.code in (429, 500, 502, 503, 504, 529):
                    saw_busy = True
                    if try_index < len(busy_waits):
                        time.sleep(busy_waits[try_index])
                        continue
                    break
                raise RuntimeError(("رفض Claude الطلب: " + detail) if detail else f"تعذر الاتصال بـ Claude (رمز {exc.code}).") from None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if try_index < 2:
                    time.sleep(busy_waits[try_index])
                    continue
                raise RuntimeError("تعذر الاتصال بـ Claude") from exc
        if not isinstance(payload, dict):
            break
        if payload.get("stop_reason") == "refusal":
            raise RuntimeError("امتنع Claude عن التعليق على هذه التغريدة.")
        text = clean_comment(claude_answer_text(payload))
        if comment_is_complete(text):
            record_comment_generated()
            remember_reply(account, tweet_id, text)
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        record_comment_generated()
        remember_reply(account, tweet_id, best)
        return best
    if saw_busy:
        raise RuntimeError("Claude مشغول مؤقتًا بعد عدة محاولات. أعد التنفيذ بعد قليل.")
    return ""


# Arabic diacritics (تشكيل): tanween, fatha, damma, kasra, shadda, sukun, and the other marks over or under letters.
ARABIC_DIACRITICS = re.compile(r"[\u064B-\u0652\u0656-\u065F\u0670]")
# Words a reply cannot end on, when one that is too long is shortened.
HANGING_WORDS = {"و", "او", "أو", "بس", "لكن", "لان", "لأن", "عشان", "اللي", "الي", "في", "من", "على", "عن", "مع", "الى",
                 "إلى", "يعني", "ثم", "حتى", "اذا", "إذا", "لو", "ما", "لا", "كان", "and", "or", "but", "the", "to", "of",
                 "in", "a", "an", "with", "for", "that", "ve", "ama", "ile", "için", "bir", "de", "da"}


def polish_comment(text: str) -> str:
    """The reply as it is posted: no Arabic diacritics and no commas (a number's thousands keep their digits)."""
    text = ARABIC_DIACRITICS.sub("", text or "")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    text = re.sub(r"\s*[،,؛;]\s*", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fit_comment(text: str, limit: int = REPLY_MAX_CHARS) -> str:
    """A reply cut to at most limit characters, at a word, and not on a word that leaves it hanging."""
    words = text.split()
    while words and len(" ".join(words)) > limit:
        words.pop()
    while words and words[-1].lower() in HANGING_WORDS:
        words.pop()
    return " ".join(words)


def write_comment(api_key: str, model: str, image: bytes | None, tweet_text: str, voice: str = DEFAULT_REPLY_VOICE,
                  account: str = "", tweet_id: str = "") -> str:
    """One reply, by whoever writes this copy's comments: Grok or Claude when the owner chose one of them in
    «توزيع البرنامج» (for users' copies and the owner's alike), else Gemini (with the task's key and model, or
    this copy's saved ones). It is posted with no diacritics and no commas, in at most REPLY_MAX_CHARS
    characters: one that comes back longer is asked for once more, then shortened at a word."""
    if not is_owner_copy() and not wait_for_owner_keys():
        raise RuntimeError(no_keys_message())
    engine, key, engine_model = comments_writer()
    if engine == "server":
        return server_comment(key, tweet_id, voice, account)       # (whole, plain and within the limit already)
    if engine == "none":
        raise RuntimeError(NO_SERVER_LINK)
    if engine == "gemini" and not api_key:
        with gemini_lock:
            api_key = str(saved_gemini.get("api_key") or "")
            model = model or normalize_gemini_model(str(saved_gemini.get("model") or ""))

    def ask() -> str:
        if engine == "grok":
            return grok_comment(key, engine_model, image, tweet_text, voice, account, tweet_id)
        if engine == "claude":
            return claude_comment(key, engine_model, image, tweet_text, voice, account, tweet_id)
        return gemini_comment(api_key, model, image, tweet_text, voice, account, tweet_id)

    first = polish_comment(ask())
    if len(first) <= REPLY_MAX_CHARS:
        return first
    second = polish_comment(ask())               # too long: asked for once more
    if second and len(second) <= REPLY_MAX_CHARS:
        return second
    return fit_comment(min((text for text in (first, second) if text), key=len))


def screenshot_article(card) -> bytes | None:
    if IN_APP:
        return None              # (the comments server reads the tweet itself)
    try:
        if not IN_APP:
            try:
                card.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
        return card.screenshot(type="jpeg", quality=70, timeout=12000)
    except Exception:
        return None


def reply_block_reason(page) -> str | None:
    try:
        text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 5000)")
    except Exception:
        return None
    if re.search(r"can.?t reply|cannot reply|aren.?t allowed to reply|replies are limited|who can reply|Reply limited", text or "", re.I):
        return "الرد غير مسموح على هذه التغريدة"
    if re.search(r"daily limit|rate limit|Try again later", text or "", re.I):
        return "الحساب وصل حد الردود مؤقتًا"
    if re.search(r"لا يمكنك الرد|غير مسموح بالرد|الردود محدودة", text or ""):
        return "الرد غير مسموح على هذه التغريدة"
    return None


def reply_button_ready(page) -> bool:
    try:
        return bool(page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]')];
              return nodes.some((button) => button.getAttribute("aria-disabled") !== "true" && !button.disabled);
            }"""
        ))
    except Exception:
        return False


def composer_visible(page) -> bool:
    try:
        box = page.locator('[data-testid="tweetTextarea_0"]')
        return box.count() > 0 and box.first.is_visible()
    except Exception:
        return False


def open_reply_composer(page, path: str) -> None:
    """Wait for the tweet to settle, then open the reply box. On the phone, call the reply
    button's own handler. A coordinate press was landing on the account name."""
    match = re.search(r"status/(\d{6,25})", path or "")
    status_id = match.group(1) if match else ""
    if IN_APP:
        for _ in range(3):
            if composer_visible(page):
                trace_click("reply مربع الكتابة ظاهر")
                return
            try:
                pressed = page.evaluate(PRESS_PAGE_JS, {"statusId": status_id, "testid": "reply"})
            except Exception as exc:
                pressed = {"how": "error", "error": str(exc).splitlines()[0][:160]}
            trace_click(f"reply فتح {pressed}")
            try:
                page.locator('[data-testid="tweetTextarea_0"]').last.wait_for(state="visible", timeout=8000)
                trace_click("reply ظهر مربع الكتابة")
                return
            except PlaywrightTimeoutError:
                page.wait_for_timeout(500)
        raise PlaywrightTimeoutError("tweetTextarea_0 not visible after retries")
    for attempt in range(3):
        page.wait_for_timeout(800 if attempt == 0 else 1400)
        if composer_visible(page):
            real_click(page.locator('[data-testid="tweetTextarea_0"]').first)
            page.wait_for_timeout(400)
            return
        card = article_locator(page, path)
        try:
            if card.count() and not IN_APP:
                card.first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(500)
        except Exception:
            pass
        button = card.locator('[data-testid="reply"]') if card.count() else page.locator('[data-testid="reply"]')
        if button.count() == 0:
            button = page.locator('[data-testid="reply"]')
        if button.count() == 0:
            continue
        try:
            if not IN_APP:
                button.first.scroll_into_view_if_needed(timeout=2000)
                page.wait_for_timeout(400)
            real_click(button.first)
        except Exception:
            continue
        try:
            page.locator('[data-testid="tweetTextarea_0"]').last.wait_for(state="visible", timeout=8000)
            page.wait_for_timeout(500)
            return
        except PlaywrightTimeoutError:
            continue
    raise PlaywrightTimeoutError("tweetTextarea_0 not visible after retries")


def insert_reply_text(page, text: str) -> None:
    if IN_APP:
        page.evaluate(
            """(value) => {
              const box = document.querySelector('[role="dialog"] [data-testid="tweetTextarea_0"]')
                || document.querySelector('[data-testid="tweetTextarea_0"]');
              if (!box) return false;
              const editable = box.querySelector('[contenteditable="true"]') || box;
              editable.focus();
              document.execCommand("selectAll", false);
              document.execCommand("delete", false);
              document.execCommand("insertText", false, value);
              editable.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
              return true;
            }""",
            text,
        )
        page.wait_for_timeout(400)
        if reply_button_ready(page):
            trace_click("reply كُتب النص")
            return
        page.keyboard.insert_text(text)
        page.wait_for_timeout(400)
        if not reply_button_ready(page):
            raise PlaywrightTimeoutError("reply button stayed disabled")
        trace_click("reply كُتب النص من لوحة المفاتيح")
        return
    box = page.locator('[role="dialog"] [data-testid="tweetTextarea_0"]')
    if box.count() == 0:
        box = page.locator('[data-testid="tweetTextarea_0"]')
    box.last.wait_for(state="visible", timeout=10000)
    real_click(box.last)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.insert_text(text)
    page.wait_for_timeout(350)
    if reply_button_ready(page):
        return
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(text, delay=8)
    page.wait_for_timeout(250)
    if reply_button_ready(page):
        return
    page.evaluate(
        """(value) => {
          const box = document.querySelector('[role="dialog"] [data-testid="tweetTextarea_0"]')
            || document.querySelector('[data-testid="tweetTextarea_0"]');
          if (!box) return;
          const editable = box.querySelector('[contenteditable="true"]') || box;
          editable.focus();
          document.execCommand("selectAll", false);
          document.execCommand("delete", false);
          document.execCommand("insertText", false, value);
          editable.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
        }""",
        text,
    )
    page.wait_for_function(
        """() => {
          const nodes = [...document.querySelectorAll('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]')];
          return nodes.some((button) => button.getAttribute("aria-disabled") !== "true" && !button.disabled);
        }""",
        timeout=8000,
    )


PRESS_SEND_JS = r"""
() => {
  const nodes = [...document.querySelectorAll('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]')];
  const button = [...nodes].reverse().find((node) => node.getAttribute("aria-disabled") !== "true" && !node.disabled);
  if (!button) return {how: "missing"};
  const before = (button.innerText || button.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
  const event = {
    preventDefault() {}, stopPropagation() {}, persist() {},
    nativeEvent: {isTrusted: true, button: 0},
    isTrusted: true, button: 0, type: "click",
    target: button, currentTarget: button
  };
  const keys = Object.keys(button);
  for (const key of keys) {
    if (!key.startsWith("__reactProps$")) continue;
    const props = button[key];
    if (props && typeof props.onClick === "function") {
      props.onClick(event);
      return {how: "react", before};
    }
  }
  for (const child of button.querySelectorAll("*")) {
    for (const key of Object.keys(child)) {
      if (!key.startsWith("__reactProps$")) continue;
      const props = child[key];
      if (props && typeof props.onClick === "function") {
        props.onClick(event);
        return {how: "react", before};
      }
    }
  }
  return {how: "no-handler", before};
}
"""


def click_reply_send(page) -> None:
    enabled = page.locator(
        '[data-testid="tweetButtonInline"]:not([aria-disabled="true"]), '
        '[data-testid="tweetButton"]:not([aria-disabled="true"])'
    )
    if enabled.count() == 0:
        raise PlaywrightTimeoutError("reply button stayed disabled")
    if IN_APP:
        sent = page.evaluate(PRESS_SEND_JS)
        trace_click(f"reply نشر {sent}")
        if not isinstance(sent, dict) or sent.get("how") not in ("react", "fiber"):
            raise PlaywrightTimeoutError("reply button stayed disabled")
        return
    real_click(enabled.last)


def publish_reply(page, text: str, path: str) -> tuple[bool, str]:
    blocked = reply_block_reason(page)
    if blocked:
        return False, blocked
    open_reply_composer(page, path)
    page.wait_for_timeout(500)
    blocked = reply_block_reason(page)
    if blocked:
        return False, blocked
    insert_reply_text(page, text)
    page.wait_for_timeout(500)
    click_reply_send(page)
    snippet = text[:24]
    try:
        page.wait_for_function(
            """(snippet) => [...document.querySelectorAll('article[data-testid="tweet"]')]
              .some((article) => (article.innerText || "").includes(snippet))""",
            arg=snippet,
            timeout=12000,
        )
        return True, ""
    except PlaywrightTimeoutError:
        blocked = reply_block_reason(page)
        return False, blocked or "أُرسل الطلب لكن الرد لم يظهر في الصفحة"


def comment_failure_text(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, PlaywrightTimeoutError):
        if "tweetTextarea" in message:
            return "لم يظهر مربع كتابة الرد"
        if "reply button" in message or "aria-disabled" in message or "disabled" in message:
            return "زر النشر بقي معطّلًا"
        if "article" in message:
            return "لم تُفتح التغريدة في الوقت المناسب"
        return "انتهت المهلة أثناء تجهيز الرد"
    line = message.splitlines()[0][:110] if message else type(exc).__name__
    return line or "خطأ غير متوقع"


RESOLVE_PARENT_JS = r"""
(replyPath) => {
  const statusPath = (href) => {
    const match = (href || "").match(/^(?:https?:\/\/(?:www\.)?(?:x|twitter)\.com)?(\/[A-Za-z0-9_]+\/status\/\d+)/);
    return match ? match[1] : "";
  };
  const articles = [...document.querySelectorAll('article[data-testid="tweet"]')];
  const paths = articles.map((article) => {
    const time = article.querySelector("time");
    const link = time && time.closest('a[href*="/status/"]');
    return statusPath(link ? link.getAttribute("href") : "");
  });
  const index = paths.findIndex((path) => path && (replyPath.endsWith(path) || path.endsWith(replyPath)));
  if (index > 0) return paths[index - 1];
  return "";
}
"""


def article_text(card) -> str:
    """The tweet's own text as its opened page shows it (a long post whole, where the collected text stops
    at 280 letters); "" when it has none (a picture alone)."""
    try:
        found = card.locator('[data-testid="tweetText"]')
        if not found.count():
            return ""
        text = found.first.inner_text(timeout=2000)
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text or "").strip()


def resolve_comment_target(page, path: str, needs_parent: bool) -> tuple[str, object]:
    if needs_parent:
        try:
            parent = page.evaluate(RESOLVE_PARENT_JS, path) or ""
        except Exception:
            parent = ""
        parent_path = normalize_status_path(str(parent))
        if parent_path and parent_path != path:
            matches = article_locator(page, parent_path)
            if matches.count() > 0:
                return parent_path, matches.first
    matches = article_locator(page, path)
    if matches.count() > 0:
        return path, matches.first
    return path, page.locator('article[data-testid="tweet"]').first


def execute_comment_actions(page, items: list[dict], actions: list[str], api_key: str, model: str, on_progress=None, should_stop=None, on_snapshot=None, account: str = "", voice: str = DEFAULT_REPLY_VOICE) -> list[dict]:
    if not IN_APP:
        allow_media(page)        # (in the app the comments server reads the tweet itself: no picture is downloaded)
    results: list[dict] = LoggedResults()
    plain = [action for action in actions if action != "comment"]
    total = len(items)
    failures = FailedTweets()
    # Where each tweet's result starts in results: going back after a pause drops them from there on.
    starts: dict[int, int] = {}
    if on_progress:
        on_progress(0, total, "act")
    index = -1
    while index + 1 < total:
        index += 1
        item = items[index]
        if should_stop and should_stop():
            break
        starts[index] = len(results)
        result = {"url": item.get("url", ""), "text": item.get("text", ""), "actions": {}, "comment": ""}
        path = normalize_status_path(str(item.get("url") or ""))
        if not path:
            result["actions"] = {key: "تعذر العثور على المنشور" for key in actions}
            results.append(result)
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        status_id = tweet_status_id(str(item.get("url") or ""))
        if already_done_recently(account, status_id, actions):
            # Everything asked was done on this tweet by this account in the last 8 hours: skip without opening it.
            result["actions"] = {key: "سبق التفاعل خلال ٨ ساعات — تم التخطي" for key in actions}
            results.append(result)
            if on_snapshot:
                on_snapshot(list(results))
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        # This account already replied to this tweet in the last 8 hours: never reply to it again.
        replied_before = "comment" in actions and already_done_recently(account, status_id, ["comment"])
        try:
            opened, reason = open_task_tweet(page, str(item.get("url") or ""), None)
        except Exception as exc:
            if browser_closed_error(exc):
                if on_snapshot and results:
                    on_snapshot(list(results))
                raise BrowserClosed() from exc
            raise
        if opened or reason == "gone":
            failures.opened()
        else:
            back = failures.failed(index)
            new_page = None
            if back is not None:
                seconds = failures.next_pause()
                if seconds is None:
                    # A third time: the task stops here, with what it did (these two would not open).
                    result["actions"] = {key: "تعذر فتح التغريدة" for key in actions}
                    results.append(result)
                    if on_snapshot:
                        on_snapshot(list(results))
                    raise TweetsKeptFailing(results, index + 1)
                new_page = pause_after_failures(should_stop, on_progress, total, allow_media, seconds,
                                                "pause" if failures.pauses == 1 else "pause_again")
            if new_page is not None:
                # Back to the first of the two that would not open; what was recorded from it on goes.
                page = new_page
                del results[starts[back]:]
                if on_snapshot:
                    on_snapshot(list(results))
                if on_progress:
                    on_progress(back, total, "act")
                index = back - 1
                continue
        if not opened:
            note = "محذوفة — تم التخطي" if reason == "gone" else "تعذر فتح التغريدة"
            result["actions"] = {key: note for key in actions}
            results.append(result)
            if on_snapshot:
                on_snapshot(list(results))
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        if tweet_is_ignored("https://x.com" + opened):
            # The opened tweet's author is an ignored account: take no action and leave no trace.
            forget_collected("https://x.com" + opened)
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        path = opened
        result["url"] = "https://x.com" + path
        try:
            page.wait_for_timeout(400)
            target_path, card = resolve_comment_target(page, path, bool(item.get("needs_parent")))
            result["url"] = "https://x.com" + target_path
            if not IN_APP:
                try:
                    card.scroll_into_view_if_needed(timeout=4000)
                except Exception:
                    pass
            page.wait_for_timeout(700)
            image = screenshot_article(card)
            if image is None:
                try:
                    image = page.screenshot(type="jpeg", quality=60, timeout=8000)
                except Exception:
                    image = None
            if plain:
                result["actions"].update(apply_actions_on_path(page, target_path, plain))
            if replied_before or ("comment" in actions and already_done_recently(account, tweet_status_id(target_path), ["comment"])):
                result["actions"]["comment"] = "سبق الرد خلال ٨ ساعات — تم التخطي"
            elif "comment" in actions:
                reply_to = tweet_status_id(target_path)
                tweet_text = article_text(card) or str(item.get("text") or "")
                comment = write_comment(api_key, model, image, tweet_text, voice, account, reply_to)
                if len(comment.split()) < 4:
                    comment = write_comment(api_key, model, image, tweet_text, voice, account, reply_to)
                if len(comment.split()) < 4:
                    result["actions"]["comment"] = "لم يُرجع النموذج تعليقًا صالحًا"
                else:
                    result["comment"] = comment
                    page.wait_for_timeout(800)
                    posted, detail = publish_reply(page, comment, target_path)
                    result["actions"]["comment"] = "تم التنفيذ" if posted else (detail or "أُرسل الطلب لكن تعذر تأكيد الحالة")
        except RuntimeError as exc:
            result["actions"]["comment"] = "تعذر توليد التعليق: " + str(exc)[:120]
        except Exception as exc:
            if browser_closed_error(exc):
                if on_snapshot and results:
                    on_snapshot(list(results))
                raise BrowserClosed() from exc
            print(f"X reply failed on {path}: {type(exc).__name__}: {str(exc).splitlines()[0][:180]}", flush=True)
            if "comment" in actions and "comment" not in result["actions"]:
                result["actions"]["comment"] = "تعذر نشر التعليق: " + comment_failure_text(exc)
            for action in plain:
                result["actions"].setdefault(action, "تعذر التنفيذ")
        # Save what was done, for the tweet in the group and for the tweet actually replied to.
        for done_id in {status_id, tweet_status_id(result["url"])}:
            remember_done(account, done_id, result["actions"])
        count_successes(result["actions"])
        results.append(result)
        if on_snapshot:
            on_snapshot(list(results))
        if on_progress:
            on_progress(index + 1, total, "act")
        page.wait_for_timeout(1400)
    return results


# Do not download images, videos or audio in the automation browser.
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


def allow_media(page) -> None:
    """Let this browser load images and videos again (used for comments)."""
    state = getattr(page.context, "_media_state", None)
    if state is not None:
        state["allow"] = True


MAX_PARALLEL_JOBS = 4


PROFILES_DIR = DATA_DIR / "profiles"
# "شاشة جوال" on the page: that task's browser is phone-wide at every step, and while it opens the
# chat and collects from the group (where the normal page is 25000 tall) it has the height chosen
# there; elsewhere it keeps NORMAL_VIEWPORT_HEIGHT. Set per worker thread, like browser_restart.
PHONE_SCREEN = {"width": 412, "height": 10000}
task_screen = threading.local()
# Page height while opening the chat and collecting from the group, and for everything else.
COLLECT_VIEWPORT_HEIGHT = 25000
NORMAL_VIEWPORT_HEIGHT = 1800
# Width of the script's browser page (the height stays as set above). Narrower means less
# to draw; 600 keeps X's normal layout (below 500 X switches to its phone layout).
VIEWPORT_WIDTH = 600
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


# On Android, in Termux: the browser is Termux's own Chromium, and Playwright's driver runs on Termux's
# Node (the Node that comes with Playwright is built for desktop Linux and does not run on Android).
# The driver also works out where browsers would be downloaded as soon as it starts, and it does not
# know Android ("Unsupported platform: android"): it is given that folder, so it never asks.
TERMUX_PREFIX = os.getenv("PREFIX", "") if "/com.termux/" in os.getenv("PREFIX", "") else ""
if TERMUX_PREFIX:
    if not os.getenv("PLAYWRIGHT_NODEJS_PATH") and Path(TERMUX_PREFIX, "bin", "node").is_file():
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(Path(TERMUX_PREFIX, "bin", "node"))
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))


# A copy on its own (see on_its_own) opens the full Chromium in its new headless mode (the "chromium" channel),
# not Playwright's lighter headless browser; the owner's main copy and users' copies keep the light one. It needs
# Playwright 1.49 or newer and the browser installed with what it needs from the system (python -m playwright
# install --with-deps chromium). Where it cannot be opened, the light browser is used as before, with one message on
# how to install it, and it is not asked again.
OWN_COPY_CHANNEL = "chromium"
channel_unavailable: list[str] = []


def browser_launch_options() -> dict:
    """The browser's switches, on Termux where its Chromium is, and on a copy on its own the full Chromium
    (OWN_COPY_CHANNEL). GG_CHROMIUM_ARGS adds switches (e.g. "--single-process" if Android keeps stopping the
    browser's helper processes)."""
    options: dict = {}
    args = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"]
    if TERMUX_PREFIX:
        for candidate in (Path(TERMUX_PREFIX, "lib", "chromium", "chrome"), Path(TERMUX_PREFIX, "bin", "chromium-browser")):
            if candidate.is_file():
                options["executable_path"] = str(candidate)
                break
        args.append("--disable-gpu")
    elif OWN_COPY_CHANNEL and on_its_own() and not channel_unavailable:
        options["channel"] = OWN_COPY_CHANNEL
    options["args"] = args + os.getenv("GG_CHROMIUM_ARGS", "").split()
    return options


def channel_not_installed(exc: Exception) -> bool:
    """Whether the full Chromium could not be opened only because it is not ready here: not installed, missing
    something it needs from the system, or a Playwright too old for the channel."""
    text = str(exc).lower()
    return any(part in text for part in ("executable doesn't exist", "unsupported chromium channel", "is not found at",
                                         "distribution 'chromium'", "missing dependencies",
                                         "error while loading shared libraries"))


def open_x_profile(playwright, auth_token: str, profile_dir: Path, viewport: dict | None = None):
    """Open the account's permanent browser profile (created on first use).

    In the app the browser is the phone's own, as it is: no user agent, language or other mark of another
    browser is given to it, and its page is as wide as the phone's screen. Only the page's height is set here
    (the width in the size below is not used by the app's browser)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    options = browser_launch_options()
    settings = dict(
        headless=True,
        viewport=viewport or screen_size(NORMAL_VIEWPORT_HEIGHT),
    )
    if not IN_APP:
        settings.update(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
    try:
        context = playwright.chromium.launch_persistent_context(str(profile_dir), **options, **settings)
    except Exception as exc:
        if "channel" not in options or not channel_not_installed(exc):
            raise
        first = not channel_unavailable
        channel_unavailable.append((str(exc).strip().splitlines() or [type(exc).__name__])[0][:200])
        if first:
            print("⚠️ متصفح Chromium الكامل لا يعمل هنا (غير مثبّت، أو تنقصه مكتبات من النظام، أو Playwright أقدم من 1.49)، "
                  "فيُفتح المتصفح الخفيف كما كان. للتثبيت: python -m pip install -U playwright "
                  "ثم python -m playwright install --with-deps chromium", flush=True)
            print(f"   ({channel_unavailable[0]})", flush=True)
        options.pop("channel", None)
        context = playwright.chromium.launch_persistent_context(str(profile_dir), **options, **settings)
    if not IN_APP:
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    else:
        context.add_init_script(CHAT_PAGES_JS)
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


_login_jobs: queue.Queue = queue.Queue()
_login_state: dict = {}
_login_worker_started = False
_login_worker_guard = threading.Lock()
# The height of the page where the user signs in by hand, and of a task that runs in that same browser.
MANUAL_LOGIN_SCREEN = {"width": 412, "height": 820}


def _login_worker() -> None:
    while True:
        fn, box = _login_jobs.get()
        try:
            box["result"] = fn()
        except Exception as exc:
            box["error"] = exc
        finally:
            box["done"].set()


def _ensure_login_worker() -> None:
    global _login_worker_started
    with _login_worker_guard:
        if _login_worker_started:
            return
        threading.Thread(target=_login_worker, name="manual-login", daemon=True).start()
        _login_worker_started = True


def _on_login_thread(fn, timeout: float = 90):
    _ensure_login_worker()
    box: dict = {"done": threading.Event()}
    _login_jobs.put((fn, box))
    if not box["done"].wait(timeout):
        raise TimeoutError("manual login")
    if box.get("error") is not None:
        raise box["error"]
    return box.get("result")


def _show_manual_browser() -> None:
    if not IN_APP:
        return
    from java import jclass
    jclass("app.tg.groups.BrowserHost").prepareManualLogin()


def _login_page_open() -> bool:
    page = _login_state.get("page")
    if page is None:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def _open_login_browser() -> None:
    """One browser the user types into. Kept open so «تحقق» can read its session."""
    if not _login_page_open():
        playwright = sync_playwright().start()
        context, page = open_x_profile(playwright, "", PROFILES_DIR / "manual-login", viewport=dict(MANUAL_LOGIN_SCREEN))
        allow_media(page)
        _login_state.update(playwright=playwright, context=context, page=page)
    page = _login_state["page"]
    _show_manual_browser()
    page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60_000)
    _show_manual_browser()


def start_manual_login() -> None:
    _on_login_thread(_open_login_browser)


def _read_login_token() -> str:
    if not _login_page_open():
        return ""
    try:
        cookies = _login_state["page"].context.cookies(["https://x.com", "https://twitter.com"])
    except Exception:
        return ""
    for cookie in cookies:
        if cookie.get("name") == "auth_token" and cookie.get("value"):
            return str(cookie["value"])
    return ""


def manual_login_token() -> str:
    if not _login_worker_started:
        return ""
    try:
        token = str(_on_login_thread(_read_login_token, timeout=20) or "")
    except Exception:
        return ""
    if valid_auth_token(token):
        remember_manual_profile(token)
    return token


def _close_login_browser() -> None:
    """Close the login window without deleting its profile, so a task can open that same browser."""
    context = _login_state.pop("context", None)
    playwright = _login_state.pop("playwright", None)
    _login_state.pop("page", None)
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            pass


def release_manual_browser() -> None:
    if not _login_worker_started:
        return
    try:
        _on_login_thread(_close_login_browser, timeout=20)
    except Exception:
        pass


MANUAL_BROWSER = "manual-login"
MANUAL_PROFILES_PATH = DATA_DIR / "manual_profiles.json"
_manual_profiles_lock = threading.Lock()


def _load_manual_profiles() -> dict:
    try:
        data = json.loads(MANUAL_PROFILES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def remember_manual_profile(auth_token: str) -> None:
    """This token belongs to the browser the user signed in to by hand."""
    key = profile_key(auth_token)
    with _manual_profiles_lock:
        saved = {name: folder for name, folder in _load_manual_profiles().items() if folder != MANUAL_BROWSER}
        saved[key] = MANUAL_BROWSER
        MANUAL_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANUAL_PROFILES_PATH.write_text(json.dumps(saved), encoding="utf-8")


def manual_profile_folder(auth_token: str) -> str:
    key = profile_key(auth_token)
    with _manual_profiles_lock:
        saved = _load_manual_profiles()
    return str(saved.get(key) or key)


def forget_manual_profile(auth_token: str) -> None:
    key = profile_key(auth_token)
    with _manual_profiles_lock:
        saved = _load_manual_profiles()
        if key not in saved:
            return
        del saved[key]
        MANUAL_PROFILES_PATH.write_text(json.dumps(saved), encoding="utf-8")


# The running task's "close and reopen the browser" function (one per worker thread).
browser_restart = threading.local()

# Live view of a task's browser ("مراقبة" on its card). Pictures are taken in the task's own
# thread during its waits, and only while someone is watching.
LIVE_VIEW_EVERY = 0.25
live_task = threading.local()
live_views: dict[str, dict] = {}
live_views_lock = threading.Lock()
_page_wait_for_timeout = Page.wait_for_timeout


def capture_live_view(page, job_id: str) -> bool:
    """Take the task's next picture when one is due. Returns whether someone is watching."""
    now = time.monotonic()
    with live_views_lock:
        view = live_views.get(job_id)
        if not view or view["wanted_until"] < now:
            return False
        if now - view["taken_at"] < LIVE_VIEW_EVERY:
            return True
        view["taken_at"] = now
    try:
        size = page.viewport_size or {"width": VIEWPORT_WIDTH, "height": NORMAL_VIEWPORT_HEIGHT}
        image = page.screenshot(type="jpeg", quality=40, timeout=3000,
                                clip={"x": 0, "y": 0, "width": size["width"], "height": min(size["height"], 1000)})
    except Exception:
        return True
    with live_views_lock:
        # An unchanged page gives the same picture: keep the old one so nothing is sent again.
        if image != view["image"]:
            view["image"] = image
            view["seq"] += 1
    return True


def wait_and_watch(page, timeout: float) -> None:
    """page.wait_for_timeout for every page: in a task's thread it also feeds that task's live view."""
    job_id = getattr(live_task, "job_id", "")
    if not job_id:
        return _page_wait_for_timeout(page, timeout)
    end = time.monotonic() + max(0.0, timeout) / 1000
    while True:
        watched = capture_live_view(page, job_id)
        left = end - time.monotonic()
        if left <= 0:
            return
        # Watched: wake up for every picture. Otherwise once a second, to notice a new viewer.
        _page_wait_for_timeout(page, min(left, LIVE_VIEW_EVERY if watched else 1.0) * 1000)


Page.wait_for_timeout = wait_and_watch


def restart_browser(while_closed=None):
    fn = getattr(browser_restart, "fn", None)
    return fn(while_closed) if fn else None


# Highest number of profiles (parallel tasks) one account may use.
MAX_PROFILES_PER_ACCOUNT = 5


def acquire_profile_slot(key: str, slots: int) -> str:
    """Take a free profile of the account and return its folder name.

    Profile 1 is the account's original folder (data/profiles/<key>); profiles 2, 3, ...
    are data/profiles/<key>-2, <key>-3, ... and are created on first use. When all of
    them are busy, wait until one is free.
    """
    slots = max(1, min(MAX_PROFILES_PER_ACCOUNT, int(slots or 1)))
    if slots == 1:
        profile_lock(key).acquire()
        return key
    names = [key] + [f"{key}-{number}" for number in range(2, slots + 1)]
    while True:
        for name in names:
            if profile_lock(name).acquire(blocking=False):
                return name
        time.sleep(1)


def run_on_fresh_browser(auth_token: str, work, slots: int = 1):
    """Open one of the account's permanent browser profiles for one task, then close the browser.

    By default each account has one profile folder under data/profiles, so two tasks
    on the same account take turns. With slots > 1 the account gets that many profile
    folders and that many of its tasks run at the same time.
    """
    folder = manual_profile_folder(auth_token)
    if folder == MANUAL_BROWSER:
        slots = 1
        release_manual_browser()
    key = folder
    slot_name = acquire_profile_slot(key, slots)
    same_browser = slot_name == MANUAL_BROWSER or slot_name.startswith(MANUAL_BROWSER + "-")
    try:
        playwright = sync_playwright().start()
        state = {"context": None}

        def open_saved(target):
            if same_browser:
                trace_click("التنفيذ على المتصفح الذي سُجّل الدخول إليه يدوياً")
                return open_x_profile(target, auth_token, PROFILES_DIR / slot_name, viewport=dict(MANUAL_LOGIN_SCREEN))
            return open_x_profile(target, auth_token, PROFILES_DIR / slot_name)

        def restart(while_closed=None):
            """Close the profile's browser, reopen it on Google, then go to X. Returns the new page.
            while_closed runs with the browser closed; when it returns False the browser stays
            closed and None is returned."""
            try:
                if state["context"] is not None:
                    state["context"].close()
            except Exception:
                pass
            state["context"] = None
            if while_closed is not None and while_closed() is False:
                return None
            state["context"], new_page = open_saved(playwright)
            try:
                new_page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=60_000)
                new_page.wait_for_timeout(2000)
            except Exception:
                pass
            try:
                goto_x(new_page, "https://x.com/home")
                session_is_logged_in(new_page)
            except Exception:
                pass
            return new_page

        browser_restart.fn = restart
        try:
            state["context"], page = open_saved(playwright)
            return work(page)
        finally:
            browser_restart.fn = None
            try:
                if state["context"] is not None:
                    state["context"].close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass
    finally:
        profile_lock(slot_name).release()


def screen_size(height: int) -> dict:
    """The page size for this thread's task. With "شاشة جوال" it is phone-wide, and the collecting
    height is the one chosen on the page."""
    phone_height = getattr(task_screen, "phone_height", 0)
    if phone_height:
        return {"width": PHONE_SCREEN["width"], "height": int(phone_height) if height == COLLECT_VIEWPORT_HEIGHT else int(height)}
    return {"width": VIEWPORT_WIDTH, "height": int(height)}


def set_page_height(page, height: int) -> None:
    try:
        page.set_viewport_size(screen_size(height))
        page.wait_for_timeout(500)
    except Exception:
        pass


def delete_profile(auth_token: str) -> None:
    key = profile_key(auth_token)
    folder = manual_profile_folder(auth_token)
    forget_manual_profile(auth_token)
    names = {key, folder}
    lock = profile_lock(key)
    if not lock.acquire(timeout=5):
        return
    try:
        for name in names:
            shutil.rmtree(PROFILES_DIR / name, ignore_errors=True)
            for extra in PROFILES_DIR.glob(f"{name}-*"):
                shutil.rmtree(extra, ignore_errors=True)
    finally:
        lock.release()



def run_on_browser(auth_token: str, work):
    """Every action gets its own browser, closed as soon as the action ends."""
    return run_on_fresh_browser(auth_token, work)


def goto_x(page, url: str) -> None:
    """page.goto for X pages. X sometimes answers with a file instead of the page (Playwright:
    "Download is starting"): ask again, and after two such answers ask past the browser's cache
    and X's service worker for that one load (nothing is deleted)."""
    for attempt in range(4):
        cdp = None
        try:
            if attempt >= 2:
                cdp = page.context.new_cdp_session(page)
                cdp.send("Network.enable")
                cdp.send("Network.setBypassServiceWorker", {"bypass": True})
                cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return
        except Exception as exc:
            if "Download is starting" not in str(exc):
                raise
            if attempt == 3:
                raise RuntimeError("أرسل X ملفًا بدل الصفحة أكثر من مرة. أعد المحاولة بعد قليل.") from exc
        finally:
            if cdp is not None:
                try:
                    cdp.send("Network.setBypassServiceWorker", {"bypass": False})
                    cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
                    cdp.detach()
                except Exception:
                    pass
        page.wait_for_timeout(1500)


def ensure_logged_in(page) -> bool:
    probe = page.locator(
        'button[aria-label="Account menu"], '
        '[data-testid="SideNav_AccountSwitcher_Button"], '
        'a[data-testid="AppTabBar_Profile_Link"], '
        # X's phone layout (narrow screens) has no side menu: its avatar button and the Home tab.
        '[data-testid="DashButton_ProfileIcon_Link"], '
        'a[data-testid="AppTabBar_Home_Link"]'
    )
    try:
        if "x.com" in (page.url or "") and probe.first.is_visible(timeout=1500):
            return True
    except Exception:
        pass
    goto_x(page, "https://x.com/home")
    return session_is_logged_in(page)


def session_is_logged_in(page) -> bool:
    try:
        page.locator(
            'button[aria-label="Account menu"], '
            '[data-testid="SideNav_AccountSwitcher_Button"], '
            'a[data-testid="AppTabBar_Profile_Link"], '
            # X's phone layout (narrow screens) has no side menu: its avatar button and the Home tab.
            '[data-testid="DashButton_ProfileIcon_Link"], '
            'a[data-testid="AppTabBar_Home_Link"]'
        ).first.wait_for(state="visible", timeout=20_000)
        return True
    except PlaywrightTimeoutError:
        return False


def read_session_username(page) -> str:
    """The logged-in handle. The phone has no side menu, so the desktop profile link is
    often missing; the avatar button and the session itself still carry the name."""
    try:
        handle = page.evaluate(
            """async () => {
              const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","unknown","undefined","null","none","user","profile","guest"]);
              const clean = (value) => {
                const name = String(value || "").trim().replace(/^@/, "");
                if (!/^[A-Za-z0-9_]{1,15}$/.test(name) || reserved.has(name.toLowerCase())) return "";
                return name;
              };
              const fromHref = (node) => {
                if (!node) return "";
                const match = (node.getAttribute("href") || "").match(/^\\/([A-Za-z0-9_]{1,15})\\/?$/);
                return match ? clean(match[1]) : "";
              };
              const fromAvatar = (node) => {
                if (!node) return "";
                const tagged = node.getAttribute && (node.getAttribute("data-testid") || "").startsWith("UserAvatar-Container-")
                  ? node
                  : node.querySelector('[data-testid^="UserAvatar-Container-"]');
                if (!tagged) return "";
                return clean((tagged.getAttribute("data-testid") || "").slice("UserAvatar-Container-".length));
              };
              const profile = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
              const dash = document.querySelector('[data-testid="DashButton_ProfileIcon_Link"]');
              const visible = fromHref(profile) || fromAvatar(profile) || fromHref(dash) || fromAvatar(dash);
              if (visible) return {name: visible, source: "page"};
              const nodes = document.querySelectorAll('[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="UserName"], [data-testid="DashButton_ProfileIcon_Link"]');
              for (const node of nodes) {
                const match = ((node.innerText || node.textContent || "")).match(/@([A-Za-z0-9_]{1,15})/);
                if (match && clean(match[1])) return {name: match[1], source: "text"};
              }
              const dig = (value, depth, seen) => {
                if (!value || depth > 8 || typeof value !== "object") return "";
                if (seen.has(value)) return "";
                seen.add(value);
                const direct = clean(value.screen_name || value.screenName || "");
                if (direct) return direct;
                for (const key of ["legacy", "core", "user", "viewer", "result", "data", "next", "memoizedState"]) {
                  if (value[key]) {
                    const found = dig(value[key], depth + 1, seen);
                    if (found) return found;
                  }
                }
                return "";
              };
              const root = dash || profile;
              if (root) {
                const fiberKey = Object.keys(root).find((key) => key.startsWith("__reactFiber$"));
                let fiber = fiberKey ? root[fiberKey] : null;
                for (let step = 0; fiber && step < 40; step++, fiber = fiber.return) {
                  const found = dig(fiber.memoizedProps, 0, new Set()) || dig(fiber.memoizedState, 0, new Set());
                  if (found) return {name: found, source: "account"};
                }
              }
              return {name: "", source: "empty"};
            }"""
        ) or {}
    except Exception as exc:
        handle = ""
        trace_click("قراءة الاسم فشلت: " + type(exc).__name__)
    else:
        if isinstance(handle, dict):
            trace_click(f"قراءة الاسم {handle}")
            handle = str(handle.get("name") or "")
        else:
            handle = str(handle or "")
    handle = handle.strip().lstrip("@")
    if handle.lower() in {"unknown", "undefined", "null", "none", "user"}:
        handle = ""
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return handle
    try:
        goto_x(page, "https://x.com/settings/screen_name")
        page.wait_for_timeout(2500)
        found = page.evaluate(
            """() => {
              const reserved = new Set(["home","explore","search","settings","messages","notifications","i","compose","unknown","undefined","null","none","user","profile","guest","screen_name","username"]);
              const clean = (value) => {
                const name = String(value || "").trim().replace(/^@/, "");
                if (!/^[A-Za-z0-9_]{1,15}$/.test(name) || reserved.has(name.toLowerCase())) return "";
                return name;
              };
              const preferred = document.querySelector('input[name="username"], input[name="screen_name"], input[autocomplete="username"]');
              const fromInput = clean(preferred && preferred.value);
              if (fromInput) return fromInput;
              for (const input of document.querySelectorAll("input")) {
                const name = clean(input.value);
                if (name) return name;
              }
              const match = ((document.body && document.body.innerText) || "").match(/@([A-Za-z0-9_]{1,15})/);
              return match ? clean(match[1]) : "";
            }"""
        ) or ""
        trace_click(f"قراءة الاسم من الإعدادات: {found or 'فارغ'}")
        found = str(found).strip().lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", found) and found.lower() not in {"unknown", "settings"}:
            return found
    except Exception as exc:
        trace_click("قراءة الاسم من الإعدادات فشلت: " + type(exc).__name__)
    return ""


def verify_x_login(auth_token: str) -> tuple[bool, str]:
    """Verify only that the X session is authenticated; do not open a profile."""
    def work(page):
        if not ensure_logged_in(page):
            return False, ""
        username = read_session_username(page)
        if not username:
            page.wait_for_timeout(1200)
            username = read_session_username(page)
        trace_click(f"التحقق اسم الحساب: {username or 'غير معروف'}")
        return True, username
    return run_on_browser(auth_token, work)


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
    goto_x(page, CHAT_URLS[0])
    typed = False
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        accept_cookies(page)
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
# Each group's conversation link (…/messages/<id> or …/i/chat/<id>), so a task can open the group by
# going straight to it instead of waiting for it to show up in the inbox list.
group_paths: dict[str, str] = {}


def load_group_ids() -> None:
    loaded = read_private_json(GROUP_IDS_PATH) or {}
    rows = loaded.get("groups")
    if isinstance(rows, dict):
        with group_ids_lock:
            group_ids.update({str(k): str(v) for k, v in rows.items() if k and v})
    saved_paths = loaded.get("paths")
    if isinstance(saved_paths, dict):
        with group_ids_lock:
            group_paths.update({str(k): str(v) for k, v in saved_paths.items() if k and v})


def save_group_ids_unlocked() -> None:
    try:
        write_private_json(GROUP_IDS_PATH, {"version": 2, "groups": dict(group_ids), "paths": dict(group_paths)})
    except Exception:
        pass


def remember_group_id(name: str, testid: str) -> None:
    """Remember each group's id (it does not change when the group is renamed)."""
    with group_ids_lock:
        if group_ids.get(name) == testid:
            return
        group_ids[name] = testid
        save_group_ids_unlocked()


def remember_group_path(name: str, path: str) -> None:
    """Remember a group's conversation link, so it can be opened directly next time."""
    cleaned = (path or "").strip()
    if not cleaned or "/status/" in cleaned:
        return
    with group_ids_lock:
        if group_paths.get(name) == cleaned:
            return
        group_paths[name] = cleaned
        save_group_ids_unlocked()


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
        paths = payload.get("paths") or []
        for index, name in enumerate(payload.get("groups") or []):
            clean = re.sub(r"\s+", " ", str(name)).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            found.append(clean)
            if index < len(ids) and ids[index]:
                remember_group_id(clean, str(ids[index]))
            if index < len(paths) and paths[index]:
                remember_group_path(clean, str(paths[index]))
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


# A group's conversation view, once its page has loaded.
GROUP_VIEW_SELECTOR = '[data-testid="dm-message-list"], [data-testid="dm-message-scroller"], [data-testid="dm-conversation-content"]'


def conversation_key(url: str) -> str:
    """The conversation id in a chat link (…/i/chat/<id> or …/messages/<id>): its digits when it has
    any (so "g123" and "123" are the same group), else the id itself. "" for any other page."""
    match = re.search(r"/(?:i/chat|messages)/([^/?#\s]+)", url or "")
    if not match:
        return ""
    raw = match.group(1)
    if raw.lower() in ("pin", "compose", "requests", "settings"):
        return ""
    return re.sub(r"\D", "", raw) or raw.lower()


def conversation_link(path: str) -> str:
    """The full address of a saved group link, or "" when it is not a chat conversation link."""
    cleaned = (path or "").strip()
    if not conversation_key(cleaned):
        return ""
    if cleaned.startswith("/"):
        return "https://x.com" + cleaned
    if re.match(r"https://(?:www\.)?(?:x|twitter)\.com/", cleaned):
        return cleaned
    return ""


def land_in_group(page, target: str, pin: str, wait_s: float) -> bool:
    """After a group's page was loaded, wait until that group shows: its conversation view, at its own
    link. Types the chat PIN if X asks for it; if X then shows the chat list instead of the group,
    goes to the group's link once more. Only the view is waited for, not the messages: after a full
    page load X can take a while to show them. True once the group shows."""
    want = conversation_key(target)
    plain_target = (target or "").split("?")[0].rstrip("/")
    typed = False
    again = False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        accept_cookies(page)
        url = page.url or ""
        at_group = bool(want and conversation_key(url) == want) or url.split("?")[0].rstrip("/") == plain_target
        if at_group and page.locator(GROUP_VIEW_SELECTOR).count():
            return True
        if "pin" in url:
            if not typed and pin_fields(page):
                type_pin_once(page, pin)
                typed = True
                error = pin_error_message(page)
                if error:
                    raise RuntimeError(error)
        elif typed and not at_group and not again and (target or "").startswith("http"):
            again = True
            try:
                goto_x(page, target)
            except Exception as exc:
                if browser_closed_error(exc):
                    raise
        page.wait_for_timeout(500)
    return False


def open_group_direct(page, path: str, pin: str, wait_s: float = 40.0) -> bool:
    """Open the group by going straight to its saved link (the chat is already unlocked), instead of
    waiting for it to show up in the chat list. True once the group shows; False to find it in the
    chat list instead."""
    target = conversation_link(path)
    if not target:
        return False
    try:
        goto_x(page, target)
    except Exception as exc:
        if browser_closed_error(exc):
            raise
        return False
    opened = land_in_group(page, target, pin, wait_s)
    if os.environ.get("COLLECT_DEBUG"):
        print(f"group opened directly by its link {target}: {opened}", flush=True)
    return opened


def wait_group_messages(page, wait_s: float) -> bool:
    """Wait up to wait_s for the open group's messages to show. True when they did."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            state = page.evaluate(GROUP_CHAT_SCROLL_JS, "state") or {}
        except Exception as exc:
            if browser_closed_error(exc):
                raise
            state = {}
        if state.get("hasList") and state.get("messages"):
            page.wait_for_timeout(800)
            return True
        page.wait_for_timeout(400)
    return False


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
            # Remember the group's link, so the next task opens it straight away.
            if payload.get("path"):
                remember_group_path(wanted, str(payload.get("path")))
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


def peek_named_group(page, name: str, wait_s: float = 10.0) -> bool:
    """Open the group once (found the same way as open_named_group) and wait until its
    messages show, at most wait_s. True when the group was opened, whether or not the
    messages showed in time. Used before the real open, because X first opens a group at
    the place this account last read up to, not at the newest messages."""
    wanted = re.sub(r"\s+", " ", name).strip()
    with group_ids_lock:
        saved_id = group_ids.get(wanted, "")
    started = time.monotonic()
    clicked = False
    while time.monotonic() - started < 10:
        try:
            payload = page.evaluate(READ_GROUPS_JS, {"name": wanted, "testid": saved_id}) or {}
        except Exception:
            payload = {}
        if payload.get("clicked"):
            clicked = True
            if payload.get("path"):
                remember_group_path(wanted, str(payload.get("path")))
            break
        if time.monotonic() - started > 3:
            try:
                page.evaluate(SCROLL_INBOX_JS)
            except Exception:
                pass
        page.wait_for_timeout(700)
    if not clicked:
        return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            state = page.evaluate(GROUP_CHAT_SCROLL_JS, "state") or {}
        except Exception:
            state = {}
        if state.get("hasList") and state.get("messages"):
            page.wait_for_timeout(1000)
            return True
        page.wait_for_timeout(300)
    return True


def sway_group_while_waiting(page, seconds: int, should_stop=None) -> None:
    """During the wait inside the group, move the chat up and down. The last ten seconds
    stay at the bottom, where the newest messages are."""
    seconds = int(seconds)
    if seconds <= 0:
        return
    step = """(dir) => {
      const start = document.querySelector('[data-testid="dm-message-scroller"]')
        || document.querySelector('[data-testid="dm-message-list"]');
      const scrollerOf = (node) => {
        for (let el = node; el && el.nodeType === 1; el = el.parentElement) {
          if (/(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 10) return el;
        }
        const page = document.scrollingElement;
        if (page && page.scrollHeight > page.clientHeight + 10) return page;
        return node;
      };
      const scroller = start ? scrollerOf(start) : document.scrollingElement;
      if (!scroller) return false;
      if (dir === 0) scroller.scrollTop = scroller.scrollHeight;
      else {
        const client = scroller.clientHeight || 800;
        const base = dir > 0
          ? Math.max(560, Math.min(1200, Math.round(client * 0.75)))
          : Math.max(400, Math.min(800, Math.round(client * 0.5)));
        const jump = Math.max(base, Math.min(base * 2, Math.round(client * 0.9)));
        scroller.scrollTop = dir < 0
          ? Math.max(0, scroller.scrollTop - jump)
          : Math.min(scroller.scrollHeight, scroller.scrollTop + jump);
      }
      try { scroller.dispatchEvent(new Event("scroll", {bubbles: true})); } catch (error) {}
      return true;
    }"""
    end = time.monotonic() + seconds
    bottom_at = end - 10 if seconds > 10 else time.monotonic()
    direction = -1
    while True:
        if should_stop and should_stop():
            return
        now = time.monotonic()
        if now >= end:
            return
        if now >= bottom_at:
            try:
                page.evaluate(step, 0)
            except Exception:
                pass
            page.wait_for_timeout(int(max(0, end - time.monotonic()) * 1000))
            return
        try:
            page.evaluate(step, direction)
        except Exception:
            pass
        direction = 1 if direction < 0 else -1
        page.wait_for_timeout(800)


def leave_group(page, pin: str) -> None:
    """Go back from the open group to the chat list, like pressing back."""
    try:
        page.go_back(wait_until="domcontentloaded", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(800)
    still_inside = re.search(r"/(?:i/chat|messages)/[^/?#]+", page.url or "")
    if still_inside or not chat_inbox_open(page):
        # Back did not land on the chat list: open it directly (types the chat PIN if X asks).
        goto_x(page, CHAT_URLS[0])
        error = open_chat(page, pin)
        if error:
            raise RuntimeError(error)


def open_group_for_task(page, pin: str, group_name: str, group_wait_s: int, say) -> str:
    """Open the task's group once the chat is unlocked. With the group's saved link it goes straight
    to the group (no waiting for it in the chat list, no leaving and coming back); without one, or
    if the link does not open it, it finds the group in the chat list as before. say(text) reports
    each step. Returns the name the group has now."""
    with group_ids_lock:
        group_link = group_paths.get(re.sub(r"\s+", " ", group_name).strip(), "")
    if group_link:
        say(f"فتح القروب مباشرة: {group_name}")
        if open_group_direct(page, group_link, pin):
            wait_group_messages(page, 25)
            return group_name
        # The link did not open the group: back to the chat list, to find it there.
        error = open_chat(page, pin)
        if error:
            raise RuntimeError(error)
    show_groups_only(page)
    say(f"فتح القروب: {group_name}")
    return open_named_group(page, group_name)


def group_chat_state(page) -> dict:
    try:
        return page.evaluate(GROUP_CHAT_SCROLL_JS, "state") or {}
    except Exception:
        return {}


CHAT_SPOT_JS = r"""
() => {
""" + CHAT_FIND_JS + r"""
  const start = query('[data-testid="dm-message-scroller"]') || query('[data-testid="dm-message-list"]');
  if (!start) return null;
  // The chat's part of the screen: its scroller, else its list, else what really scrolls it.
  const area = [start, query('[data-testid="dm-message-list"]'), scrollerOf(start)].find((el) => el && shownArea(el) >= 1600);
  if (!area) return null;
  const rect = area.getBoundingClientRect();
  const left = Math.max(rect.left, 0);
  const top = Math.max(rect.top, 0);
  const width = Math.min(rect.right, innerWidth) - left;
  const height = Math.min(rect.bottom, innerHeight) - top;
  const inside = (node) => {
    for (let el = node; el; el = el.parentElement || (el.parentNode && el.parentNode.host) || null) {
      if (el === area) return true;
    }
    return false;
  };
  const spots = [[0.45, Math.min(0.42, 640 / height)], [0.5, 0.5], [0.5, 0.25], [0.5, 0.75], [0.25, 0.5], [0.75, 0.5]]
    .map(([fx, fy]) => ({x: Math.round(left + width * fx), y: Math.round(top + height * fy)}));
  for (const spot of spots) {
    let hit = document.elementFromPoint(spot.x, spot.y);
    while (hit && hit.shadowRoot) {
      const deeper = hit.shadowRoot.elementFromPoint(spot.x, spot.y);
      if (!deeper || deeper === hit) break;
      hit = deeper;
    }
    if (hit && inside(hit)) return spot;
  }
  return spots[0];
}
"""


def focus_group_scroller(page) -> None:
    """Put the mouse over the part of the chat that shows on the screen, on a spot nothing covers."""
    try:
        spot = page.evaluate(CHAT_SPOT_JS)
    except Exception:
        spot = None
    if spot:
        page.mouse.move(spot["x"], spot["y"])


CHAT_UNSTICK_JS = r"""
(pixels) => {
""" + CHAT_FIND_JS + r"""
  const start = query('[data-testid="dm-message-scroller"]') || query('[data-testid="dm-message-list"]');
  if (!start) return false;
  const scroller = scrollerOf(start);
  const room = scroller.scrollHeight - scroller.clientHeight;
  // Step down from the top; the mouse wheel then takes the chat back up to it.
  if (room > 10) scroller.scrollTop = Math.min(room, Math.max(120, pixels));
  if (start.scrollHeight - start.clientHeight > 10) return false;
  // The chat's own scroller cannot scroll (the loaded messages fit it): tell it it was scrolled up anyway.
  for (const target of new Set([start, scroller])) {
    try { target.dispatchEvent(new WheelEvent("wheel", {deltaY: -Math.max(120, pixels), bubbles: true, cancelable: true})); } catch (error) {}
    try { target.dispatchEvent(new Event("scroll", {bubbles: true})); } catch (error) {}
  }
  // Whether the loaded messages fit a chat made to scroll by itself: only a shorter screen lets it scroll.
  return /(auto|scroll|overlay)/.test(getComputedStyle(start).overflowY);
}
"""

# A screen taller than this is made this short for a moment when the chat's messages fit it.
SHORT_SCREEN_HEIGHT = 900


def unstick_group_chat(page, pixels: int) -> None:
    """For a climb that stopped at the oldest loaded messages while X loads no older ones: step
    down a little and wheel back up to the top, so X sees its chat being scrolled up. When the
    loaded messages fit the chat (on the tall phone screen nothing in it can scroll), this is done
    on a short screen, and the screen gets its height back after."""
    try:
        fits = bool(page.evaluate(CHAT_UNSTICK_JS, pixels))
    except Exception:
        return
    size = page.viewport_size or {}
    short = fits and int(size.get("height") or 0) > SHORT_SCREEN_HEIGHT
    try:
        if short:
            page.set_viewport_size({"width": int(size["width"]), "height": SHORT_SCREEN_HEIGHT})
            page.wait_for_timeout(600)
            page.evaluate(CHAT_UNSTICK_JS, pixels)
        page.wait_for_timeout(300)
        focus_group_scroller(page)
        page.mouse.wheel(0, -2 * pixels)
        page.wait_for_timeout(1500 if short else 300)
    except Exception as exc:
        if browser_closed_error(exc):
            raise
    finally:
        if short:
            try:
                page.set_viewport_size({"width": int(size["width"]), "height": int(size["height"])})
                page.wait_for_timeout(500)
            except Exception:
                pass


def tweet_url(raw_url: str, status_id: str) -> str:
    raw = (raw_url or "").strip()
    for match in re.finditer(rf"(?<![A-Za-z0-9_])/([A-Za-z0-9_]{{1,15}})/status/{status_id}(?!\d)", raw):
        owner = match.group(1)
        if owner.lower() in {"i", "web", "intent", "share"}:
            continue
        return f"https://x.com/{owner}/status/{status_id}"
    return f"https://x.com/i/web/status/{status_id}"


def extract_status_id(value: str) -> str:
    raw = (value or "").strip()
    match = re.search(r"status/(\d{6,25})", raw)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{15,25}", raw):
        return raw
    return ""


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
""" + CHAT_FIND_JS + r"""
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
  const scroller = scrollerOf(query('[data-testid="dm-message-scroller"]') || list);
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
""" + CHAT_FIND_JS + r"""
  const start = query('[data-testid="dm-message-scroller"]') || query('[data-testid="dm-message-list"]');
  if (!start) return {ok: false};
  const scroller = scrollerOf(start);
  const before = scroller.scrollTop;
  const atTop = before <= 48;
  if (!atTop) scroller.scrollTop = Math.max(0, before - Math.max(80, pixels));
  else scroller.scrollTop = 0;
  scroller.dispatchEvent(new Event("scroll", {bubbles: true}));
  return {ok: true, before: Math.round(before), after: Math.round(scroller.scrollTop), atTop, height: Math.round(scroller.scrollHeight)};
}
"""


# Inside the group, each scroll step is this many times the base step (never past what the
# chat shows at once).
GROUP_SCROLL_FACTOR = 2


# In the app, while a group's tweets are collected: a measure of where the time goes, written to the log. Every
# 10 s: how far the collecting got, and what the page asked the network for in that time (by kind: how many, how
# much, how long). Once: what the page holds for its chat (its databases, and the names of the calls its code knows
# for messages). And the shape of the chat's first answers (their fields and sizes, never their content). It is
# what is needed to ask the page itself for the chat's messages, without scrolling it.
CHAT_FACTS_JS = r"""
async () => {
  const out = {dbs: [], ops: [], wasm: [], scripts: 0};
  try { if (indexedDB.databases) out.dbs = (await indexedDB.databases()).map((d) => d.name + ":" + d.version).slice(0, 25); } catch (error) {}
  try {
    const ops = new Set();
    const re = /operationName:"([A-Za-z0-9_]*(?:Message|Conversation|Chat|Inbox|Thread|Group|Key)[A-Za-z0-9_]*)"/g;
    for (const chunk of (self.webpackChunk_twitter_responsive_web || [])) {
      for (const fn of Object.values((chunk && chunk[1]) || {})) {
        let source = "";
        try { source = Function.prototype.toString.call(fn); } catch (error) { continue; }
        if (source.indexOf("operationName") < 0) continue;
        let match;
        while ((match = re.exec(source)) && ops.size < 80) ops.add(match[1]);
      }
    }
    out.ops = [...ops].sort();
  } catch (error) {}
  try {
    const entries = performance.getEntriesByType("resource");
    out.wasm = entries.filter((e) => /\.wasm(\?|$)/.test(e.name)).map((e) => e.name.split("/").pop().split("?")[0]).slice(0, 8);
    out.scripts = entries.filter((e) => e.initiatorType === "script").length;
  } catch (error) {}
  return out;
}
"""
CHAT_TICK_JS = r"""
() => {
""" + CHAT_FIND_JS + r"""
  const list = query('[data-testid="dm-message-list"]') || query('[data-testid="dm-conversation-content"]') || query('[data-testid="dm-message-scroller"]');
  if (!list) return {list: false};
  const all = queryAll('[data-testid^="message-"]').filter((el) => list.contains(el) || el.getRootNode() !== document);
  let linked = 0, textLink = 0, cards = 0;
  for (const bubble of all) {
    if (bubble.querySelector('[href*="status/"]')) linked++;
    if (/(?:x|twitter)\.com\/[^\s]*status\/\d/.test(bubble.innerText || "")) textLink++;
    if (bubble.querySelector('[data-testid="tweet"], article, [data-testid*="card"]')) cards++;
  }
  let scroller = list;
  for (let el = list; el && el.nodeType === 1; el = el.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 10) { scroller = el; break; }
  }
  return {list: true, bubbles: all.length, linked, textLink, cards, loading: queryAll('[role="progressbar"]').length,
          top: Math.round(scroller.scrollTop || 0), height: Math.round(scroller.scrollHeight || 0)};
}
"""


# In the app: X's page loads a group's chat a few messages at a time (each of its GetConversationPageQuery asks
# brings a handful, and each takes half a second or more on a phone's network), and decrypts them itself. This,
# put in the page before X's own code, asks X for bigger pages instead: where the page's own ask names how many to
# bring (a number named like count or limit), it asks for CHAT_PAGE_SIZE, and X's page decrypts and shows them as
# ever. If X refuses a bigger page, its own ask is sent as it was, and no other ask is changed again. What it saw
# (the ask's fields, never their content) is read back for the log.
CHAT_PAGE_SIZE = 60
CHAT_PAGES_JS = r"""
(() => {
  if (window.__ggChatPages) return;
  const state = window.__ggChatPages = {seen: 0, changed: 0, off: false, failed: "", fields: "", how: ""};
  const WANT = __WANT__;
  const isPage = (url) => /\/GetConversationPageQuery(\?|$)/.test(String(url || ""));
  const COUNT = /^(count|limit|page_?size|max_?(events|messages|count|results)|num_?(events|messages)|size|first|last)$/i;
  const describe = (value, depth) => {
    if (depth > 3) return "…";
    if (Array.isArray(value)) return "[" + (value.length ? describe(value[0], depth + 1) : "") + "]";
    if (value && typeof value === "object") return "{" + Object.keys(value).slice(0, 16).map((k) => k + ":" + describe(value[k], depth + 1)).join(",") + "}";
    if (typeof value === "number") return String(value);
    if (typeof value === "string") return "str" + value.length;
    return typeof value;
  };
  const bigger = (vars) => {
    let touched = "";
    const walk = (obj, depth) => {
      if (!obj || typeof obj !== "object" || depth > 3) return;
      for (const key of Object.keys(obj)) {
        const value = obj[key];
        if (typeof value === "number" && COUNT.test(key) && value > 0 && value < WANT) {
          obj[key] = WANT;
          touched += key + ":" + value + "→" + WANT + " ";
        } else if (value && typeof value === "object") walk(value, depth + 1);
      }
    };
    walk(vars, 0);
    return touched.trim();
  };
  // The ask with bigger pages: its new address (and body), or null when there is nothing to change.
  const remake = (url, body) => {
    if (state.off) return null;
    try {
      const address = new URL(String(url), location.href);
      const raw = address.searchParams.get("variables");
      if (raw) {
        const vars = JSON.parse(raw);
        if (!state.fields) state.fields = describe(vars, 0);
        const touched = bigger(vars);
        if (!touched) return null;
        state.how = touched;
        address.searchParams.set("variables", JSON.stringify(vars));
        return {url: address.toString(), body};
      }
      if (typeof body === "string" && body.charAt(0) === "{") {
        const data = JSON.parse(body);
        const vars = data.variables || data;
        if (!state.fields) state.fields = describe(vars, 0);
        const touched = bigger(vars);
        if (!touched) return null;
        state.how = touched;
        return {url: String(url), body: JSON.stringify(data)};
      }
    } catch (error) {}
    return null;
  };
  const refused = (why) => { state.off = true; state.failed = String(why).slice(0, 160); };
  const realFetch = window.fetch;
  if (realFetch) {
    window.fetch = async function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || String(input || "");
      if (!isPage(url)) return realFetch.apply(this, arguments);
      state.seen++;
      const plainBody = init && typeof init.body === "string" ? init.body : null;
      const made = (typeof input === "string" || input instanceof URL || plainBody !== null) ? remake(url, plainBody) : null;
      if (!made) return realFetch.apply(this, arguments);
      try {
        const nextInit = Object.assign({}, init || {});
        if (made.body !== null && made.body !== undefined) nextInit.body = made.body;
        const answer = await realFetch.call(this, typeof input === "string" || input instanceof URL ? made.url : new Request(made.url, input), nextInit);
        if (answer.ok) {
          const data = await answer.clone().json().catch(() => null);
          if (data && !data.errors) { state.changed++; return answer; }
          refused("errors: " + ((data && data.errors && data.errors[0] && data.errors[0].message) || "?"));
        } else refused("HTTP " + answer.status);
      } catch (error) { refused(error); }
      return realFetch.apply(this, arguments);
    };
  }
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__ggPage = isPage(url) ? {method: String(method || "GET").toUpperCase(), url: String(url)} : null;
    if (this.__ggPage) {
      state.seen++;
      const made = this.__ggPage.method === "GET" ? remake(url, null) : null;
      if (made) {
        arguments[1] = made.url;
        this.__ggPage.changed = true;
      }
    }
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const page = this.__ggPage;
    if (page && page.method !== "GET") {
      const made = remake(page.url, typeof body === "string" ? body : null);
      if (made) { body = made.body; page.changed = true; }
    }
    if (page && page.changed) {
      this.addEventListener("loadend", () => {
        if (this.status === 200) state.changed++;
        else refused("HTTP " + this.status);
      });
    }
    return send.call(this, body);
  };
})();
""".replace("__WANT__", str(CHAT_PAGE_SIZE))


def json_shape(value, depth: int = 0):
    """A JSON answer's fields and sizes, without what is in them."""
    if depth > 5:
        return "…"
    if isinstance(value, dict):
        return {str(key)[:40]: json_shape(item, depth + 1) for key, item in list(value.items())[:14]}
    if isinstance(value, list):
        return [json_shape(value[0], depth + 1), f"×{len(value)}"] if value else []
    if isinstance(value, str):
        return f"str{len(value)}"
    return type(value).__name__


class CollectProbe:
    """The measure of a group's collecting in the app (see CHAT_FACTS_JS)."""

    CHAT_WORDS = re.compile(r"message|conversation|chat|inbox|thread|xchat|dm_|/dm/|keyregistry|juicebox", re.I)

    def __init__(self, page, found_now):
        self.page = page
        self.found_now = found_now
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.kinds: dict[str, list] = {}
        self.total = {"count": 0, "bytes": 0}
        self.shapes = 0
        self.stopped = threading.Event()
        try:
            facts = page.evaluate(CHAT_FACTS_JS)
            print("قياس الجمع: الصفحة " + json.dumps(facts, ensure_ascii=False)[:1500], flush=True)
        except Exception as exc:
            print(f"قياس الجمع: تعذّرت قراءة الصفحة ({type(exc).__name__})", flush=True)
        try:
            page.on("requestfinished", self.finished)
        except Exception:
            pass
        threading.Thread(target=self.every, name="collect-probe", daemon=True).start()

    @staticmethod
    def kind(url: str) -> str:
        found = re.match(r"https?://([^/?#]+)([^?#]*)", url or "")
        if not found:
            return "?"
        host, path = found.group(1), found.group(2)
        operation = re.search(r"/graphql/[^/]+/([A-Za-z0-9_]+)", path)
        if operation:
            return operation.group(1)
        if host.endswith("twimg.com"):
            return host.split(".")[0] + ":" + (path.rsplit(".", 1)[-1][:6] if "." in path.rsplit("/", 1)[-1] else "؟")
        parts = [part for part in path.split("/") if part][:3]
        return host + "/" + "/".join(re.sub(r"\d{5,}", "#", part) for part in parts)

    def finished(self, request) -> None:
        try:
            size = int((request.sizes() or {}).get("responseBodySize") or 0)
        except Exception:
            size = 0
        try:
            took = float((request.timing or {}).get("responseEnd") or 0)
        except Exception:
            took = 0.0
        kind = self.kind(str(request.url))
        with self.lock:
            row = self.kinds.setdefault(kind, [0, 0, 0.0])
            row[0] += 1
            row[1] += size
            row[2] += max(0.0, took)
            self.total["count"] += 1
            self.total["bytes"] += size
            look = self.shapes < 4 and bool(self.CHAT_WORDS.search(str(request.url)))
            if look:
                self.shapes += 1
        if look:
            try:
                response = request.response()
                body = response.body() if response else b""
                kind_of = (response.headers.get("content-type") or "?") if response else "?"
                try:
                    shape = json.dumps(json_shape(json.loads(body.decode("utf-8"))), ensure_ascii=False)
                except Exception:
                    shape = f"ليس JSON ({len(body)} بايت)"
                print(f"قياس الجمع: رد {kind} [{kind_of[:40]}] {shape[:1400]}", flush=True)
            except Exception as exc:
                print(f"قياس الجمع: رد {kind}: تعذّرت قراءته ({type(exc).__name__})", flush=True)

    def report(self) -> None:
        seconds = int(time.monotonic() - self.started)
        try:
            chat = self.page.evaluate(CHAT_TICK_JS)
        except Exception as exc:
            chat = {"error": type(exc).__name__}
        with self.lock:
            kinds, self.kinds = self.kinds, {}
            total = dict(self.total)
        top = sorted(kinds.items(), key=lambda item: -item[1][1])[:8]
        net = "، ".join(f"{name}×{row[0]} {row[1] // 1024}KB {int(row[2] / max(1, row[0]))}ms" for name, row in top) or "لا شيء"
        try:
            pages = self.page.evaluate("() => window.__ggChatPages || null")
        except Exception:
            pages = None
        print(f"قياس الجمع ({seconds} ث): جُمع {self.found_now()} · المحادثة {json.dumps(chat, ensure_ascii=False)} · "
              f"الشبكة في آخر فترة: {net} · منذ البداية {total['count']} طلب {total['bytes'] // 1024}KB · "
              f"صفحات المحادثة {json.dumps(pages, ensure_ascii=False)[:600]}", flush=True)

    def every(self) -> None:
        while not self.stopped.wait(10):
            try:
                self.report()
            except Exception:
                pass

    def close(self) -> None:
        self.stopped.set()
        try:
            self.page.remove_listener("requestfinished", self.finished)
        except Exception:
            pass
        try:
            self.report()
        except Exception:
            pass
        print(f"قياس الجمع: انتهى بعد {int(time.monotonic() - self.started)} ث", flush=True)


def collect_group_tweets(page, limit: int, on_progress=None, stop_id: str = "", should_stop=None) -> tuple[list[dict], bool]:
    """In the app the collecting is measured (see CollectProbe)."""
    if not IN_APP:
        return collect_group_tweets_now(page, limit, on_progress, stop_id, should_stop)
    counted = {"n": 0}

    def progress(done, total, phase):
        counted["n"] = done
        if on_progress:
            on_progress(done, total, phase)

    probe = CollectProbe(page, lambda: counted["n"])
    try:
        return collect_group_tweets_now(page, limit, progress, stop_id, should_stop)
    finally:
        probe.close()


def collect_group_tweets_now(page, limit: int, on_progress=None, stop_id: str = "", should_stop=None) -> tuple[list[dict], bool]:
    found: list[dict] = []
    seen: set[str] = set()
    reached = False

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
            if status_id in seen:
                if stop_id and status_id == stop_id:
                    reached = True
                    break
                continue
            seen.add(status_id)
            found.append({
                "url": tweet_url(str(row.get("url") or ""), status_id),
                "text": re.sub(r"\s+", " ", str(row.get("text") or "")).strip()[:280] or "تغريدة",
            })
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
        if not status_id or status_id in seen:
            return False
        seen.add(status_id)
        found.append({
            "url": tweet_url(str(row.get("url") or ""), status_id),
            "text": re.sub(r"\s+", " ", str(row.get("text") or "")).strip()[:280] or "تغريدة",
        })
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
            step = max(step, min(step * GROUP_SCROLL_FACTOR, int(client * 0.9)))
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
            if not moved and not grew and top_now <= 48:
                # At the oldest loaded messages and X has not loaded older ones.
                unstick_group_chat(page, step)
                state = scan_stop(0)
                if state.get("hit"):
                    spotted = True
                    hit_top = float(state.get("hitTop") or 0)
                    break
                top_now = int(state.get("scrollTop") or 0)
                oldest = str(state.get("oldest") or "")
                height = int(state.get("scrollHeight") or 0)
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
                payload = page.evaluate(GROUP_READ_TWEETS_JS, {"dir": 1, "stopId": stop_id, "factor": GROUP_SCROLL_FACTOR}) or {}
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
            payload = page.evaluate(GROUP_READ_TWEETS_JS, {"dir": -1, "stopId": "", "factor": GROUP_SCROLL_FACTOR}) or {}
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
            client = int(payload.get("clientHeight") or 0) or 800
            unstick_group_chat(page, max(250, min(500, int(client * 0.2))))
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


def reveal_status(page) -> None:
    try:
        clicked = page.evaluate(
            """() => {
              const words = /^(view|show|see post|yes,? view|عرض|عرض المنشور|نعم، عرض|عرض الوسائط|متابعة المشاهدة)$/i;
              const nodes = [...document.querySelectorAll('button, [role="button"], a')];
              const hit = nodes.find((node) => words.test((node.innerText || "").replace(/\\s+/g, " ").trim()) && node.getClientRects().length);
              if (!hit) return false;
              hit.click();
              return true;
            }"""
        )
        if clicked:
            page.wait_for_timeout(900)
    except Exception:
        pass


def read_status_gate(page) -> str:
    try:
        state = page.evaluate(
            """() => {
              const articles = [...document.querySelectorAll('article[data-testid="tweet"]')];
              const live = articles.find((article) => !/was deleted|deleted by the post author|تم حذف|غير متاح/i.test(article.innerText || ""));
              if (live) return "tweet";
              const chunks = [];
              for (const node of document.querySelectorAll('[data-testid="primaryColumn"], [data-testid="empty_state_header_text"], [data-testid="error-detail"], [role="status"]')) {
                chunks.push(node.innerText || "");
              }
              if (!chunks.length && document.body) chunks.push((document.body.innerText || "").slice(0, 1800));
              const text = chunks.join(" ").replace(/\\s+/g, " ");
              if (/was deleted|deleted by the post author|this post is unavailable|this post was deleted|doesn.?t exist|account is suspended|unable to view this post|هذه الصفحة غير موجودة|هذا المنشور غير متاح|تم حذف|حذف صاحب|الحساب موقوف/i.test(text)) return "gone";
              if (document.querySelector('[data-testid="empty_state_header_text"], [data-testid="error-detail"]')) return "gone";
              return "wait";
            }"""
        )
    except Exception:
        return "wait"
    return state if state in {"tweet", "gone", "wait"} else "wait"


QUICK_ACTIONS_ON_JS = r"""
({statusId, names}) => {
  if (!statusId) return false;
  const card = [...document.querySelectorAll('article[data-testid="tweet"], [data-testid="tweet"]')].find((article) =>
    [...article.querySelectorAll('a[href*="/status/"]')].some((node) => (node.getAttribute("href") || "").includes("/status/" + statusId)));
  if (!card) return false;
  const root = card.closest('[data-testid="cellInnerDiv"]') || card;
  const on = {repost: "unretweet", like: "unlike", bookmark: "removeBookmark"};
  const labels = {unretweet: /undo repost|unretweet|تم إعادة|إلغاء إعادة/i, unlike: /unlike|liked|أعجبني|معجب/i, removeBookmark: /remove bookmark|إزالة الإشارة/i};
  const has = (testid) => {
    if (root.querySelector('[data-testid="' + testid + '"]')) return true;
    const re = labels[testid];
    if (!re) return false;
    return [...root.querySelectorAll("button, [role='button']")].some((node) => re.test(node.getAttribute("aria-label") || ""));
  };
  return names.length > 0 && names.every((name) => on[name] && has(on[name]));
}
"""


def tweet_flags_from_api(data, status_id: str) -> dict | None:
    """Find the tweet in X's own API reply and read its like / repost / bookmark state."""
    stack = [data]
    seen = 0
    while stack and seen < 20000:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            legacy = node.get("legacy")
            if str(node.get("rest_id") or "") == status_id and isinstance(legacy, dict):
                return {
                    "like": bool(legacy.get("favorited")),
                    "repost": bool(legacy.get("retweeted")),
                    "bookmark": bool(legacy.get("bookmarked")),
                }
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


# In the app, on a phone's network: the next tweet is opened inside the X page already open, as a click on a link
# there does (X's own app moves to it and asks only for that tweet), instead of loading the whole site again for
# each tweet. Where X's page does not move within a few seconds, the tweet is loaded in full as before; after two
# such times in a row it is always loaded in full.
APP_ROUTE_JS = r"""
(path) => {
  if (location.hostname !== "x.com" || !document.querySelector('[data-testid="primaryColumn"]')) return false;
  for (const node of document.querySelectorAll('[data-testid="primaryColumn"] article, [data-testid="primaryColumn"] [data-testid="cellInnerDiv"]')) {
    node.setAttribute("data-gg-old", "1");
  }
  history.pushState({}, "", path);
  dispatchEvent(new PopStateEvent("popstate", {state: history.state}));
  return true;
}
"""
APP_ROUTED_JS = r"""
(id) => location.pathname.indexOf(id) >= 0 && !document.querySelector("[data-gg-old]")
  && !!document.querySelector('[data-testid="primaryColumn"]')
"""
app_route_failures = [0]


def app_route(page, target: str, status_id: str) -> bool:
    """Whether X's own page moved to the tweet (see APP_ROUTE_JS): its earlier content is gone and its address is
    the tweet's."""
    if not IN_APP or app_route_failures[0] >= 2 or not status_id:
        return False
    try:
        here = page.url or ""
        if not here.startswith("https://x.com/") or status_id in here:
            return False
        if not page.evaluate(APP_ROUTE_JS, target.replace("https://x.com", "", 1)):
            return False
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if page.evaluate(APP_ROUTED_JS, status_id):
                app_route_failures[0] = 0
                return True
            page.wait_for_timeout(120)
    except Exception as exc:
        if browser_closed_error(exc):
            raise
    app_route_failures[0] += 1
    print(f"✗ فتح التغريدة داخل صفحة X لم يعمل ({app_route_failures[0]})، فتُحمَّل كاملة", flush=True)
    return False


def open_status_page(page, url: str, skip_if_on: list[str] | None = None) -> tuple[str | None, str]:
    """Open a tweet. With skip_if_on, return (path, "already_on") the moment the
    tweet's buttons show every listed action already on, without waiting for the
    rest of the page to load."""
    match = re.search(r"status/(\d{6,25})", url or "")
    status_id = match.group(1) if match else ""
    canonical = normalize_status_path(url) or ""
    if status_id:
        target = f"https://x.com/i/web/status/{status_id}"
    elif canonical:
        target = "https://x.com" + canonical
    else:
        return None, "fail"
    quick = [name for name in (skip_if_on or []) if name in ACTION_CONTROLS]
    if skip_if_on and len(quick) != len(skip_if_on):
        quick = []
    # X's page asks its API for the tweet before drawing it. Listening to that reply
    # gives the button states before they appear on screen. Only replies that have
    # finished downloading are read: reading one that is still arriving waits with no
    # time limit, so a reply that stalls halfway would freeze the task on this tweet.
    replies: list = []
    api_flags: dict | None = None

    def on_finished(request):
        address = request.url
        if "/graphql/" in address and ("TweetDetail" in address or "TweetResultByRestId" in address):
            replies.append(request)

    if quick and status_id:
        page.on("requestfinished", on_finished)
    try:
        if not app_route(page, target, status_id):
            try:
                page.goto(target, wait_until="commit" if quick else "domcontentloaded", timeout=20_000)
            except Exception:
                return None, "fail"
        revealed = False
        deadline = time.monotonic() + (12 if quick else 8)
        while time.monotonic() < deadline:
            if quick and status_id:
                while replies and api_flags is None:
                    request = replies.pop(0)
                    try:
                        response = request.response()
                        api_flags = tweet_flags_from_api(response.json(), status_id) if response else None
                    except Exception:
                        api_flags = None
                    if api_flags is not None and os.environ.get("COLLECT_DEBUG"):
                        print(f"tweet {status_id} api state: {api_flags}", flush=True)
                if api_flags is not None and all(api_flags.get(name) for name in quick):
                    return canonical or f"/i/web/status/{status_id}", "already_on"
                try:
                    if page.evaluate(QUICK_ACTIONS_ON_JS, {"statusId": status_id, "names": quick}):
                        return canonical or f"/i/web/status/{status_id}", "already_on"
                except Exception:
                    pass
            state = read_status_gate(page)
            if state == "gone":
                return None, "gone"
            if state == "tweet":
                href = ""
                if status_id:
                    try:
                        href = page.evaluate(
                            """(id) => {
                              const link = [...document.querySelectorAll('a[href*="/status/"]')]
                                .find((node) => (node.getAttribute("href") || "").includes("/status/" + id));
                              return link ? (link.getAttribute("href") || "") : "";
                            }""",
                            status_id,
                        ) or ""
                    except Exception:
                        href = ""
                return normalize_status_path(str(href)) or canonical or f"/i/web/status/{status_id}", ""
            if not revealed and time.monotonic() + 5 < deadline:
                pass
            elif not revealed:
                reveal_status(page)
                revealed = True
                if read_status_gate(page) == "gone":
                    return None, "gone"
            page.wait_for_timeout(120)
        if read_status_gate(page) == "gone":
            return None, "gone"
        return None, "fail"
    finally:
        if quick and status_id:
            try:
                page.remove_listener("requestfinished", on_finished)
            except Exception:
                pass


def all_actions_already_on(page, path: str, actions: list[str], timeout_s: float = 1.5) -> bool:
    """Read the tweet's buttons as soon as they appear. True only when every
    requested action is already on, so the tweet can be skipped without waiting
    for the rest of the page to load."""
    if not actions or any(action not in ACTION_CONTROLS for action in actions):
        return False
    match = re.search(r"status/(\d{6,25})", path or "")
    status_id = match.group(1) if match else ""
    deadline = time.monotonic() + timeout_s
    while True:
        state = read_action_state(page, status_id, actions) or {}
        rows = [state.get(action) if isinstance(state.get(action), dict) else {} for action in actions]
        if rows and all(row.get("on") for row in rows):
            return True
        if rows and all(row.get("on") or row.get("box") for row in rows):
            return False
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(150)


DONE_HISTORY_PATH = DATA_DIR / "done_history.json"
DONE_HISTORY_HOURS = 8
done_history_lock = threading.Lock()
done_history: dict[str, dict[str, dict[str, float]]] = {}


def prune_done_history_unlocked() -> None:
    cutoff = time.time() - DONE_HISTORY_HOURS * 3600
    for account in list(done_history):
        tweets = done_history[account]
        for status_id in list(tweets):
            kept = {action: stamp for action, stamp in tweets[status_id].items() if stamp >= cutoff}
            if kept:
                tweets[status_id] = kept
            else:
                del tweets[status_id]
        if not tweets:
            del done_history[account]


def load_done_history() -> None:
    loaded = read_private_json(DONE_HISTORY_PATH) or {}
    rows = loaded.get("accounts")
    if not isinstance(rows, dict):
        return
    with done_history_lock:
        for account, tweets in rows.items():
            if not isinstance(tweets, dict):
                continue
            clean: dict[str, dict[str, float]] = {}
            for status_id, done in tweets.items():
                if isinstance(done, dict):
                    clean[str(status_id)] = {str(a): float(t) for a, t in done.items() if isinstance(t, (int, float))}
            done_history[str(account)] = clean
        prune_done_history_unlocked()


def tweet_status_id(url: str) -> str:
    match = re.search(r"status/(\d{6,25})", url or "")
    return match.group(1) if match else ""


def already_done_recently(account: str, status_id: str, actions: list[str]) -> bool:
    """True when every requested action was done on this tweet by this account in the last 8 hours."""
    if not account or not status_id or not actions:
        return False
    cutoff = time.time() - DONE_HISTORY_HOURS * 3600
    with done_history_lock:
        done = (done_history.get(account) or {}).get(status_id) or {}
        return all(done.get(action, 0) >= cutoff for action in actions)


ACTION_COUNTS_PATH = DATA_DIR / "action_counts.json"
action_counts_lock = threading.Lock()
action_counts: dict[str, int] = {"repost": 0, "like": 0, "bookmark": 0, "comment": 0}


def load_action_counts() -> None:
    loaded = read_private_json(ACTION_COUNTS_PATH) or {}
    with action_counts_lock:
        for action in action_counts:
            try:
                action_counts[action] = max(0, int(loaded.get(action) or 0))
            except (TypeError, ValueError):
                action_counts[action] = 0


def count_successes(outcome: dict[str, str]) -> None:
    """Add this tweet's successful actions to the totals shown at the top of the page."""
    done = [action for action, note in (outcome or {}).items() if note == "تم التنفيذ" and action in action_counts]
    if not done:
        return
    with action_counts_lock:
        for action in done:
            action_counts[action] += 1
        try:
            write_private_json(ACTION_COUNTS_PATH, dict(action_counts))
        except Exception:
            pass


def remember_done(account: str, status_id: str, outcome: dict[str, str]) -> None:
    """Save the actions that are now on for this tweet (done now or already on)."""
    if not account or not status_id:
        return
    now = time.time()
    finished = [
        action for action, note in (outcome or {}).items()
        if (action in ACTION_CONTROLS and (note == "تم التنفيذ" or "مفعّل مسبقًا" in str(note)))
        or (action == "comment" and (note == "تم التنفيذ" or str(note).startswith("أُرسل الطلب")))
    ]
    if not finished:
        return
    with done_history_lock:
        tweets = done_history.setdefault(account, {})
        record = tweets.setdefault(status_id, {})
        for action in finished:
            record[action] = now
        prune_done_history_unlocked()
        try:
            write_private_json(DONE_HISTORY_PATH, {"version": 1, "accounts": done_history})
        except Exception:
            pass


# Two tweets in a row that would not open: the browser is closed for a while, then the task goes back
# to the first of them. The first time for 3.5 minutes, the second time for 1 minute; the third time
# the task stops (X is likely not letting this account open tweets now).
TWEET_FAIL_PAUSES = (210, 60)


def open_task_tweet(page, url: str, skip_if_on):
    """Open a task's tweet with two tries. Returns (opened path or None, reason)."""
    opened, reason = open_status_page(page, url, skip_if_on)
    if not opened and reason != "gone":
        opened, reason = open_status_page(page, url, skip_if_on)
    return opened, reason


class TweetsKeptFailing(Exception):
    """Two tweets in a row would not open a third time in one task, so it stops. results: what it did
    before (with the two that would not open); at: the number of the tweet it stopped at (from 1)."""

    def __init__(self, results: list[dict], at: int):
        super().__init__("تعذّر فتح التغريدات ثلاث مرات")
        self.results = results
        self.at = at


class FailedTweets:
    """The tweets of a task that would not open (a deleted one does not count). Two in a row that would
    not open close the browser for a pause, then the task goes back to the first of them: the first
    pause is TWEET_FAIL_PAUSES[0] seconds, the second TWEET_FAIL_PAUSES[1]; the third time, the task
    stops."""

    def __init__(self):
        self.last: int | None = None   # the tweet tried before, when it would not open
        self.pauses = 0                # the pauses this task has had

    def opened(self) -> None:
        self.last = None

    def failed(self, index: int) -> int | None:
        """This tweet would not open. Returns the first of the two in a row that would not (to go back to
        after a pause), or None to go on."""
        before, self.last = self.last, index
        if before is None:
            return None
        self.last = None
        return before

    def next_pause(self) -> int | None:
        """How many seconds the next pause lasts; None when the task has had them all and stops."""
        if self.pauses >= len(TWEET_FAIL_PAUSES):
            return None
        self.pauses += 1
        return TWEET_FAIL_PAUSES[self.pauses - 1]


def pause_after_failures(should_stop=None, on_progress=None, total: int = 0, after_reopen=None,
                         seconds: int = TWEET_FAIL_PAUSES[0], phase: str = "pause"):
    """Close the browser for seconds, then reopen it. Returns the new page, or None when the task was
    stopped during the wait (the browser then stays closed) or the browser cannot be reopened here.
    phase: "pause" for the first pause, "pause_again" for the next one (said on the page)."""

    def wait_closed() -> bool:
        end = time.monotonic() + seconds
        shown = None
        while (left := math.ceil(end - time.monotonic())) > 0:
            if should_stop and should_stop():
                return False
            if on_progress and (shown is None or shown - left >= 5):
                on_progress(left, total, phase)
                shown = left
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        if on_progress:
            on_progress(0, total, phase)
        return True

    new_page = restart_browser(wait_closed)
    if new_page is not None and after_reopen:
        after_reopen(new_page)
    return new_page


def visit_and_apply(page, items: list[dict], actions: list[str], on_progress=None, on_snapshot=None, should_stop=None, account: str = "") -> list[dict]:
    results: list[dict] = LoggedResults()
    total = len(items)
    failures = FailedTweets()
    # Where each tweet's result starts in results: going back after a pause drops them from there on.
    starts: dict[int, int] = {}
    if on_progress:
        on_progress(0, total, "act")
    index = -1
    while index + 1 < total:
        index += 1
        item = items[index]
        if should_stop and should_stop():
            break
        starts[index] = len(results)
        result = {"url": item.get("url", ""), "text": item.get("text", ""), "actions": {}}
        status_id = tweet_status_id(str(item.get("url") or ""))
        if already_done_recently(account, status_id, actions):
            # Done by this account in the last 8 hours: skip without opening the tweet.
            result["actions"] = {key: "سبق التفاعل خلال ٨ ساعات — تم التخطي" for key in actions}
            results.append(result)
            if on_snapshot:
                on_snapshot(list(results))
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        try:
            opened, reason = open_task_tweet(page, str(item.get("url") or ""), actions)
        except Exception as exc:
            if browser_closed_error(exc):
                if on_snapshot and results:
                    on_snapshot(list(results))
                raise BrowserClosed() from exc
            raise
        if opened or reason == "gone":
            failures.opened()
        else:
            back = failures.failed(index)
            new_page = None
            if back is not None:
                seconds = failures.next_pause()
                if seconds is None:
                    # A third time: the task stops here, with what it did (these two would not open).
                    result["actions"] = {key: "تعذر فتح التغريدة" for key in actions}
                    results.append(result)
                    if on_snapshot:
                        on_snapshot(list(results))
                    raise TweetsKeptFailing(results, index + 1)
                new_page = pause_after_failures(should_stop, on_progress, total, None, seconds,
                                                "pause" if failures.pauses == 1 else "pause_again")
            if new_page is not None:
                # Back to the first of the two that would not open; what was recorded from it on goes.
                page = new_page
                del results[starts[back]:]
                if on_snapshot:
                    on_snapshot(list(results))
                if on_progress:
                    on_progress(back, total, "act")
                index = back - 1
                continue
        if not opened:
            note = "محذوفة — تم التخطي" if reason == "gone" else "تعذر فتح التغريدة"
            result["actions"] = {key: note for key in actions}
            results.append(result)
            if on_snapshot:
                on_snapshot(list(results))
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        if tweet_is_ignored("https://x.com" + opened):
            # The opened tweet's author is an ignored account: take no action and leave no trace.
            forget_collected("https://x.com" + opened)
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        result["url"] = "https://x.com" + opened
        try:
            already_on = reason == "already_on" or all_actions_already_on(page, opened, actions)
        except Exception as exc:
            if browser_closed_error(exc):
                raise BrowserClosed() from exc
            already_on = False
        if already_on:
            result["actions"] = {key: "مفعّل مسبقًا — تم التجاوز" for key in actions}
            remember_done(account, status_id or tweet_status_id(opened), result["actions"])
            results.append(result)
            if on_snapshot:
                on_snapshot(list(results))
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        page.wait_for_timeout(2000)
        close_popups(page)
        try:
            result["actions"] = apply_actions_on_path(page, opened, actions)
        except Exception as exc:
            if browser_closed_error(exc):
                raise BrowserClosed() from exc
            result["actions"] = {key: "تعذر تنفيذ الإجراء بعد فتح التغريدة" for key in actions}
        remember_done(account, status_id or tweet_status_id(opened), result["actions"])
        count_successes(result["actions"])
        results.append(result)
        if on_snapshot:
            on_snapshot(list(results))
        if on_progress:
            on_progress(index + 1, total, "act")
    return results


def fetch_group_names(auth_token: str, pin: str, settle: bool = False, settle_seconds: int = 10) -> tuple[list[str], str | None]:
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
        if settle:
            page.wait_for_timeout(max(1, min(120, int(settle_seconds))) * 1000)
        names = collect_groups(page)
        if not names:
            return [], "فُتحت الدردشة لكن لم تظهر قروبات."
        return names, None
    try:
        return run_on_browser(auth_token, work)
    except Exception:
        return [], "تعذّر فتح المتصفح."


class JobStopped(Exception):
    pass


# Arguments of each group task, kept in memory so the retry button can continue it.
job_run_args: dict[str, tuple] = {}


class BrowserClosed(Exception):
    """Chromium died or the tab was closed. The job can reopen and continue."""


def browser_closed_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "has been closed" in text
        or "target closed" in text
        or "connection closed" in text
        or "browser closed" in text
    )


def job_cancelled(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id) or {}
        return bool(job.get("cancel"))


DEFAULT_GROUP_WAIT_SECONDS = 8
# «روابط التغريدات»: a task on tweets pasted on the page, instead of opening a group and collecting.
# The page sends each tweet as "handle/id", or "id" when its link has no author's name.
MAX_TWEET_LINKS = MAX_REPOSTS_REQUEST
LINKS_TASK_NAME = "روابط التغريدات"


def tweet_links_from(entries) -> list[dict]:
    """The pasted tweets as the same {"url", "text"} rows a group's collecting gives: each tweet
    once, in the order pasted."""
    items: list[dict] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        match = re.fullmatch(r"(?:([A-Za-z0-9_]{1,15})/)?(\d{6,25})", str(entry or "").strip())
        if not match or match.group(2) in seen:
            continue
        handle, status_id = match.group(1) or "", match.group(2)
        seen.add(status_id)
        if handle.lower() in _HANDLE_NOT_A_USER:
            handle = ""
        url = f"https://x.com/{handle}/status/{status_id}" if handle else f"https://x.com/i/web/status/{status_id}"
        items.append({"url": url, "text": ""})
    return items


def run_group_job(job_id: str, auth_token: str, pin: str, group_name: str, requested_count: int, actions: list[str], gemini_api_key: str = "", gemini_model: str = "", stop_id: str = "", group_wait_s: int = DEFAULT_GROUP_WAIT_SECONDS, profiles_per_account: int = 1, phone_height: int = 0, reply_voice: str = DEFAULT_REPLY_VOICE, tweet_links: list[dict] | None = None) -> None:
    def on_progress(done: int, requested: int, phase: str) -> None:
        if job_cancelled(job_id):
            raise JobStopped
        if phase == "collect":
            if stop_id:
                publish_job(job_id, phase="collect", collected=done, requested=0, message=f"جُمعت {done}. التمرير مستمر حتى التغريدة المحددة.")
            else:
                publish_job(job_id, phase="collect", collected=done, requested=requested, message=f"جُمعت {done} من {requested}.")
        elif phase == "seek":
            publish_job(job_id, phase="collect", collected=0, requested=0, message=f"يبحث عن التغريدة المحددة. الخطوة {done}، وما زال يحمّل الأقدم.")
        elif phase == "act":
            publish_job(job_id, phase="act", done=done, collected=done, requested=requested, message=f"تنفيذ {done} من {requested}.")
        elif phase in ("pause", "pause_again"):
            # Two tweets in a row would not open: the browser is closed, done = seconds left. The second
            # time ("pause_again") is the last: a third time stops the task.
            again = phase == "pause_again"
            if done > 0:
                publish_job(job_id, phase="act", message=(
                    f"تعذّر فتح تغريدتين متتاليتين{' مرة أخرى' if again else ''}، فأُغلق المتصفح. المتابعة منهما بعد {done // 60}:{done % 60:02d}."
                    + (" إن تعذّر مرة ثالثة تتوقف المهمة." if again else "")))
            else:
                publish_job(job_id, phase="act", message="انتهى الانتظار. إعادة فتح المتصفح والمتابعة من التغريدتين…")
        else:
            publish_job(job_id, phase=phase, message="فتح الدردشة والقروب…")

    job_run_args[job_id] = (auth_token, pin, group_name, requested_count, actions, gemini_api_key, gemini_model, stop_id, group_wait_s, profiles_per_account, phone_height, reply_voice, tweet_links)

    def work(page):
        try:
            # Pick up any change to the ignored-accounts list (the owner sets it in «توزيع البرنامج»,
            # which shares this folder; a user's copy keeps it current from its check-in).
            load_skip_users()
            with jobs_lock:
                saved = jobs.get(job_id) or {}
                items = list(saved["items"]) if isinstance(saved.get("items"), list) else []
                prefix = list(saved["results"]) if isinstance(saved.get("results"), list) else []
            if items and len(prefix) > len(items):
                prefix = prefix[: len(items)]
            if not items and tweet_links:
                # «روابط التغريدات»: the tweets were pasted on the page, so no chat, no group and no
                # collecting: check the session, then straight to the actions.
                publish_job(job_id, phase="prepare", message="التحقق من الجلسة…")
                if not ensure_logged_in(page):
                    publish_job(job_id, status="done", success=False, message="فشل التحقق: الجلسة غير مسجّلة الدخول.")
                    return
                accept_cookies(page)
                handle = read_session_username(page)
                if handle:
                    remember_session(auth_token, handle, pin)
                # A tweet by an ignored account leaves no trace here either (one whose link has no
                # author's name is caught once it is open, as for a group).
                items = [dict(item) for item in tweet_links if not tweet_is_ignored(str(item.get("url") or ""))]
                if not items:
                    publish_job(job_id, status="done", success=False, message="لا توجد تغريدات للتنفيذ.")
                    return
                prefix = []
                publish_job(job_id, phase="act", collected=len(items), requested=len(items), items=items,
                            message=f"بدأ تنفيذ الإجراءات على {len(items)} تغريدة من الروابط.")
            elif not items:
                publish_job(job_id, phase="login", message="التحقق من الجلسة ثم فتح الدردشة…")
                if not ensure_logged_in(page):
                    publish_job(job_id, status="done", success=False, message="فشل التحقق: الجلسة غير مسجّلة الدخول.")
                    return
                accept_cookies(page)
                # (In the app the account's name is read only when it is not known yet: on a phone's X page it is
                # not shown, and reading it from the settings costs a page and seconds at each task.)
                handle = (IN_APP and saved_username(auth_token)) or read_session_username(page)
                if handle:
                    remember_session(auth_token, handle, pin)
                publish_job(job_id, phase="login", message="إدخال رمز الدردشة…")
                set_page_height(page, COLLECT_VIEWPORT_HEIGHT)
                error = open_chat(page, pin)
                if error:
                    publish_job(job_id, status="done", success=False, message=error)
                    return
                opened_name = open_group_for_task(
                    page, pin, group_name, group_wait_s,
                    lambda text: publish_job(job_id, phase="login", message=text),
                )
                if opened_name != group_name:
                    publish_job(job_id, phase="login", message=f"تغيّر اسم القروب إلى «{opened_name}» وفُتح.")
                if group_wait_s > 0:
                    publish_job(job_id, phase="login", message=f"فُتح القروب. انتظار {group_wait_s} ث، مع التحريك للأعلى والأسفل ثم العودة للأسفل…")
                    sway_group_while_waiting(page, group_wait_s, lambda: job_cancelled(job_id))
                cap = MAX_REPOSTS_REQUEST if stop_id else requested_count
                items, reached = collect_group_tweets(page, cap, on_progress, stop_id, lambda: job_cancelled(job_id))
                # Drop tweets by ignored accounts here, before anything is opened: they leave no trace
                # in the counts or the result, as if the group never carried them.
                items = [item for item in items if not tweet_is_ignored(str(item.get("url") or ""))]
                remember_collected(items, group_name)
                set_page_height(page, NORMAL_VIEWPORT_HEIGHT)
                if job_cancelled(job_id):
                    raise JobStopped
                if stop_id and not reached:
                    publish_job(job_id, status="done", success=False, collected=len(items), message="وصلت نهاية السجل ولم تظهر التغريدة المحددة. لم يُنفَّذ شيء.")
                    return
                if not items:
                    publish_job(job_id, status="done", success=False, message="فُتح القروب لكن لم تُستخرج تغريدات.")
                    return
                prefix = []
                publish_job(job_id, phase="act", collected=len(items), requested=len(items), items=items, message="بدأ تنفيذ الإجراءات على التغريدات المستخرجة.")
            elif not ensure_logged_in(page):
                publish_job(job_id, status="done", success=False, message="فشل التحقق: الجلسة غير مسجّلة الدخول.")
                return
            else:
                publish_job(job_id, phase="act", requested=len(items), collected=len(items), done=len(prefix), message=f"استئناف التنفيذ من {len(prefix)} من {len(items)}.")
            stopped_at = 0   # the tweet the task stopped at when two kept failing to open
            if len(prefix) >= len(items):
                results = prefix
            else:
                pending = items[len(prefix) :]

                def snapshot(rows: list[dict]) -> None:
                    merged = prefix + list(rows)
                    publish_job(
                        job_id, phase="act", done=len(merged), requested=len(items), collected=len(items),
                        results=merged, items=items, message=f"تنفيذ {len(merged)} من {len(items)}.",
                    )

                def on_act(done: int, requested: int, phase: str) -> None:
                    if phase in ("pause", "pause_again"):
                        on_progress(done, requested, phase)
                    else:
                        on_progress(len(prefix) + done, len(items), "act")

                try:
                    if "comment" in actions:
                        fresh = execute_comment_actions(
                            page, pending, actions, gemini_api_key, gemini_model, on_act,
                            lambda: job_cancelled(job_id), snapshot, profile_key(auth_token), reply_voice,
                        )
                    else:
                        fresh = visit_and_apply(page, pending, actions, on_act, snapshot, lambda: job_cancelled(job_id), profile_key(auth_token))
                except TweetsKeptFailing as stop:
                    fresh, stopped_at = stop.results, len(prefix) + stop.at
                results = prefix + fresh
                refine_collected_links(fresh)
            if stopped_at:
                publish_job(
                    job_id, status="done", success=False, phase="act", done=len(results), requested=len(items),
                    collected=len(items), results=results, items=items, actions=actions, username=group_name,
                    message=(f"توقفت المهمة عند التغريدة {stopped_at} من {len(items)}: تعذّر فتح تغريدتين متتاليتين ثلاث مرات "
                             f"(بعد انتظار ثلاث دقائق ونصف ثم دقيقة)، والأرجح أن X يمنع هذا الحساب من فتح التغريدات الآن."),
                )
                return
            if job_cancelled(job_id):
                publish_job(
                    job_id, status="done", success=False, phase="act", done=len(results), requested=len(items),
                    collected=len(items), message="تم إيقاف التنفيذ.",
                    results=results, items=items, actions=actions, username=group_name,
                )
                return
            publish_job(
                job_id, status="done", success=True, phase="act", done=len(results), requested=len(items),
                collected=len(items), message=f"اكتمل التنفيذ على {len(results)} تغريدة من {group_name}.",
                results=results, items=items, actions=actions, username=group_name,
            )
        except JobStopped:
            publish_job(job_id, status="done", success=False, message="تم إيقاف التنفيذ.")
            return
        except BrowserClosed:
            raise
        except Exception as exc:
            if browser_closed_error(exc):
                raise BrowserClosed() from exc
            app.logger.warning("group run failed")
            trace_error("فشلت المهمة")
            publish_job(job_id, status="done", success=False, message=str(exc) or "تعذّر تنفيذ إجراءات القروب.")
            raise
    # One attempt only. If anything fails, the task stops with a message and is not retried.
    live_task.job_id = job_id
    task_screen.phone_height = phone_height
    try:
        run_on_fresh_browser(auth_token, work, profiles_per_account)
    except BrowserClosed:
        with jobs_lock:
            job = jobs.get(job_id) or {}
            already_done = job.get("status") == "done"
        if not already_done:
            publish_job(
                job_id, status="done", success=False,
                message="توقف التنفيذ لأن المتصفح أُغلق. ما اكتمل قبل الإغلاق ما زال ظاهرًا.",
            )
    except Exception as exc:
        with jobs_lock:
            job = jobs.get(job_id) or {}
            already_done = job.get("status") == "done"
        if not already_done:
            app.logger.warning("group run failed")
            trace_error("فشلت المهمة")
            message = "أُغلق المتصفح أثناء العمل." if browser_closed_error(exc) else (str(exc) or "تعذّر تنفيذ إجراءات القروب.")
            publish_job(job_id, status="done", success=False, message=message)
    finally:
        live_task.job_id = ""
        task_screen.phone_height = 0
        with live_views_lock:
            live_views.pop(job_id, None)


def allow_attempt(client: str) -> bool:
    now = time.monotonic()
    with attempts_lock:
        q = attempts[client]
        while q and now - q[0] > ATTEMPT_WINDOW_SECONDS:
            q.popleft()
        if len(q) >= MAX_ATTEMPTS_PER_CLIENT:
            return False
        q.append(now)
        return True


def csrf_matches(submitted: str) -> bool:
    expected = session.get("csrf_token", "")
    return bool(expected) and hmac.compare_digest(str(submitted), expected)


def csrf_ok(data: dict) -> bool:
    return csrf_matches(str(data.get("csrf_token", "")))


def valid_auth_token(value: str) -> bool:
    return 20 <= len(value) <= 512


@app.before_request
def limit_request_size():
    if (request.content_length or 0) > SMALL_REQUEST_BYTES and request.endpoint != "group_run_endpoint":
        abort(413)


@app.after_request
def secure_response(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: https://pbs.twimg.com; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'"
    return response


@app.after_request
def compress_response(response):
    """Big answers (a finished task's list, the page itself) go gzipped: a phone loads them in a
    fraction of the time."""
    if (
        response.status_code != 200
        or response.direct_passthrough
        or "Content-Encoding" in response.headers
        or "gzip" not in (request.headers.get("Accept-Encoding") or "").lower()
        or response.mimetype not in ("application/json", "text/html")
    ):
        return response
    body = response.get_data()
    if len(body) < 2048:
        return response
    response.set_data(gzip.compress(body, compresslevel=5))
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    return response


@app.get("/healthz")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/")
def root():
    return redirect("/" + ACCESS_PATH, code=302)


@app.get("/<access_path>")
def login_page(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    csrf_token = session.get("csrf_token")
    if not csrf_token:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token
    response = make_response(render_template_string(
        PAGE, csrf_token=csrf_token, api_root="/" + ACCESS_PATH, app_version=shown_version(), owner_copy=is_owner_copy(), max_links=MAX_TWEET_LINKS,
        claude_here=key_typed_here(), claude_default=CLAUDE_DEFAULT_MODEL,
        content_ready=content_ready.is_set() or (CONTENT_FILE.is_file() if is_owner_copy() else bool(CONTENT_EMBEDDED)),
        tweets_in_content=content_shows_tweets, in_app=IN_APP,
    ))
    return response


@app.post("/<access_path>/browser-login")
def browser_login_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    if not IN_APP:
        return jsonify(success=False, message="تسجيل الدخول من المتصفح متاح داخل التطبيق."), 400
    try:
        start_manual_login()
    except Exception:
        app.logger.warning("manual login browser failed", exc_info=True)
        return jsonify(success=False, message="تعذّر فتح المتصفح. أعد المحاولة."), 502
    return jsonify(success=True, message="سجّل الدخول في تبويب المتصفح، ثم ارجع إلى اللوحة واضغط تحقق.")


@app.post("/<access_path>/browser-session")
def browser_session_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    token = manual_login_token()
    if not valid_auth_token(token):
        return jsonify(success=False, message="لم تكتمل جلسة المتصفح بعد.")
    return jsonify(success=True, auth_token=token)


@app.post("/<access_path>/verify-login")
def verify_login_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    if not allow_attempt(request.remote_addr or "unknown"):
        return jsonify(success=False, message="تجاوزت عدد المحاولات المسموح به مؤقتًا. انتظر قليلًا ثم أعد المحاولة."), 429
    auth_token = str(data.get("auth_token", "")).strip()
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    try:
        success, handle = verify_x_login(auth_token)
        if success:
            remember_session(auth_token, handle)
        if success and handle:
            message = f"تم التحقق بنجاح: هذه الجلسة تخص @{handle}."
        elif success:
            message = "تم التحقق من تسجيل الدخول، لكن تعذّر قراءة اسم الحساب. حُفظ الرمز ويمكن اختياره لاحقًا."
        else:
            message = "فشل التحقق: لم يظهر الحساب مسجّل الدخول. قد يكون الرمز غير صالح أو منتهيًا."
        return jsonify(success=success, message=message, username=handle)
    except Exception:
        app.logger.warning("X login verification failed (details suppressed)")
        trace_error("فشل التحقق من الجلسة")
        return jsonify(success=False, message="تعذّر إكمال التحقق بسبب مشكلة في الاتصال أو تشغيل المتصفح."), 502
    finally:
        auth_token = ""


def write_private_json(path: Path, payload: dict, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(payload, ensure_ascii=False, indent=2)
    temporary.write_text(text, encoding="utf-8")
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


# Ignored accounts. The owner sets a list of handles in «توزيع البرنامج» (stored beside g.py in
# data/skip_users.json); every user's copy takes the list on its check-in and caches it in the same
# file on its own server. A collected tweet whose author is on the list is dropped right after it is
# gathered — before it is opened — so it is never acted on and never appears anywhere in the result.
SKIP_USERS_PATH = DATA_DIR / "skip_users.json"
skip_users_lock = threading.Lock()
skip_users_cache: set[str] = set()
_HANDLE_NOT_A_USER = {"i", "web", "intent", "share", "home", "search", "messages", "notifications", "explore"}


def normalize_handle(value: str) -> str:
    """An X handle, lowercased, no leading @; "" when it is not a valid handle."""
    handle = (value or "").strip().lstrip("@").lower()
    return handle if re.fullmatch(r"[a-z0-9_]{1,15}", handle) else ""


def _clean_handles(handles) -> list[str]:
    seen = {h for h in (normalize_handle(x) for x in (handles or [])) if h}
    return sorted(seen)


def load_skip_users() -> None:
    data = read_private_json(SKIP_USERS_PATH) or {}
    users = data.get("users") if isinstance(data, dict) else []
    cleaned = _clean_handles(users if isinstance(users, list) else [])
    with skip_users_lock:
        skip_users_cache.clear()
        skip_users_cache.update(cleaned)


def store_skip_users(handles) -> list[str]:
    """Replace the ignored-accounts list, persist it only when it changed, and return it cleaned."""
    cleaned = _clean_handles(handles)
    with skip_users_lock:
        changed = set(cleaned) != skip_users_cache
        if changed:
            skip_users_cache.clear()
            skip_users_cache.update(cleaned)
    if changed:
        try:
            write_private_json(SKIP_USERS_PATH, {"users": cleaned})
        except OSError:
            pass
    return cleaned


def skip_users_list() -> list[str]:
    with skip_users_lock:
        return sorted(skip_users_cache)


def tweet_author_handle(url: str) -> str:
    """The author handle inside an x.com/<handle>/status/<id> link, or "" for a handle-less link."""
    match = re.search(r"(?:^|[^A-Za-z0-9_])([A-Za-z0-9_]{1,15})/status/\d", url or "")
    if match:
        handle = match.group(1).lower()
        if handle not in _HANDLE_NOT_A_USER:
            return handle
    return ""


def tweet_is_ignored(url: str) -> bool:
    """True when this tweet's author is an ignored account, so it must be skipped silently."""
    handle = tweet_author_handle(url)
    if not handle:
        return False
    with skip_users_lock:
        return handle in skip_users_cache


# «عرض التغريدات»: the tweets the tasks collected in the last 7 days. Each tweet is saved once, when it
# is first collected (its time and group); collecting it again does not add it again. It is kept 7
# days from then. The page sorts the tweets into days in the viewer's own time zone.
COLLECTED_PATH = DATA_DIR / "collected_tweets.json"
COLLECTED_KEEP_SECONDS = 7 * 24 * 3600
collected_lock = threading.Lock()
collected_tweets: dict[str, dict] = {}


def prune_collected_unlocked(now: float) -> bool:
    """Drop the tweets saved more than 7 days ago. True when any was dropped."""
    oldest = now - COLLECTED_KEEP_SECONDS
    gone = [status_id for status_id, row in collected_tweets.items() if row["at"] < oldest]
    for status_id in gone:
        del collected_tweets[status_id]
    return bool(gone)


def persist_collected_unlocked() -> None:
    try:
        write_private_json(COLLECTED_PATH, {"version": 2, "tweets": collected_tweets}, compact=True)
    except OSError:
        pass


def load_collected() -> None:
    data = read_private_json(COLLECTED_PATH) or {}
    rows = data.get("tweets") if isinstance(data.get("tweets"), dict) else {}
    loaded: dict[str, dict] = {}
    for status_id, row in rows.items():
        if not isinstance(row, dict) or not re.fullmatch(r"\d{6,25}", str(status_id)):
            continue
        try:
            if row.get("at") is not None:
                at = int(row["at"])
            else:
                # Saved by the first version, which kept every collection: the first one is the time.
                at = min(int(entry[0]) for entry in row.get("seen") or [])
        except (TypeError, ValueError, IndexError):
            continue
        group = row.get("group")
        if not group and isinstance(row.get("groups"), list) and row["groups"]:
            group = row["groups"][0]
        record = {
            "url": str(row.get("url") or "")[:300],
            "text": str(row.get("text") or "")[:300],
            "group": str(group or "")[:80],
            "at": at,
        }
        # «عرض الأكثر مشاهدة»: the view count from the last check, and when (views None: no count then).
        try:
            if row.get("views_at") is not None:
                record["views_at"] = int(row["views_at"])
                record["views"] = None if row.get("views") is None else max(0, int(row["views"]))
        except (TypeError, ValueError):
            record.pop("views_at", None)
        record.update(tweet_content(row))
        loaded[str(status_id)] = record
    with collected_lock:
        collected_tweets.clear()
        collected_tweets.update(loaded)
        if prune_collected_unlocked(time.time()) or any(isinstance(r, dict) and "seen" in r for r in rows.values()):
            persist_collected_unlocked()


def remember_collected(items: list[dict], group_name: str) -> None:
    """Save the tweets a task has just collected from a group, each only the first time (for
    «عرض التغريدات»). A tweet already saved is not saved again; only a missing text, or a link
    without the author's name, is filled in."""
    now = int(time.time())
    group = re.sub(r"\s+", " ", group_name or "").strip()[:80]
    changed = False
    with collected_lock:
        if prune_collected_unlocked(now):
            changed = True
        for item in items or []:
            url = str(item.get("url") or "")
            status_id = tweet_status_id(url)
            if not status_id:
                continue
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            text = "" if text == "تغريدة" else text[:300]
            row = collected_tweets.get(status_id)
            if row is None:
                collected_tweets[status_id] = {"url": url[:300], "text": text, "group": group, "at": now}
                changed = True
                continue
            if text and not row["text"]:
                row["text"] = text
                changed = True
            if tweet_author_handle(url) and not tweet_author_handle(row["url"]):
                row["url"] = url[:300]
                changed = True
        if changed:
            persist_collected_unlocked()


def forget_collected(url: str) -> None:
    """Remove a saved tweet: it turned out to be by an ignored account, which leaves no trace."""
    status_id = tweet_status_id(url)
    if not status_id:
        return
    with collected_lock:
        if collected_tweets.pop(status_id, None) is not None:
            persist_collected_unlocked()


def refine_collected_links(results: list[dict]) -> None:
    """After a task, give each saved tweet the link it really opened at (it has the author's name)."""
    changed = False
    with collected_lock:
        for result in results or []:
            url = str(result.get("url") or "")
            status_id = tweet_status_id(url)
            row = collected_tweets.get(status_id) if status_id else None
            if row and tweet_author_handle(url) and not tweet_author_handle(row["url"]):
                row["url"] = url[:300]
                changed = True
        if changed:
            persist_collected_unlocked()


# Once per copy, when it starts: the tweets it worked on lately put in «التغريدات المجموعة» as if they had
# been collected then, so a copy that has not collected any yet (it was updated since) has them on its page.
# From its tasks of the last day: each tweet a group task collected, with its text and group, at the
# task's time; then from the tweets its accounts acted on in the last 8 hours (kept to skip them), the
# ones not found there, at their first action. Their views and content are then asked for.
COLLECTED_FILLED_PATH = DATA_DIR / "collected_filled.json"


def fill_collected_from_history() -> int:
    """Add those tweets (the ones not saved yet) and ask for their views; how many were added."""
    if COLLECTED_FILLED_PATH.exists():
        return 0
    found: dict[str, dict] = {}
    with jobs_lock:
        tasks = [dict(job) for job in jobs.values()]
    for job in tasks:
        group = re.sub(r"\s+", " ", str(job.get("username") or "")).strip()[:80]
        if job.get("kind") != "execute" or group == LINKS_TASK_NAME or not isinstance(job.get("items"), list):
            continue
        try:
            at = int(float(job.get("created_at") or 0))
        except (TypeError, ValueError):
            continue
        for item in job["items"]:
            url = str(item.get("url") or "") if isinstance(item, dict) else ""
            status_id = tweet_status_id(url)
            if not status_id or not at:
                continue
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            text = "" if text == "تغريدة" else text[:300]
            row = found.get(status_id)
            if row is None or at < row["at"]:
                found[status_id] = {"url": url[:300], "text": text or (row or {}).get("text", ""), "group": group, "at": at}
            elif text and not row["text"]:
                row["text"] = text
    with done_history_lock:
        acted: dict[str, float] = {}
        for tweets in done_history.values():
            for status_id, done in tweets.items():
                if done and re.fullmatch(r"\d{6,25}", status_id):
                    acted[status_id] = min(min(done.values()), acted.get(status_id, float("inf")))
    for status_id, at in acted.items():
        if status_id not in found:
            found[status_id] = {"url": f"https://x.com/i/web/status/{status_id}", "text": "", "group": "", "at": int(at)}
    now = time.time()
    added = []
    with collected_lock:
        for status_id, row in found.items():
            if status_id not in collected_tweets and now - row["at"] < COLLECTED_KEEP_SECONDS and not tweet_is_ignored(row["url"]):
                collected_tweets[status_id] = row
                added.append(status_id)
        if added:
            persist_collected_unlocked()
    try:
        write_private_json(COLLECTED_FILLED_PATH, {"at": now, "added": len(added)})
    except OSError:
        pass
    if added:
        queue_views(sorted(added, key=lambda status_id: -found[status_id]["at"]))
    return len(added)


def collected_payload() -> list[dict]:
    """The saved tweets for the page, newest first; ignored accounts left out."""
    with collected_lock:
        if prune_collected_unlocked(time.time()):
            persist_collected_unlocked()
        rows = [{"id": status_id, **row} for status_id, row in collected_tweets.items()]
    rows = [row for row in rows if not tweet_is_ignored(row["url"])]
    rows.sort(key=lambda row: row["at"], reverse=True)
    return rows


# «عرض الأكثر مشاهدة» in «عرض التغريدات»: the current view count of a day's tweets, from FxTwitter's free
# API (api.fxtwitter.com/status/<id>). It is asked FX_AT_ONCE tweets at a time, with no wait between
# them (a test asked 140 at once, then 140 one after another, and none was refused). If the service
# refuses (too many requests), or several tweets get no answer while none is answered, the fetching
# waits a few minutes (longer each time), then goes on where it stopped, one tweet at a time, slowly.
# The counts are kept with the saved tweets. Every day of the last 7 can be checked (today's too); once a
# day is asked for, its button waits VIEWS_AGAIN_SECONDS, then it can be asked for again (all its tweets).
FX_STATUS_URL = "https://api.fxtwitter.com/status/{}"
FX_AT_ONCE = 10                      # tweets asked at the same time
FX_SLOW_GAP_SECONDS = (5.0, 8.0)     # once the service has refused: one tweet at a time, this far apart
FX_PAUSE_SECONDS = 180               # the first wait; the next ones wait 6, 9, 12 then 15 minutes
FX_PAUSE_MAX_SECONDS = 900
FX_MAX_PAUSES = 6                    # waits in a row with no answer between them: then it stops
VIEWS_DAYS = (0, 6)                  # the days that can be checked: today to 6 days ago
VIEWS_AGAIN_SECONDS = 600            # a day asked for can be asked for again after 10 minutes
# With its views, a tweet's content is kept: its text, its pictures (a photo, or the still of a video or
# a GIF; from X's picture server only, at most 4) and the tweet it quotes, if any.
TWEET_PICTURE = re.compile(r"https://pbs\.twimg\.com/[A-Za-z0-9_./-]{1,200}(?:\?[A-Za-z0-9_=&.-]{1,80})?")
TWEET_HANDLE = re.compile(r"[A-Za-z0-9_]{1,30}")
TWEET_BODY_MAX = 2000
QUOTE_BODY_MAX = 1000
views_lock = threading.Lock()
views_queue: deque = deque()
views_state: dict = {"running": False, "paused_until": 0.0, "error": "", "removed": [], "slow": False}
# The days asked for in the last VIEWS_AGAIN_SECONDS: the start of the day (in the asker's time zone) → when.
views_asked: dict[int, float] = {}


def recent_views_asks(now: float) -> dict[int, float]:
    """The days still waiting after being asked for (the older ones are dropped). Call with views_lock held."""
    for start in [start for start, at in views_asked.items() if now - at >= VIEWS_AGAIN_SECONDS]:
        del views_asked[start]
    return dict(views_asked)


def minutes_text(seconds: float) -> str:
    """«دقيقة»، «دقيقتين»، «3 دقائق»، «11 دقيقة»: the minutes, rounded up."""
    minutes = max(1, math.ceil(seconds / 60))
    if minutes <= 2:
        return ("دقيقة", "دقيقتين")[minutes - 1]
    return f"{minutes} {'دقائق' if 3 <= minutes % 100 <= 10 else 'دقيقة'}"


def tweet_pictures(value) -> list[list[str]]:
    """Pictures as kept, [kind, address] with kind "photo" or "video" (its still): only X's picture
    server, at most 4."""
    found = []
    for pair in value if isinstance(value, list) else []:
        if (isinstance(pair, list) and len(pair) == 2 and pair[0] in ("photo", "video")
                and isinstance(pair[1], str) and TWEET_PICTURE.fullmatch(pair[1])):
            found.append([pair[0], pair[1]])
    return found[:4]


def tweet_content(row: dict) -> dict:
    """A saved tweet's content ("body", "pictures", and "quote": "by", "body", "pictures"), checked
    again; {} when it has none (its views were not fetched since this was added)."""
    if not isinstance(row.get("body"), str):
        return {}
    content = {"body": row["body"][:TWEET_BODY_MAX], "pictures": tweet_pictures(row.get("pictures"))}
    quoted = row.get("quote")
    if isinstance(quoted, dict):
        by = str(quoted.get("by") or "")
        content["quote"] = {"by": by if TWEET_HANDLE.fullmatch(by) else "", "body": str(quoted.get("body") or "")[:QUOTE_BODY_MAX],
                            "pictures": tweet_pictures(quoted.get("pictures"))}
    return content


def fx_content(tweet: dict) -> dict:
    """The content kept from FxTwitter's answer about a tweet (see tweet_content)."""
    def pictures(of: dict) -> list[list[str]]:
        media = of.get("media")
        found = []
        for item in (media.get("all") or []) if isinstance(media, dict) else []:
            if isinstance(item, dict) and item.get("type") in ("photo", "video", "gif"):
                found.append(["photo", item.get("url")] if item["type"] == "photo" else ["video", item.get("thumbnail_url")])
        return tweet_pictures(found)
    content = {"body": str(tweet.get("text") or "")[:TWEET_BODY_MAX], "pictures": pictures(tweet)}
    quoted = tweet.get("quote")
    if isinstance(quoted, dict):
        author = quoted.get("author") if isinstance(quoted.get("author"), dict) else {}
        by = str(author.get("screen_name") or "")
        content["quote"] = {"by": by if TWEET_HANDLE.fullmatch(by) else "", "body": str(quoted.get("text") or "")[:QUOTE_BODY_MAX],
                            "pictures": pictures(quoted)}
    return content


def fx_tweet_views(status_id: str) -> tuple[str, int | None, str, dict | None]:
    """Ask FxTwitter about one tweet. Returns (outcome, views, detail, content): ("ok", views or None,
    the tweet's link, its content) · ("gone", None, "", None) deleted, private or not found ·
    ("blocked", None, seconds to wait or "", None) too many requests · ("error", None, why, None) no
    usable answer."""
    asking = urllib.request.Request(FX_STATUS_URL.format(status_id), headers={
        "User-Agent": "x-group-tool/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(asking, timeout=20) as response:
            data = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(4096).decode("utf-8"))
        except Exception:
            body = None
        if exc.code == 429:
            return "blocked", None, str(exc.headers.get("Retry-After") or ""), None
        if isinstance(body, dict) and body.get("code") in (401, 403, 404, 410):
            return "gone", None, "", None    # FxTwitter's own answer about this tweet
        if exc.code in (403, 503):
            return "blocked", None, str(exc.headers.get("Retry-After") or ""), None
        if exc.code in (404, 410):
            return "gone", None, "", None
        return "error", None, f"HTTP {exc.code}", None
    except Exception as exc:                 # no answer, a cut connection, or not JSON
        return "error", None, type(exc).__name__, None
    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        return "gone", None, "", None
    views = tweet.get("views")
    if isinstance(views, bool) or not isinstance(views, (int, float)) or views < 0:
        views = None
    return "ok", None if views is None else int(views), str(tweet.get("url") or ""), fx_content(tweet)


def views_to_end_of_line(status_ids: list[str]) -> None:
    """These tweets are asked again after the others."""
    with views_lock:
        for status_id in status_ids:
            try:
                views_queue.remove(status_id)
            except ValueError:
                continue
            views_queue.append(status_id)


def views_worker() -> None:
    """The fetching thread. It marks itself finished under the lock when the line is empty, so a new
    request starts a new thread; if something unexpected breaks it, it stops cleanly the same way."""
    try:
        fetch_queued_views()
    except Exception:
        app.logger.warning("views fetching failed")
        with views_lock:
            views_queue.clear()
            views_state.update(running=False, paused_until=0.0, slow=False,
                               error="توقف جلب المشاهدات بسبب خطأ. اضغط «عرض الأكثر مشاهدة» ليكمل.")


def keep_views(answers: list[tuple[str, str, int | None, str, dict | None]]) -> None:
    """Keep what FxTwitter said of these tweets ((id, outcome, views, link, content)): the count, or
    «لا يوجد رقم» (deleted, private, no count, or no answer three times), and the tweet's content,
    then take them out of the line. A tweet whose author turns out to be an ignored account is
    removed, and the page is told."""
    ignored = {status_id for status_id, outcome, _, link, _ in answers if outcome == "ok" and link and tweet_is_ignored(link)}
    with collected_lock:
        for status_id, outcome, views, link, content in answers:
            row = collected_tweets.get(status_id)
            if row is None:
                continue
            if status_id in ignored:
                del collected_tweets[status_id]
                continue
            row["views"] = views if outcome == "ok" else None
            row["views_at"] = int(time.time())
            if outcome == "ok" and link and tweet_author_handle(link) and not tweet_author_handle(row["url"]):
                row["url"] = link[:300]
            if outcome == "ok" and content:
                for key in ("body", "pictures", "quote"):
                    row.pop(key, None)
                row.update(content)
            elif outcome == "gone":
                for key in ("body", "pictures", "quote"):
                    row.pop(key, None)       # deleted or private: nothing of it to show
        persist_collected_unlocked()
    with views_lock:
        for status_id, *_ in answers:
            try:
                views_queue.remove(status_id)
            except ValueError:
                pass
        if ignored:
            views_state["removed"] = (views_state["removed"] + [status_id for status_id, *_ in answers if status_id in ignored])[-500:]


def fetch_queued_views() -> None:
    """Fetch the queued tweets' view counts, FX_AT_ONCE at a time with no wait between them. A refusal
    (or no answer at all) makes it wait out the block, then go on one tweet at a time, slowly."""
    pauses = 0                       # waits in a row, with no answer between them
    failed: dict[str, int] = {}      # failed asks of each tweet
    failing: set[str] = set()        # the tweets that failed since the last answer
    slow = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=FX_AT_ONCE, thread_name_prefix="tweet-views") as pool:
        while True:
            with views_lock:
                if not views_queue:
                    views_state.update(running=False, paused_until=0.0, slow=False)
                    return
                asking = list(itertools.islice(views_queue, 1 if slow else FX_AT_ONCE))
            done, again, refused = [], [], []
            for status_id, (outcome, views, detail, content) in zip(asking, pool.map(fx_tweet_views, asking)):
                if outcome == "blocked":
                    refused.append(detail)           # stays where it is: asked again after the wait
                    continue
                if outcome == "error":
                    failed[status_id] = failed.get(status_id, 0) + 1
                    failing.add(status_id)
                    if failed[status_id] < 3:
                        again.append(status_id)      # asked again after the others
                        continue
                    # It keeps failing while others are answered: no count for it.
                done.append((status_id, outcome, views, detail if outcome == "ok" else "", content if outcome == "ok" else None))
            answered = any(outcome in ("ok", "gone") for _, outcome, *_ in done)
            if answered:
                pauses = 0
                failing.clear()
            if done:
                keep_views(done)
            views_to_end_of_line(again)
            if not refused and not answered and len(failing) >= 3:
                refused.append("")                   # three tweets and no answer: the service or the connection is down
            if refused:
                pauses += 1
                if pauses > FX_MAX_PAUSES:
                    with views_lock:
                        views_queue.clear()
                        views_state.update(running=False, paused_until=0.0, slow=False,
                                           error="توقف جلب المشاهدات لأن الخدمة لم تعد تجيب. اضغط «عرض الأكثر مشاهدة» لاحقًا ليكمل.")
                    return
                asked = 0
                for detail in refused:
                    try:
                        asked = max(asked, int(float(detail)))
                    except (ValueError, OverflowError):
                        pass                         # no number of seconds (empty, a date, "inf")
                wait = min(max(asked, FX_PAUSE_SECONDS * pauses), FX_PAUSE_MAX_SECONDS)
                slow = True
                with views_lock:
                    views_state.update(paused_until=time.time() + wait, slow=True)
                time.sleep(wait)
                with views_lock:
                    views_state["paused_until"] = 0.0
                continue
            if again and not answered:
                time.sleep(10)                       # a moment's trouble: try them again shortly
            elif slow:
                time.sleep(random.uniform(*FX_SLOW_GAP_SECONDS))


def queue_views(ids: list[str]) -> int:
    """Add tweets to the ones waiting for their view count; start fetching if it is not running."""
    with views_lock:
        waiting = set(views_queue)
        added = [status_id for status_id in dict.fromkeys(ids) if status_id not in waiting]
        views_queue.extend(added)
        views_state["error"] = ""
        if views_queue and not views_state["running"]:
            views_state.update(running=True, slow=False)
            threading.Thread(target=views_worker, name="tweet-views", daemon=True).start()
        return len(added)


def views_status_payload(since: float) -> dict:
    """How the fetching is going, and the counts fetched since `since` (a time this server gave)."""
    now = time.time()
    with views_lock:
        state = {"running": views_state["running"], "waiting": list(views_queue), "error": views_state["error"],
                 "paused": max(0, int(views_state["paused_until"] - now)), "removed": list(views_state["removed"]),
                 "slow": views_state["slow"]}
    with collected_lock:
        state["views"] = {status_id: [row.get("views"), row["views_at"], tweet_content(row) or None] for status_id, row in collected_tweets.items()
                          if row.get("views_at") and row["views_at"] >= since - 2}
    state["now"] = now
    return state


def local_day_number(seconds: float, offset_minutes: int) -> int:
    """The calendar day (days since 1970) a moment falls on, at the viewer's UTC offset."""
    return math.floor((seconds + offset_minutes * 60) / 86400)


def day_spans(data: dict) -> list[tuple[int, int]]:
    """The days the page asks about, as [start, end) in seconds (in the viewer's own time zone)."""
    spans = []
    for pair in (data.get("ranges") or [])[:10]:
        try:
            start, end = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 < end - start <= 26 * 3600:
            spans.append((start, end))
    return spans


# Updates. Users and their keys live in g.py beside the owner's script ("توزيع البرنامج"),
# which this script starts on its own port; a user's copy takes updates from it with its key.
CONTROL_FILE = Path(__file__).resolve().with_name("g.py")
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "5050"))
# Where «توزيع البرنامج» opens, when not this host on CONTROL_PORT (e.g. behind a domain).
CONTROL_URL = os.getenv("CONTROL_URL", "")
BACKUPS_DIR = DATA_DIR / "backups"
MAX_PROGRAM_BYTES = 8 * 1024 * 1024
# Set once g.py's page is up.
distribution_started = threading.Event()
# g.py's page, once this program serves it.
distribution_page = None
# Set while a user's copy installs an update: no new task starts.
updating = threading.Event()
update_check_cache: dict = {"at": 0.0, "answer": None}


def is_owner_copy() -> bool:
    """The owner's own copy has «توزيع البرنامج»; a copy given to a user takes updates."""
    return not UPDATE_SOURCE


def on_its_own() -> bool:
    """A copy on its own: the owner's kind of copy with no «توزيع البرنامج» (no g.py beside it, nor what that
    page saved), such as the script downloaded alone into a folder of its own."""
    return (is_owner_copy() and distribution_page is None and not CONTROL_FILE.is_file()
            and not (CONTROL_FILE.with_name("data") / "distribution.json").is_file())


def shown_version() -> str:
    """The version on the page: in the owner's copy, the one last published from «توزيع البرنامج»."""
    if is_owner_copy():
        published = (read_private_json(DATA_DIR / "distribution.json") or {}).get("published_version")
        if published:
            return str(published)
    return APP_VERSION


def start_distribution(bind_host: str) -> None:
    """Serve g.py's page («توزيع البرنامج») from this same program, on CONTROL_PORT, when g.py
    sits beside the owner's script. It hands out this script."""
    if not is_owner_copy() or not CONTROL_FILE.is_file():
        return
    try:
        spec = importlib.util.spec_from_file_location("distribution_page", CONTROL_FILE)
        control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(control)
        control.PROGRAM_PATH = Path(__file__).resolve()
        server = make_server(bind_host, CONTROL_PORT, control.app, threaded=True)
    except (Exception, SystemExit) as exc:
        # Werkzeug exits the program when the port is taken; the script keeps running without the page.
        reason = "المنفذ مستخدم من برنامج آخر" if isinstance(exc, SystemExit) else str(exc)
        print(f"لم تبدأ صفحة التوزيع ({CONTROL_FILE.name}) على المنفذ {CONTROL_PORT}: {reason}", flush=True)
        return
    threading.Thread(target=server.serve_forever, name="distribution", daemon=True).start()
    global distribution_page
    distribution_page = control             # (its choice of who writes the comments is this copy's too)
    distribution_started.set()
    print(f"صفحة التوزيع ({CONTROL_FILE.name}) على المنفذ {CONTROL_PORT}", flush=True)


def version_of(text: str) -> str:
    match = re.search(r'^APP_VERSION = "([0-9][0-9.]*)"', text, re.M)
    return match.group(1) if match else ""


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version or "")) or (0,)


def ask_update_source(path: str, timeout: float, report: dict | None = None) -> tuple[int, bytes]:
    """A user's copy asks the owner's g.py (posting report when given). Returns (HTTP status, body);
    status 0 when unreachable."""
    headers = {"X-Update-Key": UPDATE_KEY, "User-Agent": "x-group-update"}
    body = None
    if report is not None:
        body = json.dumps(report).encode("utf-8")
        headers["Content-Type"] = "application/json"
    asking = urllib.request.Request(UPDATE_SOURCE.rstrip("/") + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(asking, timeout=timeout) as answer:
            return answer.status, answer.read(MAX_PROGRAM_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096)
    except (urllib.error.URLError, OSError, ValueError):
        return 0, b""


# A user's copy checks in with the owner this often, and when its page opens.
OWNER_SYNC_EVERY = 120
owner_sync_lock = threading.Lock()
owner_sync_state = {"at": 0.0, "ok": False}


def apply_owner_gemini(key: str, model: str = "") -> None:
    """Use the Gemini key and model the owner set for this copy; when the owner deleted the key, drop
    it here too. With no model from the owner, the copy keeps its own."""
    model = normalize_gemini_model(model) or ""
    with gemini_lock:
        current, managed = saved_gemini.get("api_key") or "", bool(saved_gemini.get("managed"))
        changed = False
        if key and valid_gemini_key(key):
            if key != current or not managed:
                saved_gemini.update(api_key=key, managed=True)
                changed = True
        elif managed and current:
            saved_gemini.update(api_key="", managed=False)
            changed = True
        if model and model != saved_gemini.get("model"):
            saved_gemini["model"] = model
            changed = True
        if changed:
            persist_gemini_unlocked()


def sync_with_owner() -> None:
    """A user's copy checks in with the owner's g.py: it reports its Gemini comments and cost, takes
    the Gemini key the owner set for it (or drops the one deleted), and resets its cost when asked."""
    if is_owner_copy() or not owner_sync_lock.acquire(blocking=False):
        return
    try:
        for _ in range(2):  # after a reset, report the new cost straight away
            with gemini_usage_lock:
                done = str(gemini_usage.get("reset_id") or "")
                report = {
                    # Since the cost was last reset, like the cost beside it in «إدارة».
                    "comments": int(gemini_usage.get("cost_comments") or 0),
                    "cost": round(int(gemini_usage.get("cost_comments") or 0) * GEMINI_PRICE_PER_COMMENT, 6),
                    "reset_id": done,
                    # Since this copy began (never reset): its total cost from the beginning.
                    "total_comments": int(gemini_usage.get("comments") or 0),
                    "total_cost": round(int(gemini_usage.get("comments") or 0) * GEMINI_PRICE_PER_COMMENT, 6),
                }
            status, body = ask_update_source("/user/sync", 15, report)
            try:
                answer = json.loads(body.decode("utf-8")) if status == 200 else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                answer = {}
            ok = isinstance(answer, dict) and bool(answer.get("success"))
            if ok:
                # The keys, into memory only (see apply_owner_gemini, apply_owner_comments).
                apply_owner_gemini(str(answer.get("gemini_key") or ""), str(answer.get("gemini_model") or ""))
                engine = str(answer.get("comments_engine") or "")
                kind = "claude" if engine == "claude" else "grok"
                apply_owner_comments(engine, str(answer.get(kind + "_key") or ""), str(answer.get(kind + "_model") or ""))
            # (Set once the answer is applied: whoever waits for the keys sees them with it.)
            owner_sync_state.update(at=time.monotonic(), ok=ok)
            if not ok:
                return
            if isinstance(answer.get("skip_users"), list):
                store_skip_users(answer["skip_users"])
            wanted = str(answer.get("reset_id") or "")
            if not wanted or wanted == done:
                return
            reset_gemini_cost(wanted)
    finally:
        owner_sync_lock.release()


def owner_keys_ready(gemini_only: bool = False) -> bool:
    """A user's copy has what writes its comments: Grok (the owner's choice) or its Gemini key; with
    gemini_only, its Gemini key (for «إنشاء محتوى»)."""
    with gemini_lock:
        gemini = valid_gemini_key(str(saved_gemini.get("api_key") or ""))
    return gemini or (not gemini_only and not gemini_writes_comments())


def wait_for_owner_keys(seconds: float = 10.0, gemini_only: bool = False) -> bool:
    """A user's copy keeps its keys in memory only, from its check-ins: when it has none (it has just
    started, or the owner could not be reached), it checks in now and waits a little for them. It stops
    waiting as soon as the owner answers without them. True when it has them."""
    if is_owner_copy() or owner_keys_ready(gemini_only):
        return True
    began = time.monotonic()
    while True:
        sync_with_owner()                      # (returns at once while another check-in is under way)
        answered = owner_sync_state["ok"] and owner_sync_state["at"] >= began
        if owner_keys_ready(gemini_only) or answered or time.monotonic() - began >= seconds:
            return owner_keys_ready(gemini_only)
        time.sleep(1)


def no_keys_message() -> str:
    """Why a user's copy has no key for its comments."""
    if not owner_sync_state.get("ok"):
        return "تعذّر الاتصال بصاحب البرنامج لجلب مفتاح التعليقات. حاول بعد قليل."
    return "لا يوجد مفتاح للتعليقات في هذه النسخة. اطلبه من صاحب البرنامج."


def owner_sync_loop() -> None:
    while True:
        try:
            sync_with_owner()
        except Exception:
            pass
        time.sleep(OWNER_SYNC_EVERY)


def latest_update() -> dict:
    """The version the owner offers this copy {"version", "sha256"}, or {"message"} saying why not."""
    status, body = ask_update_source("/update/latest", 15)
    try:
        answer = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        answer = {}
    if status == 200 and isinstance(answer, dict) and answer.get("version"):
        return {"version": str(answer["version"]), "sha256": str(answer.get("sha256") or "")}
    message = answer.get("message") if isinstance(answer, dict) else ""
    return {"message": str(message or "تعذّر الاتصال بمصدر التحديث.")}


def new_program_problem(body: bytes, latest: dict) -> str:
    """Why a downloaded program must not be installed; "" when it is fine."""
    if not body or len(body) > MAX_PROGRAM_BYTES:
        return "لم يصل ملف التحديث كاملًا. أعد المحاولة."
    if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), latest.get("sha256") or ""):
        return "لم يصل ملف التحديث كاملًا. أعد المحاولة."
    try:
        text = body.decode("utf-8")
        compile(text, "update.py", "exec")
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return "ملف التحديث غير صالح. لم يتغير شيء."
    if version_of(text) != latest.get("version"):
        return "رقم إصدار ملف التحديث لا يطابق. لم يتغير شيء."
    if not re.search(r'^UPDATE_SOURCE = "https?://[^"\s]+"$', text, re.M) or not re.search(r'^UPDATE_KEY = "[^"\s]+"$', text, re.M):
        return "ملف التحديث بلا عنوان أو مفتاح. لم يتغير شيء."
    return ""


def program_starts(body: bytes) -> bool:
    """Load the new program once in a separate Python, in a scratch folder, to see that it starts."""
    trial = BACKUPS_DIR / "trial"
    shutil.rmtree(trial, ignore_errors=True)
    try:
        trial.mkdir(parents=True)
        program = trial / "program.py"
        program.write_bytes(body)
        load = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('trial', sys.argv[1]); "
                "spec.loader.exec_module(importlib.util.module_from_spec(spec))")
        result = subprocess.run([sys.executable, "-c", load, str(program)], cwd=trial, capture_output=True, timeout=90)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(trial, ignore_errors=True)


def install_program(body: bytes) -> None:
    """Keep the running file in data/backups, then put the new one in its place."""
    script = Path(__file__).resolve()
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(script, BACKUPS_DIR / f"{script.stem}-{APP_VERSION}.py")
    temporary = script.with_name(script.name + ".new")
    temporary.write_bytes(body)
    try:
        shutil.copymode(script, temporary)
    except OSError:
        pass
    temporary.replace(script)


def restart_program() -> None:
    """Run the program again in this same process, so the new file takes over."""
    time.sleep(1.5)
    try:
        shutdown_jobs()
    except Exception:
        pass
    # Flask's server leaves its listening socket open across exec; close it there so the new
    # program can listen on the same port.
    server_fd = os.environ.pop("WERKZEUG_SERVER_FD", "")
    if server_fd.isdigit():
        try:
            os.set_inheritable(int(server_fd), False)
        except OSError:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    command = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    os.execv(sys.executable, [sys.executable, *command[1:]])


def remember_gemini(api_key: str, model: str) -> None:
    key = (api_key or "").strip()
    normalized = normalize_gemini_model(model or "") or saved_gemini.get("model") or "gemini-3.5-flash"
    if key and not valid_gemini_key(key):
        return
    with gemini_lock:
        if key and key != saved_gemini.get("api_key"):
            # A key typed on this page is this copy's own, not the owner's.
            saved_gemini["api_key"] = key
            saved_gemini["managed"] = False
        saved_gemini["model"] = normalized
        persist_gemini_unlocked()


# A user's copy never keeps an API key on its disk: the owner's Gemini and Grok keys come with each
# check-in and stay in the running program's memory only (a copy just started asks for them at once). The
# owner's own copy keeps the Gemini key typed in its panel in data/gemini.json, sealed, so the file shows
# nothing readable: it is encrypted with a key made from a secret in this program and this copy's update key
# (a stream from SHA-256 with a random nonce each time it is saved, and a tag that tells a damaged file).
# A file from before, with a key in the clear (or a user's copy's sealed one), is rewritten without it.
KEY_SEAL_SECRET = bytes.fromhex("d8867d215ab7fb097f3a535a380696b36506ee694e0ef27fdba0c4f8fd77b658")


def key_seal_material() -> bytes:
    return hashlib.sha256(KEY_SEAL_SECRET + UPDATE_KEY.encode("utf-8")).digest()


def key_stream(material: bytes, nonce: bytes, length: int) -> bytes:
    stream = bytearray()
    counter = 0
    while len(stream) < length:
        stream += hashlib.sha256(material + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(stream[:length])


def seal_key(text: str) -> str:
    """A key as it is kept on disk ("" for none)."""
    if not text:
        return ""
    material, nonce, raw = key_seal_material(), os.urandom(16), text.encode("utf-8")
    body = bytes(a ^ b for a, b in zip(raw, key_stream(material, nonce, len(raw))))
    tag = hmac.new(material, nonce + body, hashlib.sha256).digest()[:16]
    return "s1:" + base64.b64encode(nonce + tag + body).decode("ascii")


def open_key(sealed) -> str:
    """The key a sealed one holds; "" when there is none, or it is damaged or another copy's."""
    if not isinstance(sealed, str) or not sealed.startswith("s1:"):
        return ""
    try:
        data = base64.b64decode(sealed[3:], validate=True)
    except ValueError:
        return ""
    if len(data) <= 32:
        return ""
    nonce, tag, body = data[:16], data[16:32], data[32:]
    material = key_seal_material()
    if not hmac.compare_digest(tag, hmac.new(material, nonce + body, hashlib.sha256).digest()[:16]):
        return ""
    try:
        return bytes(a ^ b for a, b in zip(body, key_stream(material, nonce, len(body)))).decode("utf-8")
    except UnicodeDecodeError:
        return ""


def persist_gemini_unlocked() -> None:
    # The owner's copy keeps its own key, sealed; a user's copy keeps the owner's key in memory only, from its
    # check-ins, never on its disk.
    sealed = seal_key(saved_gemini["api_key"]) if is_owner_copy() else ""
    write_private_json(GEMINI_PATH, {"version": 2, "api_key_sealed": sealed, "model": saved_gemini["model"],
                                     "managed": bool(saved_gemini.get("managed"))})


def load_gemini() -> None:
    global saved_gemini
    loaded = read_private_json(GEMINI_PATH) or {}
    owner = is_owner_copy()
    plain = str(loaded.get("api_key") or "").strip()
    sealed = loaded.get("api_key_sealed")
    # A user's copy reads no key from its disk: the owner's comes with its first check-in.
    key = ((open_key(sealed).strip() if sealed else plain) if owner else "")
    model = normalize_gemini_model(str(loaded.get("model") or "")) or "gemini-3.5-flash"
    if key and not valid_gemini_key(key):
        key = ""
    # managed: the owner set this key from «توزيع البرنامج», and may delete it from there.
    saved_gemini = {"api_key": key, "model": model, "managed": bool(key and loaded.get("managed"))}
    if plain or (sealed and not owner):
        with gemini_lock:
            persist_gemini_unlocked()      # the owner's key now sealed; a user's copy: the key taken off its disk


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


def saved_username(auth_token: str) -> str:
    """The account's name saved with this session, or ""."""
    token = (auth_token or "").strip()
    with sessions_lock:
        for row in saved_sessions:
            if row.get("token") == token and row.get("username"):
                return str(row["username"])
    return ""


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


def public_session(row: dict) -> dict:
    token = row["token"]
    hint = token[:4] + "…" + token[-4:] if len(token) >= 12 else "••••"
    return {
        "id": row["id"],
        "username": row.get("username") or "",
        "hint": hint,
        "token": token,
        "chat_pin": row.get("chat_pin") or "",
        "verified_at": row.get("verified_at") or 0,
    }


def request_client_id() -> str:
    raw = (request.headers.get("X-Client-Id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{16,80}", raw):
        return raw
    return ""


def job_record(job: dict) -> dict:
    return {field: job.get(field) for field in JOB_FIELDS}


# Task progress is saved to jobs.json at most this often; a change of status is saved at once.
JOBS_SAVE_EVERY = 2.0
jobs_saved_at = 0.0
jobs_unsaved = False


def persist_jobs_unlocked() -> None:
    global jobs_saved_at, jobs_unsaved
    write_private_json(JOBS_PATH, {"version": 1, "jobs": {key: job_record(job) for key, job in jobs.items()}}, compact=True)
    jobs_saved_at = time.monotonic()
    jobs_unsaved = False


def save_jobs_in_background() -> None:
    """Write progress that was held back by the 2-second limit."""
    while True:
        time.sleep(JOBS_SAVE_EVERY)
        try:
            with jobs_lock:
                if jobs_unsaved:
                    persist_jobs_unlocked()
        except Exception:
            pass


def mark_job_interrupted(job: dict) -> None:
    if job.get("status") != "running":
        return
    previous = (job.get("message") or "").strip()
    job["status"] = "interrupted"
    job["success"] = False
    job["updated_at"] = time.time()
    job["message"] = "توقفت العملية مع إيقاف السكربت ولم تُستكمل." + (f" آخر حالة: {previous}" if previous else "")


def load_jobs() -> None:
    loaded = read_private_json(JOBS_PATH)
    rows = loaded.get("jobs") if loaded else None
    if not isinstance(rows, dict):
        return
    now = time.time()
    with jobs_lock:
        for job_id, row in rows.items():
            if not isinstance(job_id, str) or not isinstance(row, dict):
                continue
            job = job_record(row)
            job["items"] = job["items"] if isinstance(job["items"], list) else []
            job["results"] = job["results"] if isinstance(job["results"], list) else []
            job["actions"] = job["actions"] if isinstance(job["actions"], list) else []
            if job.get("status") not in {"running", "done", "interrupted"}:
                job["status"] = "interrupted"
            mark_job_interrupted(job)
            if job.get("status") != "running" and now - float(job.get("updated_at") or 0) > JOB_TTL_SECONDS:
                continue
            jobs[job_id] = job
        persist_jobs_unlocked()


def shutdown_jobs() -> None:
    with jobs_lock:
        for job in jobs.values():
            mark_job_interrupted(job)
        if jobs:
            persist_jobs_unlocked()


def prune_jobs() -> None:
    now = time.time()
    stale = [
        key for key, job in jobs.items()
        if job.get("status") != "running" and now - float(job.get("updated_at") or job.get("created_at") or now) > JOB_TTL_SECONDS
    ]
    for key in stale:
        jobs.pop(key, None)
    if len(jobs) > 40:
        finished = sorted(
            (key for key, job in jobs.items() if job.get("status") != "running"),
            key=lambda key: float(jobs[key].get("updated_at") or 0),
        )
        for key in finished[: max(0, len(jobs) - 40)]:
            jobs.pop(key, None)


def publish_job(job_id: str, **fields) -> None:
    global jobs_unsaved
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()
        note_job(job_id, fields)
        if "status" in fields or time.monotonic() - jobs_saved_at >= JOBS_SAVE_EVERY:
            persist_jobs_unlocked()
        else:
            jobs_unsaved = True


def begin_job(owner: str, client_id: str, requested: int, phase: str, message: str, kind: str, username: str) -> tuple[str, str]:
    job_id = secrets.token_urlsafe(18)
    job_key = secrets.token_urlsafe(24)
    now = time.time()
    with jobs_lock:
        prune_jobs()
        jobs[job_id] = {
            "owner": owner,
            "client_id": client_id,
            "created_at": now,
            "updated_at": now,
            "status": "running",
            "phase": phase,
            "kind": kind,
            "collected": 0,
            "done": 0,
            "requested": requested,
            "message": message,
            "success": None,
            "username": username,
            "actions": [],
            "items": [],
            "results": [],
            "key_hash": hashlib.sha256(job_key.encode("utf-8")).hexdigest(),
        }
        persist_jobs_unlocked()
    return job_id, job_key


def job_key_matches(job: dict, presented: str) -> bool:
    if not presented or len(presented) > 200:
        return False
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    expected = str(job.get("key_hash") or "")
    return bool(expected) and hmac.compare_digest(digest, expected)


def public_job(job: dict, light: bool = False, links: bool = False) -> dict:
    """light: while the task runs, leave out its tweet and result lists (the page only
    needs the counts and message then; the full lists are sent once it finishes).
    links: add the extracted tweets' links (the task card's copy button asks once)."""
    payload = {
        "status": job["status"],
        "phase": job.get("phase") or "collect",
        "kind": job.get("kind") or "preview",
        "collected": job.get("collected") or 0,
        "done": job.get("done") or 0,
        "requested": job.get("requested") or 0,
        "requested_count": job.get("requested") or 0,
        "message": job.get("message") or "",
        "success": job.get("success"),
        "username": job.get("username") or "",
        "actions": job.get("actions") or [],
        "items": job.get("items") or [],
        "results": job.get("results") or [],
        "resumable": bool(job.get("resumable")) and job.get("status") == "done",
    }
    if light and job["status"] == "running":
        payload["items"] = []
        payload["results"] = []
    if links:
        payload["links"] = [item["url"] for item in job.get("items") or [] if isinstance(item, dict) and item.get("url")]
    return payload


SCREEN_PATH = DATA_DIR / "screen.json"
screen_lock = threading.Lock()
screen: dict = {
    "auth_token": "",
    "chat_pin": "",
    "groups": [],
    "group": "",
    "stop_tweet": "",
    "actions": ["repost", "like", "bookmark"],
    "job_id": "",
    "job_key": "",
    "started_at": 0,
}


def load_screen() -> None:
    loaded = read_private_json(SCREEN_PATH) or {}
    with screen_lock:
        groups = loaded.get("groups")
        if isinstance(groups, list):
            screen["groups"] = [re.sub(r"\s+", " ", str(name)).strip() for name in groups if str(name).strip()][:200]
        for key in ("auth_token", "chat_pin", "group", "job_id", "job_key"):
            value = str(loaded.get(key) or "").strip()
            if value:
                screen[key] = value
        actions = loaded.get("actions")
        if isinstance(actions, list):
            picked = [action for action in actions if isinstance(action, str) and action in ALLOWED_ACTIONS]
            if picked:
                screen["actions"] = list(dict.fromkeys(picked))
        screen["stop_tweet"] = str(loaded.get("stop_tweet") or "")
        try:
            screen["started_at"] = float(loaded.get("started_at") or 0)
        except (TypeError, ValueError):
            screen["started_at"] = 0


def save_screen(**fields) -> None:
    with screen_lock:
        for key, value in fields.items():
            if key in screen:
                screen[key] = value
        write_private_json(SCREEN_PATH, {"version": 1, **screen})


def running_job_rows(limit: int = 5) -> list[dict]:
    with jobs_lock:
        running = [(job_id, job) for job_id, job in jobs.items() if job.get("status") == "running"]
    running.sort(key=lambda pair: float(pair[1].get("created_at") or 0), reverse=True)
    rows = []
    for job_id, job in running[:limit]:
        row = public_job(job, light=True)
        row["id"] = job_id
        rows.append(row)
    return rows


def screen_payload() -> dict:
    with screen_lock:
        payload = {
            "auth_token": screen.get("auth_token") or "",
            "chat_pin": screen.get("chat_pin") or "",
            "groups": list(screen.get("groups") or []),
            "group": screen.get("group") or "",
            "stop_tweet": screen.get("stop_tweet") or "",
            "actions": list(screen.get("actions") or []),
            "job_id": screen.get("job_id") or "",
            "job_key": screen.get("job_key") or "",
            "started_at": screen.get("started_at") or 0,
        }
    job_id = payload["job_id"]
    with jobs_lock:
        job = jobs.get(job_id)
        payload["job"] = public_job(job, light=True) if job else None
    payload["running_jobs"] = running_job_rows()
    payload["success"] = True
    return payload


@app.get("/<access_path>/state")
def state_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    return jsonify(screen_payload())


@app.post("/<access_path>/screen")
def save_screen_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    fields: dict = {}
    token = str(data.get("auth_token") or "").strip()
    if token == "" or valid_auth_token(token):
        fields["auth_token"] = token
    pin = str(data.get("chat_pin") or "").strip()
    if valid_chat_pin(pin):
        fields["chat_pin"] = pin
    group_name = re.sub(r"\s+", " ", str(data.get("group") or "")).strip()
    if group_name and len(group_name) <= 80:
        fields["group"] = group_name
    if "stop_tweet" in data:
        fields["stop_tweet"] = str(data.get("stop_tweet") or "").strip()[:300]
    actions = data.get("actions")
    if isinstance(actions, list):
        picked = [action for action in actions if isinstance(action, str) and action in ALLOWED_ACTIONS]
        if picked:
            fields["actions"] = list(dict.fromkeys(picked))
    groups = data.get("groups")
    if isinstance(groups, list) and groups:
        fields["groups"] = [re.sub(r"\s+", " ", str(name)).strip() for name in groups if str(name).strip()][:200]
    if fields:
        save_screen(**fields)
    return jsonify(success=True)


@app.post("/<access_path>/groups")
def groups_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    auth_token = str(data.get("auth_token", "")).strip()
    pin = str(data.get("chat_pin", "")).strip()
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة يجب أن يكون من ٤ إلى ٨ أرقام."), 400
    settle = bool(data.get("wait"))
    settle_seconds = parse_requested_count(data.get("wait_seconds")) or 10
    settle_seconds = max(1, min(120, settle_seconds))
    try:
        names, error = fetch_group_names(auth_token, pin, settle, settle_seconds)
    except Exception:
        app.logger.warning("group list failed")
        trace_error("فشل جلب القروبات")
        return jsonify(success=False, message="تعذّر فتح الدردشة."), 502
    if error:
        return jsonify(success=False, message=error, groups=[])
    current_group = ""
    with screen_lock:
        current_group = str(screen.get("group") or "")
    if current_group not in names:
        current_group = ""
    save_screen(auth_token=auth_token, chat_pin=pin, groups=names, group=current_group)
    return jsonify(success=True, groups=names, message=f"ظهر {len(names)} قروب.")


@app.post("/<access_path>/group-run")
def group_run_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    auth_token = str(data.get("auth_token", "")).strip()
    pin = str(data.get("chat_pin", "")).strip()
    # «روابط التغريدات»: the page sends the pasted tweets instead of a group (no chat PIN needed then).
    by_links = data.get("links") is not None
    tweet_links: list[dict] = []
    if by_links:
        group_name, stop_raw, stop_id = LINKS_TASK_NAME, "", ""
        if not valid_chat_pin(pin):
            pin = ""
    else:
        group_name = re.sub(r"\s+", " ", str(data.get("group", ""))).strip()
        stop_raw = str(data.get("stop_tweet") or "").strip()
        stop_id = extract_status_id(stop_raw) if stop_raw else ""
    requested_actions = data.get("actions")
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    if by_links:
        if not isinstance(data.get("links"), list) or len(data["links"]) > MAX_TWEET_LINKS:
            return jsonify(success=False, message=f"الحد الأقصى {MAX_TWEET_LINKS} رابط في المهمة الواحدة."), 400
        tweet_links = tweet_links_from(data["links"])
        if not tweet_links:
            return jsonify(success=False, message="الصق رابط تغريدة واحدًا على الأقل."), 400
        tweet_links = [item for item in tweet_links if not tweet_is_ignored(item["url"])]
        if not tweet_links:
            return jsonify(success=False, message="لا توجد تغريدات للتنفيذ."), 400
    else:
        if not valid_chat_pin(pin):
            return jsonify(success=False, message="رمز الدردشة يجب أن يكون من ٤ إلى ٨ أرقام."), 400
        if not group_name or len(group_name) > 80:
            return jsonify(success=False, message="اختر قروبًا من القائمة."), 400
        if stop_raw and not stop_id:
            return jsonify(success=False, message="رابط التغريدة غير صالح. الصق رابطها أو رقمها."), 400
        if not stop_id:
            return jsonify(success=False, message="الصق رابط التغريدة المحددة أو رقمها."), 400
    requested_count = len(tweet_links)
    if not isinstance(requested_actions, list) or not requested_actions or any(not isinstance(action, str) or action not in ALLOWED_ACTIONS for action in requested_actions):
        return jsonify(success=False, message="اختر إجراء واحدًا على الأقل."), 400
    group_wait_raw = data.get("group_wait")
    group_wait_s = DEFAULT_GROUP_WAIT_SECONDS
    if group_wait_raw is not None:
        parsed_wait = parse_requested_count(group_wait_raw)
        if parsed_wait is not None:
            group_wait_s = max(0, min(300, parsed_wait))
    profiles_per_account = parse_requested_count(data.get("account_profiles")) or 1
    profiles_per_account = max(1, min(MAX_PROFILES_PER_ACCOUNT, profiles_per_account))
    reply_voice = str(data.get("reply_voice") or DEFAULT_REPLY_VOICE)
    if reply_voice not in REPLY_VOICES:
        reply_voice = DEFAULT_REPLY_VOICE
    phone_height = 0
    if data.get("phone_screen"):
        phone_height = max(600, min(25000, parse_requested_count(data.get("phone_height")) or PHONE_SCREEN["height"]))
    requested_actions = list(dict.fromkeys(requested_actions))
    gemini_api_key = str(data.get("gemini_api_key") or "").strip()
    gemini_model = normalize_gemini_model(str(data.get("gemini_model") or ""))
    if not is_owner_copy():
        # A user's copy uses the Gemini key and model the owner set; what the page sends is ignored.
        gemini_api_key, gemini_model = "", ""
    if "comment" in requested_actions and not is_owner_copy():
        wait_for_owner_keys()                  # (a copy just started gets them now)
    if "comment" in requested_actions and IN_APP and comments_writer()[0] != "server":
        return jsonify(success=False, message=NO_SERVER_LINK), 400
    if "comment" in requested_actions and gemini_writes_comments():
        if not gemini_api_key:
            gemini_api_key = saved_gemini.get("api_key") or ""
        if not gemini_model:
            gemini_model = normalize_gemini_model(saved_gemini.get("model") or "")
        if not valid_gemini_key(gemini_api_key) or not gemini_model:
            if not is_owner_copy():
                return jsonify(success=False, message=no_keys_message()), 400
            return jsonify(success=False, message="أدخل مفتاح Gemini واسم النموذج قبل التعليق."), 400
        if is_owner_copy():
            remember_gemini(gemini_api_key, gemini_model or "")
    elif "comment" in requested_actions and is_owner_copy() and valid_gemini_key(gemini_api_key) and gemini_model:
        remember_gemini(gemini_api_key, gemini_model)      # (Grok writes the comments; the key typed is kept all the same)
    if updating.is_set():
        return jsonify(success=False, message="يجري تحديث البرنامج. انتظر قليلًا ثم أعد المحاولة."), 503
    with jobs_lock:
        running = [job for job in jobs.values() if job.get("status") == "running" and job.get("kind") == "execute"]
    if len(running) >= MAX_PARALLEL_JOBS:
        return jsonify(success=False, message=f"أقصى عدد للمهام المتزامنة هو {MAX_PARALLEL_JOBS}. أوقف مهمة أو انتظر انتهائها."), 429
    owner = session.get("csrf_token", "")
    client_id = request_client_id()
    if by_links:
        job_id, job_key = begin_job(owner, client_id, requested_count, "prepare", "التحقق من الجلسة…", "execute", group_name)
        # The group chosen on the page, and its tweet, stay as they were for the next group task.
        save_screen(auth_token=auth_token, actions=requested_actions, job_id=job_id, job_key=job_key, started_at=time.time())
    else:
        job_id, job_key = begin_job(owner, client_id, requested_count, "login", "فتح القروب…", "execute", group_name)
        save_screen(
            auth_token=auth_token,
            chat_pin=pin,
            group=group_name,
            stop_tweet=stop_raw,
            actions=requested_actions,
            job_id=job_id,
            job_key=job_key,
            started_at=time.time(),
        )
    threading.Thread(
        target=run_group_job,
        args=(job_id, auth_token, pin, group_name, requested_count, requested_actions, gemini_api_key, gemini_model or "", stop_id, group_wait_s, profiles_per_account, phone_height, reply_voice, tweet_links),
        daemon=True,
    ).start()
    return jsonify(success=True, job_id=job_id, job_key=job_key, message="بدأ التنفيذ.")


@app.post("/<access_path>/retry")
def retry_job_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    job_id = str(data.get("job_id") or "").strip()
    job_key = str(data.get("job_key") or "").strip()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(success=False, message="لم يعد سجل هذه المهمة متاحًا."), 404
        key_ok = job_key_matches(job, job_key)
        owner_ok = bool(job.get("owner")) and job.get("owner") == session.get("csrf_token", "")
        if not key_ok and not owner_ok:
            return jsonify(success=False, message="لم يعد سجل هذه المهمة متاحًا."), 404
        if job.get("status") == "running":
            return jsonify(success=True, message="المهمة تعمل الآن.")
        args = job_run_args.get(job_id)
        if not job.get("resumable") or not args:
            return jsonify(success=False, message="لا يمكن إعادة هذه المهمة. ابدأ مهمة جديدة."), 400
        if updating.is_set():
            return jsonify(success=False, message="يجري تحديث البرنامج. انتظر قليلًا ثم أعد المحاولة."), 503
        running = [row for row in jobs.values() if row.get("status") == "running" and row.get("kind") == "execute"]
        if len(running) >= MAX_PARALLEL_JOBS:
            return jsonify(success=False, message=f"أقصى عدد للمهام المتزامنة هو {MAX_PARALLEL_JOBS}. أوقف مهمة أو انتظر انتهائها."), 429
        job["status"] = "running"
        job["success"] = None
        job["resumable"] = False
        job["cancel"] = False
        job["message"] = f"إعادة المحاولة من التغريدة {len(job.get('results') or []) + 1}…"
        job["updated_at"] = time.time()
        persist_jobs_unlocked()
    threading.Thread(target=run_group_job, args=(job_id, *args), daemon=True).start()
    return jsonify(success=True, message="بدأت إعادة المحاولة.")


@app.post("/<access_path>/stop")
def stop_job_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    job_id = str(data.get("job_id") or "").strip()
    job_key = str(data.get("job_key") or "").strip()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(success=False, message="لا توجد عملية تعمل الآن."), 404
        key_ok = job_key_matches(job, job_key)
        owner_ok = bool(job.get("owner")) and job.get("owner") == session.get("csrf_token", "")
        if not key_ok and not owner_ok:
            return jsonify(success=False, message="لا توجد عملية تعمل الآن."), 404
        if job.get("status") != "running":
            return jsonify(success=True, message="التنفيذ متوقف أصلًا.")
        job["cancel"] = True
        job["message"] = "جارٍ إيقاف التنفيذ…"
        job["updated_at"] = time.time()
        persist_jobs_unlocked()
    return jsonify(success=True, message="جارٍ إيقاف التنفيذ.")


@app.get("/<access_path>/jobs/<job_id>/view")
def job_view(access_path: str, job_id: str):
    """The latest picture of a running task's browser, if newer than ?after=; asking keeps pictures coming for 3 s."""
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    with jobs_lock:
        job = jobs.get(job_id)
        allowed = bool(job) and (
            job_key_matches(job, request.headers.get("X-Job-Key", ""))
            or (csrf_matches(request.headers.get("X-CSRF-Token", ""))
                and bool(job.get("owner")) and job.get("owner") == session.get("csrf_token", ""))
        )
        running = allowed and job.get("status") == "running"
    if not allowed:
        return jsonify(success=False, message="لا توجد عملية بهذا المعرّف."), 404
    if not running:
        return "", 204
    try:
        after = int(request.args.get("after") or 0)
    except ValueError:
        after = 0
    with live_views_lock:
        view = live_views.setdefault(job_id, {"wanted_until": 0.0, "taken_at": 0.0, "image": None, "seq": 0})
        view["wanted_until"] = time.monotonic() + 3
        image, seq = view["image"], view["seq"]
    if not image or seq <= after:
        return "", 204
    return image, 200, {"Content-Type": "image/jpeg", "X-Frame": str(seq)}


@app.get("/<access_path>/jobs/<job_id>")
def job_status(access_path: str, job_id: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    light = request.args.get("light") == "1"
    links = request.args.get("links") == "1"
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(success=False, message="لا توجد عملية بهذا المعرّف."), 404
        if job_key_matches(job, request.headers.get("X-Job-Key", "")):
            return jsonify(public_job(job, light=light, links=links))
        if not csrf_matches(request.headers.get("X-CSRF-Token", "")):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        owner_ok = bool(job.get("owner")) and job.get("owner") == session.get("csrf_token", "")
        if not owner_ok:
            return jsonify(success=False, message="لا توجد عملية بهذا المعرّف."), 404
        return jsonify(public_job(job, light=light, links=links))


@app.get("/<access_path>/sessions")
def list_sessions_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    if not csrf_matches(request.headers.get("X-CSRF-Token", "")):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    with sessions_lock:
        rows = [public_session(row) for row in saved_sessions]
    with gemini_lock:
        gemini_key = saved_gemini.get("api_key") or ""
        gemini_model = saved_gemini.get("model") or ""
    if gemini_key and not is_owner_copy():
        # A user's page only shows that a key is there; the copy itself uses it.
        gemini_key = f"{gemini_key[:4]}…{gemini_key[-4:]}"
    # Whether someone (Grok, or Gemini with its key) can write this copy's comments; the owner's page is also
    # told when Grok writes them (chosen in «توزيع البرنامج»), and then asks for no Gemini key.
    writer = comments_writer()[0]
    comments_ready = writer != "gemini" or valid_gemini_key(saved_gemini.get("api_key") or "")
    if IN_APP:
        comments_ready = writer == "server"
    engine = writer if is_owner_copy() else ""
    if not comments_ready and not is_owner_copy() and not owner_sync_state["ok"] and time.monotonic() - owner_sync_state["at"] > 5:
        threading.Thread(target=sync_with_owner, name="owner-sync", daemon=True).start()   # (no keys yet: ask now)
    own = own_claude_shown() if key_typed_here() else {"claude_key": "", "claude_model": ""}
    with comments_server_lock:
        own.update(comments_server=server_shown(comments_server["link"]), comments_model=comments_server["model"])
    return jsonify(success=True, sessions=rows, gemini_api_key=gemini_key, gemini_model=gemini_model, comments_ready=comments_ready,
                   comments_engine=engine, **own)


@app.get("/<access_path>/gemini-usage")
def gemini_usage_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    if not csrf_matches(request.headers.get("X-CSRF-Token", "")):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    with gemini_usage_lock:
        comments = int(gemini_usage.get("comments") or 0)
        cost_comments = int(gemini_usage.get("cost_comments") or 0)
        since = gemini_usage.get("since") or 0
    return jsonify(
        success=True,
        since=since,
        comments=comments,
        # The comments the cost counts: since it was last reset.
        cost_comments=cost_comments,
        price_per_comment=GEMINI_PRICE_PER_COMMENT,
        total=round(cost_comments * GEMINI_PRICE_PER_COMMENT, 6),
    )


@app.get("/<access_path>/stats")
def stats_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    if not csrf_matches(request.headers.get("X-CSRF-Token", "")):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    with action_counts_lock:
        counts = dict(action_counts)
    return jsonify(success=True, counts=counts)


@app.post("/<access_path>/gemini-usage/reset")
def gemini_usage_reset_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH) or not is_owner_copy():
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    if not allow_attempt("cost-reset:" + (request.remote_addr or "unknown")):
        return jsonify(success=False, message="محاولات كثيرة. انتظر بضع دقائق ثم أعد المحاولة."), 429
    password = str(data.get("password") or "")
    if not hmac.compare_digest(password.encode("utf-8"), COST_RESET_PASSWORD.encode("utf-8")):
        return jsonify(success=False, message="كلمة السر غير صحيحة."), 403
    reset_gemini_cost()
    return jsonify(success=True, message="أُعيد تعيين التكلفة وعدد تعليقاتها إلى 0. العدد في أعلى الصفحة كما هو.")


@app.post("/<access_path>/gemini")
def save_gemini_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    if not is_owner_copy():
        return jsonify(success=False, message="يضبط مفتاح Gemini والنموذج صاحب البرنامج."), 403
    api_key = str(data.get("gemini_api_key") or "").strip()
    model = normalize_gemini_model(str(data.get("gemini_model") or ""))
    if api_key and not valid_gemini_key(api_key):
        return jsonify(success=False, message="صيغة مفتاح Gemini غير صالحة."), 400
    if str(data.get("gemini_model") or "").strip() and not model:
        return jsonify(success=False, message="اسم النموذج غير صالح."), 400
    if not api_key and not model:
        return jsonify(success=True, message="لا يوجد ما يُحفظ.")
    remember_gemini(api_key, model or "")
    return jsonify(success=True, message="حُفظ مفتاح Gemini.")


@app.post("/<access_path>/comments-server")
def save_comments_server_endpoint(access_path: str):
    """«سيرفر التعليقات» in the app's panel: the link is tried (the server must answer, with the certificate the
    link names), then kept. With no link sent, the saved one is tried again."""
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    if not IN_APP:
        return jsonify(success=False, message="سيرفر التعليقات يُضبط داخل التطبيق."), 403
    link = tidy_server_link(data.get("link")) or saved_server_link()
    if not link:
        return jsonify(success=False, message="الصق رابط سيرفر التعليقات."), 400
    if not parse_server_link(link):
        return jsonify(success=False, message="رابط السيرفر غير صحيح. انسخه كاملًا كما هو" + OWNER_LINK + "."), 400
    try:
        answer = ask_server(link, "ping", None, 20)
    except RuntimeError as exc:
        trace_click(f"سيرفر التعليقات {server_shown(link)}: تعذّر الاتصال: {exc}")
        return jsonify(success=False, message=str(exc)), 502
    model = str(answer.get("model") or "")[:60]
    remember_comments_server(link, model)
    remember_own_claude("", "")            # (a model key typed in this panel before is no longer kept on the phone)
    print(f"سيرفر التعليقات {server_shown(link)}: متصل (النموذج {model or '؟'})", flush=True)
    return jsonify(success=True, comments_engine="server", comments_server=server_shown(link), comments_model=model,
                   message="تم الاتصال بسيرفر التعليقات" + (f" (النموذج فيه: {model})" if model else "") + ". التعليقات تُكتب فيه الآن.")


@app.post("/<access_path>/comments-server/delete")
def delete_comments_server_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    if not IN_APP:
        return jsonify(success=False, message="سيرفر التعليقات يُضبط داخل التطبيق."), 403
    remember_comments_server("")
    return jsonify(success=True, comments_engine="none", comments_server="", comments_model="",
                   message="حُذف رابط السيرفر: التعليقات لا تُكتب إلى أن تضع رابطًا.")


@app.post("/<access_path>/claude")
def save_claude_endpoint(access_path: str):
    """«مفتاح Claude» in the panel of a copy with no «توزيع البرنامج»: saved, its comments are Claude's."""
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    if not key_typed_here():
        return jsonify(success=False, message="مفتاح Claude هنا لنسخة تعمل بلا «توزيع البرنامج»."), 403
    api_key = str(data.get("api_key") or "").strip()
    model = str(data.get("model") or "").strip().lower() or CLAUDE_DEFAULT_MODEL
    with grok_lock:
        current = saved_grok["api_key"] if saved_grok["engine"] == "claude" else ""
    if not api_key and not current:
        return jsonify(success=False, message="الصق مفتاح Claude."), 400
    if api_key and not valid_claude_key(api_key):
        return jsonify(success=False, message="صيغة مفتاح Claude غير صالحة."), 400
    if not valid_claude_model(model):
        return jsonify(success=False, message="اسم نموذج Claude غير صالح."), 400
    remember_own_claude(api_key or current, model)
    return jsonify(success=True, message="حُفظ مفتاح Claude: التعليقات الآن بواسطة Claude.", comments_engine="claude", **own_claude_shown())


@app.post("/<access_path>/claude/delete")
def delete_claude_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    if not key_typed_here():
        return jsonify(success=False, message="مفتاح Claude هنا لنسخة تعمل بلا «توزيع البرنامج»."), 403
    remember_own_claude("", "")
    return jsonify(success=True, message="حُذف مفتاح Claude: التعليقات بواسطة Gemini.", comments_engine="gemini", **own_claude_shown())


@app.post("/<access_path>/sessions/pin")
def save_session_pin_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    auth_token = str(data.get("auth_token") or "").strip()
    pin = str(data.get("chat_pin") or "").strip()
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل رمز الجلسة أولًا."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة ٤ إلى ٨ أرقام."), 400
    digest = hashlib.sha256(auth_token.encode("utf-8")).hexdigest()
    with sessions_lock:
        found = next((row for row in saved_sessions if hmac.compare_digest(row["digest"], digest)), None)
        if not found:
            return jsonify(success=False, message="احفظ الجلسة أولًا بالتحقق من الحساب."), 404
        found["chat_pin"] = pin
        persist_sessions_unlocked()
    return jsonify(success=True, message="حُفظ رمز الدردشة لهذا الحساب.")


@app.post("/<access_path>/sessions/delete")
def delete_session_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    session_id = str(data.get("id", ""))
    removed = None
    with sessions_lock:
        removed = next((row for row in saved_sessions if row["id"] == session_id), None)
        if removed:
            saved_sessions[:] = [row for row in saved_sessions if row["id"] != session_id]
            persist_sessions_unlocked()
    if removed:
        with screen_lock:
            current = str(screen.get("auth_token") or "")
        if current and hmac.compare_digest(current, removed["token"]):
            save_screen(auth_token="")
        delete_profile(removed["token"])
    return jsonify(success=True, message="حُذفت الجلسة.")


@app.get("/<access_path>/update/check")
def update_check(access_path: str):
    """This copy's version and, in a user's copy, whether the owner offers a newer one."""
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    if is_owner_copy():
        return jsonify(success=True, owner=True, version=APP_VERSION, control_url=CONTROL_URL,
                       control_port=CONTROL_PORT if distribution_started.is_set() else 0)
    if request.args.get("light") or updating.is_set():
        return jsonify(success=True, owner=False, version=APP_VERSION, updating=updating.is_set())
    if time.monotonic() - owner_sync_state["at"] > 10:
        threading.Thread(target=sync_with_owner, name="owner-sync", daemon=True).start()
    now = time.monotonic()
    answer = update_check_cache["answer"]
    if answer is None or now - update_check_cache["at"] > 60:
        answer = latest_update()
        update_check_cache.update(at=now, answer=answer)
    if not answer.get("version"):
        return jsonify(success=True, owner=False, version=APP_VERSION, message=answer.get("message") or "")
    newer = version_tuple(answer["version"]) > version_tuple(APP_VERSION)
    return jsonify(success=True, owner=False, version=APP_VERSION, latest=answer["version"], available=newer)


@app.post("/<access_path>/update/apply")
def update_apply(access_path: str):
    """«تحديث» in a user's copy: download the owner's newest version, check it, keep the old file,
    put the new one in place and start again. Nothing changes when any step fails."""
    if not hmac.compare_digest(access_path, ACCESS_PATH) or is_owner_copy():
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400

    def busy() -> bool:
        with jobs_lock:
            return any(job.get("status") == "running" for job in jobs.values())

    with jobs_lock:
        if updating.is_set():
            return jsonify(success=False, message="التحديث جارٍ."), 409
        updating.set()
    installed = False
    try:
        if busy():
            return jsonify(success=False, message="انتظر حتى تنتهي المهام الجارية ثم اضغط «تحديث»."), 409
        latest = latest_update()
        if not latest.get("version"):
            return jsonify(success=False, message=latest.get("message") or "تعذّر الاتصال بمصدر التحديث."), 502
        if version_tuple(latest["version"]) <= version_tuple(APP_VERSION):
            return jsonify(success=False, message="هذه النسخة على آخر إصدار."), 409
        status, body = ask_update_source("/update/file", 60)
        if status != 200:
            return jsonify(success=False, message=latest_update().get("message") or "تعذّر تنزيل التحديث."), 502
        problem = new_program_problem(body, latest)
        if problem:
            return jsonify(success=False, message=problem), 502
        if not program_starts(body):
            return jsonify(success=False, message="الإصدار الجديد لا يعمل على هذه الاستضافة. لم يتغير شيء."), 502
        if busy():
            return jsonify(success=False, message="بدأت مهمة أثناء التنزيل. انتظر حتى تنتهي ثم اضغط «تحديث»."), 409
        install_program(body)
        installed = True
    except OSError:
        return jsonify(success=False, message="تعذّر حفظ ملف التحديث. لم يتغير شيء."), 500
    finally:
        if not installed:
            updating.clear()
    threading.Thread(target=restart_program, name="restart", daemon=True).start()
    return jsonify(success=True, version=latest["version"], message=f"نُزّل الإصدار {latest['version']}. يُعاد تشغيل البرنامج…")


load_sessions()
load_group_ids()
load_done_history()
load_gemini()
load_panel_key()
load_comments_server()
load_gemini_usage()
load_action_counts()
load_jobs()
threading.Thread(target=save_jobs_in_background, name="jobs-saver", daemon=True).start()
load_screen()
load_skip_users()
load_collected()
atexit.register(shutdown_jobs)


# «التغريدات المجموعة»: the tweets collected in the last 7 days, day by day, with «عرض الأكثر مشاهدة» on the
# days from yesterday to 6 days ago. One part in two places: content.py's page («إنشاء محتوى») shows it under its
# link box (see tweets_part), and without content.py it is a page of its own (TWEETS_PAGE). Its look is
# kept inside #tweets, so it changes nothing else on the page it is in; that page may set --tweets-day
# and --tweets-tweet, the backgrounds of a day and of a tweet.
TWEETS_PART_STYLE = r'''<style>
    #tweets { line-height: 1.55; }
    #tweets .tweets-summary { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
    #tweets details { margin-top: 10px; border-radius: 14px; background: var(--tweets-day, var(--sheet)); box-shadow: inset 0 0 0 1px var(--line); }
    #tweets summary { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 10px; cursor: pointer; list-style: none; font-weight: 700; }
    #tweets summary::-webkit-details-marker { display: none; }
    #tweets summary .day { font-size: 15px; white-space: nowrap; }
    #tweets summary .tail { margin-inline-start: auto; display: flex; align-items: center; gap: 6px; }
    #tweets summary .count { color: var(--muted); font-size: 13px; font-weight: 600; white-space: nowrap; }
    #tweets summary .tail::after { content: "‹"; color: var(--muted); font-size: 18px; line-height: 1; transition: transform 0.15s; }
    #tweets details[open] summary .tail::after { transform: rotate(-90deg); }
    #tweets ol { list-style: none; margin: 0; padding: 0 10px 10px; display: flex; flex-direction: column; gap: 6px; }
    #tweets li { position: relative; padding: 8px 10px; border-radius: 10px; background: var(--tweets-tweet, var(--paper)); box-shadow: inset 0 0 0 1px var(--line); }
    #tweets .text { margin: 0; font-size: 14px; white-space: pre-line; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
    #tweets li.has-content .text { padding-inline-end: 30px; }
    #tweets li.is-open .text { display: block; overflow: visible; }
    #tweets .tw-open { position: absolute; top: 6px; inset-inline-end: 6px; display: grid; place-items: center; width: 26px; height: 26px; min-height: 0;
      padding: 0; border: 0; border-radius: 50%; background: var(--tweets-day, var(--sheet)); color: var(--muted); box-shadow: inset 0 0 0 1px var(--line);
      font: inherit; font-size: 16px; font-weight: 700; line-height: 1; cursor: pointer; }
    #tweets .tw-open::before { content: "‹"; transition: transform 0.15s; }
    #tweets li.is-open .tw-open::before { transform: rotate(-90deg); }
    #tweets .more { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
    #tweets .pics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; }
    #tweets .pic { position: relative; display: block; aspect-ratio: 1; overflow: hidden; border-radius: 8px; background: var(--line); }
    #tweets .pic img { display: block; width: 100%; height: 100%; object-fit: cover; }
    #tweets .pics.one { grid-template-columns: minmax(0, 1fr); }
    #tweets .pics.one .pic { aspect-ratio: auto; min-height: 60px; }
    #tweets .pics.one .pic img { height: auto; max-height: 300px; object-fit: contain; }
    #tweets .quote .pics.one .pic img { max-height: 200px; }
    #tweets .pic.is-video::after { content: "▶"; position: absolute; inset: 0; display: grid; place-items: center; color: #fff; font-size: 24px;
      text-shadow: 0 1px 6px rgba(0, 0, 0, 0.6); }
    #tweets .pic.is-broken img { visibility: hidden; }
    #tweets .pic.is-broken::before { content: "تعذّر عرض الصورة"; position: absolute; inset: 0; display: grid; place-items: center; padding: 6px;
      color: var(--muted); font-size: 12px; text-align: center; }
    #tweets .quote { padding: 8px 10px; border-radius: 10px; background: var(--tweets-day, var(--sheet)); box-shadow: inset 0 0 0 1px var(--line); }
    #tweets .quote .by { color: var(--muted); font-size: 12px; font-weight: 600; }
    #tweets .quote p { margin: 2px 0 0; font-size: 13px; white-space: pre-line; overflow-wrap: anywhere; }
    #tweets .quote .pics { margin-top: 6px; }
    #tweets .meta { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 10px; margin-top: 4px; color: var(--muted); font-size: 12px; }
    #tweets .meta .open { margin-inline-start: auto; color: var(--pine); font-weight: 700; text-decoration: none; }
    #tweets .tweets-empty { margin: 18px 0 6px; text-align: center; color: var(--muted); }
    #tweets .vbtn { flex: 0 0 auto; min-height: 30px; padding: 0 8px; border: 0; border-radius: 8px; background: var(--pine); color: #f4fbf7; box-shadow: none;
      font: inherit; font-size: 12px; font-weight: 700; cursor: pointer; }
    #tweets .vbtn:disabled { opacity: 0.45; cursor: default; }
    #tweets .vstatus { margin: 0 12px 8px; color: var(--muted); font-size: 12px; }
    #tweets .vstatus.is-bad { color: var(--clay); }
    #tweets .views { color: var(--pine); font-weight: 800; }
    #tweets .views.is-none { color: var(--muted); font-weight: 600; }
  </style>'''
TWEETS_PART_BODY = '''<p id="tweets-summary" class="tweets-summary"></p>
      <div id="tweets-days"></div>
      <p id="tweets-empty" class="tweets-empty" hidden>لم تُجمع تغريدات خلال آخر ٧ أيام.</p>'''
TWEETS_PART_SCRIPT = r'''<script>
  (() => {
    const tweets = {{ tweets|tojson }};
    const apiRoot = {{ api_root|tojson }};
    const csrf = {{ csrf_token|tojson }};
    let since = {{ server_now|tojson }};
    const pad = (n) => String(n).padStart(2, "0");
    const dayKey = (when) => when.getFullYear() + "-" + pad(when.getMonth() + 1) + "-" + pad(when.getDate());
    const dayName = new Intl.DateTimeFormat("ar-u-nu-latn", {weekday: "long", day: "numeric", month: "long"});
    const clock = new Intl.DateTimeFormat("ar-u-nu-latn", {hour: "numeric", minute: "2-digit"});
    const number = new Intl.NumberFormat("ar-u-nu-latn");
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    const label = (key, when) => key === dayKey(now) ? "اليوم" : key === dayKey(yesterday) ? "أمس" : dayName.format(when);
    // «عرض الأكثر مشاهدة» is for the days the script allows (today to 6 days ago). A day asked for waits
    // VIEWS_AGAIN seconds (10 minutes) before it can be asked for again; VIEWS_ASKED: the days still waiting
    // (the start of the day, in seconds → when it was asked for), in the server's time.
    const VIEWS_DAYS = {{ views_days|tojson }};
    const VIEWS_AGAIN = {{ views_again|tojson }};
    const VIEWS_ASKED = {{ views_asked|tojson }};
    const skew = since - Date.now() / 1000;
    const serverNow = () => Date.now() / 1000 + skew;
    let lastState = null;            // how the fetching was going when the server last said
    const minutesText = (seconds) => {
      const minutes = Math.max(1, Math.ceil(seconds / 60));
      return minutes === 1 ? "دقيقة" : minutes === 2 ? "دقيقتين" : minutes + (minutes % 100 >= 3 && minutes % 100 <= 10 ? " دقائق" : " دقيقة");
    };
    const daysAgo = (key) => {
      const [year, month, date] = key.split("-").map(Number);
      return Math.round((todayStart - new Date(year, month - 1, date)) / 86400000);
    };
    const canCheck = (key) => daysAgo(key) >= VIEWS_DAYS[0] && daysAgo(key) <= VIEWS_DAYS[1];
    const safeLink = (url) => /^https:\/\/(?:www\.)?(?:x|twitter)\.com\/[^\s"'<>]+$/.test(url || "") ? url : "";
    const viewsText = (tweet) => !tweet.views_at ? "" : tweet.views == null ? "لا يوجد رقم" : number.format(tweet.views) + " مشاهدة";
    const showViews = (span, tweet) => {
      span.textContent = viewsText(tweet);
      span.className = tweet.views == null ? "views is-none" : "views";
      span.hidden = !span.textContent;
    };
    const checkedAt = (seconds) => {
      const when = new Date(seconds * 1000);
      const today = new Date();
      const before = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
      const key = dayKey(when);
      return (key === dayKey(today) ? "" : key === dayKey(before) ? "أمس " : dayName.format(when) + " ") + clock.format(when);
    };

    // Each tweet once, under the day (in this device's time zone) it was first collected on.
    const days = new Map();
    for (const tweet of tweets) {
      const key = dayKey(new Date(tweet.at * 1000));
      if (!days.has(key)) days.set(key, {key, items: []});
      days.get(key).items.push({tweet, first: tweet.at});
    }
    const list = [...days.values()].sort((a, b) => b.key.localeCompare(a.key));
    const byId = new Map();          // tweet id → {tweet, day, li, views}
    let total = tweets.length;
    const showTotal = () => { document.getElementById("tweets-summary").textContent = "آخر ٧ أيام · " + total + " تغريدة"; };
    showTotal();
    document.getElementById("tweets-empty").hidden = list.length > 0;
    const box = document.getElementById("tweets-days");

    // A tweet's content (its text, pictures and the tweet it quotes) comes with its views. A small arrow
    // over the tweet opens it: the text in full, then the pictures. Closed, the text shows 3 lines.
    const smaller = (address) => /[?&]name=/.test(address) ? address.replace(/([?&]name=)[^&]*/, "$1small")
      : address + (address.includes("?") ? "&" : "?") + "name=small";
    function pictures(list, tweetLink) {
      const box = document.createElement("div");
      box.className = list.length === 1 ? "pics one" : "pics";
      for (const [kind, address] of list) {
        const link = document.createElement("a");
        link.className = kind === "video" ? "pic is-video" : "pic";
        link.href = kind === "photo" ? address : tweetLink || address;     // a photo opens in full; a video plays on X
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const image = document.createElement("img");
        image.src = smaller(address);
        image.alt = kind === "video" ? "فيديو" : "صورة";
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        image.addEventListener("error", () => link.classList.add("is-broken"));
        link.append(image);
        box.append(link);
      }
      return box;
    }
    function contentBox(tweet) {
      const box = document.createElement("div");
      box.className = "more";
      const link = safeLink(tweet.url);
      if ((tweet.pictures || []).length) box.append(pictures(tweet.pictures, link));
      const quoted = tweet.quote;
      if (quoted && (quoted.body || (quoted.pictures || []).length)) {
        const quote = document.createElement("div");
        quote.className = "quote";
        if (quoted.by) {
          const by = document.createElement("bdi");
          by.className = "by";
          by.dir = "ltr";
          by.textContent = "@" + quoted.by;
          quote.append(by);
        }
        if (quoted.body) {
          const text = document.createElement("p");
          text.dir = "auto";
          text.textContent = quoted.body;
          quote.append(text);
        }
        if ((quoted.pictures || []).length) quote.append(pictures(quoted.pictures, link));
        box.append(quote);
      }
      return box;
    }
    function setOpen(entry, open) {
      entry.open = open;
      entry.li.classList.toggle("is-open", open);
      entry.toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open && !entry.more) {
        entry.more = contentBox(entry.tweet);    // made when first opened: a closed tweet loads no pictures
        entry.text.after(entry.more);
      }
      if (entry.more) entry.more.hidden = !open;
    }
    function showContent(entry) {
      const tweet = entry.tweet;
      entry.text.textContent = tweet.body || tweet.text || "تغريدة";
      const quoted = tweet.quote || {};
      // Not fetched yet (or nothing in it): no arrow.
      if (typeof tweet.body !== "string" || !(tweet.body || (tweet.pictures || []).length || quoted.body || (quoted.pictures || []).length)) return;
      if (!entry.toggle) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "tw-open";
        toggle.setAttribute("aria-label", "محتوى التغريدة");
        toggle.addEventListener("click", () => setOpen(entry, !entry.open));
        entry.li.classList.add("has-content");
        entry.li.prepend(toggle);
        entry.toggle = toggle;
      }
      if (entry.more) {
        entry.more.remove();                       // new content: made again when shown
        entry.more = null;
      }
      setOpen(entry, entry.open);
    }

    // The day's tweets: most viewed first once it has counts (tweets without one after), else newest first.
    function order(day) {
      const counted = day.items.some((item) => item.tweet.views_at);
      const sorted = [...day.items].sort((a, b) => {
        if (counted) {
          const va = a.tweet.views == null ? -1 : a.tweet.views;
          const vb = b.tweet.views == null ? -1 : b.tweet.views;
          if (va !== vb) return vb - va;
        }
        return b.first - a.first;
      });
      day.ol.replaceChildren(...sorted.map((item) => byId.get(item.tweet.id).li));
      // The first tweet with content shows it, the others are closed: so it is clear each one can be opened.
      let first = true;
      for (const item of sorted) {
        const entry = byId.get(item.tweet.id);
        if (!entry.toggle) continue;
        setOpen(entry, first);
        first = false;
      }
    }

    function showDay(day, state) {
      const status = day.status;
      const waiting = day.items.filter((item) => state && state.waiting.has(item.tweet.id)).length;
      status.className = "vstatus";
      if (waiting) {
        if (day.button) day.button.disabled = true;
        let text = (state.slow ? "يجلب المشاهدات ببطء لأن الخدمة طلبت التمهّل… بقي " : "يجلب المشاهدات… بقي ") + waiting + " من " + day.items.length;
        if (state.paused) text = "متوقف مؤقتًا لتجنّب الحظر · يتابع بعد " + Math.floor(state.paused / 60) + ":" + pad(state.paused % 60) + " · بقي " + waiting + " من " + day.items.length;
        status.textContent = text;
        status.hidden = false;
        day.busy = true;
        return;
      }
      if (day.busy) order(day);      // just finished: sort it now (not while numbers arrive)
      day.busy = false;
      // Asked for less than 10 minutes ago: its button waits (and comes back by itself).
      const wait = day.askedAt ? day.askedAt + VIEWS_AGAIN - serverNow() : 0;
      if (day.button) day.button.disabled = wait > 0;
      const counted = day.items.filter((item) => item.tweet.views_at).length;
      const last = Math.max(0, ...day.items.map((item) => item.tweet.views_at || 0));
      if (state && state.error && day.asked) {
        status.textContent = state.error;
        status.className = "vstatus is-bad";
      } else if (wait > 0) {
        status.textContent = (last ? "مرتّبة من الأكثر مشاهدة · فُحصت " + clock.format(new Date(last * 1000)) + " · " : "")
          + "يُتاح الفحص مجددًا بعد " + minutesText(wait);
      } else if (counted && counted < day.items.length && day.button) {
        status.textContent = "فُحصت " + counted + " من " + day.items.length + " · اضغط «عرض الأكثر مشاهدة» ليكمل";
      } else {
        status.textContent = last ? "مرتّبة من الأكثر مشاهدة · آخر فحص " + checkedAt(last) : "";
      }
      status.hidden = !status.textContent;
    }

    // Every day starts folded (today's too); a day opens when tapped, or when its views are asked for.
    list.forEach((day) => {
      day.items.sort((a, b) => b.first - a.first);
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      const title = document.createElement("span");
      title.className = "day";
      title.textContent = label(day.key, new Date(day.items[0].first * 1000));
      // The count, the button and the fold arrow together: on a narrow screen they go under the day's name as one.
      const tail = document.createElement("span");
      tail.className = "tail";
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = day.items.length + " تغريدة";
      tail.append(count);
      summary.append(title, tail);
      let button = null;
      if (canCheck(day.key)) {
        button = document.createElement("button");
        button.type = "button";
        button.className = "vbtn";
        button.textContent = "عرض الأكثر مشاهدة";
        tail.append(button);
      }
      const status = document.createElement("p");
      status.className = "vstatus";
      status.hidden = true;
      const ol = document.createElement("ol");
      const [year, month, date] = day.key.split("-").map(Number);
      // The day as the script knows it: its start, in seconds (and when it was last asked for, if lately).
      const start = new Date(year, month - 1, date).getTime() / 1000;
      Object.assign(day, {details, ol, status, button, count, start, askedAt: VIEWS_ASKED[String(start)] || 0});
      for (const item of day.items) {
        const li = document.createElement("li");
        const text = document.createElement("p");
        text.className = "text";
        text.dir = "auto";
        const meta = document.createElement("div");
        meta.className = "meta";
        const views = document.createElement("span");
        showViews(views, item.tweet);
        const time = document.createElement("span");
        time.textContent = clock.format(new Date(item.first * 1000));
        meta.append(views, time);
        if (item.tweet.group) {
          const group = document.createElement("span");
          group.textContent = item.tweet.group;
          meta.append(group);
        }
        const link = safeLink(item.tweet.url);
        if (link) {
          const open = document.createElement("a");
          open.className = "open";
          open.href = link;
          open.target = "_blank";
          open.rel = "noopener noreferrer";
          open.textContent = "فتح";
          meta.append(open);
        }
        li.append(text, meta);
        const entry = {tweet: item.tweet, day, li, views, text, toggle: null, more: null, open: false};
        byId.set(item.tweet.id, entry);
        showContent(entry);
      }
      order(day);
      showDay(day, null);
      if (button) button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        details.open = true;
        day.asked = true;
        button.disabled = true;
        status.className = "vstatus";
        status.textContent = "يبدأ جلب المشاهدات…";
        status.hidden = false;
        const range = [day.start, new Date(year, month - 1, date + 1).getTime() / 1000];
        let data;
        try {
          const response = await fetch(apiRoot + "/tweets/views", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ranges: [range], offset: -new Date().getTimezoneOffset(), csrf_token: csrf})
          });
          data = await response.json();
        } catch (_) {
          data = {success: false, message: "تعذّر الاتصال بالسكربت."};
        }
        if (!data.success) {
          if (data.asked_at) day.askedAt = data.asked_at;     // asked for lately (from another page): it waits
          status.textContent = data.message || "تعذّر البدء.";
          status.className = data.done ? "vstatus" : "vstatus is-bad";
          button.disabled = !!data.done;
          return;
        }
        day.askedAt = (data.asked || {})[String(day.start)] || serverNow();
        apply(data);
        follow();
      });
      details.append(summary, status, ol);
      box.append(details);
    });

    // New counts from the server go into their tweets; then each day's line is brought up to date.
    function apply(data) {
      if (typeof data.now === "number") since = data.now;
      for (const [id, pair] of Object.entries(data.views || {})) {
        const entry = byId.get(id);
        if (!entry) continue;
        entry.tweet.views = pair[0];
        entry.tweet.views_at = pair[1];
        showViews(entry.views, entry.tweet);
        if (pair[2]) {
          for (const key of ["body", "pictures", "quote"]) delete entry.tweet[key];
          Object.assign(entry.tweet, pair[2]);
          showContent(entry);
        }
      }
      for (const id of data.removed || []) {
        const entry = byId.get(id);
        if (!entry) continue;
        entry.li.remove();
        entry.day.items = entry.day.items.filter((item) => item.tweet.id !== id);
        entry.day.count.textContent = entry.day.items.length + " تغريدة";
        byId.delete(id);
        total -= 1;
        showTotal();
      }
      const state = {waiting: new Set(data.waiting || []), paused: data.paused || 0, error: data.error || "", slow: !!data.slow};
      lastState = state;
      for (const day of list) showDay(day, state);
      return data.running || state.waiting.size > 0;
    }

    // The days waiting after a check: their line counts down, and their button comes back after 10 minutes.
    setInterval(() => {
      for (const day of list) if (day.askedAt && !day.busy) showDay(day, lastState);
    }, 15000);

    let following = false;
    async function follow() {
      if (following) return;
      following = true;
      for (;;) {
        await new Promise((done) => setTimeout(done, 2000));
        let data;
        try {
          data = await (await fetch(apiRoot + "/tweets/views?since=" + since, {cache: "no-store"})).json();
        } catch (_) {
          continue;
        }
        if (!data.success || !apply(data)) break;
      }
      following = false;
    }

    // A fetch may already be running (started earlier, or from another tab): follow it.
    fetch(apiRoot + "/tweets/views?since=" + since, {cache: "no-store"})
      .then((response) => response.json())
      .then((data) => { if (data.success && apply(data)) follow(); })
      .catch(() => {});
  })();
  </script>'''
TWEETS_PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>التغريدات المجموعة</title>
  <style>
    :root { --paper: #f3efe6; --sheet: #fffdf8; --ink: #1c1915; --muted: #6e675e; --line: #e4dcd0; --pine: #0f6b50; --clay: #8d3a32; color-scheme: light; }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body { margin: 0; background: var(--paper); color: var(--ink); font-family: "IBM Plex Sans Arabic", "Segoe UI", Tahoma, sans-serif; line-height: 1.55; }
    main { width: min(560px, 100%); margin: 0 auto; padding: 14px 12px 32px; }
    header { display: flex; align-items: center; gap: 10px; }
    h1 { margin: 0; font-size: 18px; }
    .back { margin-inline-start: auto; display: inline-flex; align-items: center; min-height: 36px; padding: 0 12px; border-radius: 8px; background: var(--sheet);
      box-shadow: inset 0 0 0 1px var(--line); color: var(--ink); font-size: 13px; font-weight: 600; text-decoration: none; white-space: nowrap; }
  </style>
  ''' + TWEETS_PART_STYLE + r'''
</head>
<body>
  <main>
    <header>
      <h1>التغريدات المجموعة</h1>
      <a class="back" href="{{ back }}">رجوع</a>
    </header>
    <section id="tweets">
      ''' + TWEETS_PART_BODY + r'''
    </section>
  </main>
  ''' + TWEETS_PART_SCRIPT + r'''
</body>
</html>
'''


def tweets_part_values() -> dict:
    """What the tweets part is drawn with: the tweets, the panel's address, this browser's CSRF token
    (made here when it has none yet) and the server's time (the counts fetched after it are asked for)."""
    csrf_token = session.get("csrf_token")
    if not csrf_token:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token
    now = time.time()
    with views_lock:
        asked = {str(start): at for start, at in recent_views_asks(now).items()}
    return {"tweets": collected_payload(), "api_root": "/" + ACCESS_PATH, "csrf_token": csrf_token, "server_now": now,
            "views_days": list(VIEWS_DAYS), "views_asked": asked, "views_again": VIEWS_AGAIN_SECONDS}


def tweets_part() -> dict[str, str]:
    """The tweets part for content.py's page, drawn now: its style, its markup and its script, each
    ready to put in the page as it is (the page gives the part's element the id "tweets")."""
    return {"style": TWEETS_PART_STYLE, "body": TWEETS_PART_BODY,
            "script": render_template_string(TWEETS_PART_SCRIPT, **tweets_part_values())}


@app.get("/<access_path>/tweets")
def tweets_page(access_path: str):
    """«عرض التغريدات»: the tweets collected in the last 7 days, day by day. When content.py shows them
    on its page, under its link box, they are there (an old link to this page goes to them)."""
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    if content_shows_tweets:
        return redirect("/" + ACCESS_PATH + "/content#tweets")
    return render_template_string(TWEETS_PAGE, back="/" + ACCESS_PATH, **tweets_part_values())


@app.post("/<access_path>/tweets/views")
def tweets_views_start(access_path: str):
    """«عرض الأكثر مشاهدة»: queue a day's tweets for their current view count. Any of the last 7 days (in
    the viewer's time zone); a day asked for waits VIEWS_AGAIN_SECONDS before it can be asked for again."""
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها."), 400
    spans = day_spans(data)
    try:
        offset = int(data.get("offset"))
    except (TypeError, ValueError):
        offset = None
    if not spans or offset is None or abs(offset) > 14 * 60:
        return jsonify(success=False, message="أعد تحميل الصفحة ثم حاول مرة أخرى."), 400
    now = time.time()
    today = local_day_number(now, offset)
    if any(not VIEWS_DAYS[0] <= today - local_day_number((start + end) / 2, offset) <= VIEWS_DAYS[1] for start, end in spans):
        return jsonify(success=False, message="فحص المشاهدات متاح لتغريدات آخر ٧ أيام فقط."), 400
    with views_lock:
        asked = recent_views_asks(now)
    last = max((asked.get(int(start), 0) for start, end in spans), default=0)
    if last:
        return jsonify(success=False, done=True, asked_at=last,
                       message=f"فُحص هذا اليوم قبل قليل. يُتاح الفحص مجددًا بعد {minutes_text(last + VIEWS_AGAIN_SECONDS - now)}."), 409
    with collected_lock:
        rows = [(row.get("views_at") or 0, -row["at"], status_id, row["url"]) for status_id, row in collected_tweets.items()
                if any(start <= row["at"] < end for start, end in spans)]
    rows = [entry for entry in rows if not tweet_is_ignored(entry[3])]
    if not rows:
        return jsonify(success=False, message="لا توجد تغريدات في هذا اليوم."), 400
    # All the day's tweets: the never counted first (newest first), then the oldest counts. So pressing
    # again after a stop starts with the ones it had not reached.
    rows.sort()
    queued = queue_views([status_id for checked, _, status_id, _ in rows])
    with views_lock:
        for start, end in spans:
            views_asked[int(start)] = now
    return jsonify(success=True, queued=queued, asked={str(int(start)): now for start, end in spans}, **views_status_payload(now))


@app.get("/<access_path>/tweets/views")
def tweets_views_status(access_path: str):
    """How «عرض الأكثر مشاهدة» is going, and the counts fetched since the page last asked."""
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    try:
        since = float(request.args.get("since") or 0)
    except ValueError:
        since = 0.0
    return jsonify(success=True, **views_status_payload(since))


# «إنشاء محتوى»: content.py beside the script adds its page to this panel.
CONTENT_FILE = Path(__file__).resolve().with_name("content.py")
content_ready = threading.Event()
# Why content.py could not be added when the script started ("" if it was, or was not there).
content_problem = ""
# content.py's page shows the collected tweets (under its link box), so they have no page of their own.
content_shows_tweets = False


def content_accounts() -> list[dict]:
    with sessions_lock:
        return [{"id": row["id"], "username": row.get("username") or "", "token": row["token"]} for row in saved_sessions]


def content_gemini_key() -> str:
    """The Gemini key saved on the panel (the owner's, on users' copies: in memory, from the check-ins)."""
    wait_for_owner_keys(gemini_only=True)
    with gemini_lock:
        return str(saved_gemini.get("api_key") or "")


def content_error(exc: BaseException) -> str:
    """The error, with the line of content.py it came from."""
    if isinstance(exc, SyntaxError):
        message, line = exc.msg, exc.lineno
    else:
        message, line = str(exc), None
        for frame in traceback.extract_tb(exc.__traceback__):
            if Path(frame.filename).name == CONTENT_FILE.name:
                line = frame.lineno
    return f"{type(exc).__name__}: {message}" + (f" (line {line})" if line else "")


def attach_content() -> None:
    """Add «إنشاء محتوى»: from content.py beside the owner's script, or, on a user's copy, from the one the
    owner packed into it (a content.py put beside a user's copy is not used: the page is the owner's)."""
    global content_problem, content_shows_tweets
    owner = is_owner_copy()
    if not (CONTENT_FILE.is_file() if owner else CONTENT_EMBEDDED):
        return
    try:
        if owner:
            spec = importlib.util.spec_from_file_location("content_page", CONTENT_FILE)
            content = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(content)
        else:
            source = gzip.decompress(base64.b64decode(CONTENT_EMBEDDED)).decode("utf-8")
            content = importlib.util.module_from_spec(importlib.util.spec_from_loader("content_page", loader=None, origin=str(CONTENT_FILE)))
            content.__file__ = str(CONTENT_FILE)
            exec(compile(source, str(CONTENT_FILE), "exec"), content.__dict__)
        added = content.attach(app, ACCESS_PATH, SimpleNamespace(
            data_dir=DATA_DIR,
            accounts=content_accounts,
            current_token=lambda: screen_payload()["auth_token"],
            # Any free browser profile of the account, so a running task does not hold it up.
            run_browser=lambda token, work: run_on_fresh_browser(token, work, MAX_PROFILES_PER_ACCOUNT),
            goto_x=goto_x,
            gemini_key=content_gemini_key,
            tweets_part=tweets_part,
            posts_per_day=None if owner else USER_POSTS_PER_DAY,
        ))
    except Exception as exc:
        content_problem = content_error(exc)
        print(f"لم تُضف صفحة إنشاء المحتوى ({CONTENT_FILE.name}): {content_problem}", flush=True)
        return
    # What its page took on (an older content.py says nothing: the tweets keep their own page).
    content_shows_tweets = isinstance(added, dict) and bool(added.get("tweets"))
    content_ready.set()


CONTENT_UNAVAILABLE_PAGE = """<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>إنشاء محتوى</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; background: #f3efe6; color: #1c1915; font-family: "IBM Plex Sans Arabic", "Segoe UI", Tahoma, sans-serif; }
    main { width: min(560px, 100%); margin: 0 auto; padding: 16px; }
    h1 { margin: 0; font-size: 18px; }
    .why { margin-top: 12px; padding: 12px 14px; border-radius: 14px; background: #fbf1ee; color: #8d3a32; line-height: 1.7; }
    .why p { margin: 0; }
    .why pre { margin: 8px 0; padding: 8px 10px; border-radius: 8px; background: #fff; direction: ltr; text-align: left;
      white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
    .hint { color: #6e675e; font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
    a { color: #0f6b50; font-weight: 600; }
  </style>
</head>
<body>
  <main>
    <h1>إنشاء محتوى</h1>
    <div class="why">
      <p>{{ reason }}</p>
      {% if problem %}<pre>{{ problem }}</pre>{% endif %}
      {% if fix %}<p>{{ fix }}</p>{% endif %}
    </div>
    <p class="hint">مجلد gg.py الذي يعمل الآن: <bdi dir="ltr">{{ here }}</bdi><br>Python <bdi dir="ltr">{{ python }}</bdi></p>
    <a href="{{ panel }}">العودة للوحة</a>
  </main>
</body>
</html>"""


def content_unavailable(access: str):
    """«إنشاء محتوى» when its page could not be added: why, and what to do."""
    if not hmac.compare_digest(access, ACCESS_PATH):
        abort(404)
    problem = fix = ""
    if not is_owner_copy():
        if content_problem:
            reason, problem, fix = "تعذّر تحميل صفحة إنشاء المحتوى في هذه النسخة:", content_problem, "أرسل هذه الرسالة لصاحب البرنامج."
        else:
            reason = "صفحة إنشاء المحتوى لم تصل إلى هذه النسخة بعد: تصل مع تحديث ينشره صاحب البرنامج."
    elif not CONTENT_FILE.is_file():
        reason = "لا يوجد ملف content.py بجانب gg.py في هذا المجلد."
    elif content_problem:
        reason, problem, fix = "تعذّر تحميل content.py عند تشغيل gg.py:", content_problem, "أعد تنزيل content.py ثم أعد تشغيل gg.py."
    else:
        reason = "وُضع content.py بعد تشغيل gg.py. أعد تشغيل gg.py لتظهر صفحة إنشاء المحتوى."
    return render_template_string(CONTENT_UNAVAILABLE_PAGE, reason=reason, problem=problem, fix=fix, here=str(CONTENT_FILE.parent),
                                  python=sys.version.split()[0], panel="/" + ACCESS_PATH), 503


attach_content()
if not content_ready.is_set():
    # The button shows whenever content.py is beside the script; without its page it says why.
    app.add_url_rule("/<access>/content", "content_unavailable", content_unavailable, methods=["GET"])


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    bind_host = os.getenv("BIND_HOST", "0.0.0.0")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if public_base_url:
        print(f"X profile/repost checker: {public_base_url}/", flush=True)
    else:
        print(f"Flask listening on {bind_host}:{port}", flush=True)
        print(f"Local URL: http://127.0.0.1:{port}/", flush=True)
        try:
            for address in sorted(set(socket.gethostbyname_ex(socket.gethostname())[2])):
                if not address.startswith("127."):
                    print(f"Host URL: http://{address}:{port}/", flush=True)
        except OSError:
            pass
        print("For public use, configure HTTPS and set PUBLIC_BASE_URL.", flush=True)
    start_distribution(bind_host)
    try:
        fill_collected_from_history()
    except Exception as exc:          # (never in the way of starting)
        print(f"لم تُضف التغريدات الأخيرة إلى التغريدات المجموعة: {type(exc).__name__}: {exc}", flush=True)
    if not is_owner_copy():
        threading.Thread(target=owner_sync_loop, name="owner-sync", daemon=True).start()
    app.run(host=bind_host, port=port, debug=False, threaded=True)
