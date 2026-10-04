#!/usr/bin/env python3
"""One-file Flask page to verify an X session and list a profile's reposts.

Saved sessions (auth token + the account username) are written beside this
script and survive a restart. Job progress is kept too, so leaving the browser
and coming back shows whether the work is still running, where it stopped,
or that it finished. The token is not returned except to this same page.

The profile timeline is virtualized: X keeps roughly the first screen of posts
in the DOM (often about 10) and unmounts the rest. Collection therefore scrolls
the real timeline container and waits until new posts mount, so counts above
10 — and above 100 — resolve to whatever the timeline actually contains.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
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
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import Flask, abort, jsonify, make_response, redirect, render_template_string, request, session
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("X_APP_SECURE_COOKIE", "0") == "1",
    MAX_CONTENT_LENGTH=8 * 1024,
)

ACCESS_PATH = os.getenv("X_ACCESS_PATH", "desk")
ATTEMPT_WINDOW_SECONDS = 300
MAX_ATTEMPTS_PER_CLIENT = 5
MAX_REPOSTS_REQUEST = 1000
PREVIEW_TTL_SECONDS = 1200
pending_previews: dict[str, dict] = {}
jobs: dict[str, dict] = {}
attempts: dict[str, deque[float]] = defaultdict(deque)
attempts_lock = threading.Lock()
preview_lock = threading.Lock()
jobs_lock = threading.Lock()
sessions_lock = threading.Lock()
gemini_lock = threading.Lock()
JOB_TTL_SECONDS = 86400
DATA_DIR = Path(__file__).resolve().with_name("data")
SESSIONS_PATH = DATA_DIR / "sessions.json"
JOBS_PATH = DATA_DIR / "jobs.json"
GEMINI_PATH = DATA_DIR / "gemini.json"
saved_sessions: list[dict] = []
saved_gemini = {"api_key": "", "model": "gemini-3.5-flash"}
JOB_FIELDS = (
    "owner", "client_id", "created_at", "updated_at", "status", "phase", "kind",
    "collected", "done", "requested", "message", "success", "preview_id",
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
    .steps {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .steps li {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 2px;
      min-width: 0;
      padding: 4px 2px;
      border-radius: 8px;
      background: rgba(255, 253, 248, 0.72);
      color: var(--muted);
      font-size: 10px;
      font-weight: 500;
      text-align: center;
      line-height: 1.15;
    }
    .steps li:not(:last-child)::after { content: none; }
    .steps .n {
      width: 18px;
      height: 18px;
      display: grid;
      place-items: center;
      border-radius: 99px;
      font-size: 11px;
      font-variant-numeric: tabular-nums;
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .steps li.is-current { color: var(--ink); font-weight: 600; background: var(--sheet); }
    .steps li.is-current .n { background: var(--ink); color: var(--sheet); box-shadow: none; }
    .steps li.is-done { color: var(--pine); }
    .steps li.is-done .n { background: var(--pine); color: var(--on-pine); box-shadow: none; }

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
    #auth_token, #username, #repost-count, #stop-tweet, #gemini-model { direction: ltr; text-align: start; }
    #auth_token[readonly], #gemini-key[readonly], #gemini-model[readonly] { color: var(--muted); }
    .token-row, .prefix-field { display: flex; align-items: stretch; gap: 6px; }
    .prefix-field span {
      display: grid;
      place-items: center;
      min-width: 36px;
      border: 1px solid var(--line);
      border-radius: 10px;
      color: var(--muted);
      font-weight: 600;
      font-size: 14px;
      background: var(--paper);
    }
    .prefix-field input { flex: 1; min-width: 0; }
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
    .wait-groups input.wait-sec {
      width: 52px;
      min-height: 28px;
      height: 28px;
      padding: 0 4px;
      text-align: center;
      direction: ltr;
      font-size: 14px;
    }
    .group-wait-row { display: flex; align-items: center; gap: 6px; margin: 4px 0 8px; }
    .btn-wide { width: 100%; margin-top: 8px; min-height: 34px; height: 34px; }
    .target-row { display: grid; grid-template-columns: minmax(0, 1fr) 76px; gap: 6px; align-items: center; }
    .target-row .prefix-field { min-width: 0; }
    #repost-count { text-align: center; padding-inline: 4px; }
    .stop-row { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; align-items: center; margin-top: 6px; }
    .mode-switch { display: flex; gap: 6px; }
    .mode-switch .preset { flex: 1; min-height: 32px; height: 32px; }
    #group-box {
      margin-top: 10px;
      margin-bottom: 14px;
      padding: 8px 10px 10px;
      border-radius: 14px;
      background: var(--paper);
      box-shadow: inset 0 0 0 1px var(--line);
    }
    #group-list {
      flex-direction: row;
      flex-wrap: nowrap;
      overflow-x: auto;
      gap: 6px;
      margin-top: 6px;
      padding-bottom: 2px;
    }
    #group-list li { flex: 0 0 auto; }
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
    .presets { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 4px; margin-top: 4px; }
    .preset {
      min-height: 26px;
      height: 26px;
      min-width: 0;
      padding: 0;
      border: 0;
      border-radius: 999px;
      background: transparent;
      color: var(--ink);
      box-shadow: inset 0 0 0 1px var(--line);
      font: inherit;
      font-size: 12px;
      font-weight: 500;
      font-variant-numeric: tabular-nums;
    }
    .preset.is-on { background: var(--ink); color: var(--sheet); box-shadow: none; }
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
    .gemini-fields .field-label { margin-top: 0; }
    .gemini-edit { height: 32px; min-height: 32px; padding: 0 8px; font-size: 12px; }
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
    #stop-job { margin-top: 12px; }
    .summary-row { display: flex; flex-direction: column; align-items: stretch; gap: 4px; }
    .summary-row h2 { margin: 0; font-size: 17px; font-weight: 600; text-wrap: balance; }
    .summary-row p { margin: 0; color: var(--muted); font-size: 13px; }
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
    .row-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
    .row-actions .btn { width: 100%; min-height: 40px; }
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

    @media (min-width: 760px) {
      body { font-size: 15px; }
      .page {
        width: min(760px, calc(100% - 48px));
        padding-top: 36px;
      }
      .mast { flex-direction: row; justify-content: space-between; align-items: flex-end; gap: 24px; }
      .steps { display: flex; flex-wrap: wrap; width: auto; background: transparent; gap: 0; }
      .steps li {
        flex-direction: row;
        width: auto;
        padding: 0;
        background: transparent;
        font-size: 13px;
      }
      .steps li:not(:last-child)::after {
        content: "";
        width: 14px;
        height: 1px;
        margin-inline: 8px;
        background: var(--line);
      }
      h1 { font-size: 28px; }
      .lede { font-size: 14px; max-width: 62ch; }
      .sheet { border-radius: 18px; padding: 6px; }
      .sheet-inner { padding: 18px 18px 20px; }
      .choices { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .summary-row { flex-direction: row; justify-content: space-between; align-items: flex-start; }
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
  <main class="page">
    <header class="mast">
      <div class="brand">
        <span class="mark" aria-hidden="true"></span>
        <div>
          <p class="eyebrow">من قروب الدردشة</p>
          <h1>تنفيذ على تغريدات القروب</h1>
        </div>
      </div>
      <ol class="steps" id="steps">
        <li data-step="1" class="is-current"><span class="n">١</span> الجلسة</li>
        <li data-step="2"><span class="n">٢</span> القروب</li>
        <li data-step="3"><span class="n">٣</span> الجمع</li>
        <li data-step="4"><span class="n">٤</span> التنفيذ</li>
      </ol>
    </header>
    <p class="lede">يُفتح تبويب إعادات النشر ويُمرَّر حتى يكتمل العدد أو ينتهي الخط. ترى حالة كل إجراء، ثم تقرر. لا يُمس الحساب قبل «تأكيد التنفيذ».</p>

    <div id="notice" class="notice" role="status" hidden></div>

    <form id="repost-form" class="sheet" autocomplete="off" novalidate>
      <div class="sheet-inner">
        <input id="csrf_token" type="hidden" value="{{ csrf_token }}">
        <section class="block">
          <div class="block-head">
            <h2>الجلسة</h2>
            <p>يُحفظ الرمز مع اسم الحساب على هذا الخادم فقط، ويبقى بعد إغلاق المتصفح أو إعادة تشغيل السكربت.</p>
          </div>
          <label class="field-label" for="auth_token">Auth token</label>
          <div class="token-row">
            <input id="auth_token" name="auth_token" type="password" required maxlength="512"
                   autocomplete="off" autocapitalize="off" spellcheck="false"
                   placeholder="الصق الرمز" aria-label="Auth token">
            <button id="toggle-token" class="btn btn-ghost" type="button" aria-pressed="false">إظهار</button>
            <button id="edit-token" class="btn btn-ghost" type="button" aria-pressed="false">تحرير</button>
            <button id="login-button" class="btn btn-ghost" type="button">تحقق</button>
          </div>
          <p id="session-account" class="session-account" hidden></p>
          <div id="saved-sessions" class="saved-sessions" hidden>
            <p class="field-label">جلسات محفوظة</p>
            <ul id="saved-session-list" class="saved-list"></ul>
          </div>
        </section>

        <section class="block">
          <div class="block-head">
            <h2>القروب</h2>
            <p>يُفتح صندوق الدردشة، وتُعرض القروبات فقط. اختر واحدًا ثم العدد.</p>
          </div>
          <label class="field-label" for="chat-pin">رمز الدردشة</label>
          <div class="token-row">
            <input id="chat-pin" name="chat_pin" type="password" inputmode="numeric" maxlength="8" required
                   autocomplete="off" spellcheck="false" placeholder="٤ أرقام" aria-label="رمز الدردشة">
            <button id="prepare-button" class="btn btn-ghost" type="button">تهيئة</button>
            <button id="reload-button" class="btn btn-ghost" type="button">تحديث الصفحة</button>
            <button id="groups-button" class="btn btn-ghost" type="button">عرض القروبات</button>
            <label class="wait-groups" for="wait-groups">
              <input id="wait-groups" type="checkbox">
              انتظار
              <input id="wait-groups-sec" class="wait-sec" type="number" min="1" max="120" step="1" value="10" inputmode="numeric" aria-label="ثواني الانتظار قبل التقاط القروبات">
              ث
            </label>
          </div>
          <div id="group-box" class="saved-sessions" hidden>
            <p class="field-label">اختر قروبًا</p>
            <ul id="group-list" class="saved-list"></ul>
          </div>
          <div class="group-wait-row">
            <label class="wait-groups" for="group-wait-on">
              <input id="group-wait-on" type="checkbox">
              انتظار داخل القروب قبل التمرير
              <input id="group-wait-sec" class="wait-sec" type="number" min="0" max="300" step="1" value="8" inputmode="numeric" aria-label="ثواني الانتظار داخل القروب">
              ث
            </label>
          </div>
          <div class="mode-switch" role="group" aria-label="طريقة التوقف">
            <button id="mode-count" class="preset is-on" type="button">عدد</button>
            <button id="mode-tweet" class="preset" type="button">تغريدة</button>
          </div>
          <div class="stop-row">
            <input id="repost-count" name="repost_count" type="number" min="1" max="1000" step="1" value="25" inputmode="numeric" aria-label="العدد المطلوب">
            <input id="stop-tweet" name="stop_tweet" type="text" maxlength="300" autocomplete="off" spellcheck="false" hidden
                   placeholder="رابط التغريدة" aria-label="تغريدة التوقف">
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
          <div id="gemini-fields" class="gemini-fields" hidden>
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
        <button id="stop-job" class="btn btn-ghost" type="button" hidden>إيقاف التنفيذ</button>
      </div>
    </section>

    <section id="action-preview" class="sheet" hidden>
      <div class="sheet-inner">
        <div class="summary-row">
          <div>
            <h2>المراجعة</h2>
            <p id="preview-summary"></p>
          </div>
          <button id="copy-links" class="btn btn-ghost" type="button">نسخ الروابط</button>
        </div>
        <div class="stats" id="preview-stats"></div>
        <ol id="preview-list" class="post-list"></ol>
        <div class="row-actions">
          <button id="confirm-actions" class="btn btn-pine" type="button">تأكيد التنفيذ</button>
          <button id="cancel-actions" class="btn btn-ghost" type="button">تجاهل المعاينة</button>
        </div>
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

    <p class="footnote">الأعداد الكبيرة تأخذ وقتًا لأن كل دفعة تُنتظر حتى تظهر. إذا أغلقت المتصفح أثناء العملية فافتح الصفحة نفسها لاحقًا: يظهر التقدم من حيث بلغ، أو النتيجة إن اكتملت. الجلسات المحفوظة تبقى بعد إعادة تشغيل السكربت.</p>
  </main>
<script>
(() => {
  const apiRoot = {{ api_root|tojson }};
  const LABELS = {repost: "إعادة النشر", like: "إعجاب", bookmark: "المرجعية", comment: "تعليق"};
  const tokenField = document.getElementById("auth_token");
  const editToken = document.getElementById("edit-token");
  function lockToken(locked) {
    tokenField.readOnly = locked;
    editToken.textContent = locked ? "تحرير" : "تم";
    editToken.setAttribute("aria-pressed", locked ? "false" : "true");
  }
  lockToken(true);
  const countField = document.getElementById("repost-count");
  const stopField = document.getElementById("stop-tweet");
  const modeCount = document.getElementById("mode-count");
  const modeTweet = document.getElementById("mode-tweet");
  let targetMode = "count";
  const pinField = document.getElementById("chat-pin");
  const prepareButton = document.getElementById("prepare-button");
  const reloadButton = document.getElementById("reload-button");
  const groupsButton = document.getElementById("groups-button");
  const waitGroups = document.getElementById("wait-groups");
  const waitGroupsSec = document.getElementById("wait-groups-sec");
  const groupWaitOn = document.getElementById("group-wait-on");
  const groupWaitSec = document.getElementById("group-wait-sec");
  function rememberWaits() {
    try {
      localStorage.setItem("x_wait_settings", JSON.stringify({
        groupsOn: waitGroups.checked, groupsSec: waitGroupsSec.value,
        groupOn: groupWaitOn.checked, groupSec: groupWaitSec.value
      }));
    } catch (_) {}
  }
  try {
    const savedWaits = JSON.parse(localStorage.getItem("x_wait_settings") || "null");
    if (savedWaits) {
      waitGroups.checked = !!savedWaits.groupsOn;
      if (savedWaits.groupsSec) waitGroupsSec.value = savedWaits.groupsSec;
      groupWaitOn.checked = !!savedWaits.groupOn;
      if (savedWaits.groupSec !== undefined && savedWaits.groupSec !== "") groupWaitSec.value = savedWaits.groupSec;
    }
  } catch (_) {}
  [waitGroups, waitGroupsSec, groupWaitOn, groupWaitSec].forEach((node) => node.addEventListener("change", rememberWaits));
  function secondsFrom(field, min, max, fallback) {
    const value = Number.parseInt(field.value, 10);
    if (!Number.isInteger(value)) return fallback;
    return Math.max(min, Math.min(max, value));
  }
  const groupBox = document.getElementById("group-box");
  const groupList = document.getElementById("group-list");
  const commentToggle = document.getElementById("action-comment");
  const geminiFields = document.getElementById("gemini-fields");
  const geminiKey = document.getElementById("gemini-key");
  const geminiModel = document.getElementById("gemini-model");
  const editGemini = document.getElementById("edit-gemini");
  function lockGemini(locked) {
    geminiKey.readOnly = locked;
    geminiModel.readOnly = locked;
    editGemini.textContent = locked ? "تحرير" : "تم";
    editGemini.setAttribute("aria-pressed", locked ? "false" : "true");
  }
  lockGemini(true);
  const csrf = document.getElementById("csrf_token").value;
  const loginButton = document.getElementById("login-button");
  const toggleToken = document.getElementById("toggle-token");
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
  const actionPreview = document.getElementById("action-preview");
  const previewSummary = document.getElementById("preview-summary");
  const previewStats = document.getElementById("preview-stats");
  const previewList = document.getElementById("preview-list");
  const confirmButton = document.getElementById("confirm-actions");
  const cancelButton = document.getElementById("cancel-actions");
  const stopJobButton = document.getElementById("stop-job");
  let stoppingJob = false;
  const copyButton = document.getElementById("copy-links");
  const repostResults = document.getElementById("repost-results");
  const resultStats = document.getElementById("result-stats");
  const repostList = document.getElementById("repost-list");
  const noReposts = document.getElementById("no-reposts");
  const sessionAccount = document.getElementById("session-account");
  const savedSessions = document.getElementById("saved-sessions");
  const savedSessionList = document.getElementById("saved-session-list");
  let currentPreviewId = null;
  let pollGen = 0;
  let progressStarted = 0;
  let sessionRows = [];
  let groupNames = [];
  let selectedGroup = localStorage.getItem("x_selected_group") || "";

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

  function readStoredJob() {
    try {
      const parsed = JSON.parse(localStorage.getItem("x_reposts_active_job") || "");
      if (parsed && typeof parsed.id === "string" && typeof parsed.key === "string") return parsed;
    } catch (_) {}
    return null;
  }

  function rememberJob(id, key) {
    const prev = readStoredJob();
    const startedAt = prev && prev.id === id && prev.startedAt ? prev.startedAt : Date.now();
    localStorage.setItem("x_reposts_active_job", JSON.stringify({id, key, startedAt}));
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
    card.innerHTML = '<div class="progress-top"><div><h3></h3><p class="task-phase"></p><p class="task-msg"></p></div><b class="task-count">0</b></div><div class="meter is-indeterminate"><span class="task-bar"></span></div><button class="btn btn-ghost" type="button">إيقاف هذه المهمة</button>';
    card.querySelector("h3").textContent = task.title || "مهمة";
    card.querySelector("button").addEventListener("click", async () => {
      card.querySelector("button").disabled = true;
      try {
        await postJson("/stop", {job_id: task.id, job_key: task.key});
      } catch (_) {
        card.querySelector("button").disabled = false;
      }
    });
    taskList.prepend(card);
    const paint = (data) => {
      const phase = data.phase || "collect";
      const titles = {login: "فتح القروب", collect: "جمع", act: "تنفيذ"};
      card.querySelector(".task-phase").textContent = titles[phase] || "جارٍ العمل";
      card.querySelector(".task-msg").textContent = data.message || "";
      const current = phase === "act" ? (data.done || 0) : (data.collected || 0);
      const total = data.requested || 0;
      card.querySelector(".task-count").textContent = total ? current + " / " + total : String(current);
      const meter = card.querySelector(".meter");
      const indeterminate = data.status === "running" && (phase === "login" || !total);
      meter.classList.toggle("is-indeterminate", indeterminate);
      card.querySelector(".task-bar").style.width = indeterminate ? "" : Math.max(0, Math.min(100, total ? Math.round((current / total) * 100) : 100)) + "%";
      card.querySelector("button").hidden = data.status !== "running";
    };
    (async () => {
      try {
        for (;;) {
          const response = await fetch(apiRoot + "/jobs/" + encodeURIComponent(task.id), {
            headers: {"X-CSRF-Token": csrf, "X-Job-Key": task.key, "X-Client-Id": clientId()},
            cache: "no-store"
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.message || "تعذّر متابعة المهمة.");
          paint(data);
          if (data.status !== "running") {
            showNotice((task.title ? task.title + ": " : "") + (data.message || ""), Boolean(data.success));
            if (Array.isArray(data.results) && data.results.length) renderResults(data);
            writeTasks(readTasks().filter((row) => row.id !== task.id));
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 700));
        }
      } catch (error) {
        card.querySelector(".task-msg").textContent = error.message || "انقطع متابعة المهمة.";
      } finally {
        liveTasks.delete(task.id);
      }
    })();
  }

  function showAccount(text) {
    sessionAccount.textContent = text || "";
    sessionAccount.hidden = !text;
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
  }

  async function loadSessions() {
    try {
      const response = await fetch(apiRoot + "/sessions", {
        headers: {"X-CSRF-Token": csrf, "X-Client-Id": clientId()},
        cache: "no-store"
      });
      const data = await response.json();
      if (!response.ok) return;
      sessionRows = data.sessions || [];
      renderSessions();
      if (data.gemini_api_key && document.activeElement !== geminiKey) {
        geminiKey.value = data.gemini_api_key;
        localStorage.setItem("x_gemini_key", data.gemini_api_key);
      }
      if (data.gemini_model && document.activeElement !== geminiModel) {
        geminiModel.value = data.gemini_model;
        localStorage.setItem("x_gemini_model", data.gemini_model);
      }
    } catch (_) {}
  }

  function setStep(current) {
    document.querySelectorAll("#steps li").forEach((item) => {
      const step = Number(item.dataset.step);
      item.classList.toggle("is-current", step === current);
      item.classList.toggle("is-done", step < current);
    });
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
    geminiFields.hidden = !commentToggle.checked;
  }

  function setTargetMode(mode) {
    targetMode = mode === "tweet" ? "tweet" : "count";
    modeCount.classList.toggle("is-on", targetMode === "count");
    modeTweet.classList.toggle("is-on", targetMode === "tweet");
    countField.hidden = targetMode !== "count";
    stopField.hidden = targetMode !== "tweet";
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
    const titles = {login: "فتح القروب", collect: "جمع تغريدات القروب", act: "تنفيذ الإجراءات"};
    progressTitle.textContent = titles[phase] || "جارٍ العمل";
    progressMessage.textContent = data.message || "";
    const current = phase === "act" ? (data.done || 0) : (data.collected || 0);
    const total = data.requested || 0;
    progressCount.textContent = String(current);
    progressDenom.textContent = total ? " / " + total : "";
    const pct = total ? Math.max(0, Math.min(100, Math.round((current / total) * 100))) : 0;
    const indeterminate = phase === "login" || !total;
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
    stopJobButton.hidden = data.status !== "running";
    stopJobButton.disabled = stoppingJob;
    setStep(phase === "act" ? 4 : 2);
  }

  async function pollJob(jobId, jobKey) {
    const gen = ++pollGen;
    for (;;) {
      if (gen !== pollGen) return null;
      const response = await fetch(apiRoot + "/jobs/" + encodeURIComponent(jobId), {
        headers: {"X-CSRF-Token": csrf, "X-Job-Key": jobKey || "", "X-Client-Id": clientId()},
        cache: "no-store"
      });
      const data = await response.json();
      if (gen !== pollGen) return null;
      if (response.status === 404) {
        localStorage.removeItem("x_reposts_active_job");
        throw new Error(data.message || "لم يعد سجل هذه العملية متاحًا.");
      }
      if (!response.ok) throw new Error(data.message || "تعذّر متابعة العملية.");
      if (data.status === "running") {
        renderProgress(data);
        if (Array.isArray(data.results) && data.results.length) renderResults(data);
      }
      if (data.status !== "running") return data;
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  }

  function finishTrackedJob(data) {
    stoppingJob = false;
    if (!data || data.status === "running") return;
    showNotice(data.message || "", Boolean(data.success));
    renderProgress(data);
    if (data.status === "interrupted") {
      progressNote.textContent = "آخر حالة قبل توقف السكربت. لن تُستكمل العملية تلقائيًا.";
    }
    const hasResults = Array.isArray(data.results) && data.results.length > 0;
    const hasItems = Array.isArray(data.items) && data.items.length > 0;
    if (data.kind === "execute") {
      if (hasResults) renderResults(data);
      else if (data.status !== "interrupted") setStep(4);
    } else if (hasItems) {
      renderPreview(data);
      if (data.preview_alive === false) {
        confirmButton.disabled = true;
        confirmButton.textContent = "أعد المعاينة قبل التنفيذ";
      }
    } else if (!data.success) {
      setStep(1);
    }
    loadSessions();
  }

  function renderPreview(data) {
    currentPreviewId = data.preview_id;
    const items = data.items || [];
    const actions = data.actions || [];
    let will = 0;
    let skip = 0;
    previewList.replaceChildren();
    items.forEach((post, index) => {
      const item = document.createElement("li");
      const top = document.createElement("div");
      top.className = "post-top";
      const idx = document.createElement("span");
      idx.className = "idx";
      idx.textContent = String(index + 1);
      const link = document.createElement("a");
      link.href = post.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = shortPath(post.url);
      top.append(idx, link);
      item.appendChild(top);
      const text = document.createElement("p");
      text.className = "excerpt";
      text.textContent = post.text || "بدون نص ظاهر";
      item.appendChild(text);
      if (post.needs_parent) {
        const hint = document.createElement("p");
        hint.className = "hint";
        hint.textContent = "ستُفتح هذه التغريدة للوصول إلى الأصلية التي عُلّق عليها، ثم يُنشر الرد هناك.";
        item.appendChild(hint);
      }
      const pills = document.createElement("div");
      pills.className = "pills";
      actions.forEach((action) => {
        const active = Boolean(post.active && post.active[action]);
        if (active) skip += 1; else will += 1;
        pills.appendChild(pill(LABELS[action] + (active ? " · مفعّل" : " · سيُطبَّق"), active ? "is-skip" : "is-will"));
      });
      item.appendChild(pills);
      previewList.appendChild(item);
    });
    previewSummary.textContent = "@" + data.username + " — طُلب " + data.requested_count + " وظهرت " + items.length + ".";
    previewStats.replaceChildren(
      stat(items.length, "منشور"),
      stat(will, "سيُطبَّق"),
      stat(skip, "مفعّل مسبقًا")
    );
    confirmButton.disabled = will === 0;
    confirmButton.textContent = will === 0 ? "لا إجراءات جديدة" : "تأكيد تنفيذ " + will;
    actionPreview.hidden = false;
    reveal(actionPreview);
    setStep(3);
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
    setStep(4);
  }

  toggleToken.addEventListener("click", () => {
    const show = tokenField.type === "password";
    tokenField.type = show ? "text" : "password";
    toggleToken.textContent = show ? "إخفاء" : "إظهار";
    toggleToken.setAttribute("aria-pressed", show ? "true" : "false");
  });

  editToken.addEventListener("click", () => {
    if (tokenField.readOnly) {
      lockToken(false);
      tokenField.focus();
      tokenField.select();
      return;
    }
    lockToken(true);
    pushScreen();
  });

  modeCount.addEventListener("click", () => { setTargetMode("count"); pushScreen(); });
  modeTweet.addEventListener("click", () => { setTargetMode("tweet"); stopField.focus(); pushScreen(); });
  setTargetMode("count");

  commentToggle.addEventListener("change", syncGemini);
  const savedModel = localStorage.getItem("x_gemini_model");
  const savedKey = localStorage.getItem("x_gemini_key");
  if (savedModel) geminiModel.value = savedModel;
  if (savedKey) geminiKey.value = savedKey;
  let geminiSaveTimer = 0;
  function rememberGeminiLocal() {
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

  loginButton.addEventListener("click", async () => {
    const authToken = tokenField.value.trim();
    if (!authToken) { showNotice("أدخل الرمز أولًا.", false); tokenField.focus(); return; }
    loginButton.disabled = true;
    toggleToken.disabled = true;
    showNotice("جارٍ التحقق من الجلسة…", true);
    setStep(1);
    try {
      const data = await postJson("/verify-login", {auth_token: authToken});
      showNotice(data.message, data.success);
      if (data.success) {
        setStep(2);
        showAccount(data.username ? "تم التحقق: هذه الجلسة تخص @" + data.username : "تم التحقق من الدخول، دون قراءة الاسم.");
        await loadSessions();
      }
    } catch (_) {
      showNotice("تعذّر الاتصال لإكمال التحقق.", false);
    } finally {
      loginButton.disabled = false;
      toggleToken.disabled = false;
    }
  });

  function renderGroups() {
    groupBox.hidden = groupNames.length === 0;
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

  reloadButton.addEventListener("click", async () => {
    const authToken = tokenField.value.trim();
    if (!authToken) { showNotice("أدخل رمز الجلسة أولًا.", false); tokenField.focus(); return; }
    reloadButton.disabled = true;
    showNotice("جارٍ تحديث صفحة متصفح السكربت…", true);
    try {
      const data = await postJson("/reload", {auth_token: authToken});
      showNotice(data.message || (data.success ? "تم تحديث صفحة المتصفح." : "تعذّر التحديث."), !!data.success);
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء تحديث المتصفح.", false);
    } finally {
      reloadButton.disabled = false;
    }
  });

  prepareButton.addEventListener("click", async () => {
    const authToken = tokenField.value.trim();
    const pin = pinField.value.trim();
    if (!authToken) { showNotice("أدخل رمز الجلسة أولًا.", false); tokenField.focus(); return; }
    if (!/^\d{4,8}$/.test(pin)) { showNotice("رمز الدردشة ٤ إلى ٨ أرقام.", false); pinField.focus(); return; }
    prepareButton.disabled = true;
    groupsButton.disabled = true;
    showNotice("جارٍ الدخول إلى الخاص وانتظار تجهيز القروبات والرسائل…", true);
    try {
      const data = await postJson("/prepare", {auth_token: authToken, chat_pin: pin});
      if (!data.success) {
        showNotice(data.message || "تعذّرت التهيئة.", false);
        return;
      }
      groupNames = data.groups || [];
      if (selectedGroup && groupNames.length && !groupNames.includes(selectedGroup)) selectedGroup = "";
      renderGroups();
      showNotice(data.message || "تمت التهيئة. الخاص مفتوح وينتظر المهمة.", true);
      setStep(2);
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء التهيئة.", false);
    } finally {
      prepareButton.disabled = false;
      groupsButton.disabled = false;
    }
  });

  groupsButton.addEventListener("click", async () => {
    const authToken = tokenField.value.trim();
    const pin = pinField.value.trim();
    if (!authToken) { showNotice("أدخل رمز الجلسة أولًا.", false); tokenField.focus(); return; }
    if (!/^\d{4,8}$/.test(pin)) { showNotice("رمز الدردشة ٤ إلى ٨ أرقام.", false); pinField.focus(); return; }
    groupsButton.disabled = true;
    prepareButton.disabled = true;
    const settle = !!waitGroups.checked;
    const settleSeconds = secondsFrom(waitGroupsSec, 1, 120, 10);
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
      setStep(2);
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء جلب القروبات.", false);
    } finally {
      groupsButton.disabled = false;
      prepareButton.disabled = false;
    }
  });

  stopJobButton.addEventListener("click", async () => {
    const saved = readStoredJob();
    if (!saved) { showNotice("لا توجد عملية تعمل الآن.", false); return; }
    stoppingJob = true;
    stopJobButton.disabled = true;
    showNotice("جارٍ إيقاف التنفيذ…", true);
    try {
      const data = await postJson("/stop", {job_id: saved.id, job_key: saved.key});
      if (!data.success) {
        stoppingJob = false;
        stopJobButton.disabled = false;
        showNotice(data.message || "تعذّر الإيقاف.", false);
      }
    } catch (_) {
      stoppingJob = false;
      stopJobButton.disabled = false;
      showNotice("تعذّر الاتصال أثناء الإيقاف.", false);
    }
  });

  document.getElementById("repost-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const authToken = tokenField.value.trim();
    const pin = pinField.value.trim();
    const count = Number.parseInt(countField.value, 10);
    const stopRaw = targetMode === "tweet" ? stopField.value.trim() : "";
    const stopMatch = stopRaw.match(/status\/(\d{6,25})/);
    const stopId = stopMatch ? stopMatch[1] : (/^\d{15,25}$/.test(stopRaw) ? stopRaw : "");
    const actions = selectedActions();
    if (!authToken) { showNotice("أدخل الرمز أولًا.", false); tokenField.focus(); return; }
    if (!/^\d{4,8}$/.test(pin)) { showNotice("أدخل رمز الدردشة.", false); pinField.focus(); return; }
    if (!selectedGroup) { showNotice("اعرض القروبات ثم اختر واحدًا.", false); return; }
    if (targetMode === "tweet" && !stopId) { showNotice("الصق رابط التغريدة أو رقمها.", false); stopField.focus(); return; }
    if (targetMode === "count" && (!Number.isInteger(count) || count < 1 || count > 1000)) {
      showNotice("أدخل عددًا من 1 إلى 1000.", false);
      countField.focus();
      return;
    }
    if (!actions.length) { showNotice("اختر إجراءً واحدًا على الأقل.", false); return; }
    if (actions.includes("comment")) {
      if (!geminiKey.value.trim()) { lockGemini(false); showNotice("أدخل مفتاح Gemini.", false); geminiKey.focus(); return; }
      if (!geminiModel.value.trim()) { lockGemini(false); showNotice("أدخل اسم النموذج.", false); geminiModel.focus(); return; }
      rememberGeminiLocal();
    }
    pollGen += 1;
    repostButton.disabled = true;
    try {
      const started = await postJson("/group-run", {
        auth_token: authToken,
        chat_pin: pin,
        group: selectedGroup,
        count: stopId ? 0 : count,
        stop_tweet: stopRaw,
        actions,
        gemini_api_key: geminiKey.value.trim(),
        gemini_model: geminiModel.value.trim(),
        group_wait: groupWaitOn.checked ? secondsFrom(groupWaitSec, 0, 300, 8) : null
      });
      if (!started.success || !started.job_id || !started.job_key) {
        showNotice(started.message || "تعذّر بدء التنفيذ.", false);
        return;
      }
      const account = (sessionAccount.textContent || "").replace("الجلسة المحفوظة: ", "");
      followTask({
        id: started.job_id,
        key: started.job_key,
        title: selectedGroup + (account ? " · " + account : ""),
        startedAt: Date.now()
      });
      showNotice("بدأت مهمة «" + selectedGroup + "». يمكن بدء مهمة أخرى على نفس الحساب أو حساب مختلف.", true);
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء التنفيذ.", false);
    } finally {
      repostButton.disabled = false;
    }
  });

  confirmButton.addEventListener("click", async () => {
    if (!currentPreviewId) { showNotice("انتهت المعاينة. ابدأ معاينة جديدة.", false); return; }
    const authToken = tokenField.value.trim();
    if (!authToken) { showNotice("أدخل الرمز أولًا.", false); tokenField.focus(); return; }
    confirmButton.disabled = true;
    cancelButton.disabled = true;
    repostButton.disabled = true;
    const total = previewList.children.length;
    progressStarted = performance.now();
    renderProgress({phase: "act", done: 0, requested: total, message: "بدء التنفيذ على ما وافقت عليه…", status: "running"});
    actionPreview.hidden = true;
    showNotice("بدأ التنفيذ. يمكن متابعة العدد أدناه.", true);
    try {
      const started = await postJson("/execute-actions", {
        auth_token: authToken,
        preview_id: currentPreviewId,
        gemini_api_key: geminiKey.value.trim(),
        gemini_model: geminiModel.value.trim()
      });
      if (!started.success || !started.job_id || !started.job_key) {
        progressPanel.hidden = true;
        actionPreview.hidden = false;
        showNotice(started.message || "تعذّر بدء التنفيذ.", false);
        return;
      }
      currentPreviewId = null;
      rememberJob(started.job_id, started.job_key);
      const data = await pollJob(started.job_id, started.job_key);
      if (!data) return;
      finishTrackedJob(data);
    } catch (_) {
      progressPanel.hidden = true;
      showNotice("تعذّر الاتصال أثناء التنفيذ. راجع النتيجة قبل إعادة المحاولة.", false);
    } finally {
      confirmButton.disabled = false;
      cancelButton.disabled = false;
      repostButton.disabled = false;
    }
  });

  cancelButton.addEventListener("click", async () => {
    if (currentPreviewId) {
      try { await postJson("/cancel-preview", {preview_id: currentPreviewId}); } catch (_) {}
    }
    currentPreviewId = null;
    actionPreview.hidden = true;
    showNotice("أُلغيت المعاينة. لم يُنفَّذ أي إجراء.", true);
    setStep(2);
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

  async function resumeStoredJob() {
    readTasks().forEach((task) => followTask(task));
    const stored = readStoredJob();
    if (stored) followTask({id: stored.id, key: stored.key, title: "مهمة سابقة", startedAt: stored.startedAt || Date.now()});
  }

  let screenTimer = 0;
  function pushScreen() {
    clearTimeout(screenTimer);
    screenTimer = setTimeout(() => {
      const payload = {
        auth_token: tokenField.value.trim(),
        chat_pin: pinField.value.trim(),
        group: selectedGroup,
        count: Number.parseInt(countField.value, 10) || 25,
        stop_tweet: targetMode === "tweet" ? stopField.value.trim() : "",
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
    if (data.count) countField.value = String(data.count);
    if (typeof data.stop_tweet === "string") stopField.value = data.stop_tweet;
    setTargetMode(data.stop_tweet ? "tweet" : "count");
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
    let runningJobs = [];
    let screenJob = null;
    try {
      const response = await fetch(apiRoot + "/state", {cache: "no-store"});
      const data = await response.json();
      if (response.ok && data && data.success) {
        applyScreen(data);
        screenJob = data;
        runningJobs = Array.isArray(data.running_jobs) ? data.running_jobs : [];
      }
    } catch (_) {}
    await loadSessions();
    readTasks().forEach((task) => followTask(task));
    const stored = readStoredJob();
    if (stored) followTask({id: stored.id, key: stored.key, title: "مهمة سابقة", startedAt: stored.startedAt || Date.now()});
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
  countField.addEventListener("change", pushScreen);
  stopField.addEventListener("change", pushScreen);
  document.querySelectorAll('#repost-form input[type="checkbox"]').forEach((box) => {
    box.addEventListener("change", pushScreen);
  });

  restoreScreen();

  copyButton.addEventListener("click", async () => {
    const links = [...previewList.querySelectorAll("a")].map((anchor) => anchor.href).join("\n");
    if (!links) return;
    try {
      await navigator.clipboard.writeText(links);
      copyButton.textContent = "نُسخت";
      setTimeout(() => { copyButton.textContent = "نسخ الروابط"; }, 1400);
    } catch (_) {
      showNotice("تعذّر النسخ من هذا المتصفح.", false);
    }
  });
})();
</script>
</body>
</html>

'''

PAGE = load_page()

READ_TWEETS_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const statusPath = (href) => {
    const match = (href || "").match(/^(?:https?:\/\/(?:www\.)?(?:x|twitter)\.com)?(\/[A-Za-z0-9_]+\/status\/\d+)/);
    return match ? match[1] : "";
  };
  const posts = [];
  for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
    const time = article.querySelector("time");
    const timed = time && time.closest('a[href*="/status/"]');
    let href = timed ? (timed.getAttribute("href") || "") : "";
    if (!href) {
      const link = article.querySelector('a[href*="/status/"]');
      href = link ? (link.getAttribute("href") || "") : "";
    }
    const selfPath = statusPath(href);
    let parent = "";
    const quote = article.querySelector('[data-testid="quoteTweet"]');
    for (const link of article.querySelectorAll('a[href*="/status/"]')) {
      if (quote && quote.contains(link)) continue;
      const path = statusPath(link.getAttribute("href") || "");
      if (!path || path === selfPath) continue;
      parent = path;
      break;
    }
    let author = "";
    const name = article.querySelector('[data-testid="User-Name"] a[href^="/"]');
    if (name) {
      const match = (name.getAttribute("href") || "").match(/^\/([A-Za-z0-9_]{1,15})(?:\/|$)/);
      if (match) author = match[1];
    }
    const social = article.querySelector('[data-testid="socialContext"]');
    const text = article.querySelector('[data-testid="tweetText"]');
    const group = article.querySelector('[role="group"]');
    const scope = group || article;
    posts.push({
      href,
      parent,
      author,
      social: clean(social ? social.innerText : ""),
      text: clean(text ? text.innerText : "").slice(0, 280),
      repost: !!scope.querySelector('[data-testid="unretweet"]'),
      like: !!scope.querySelector('[data-testid="unlike"]'),
      bookmark: !!scope.querySelector('[data-testid="removeBookmark"]'),
    });
  }
  return posts;
}
"""

TIMELINE_BUSY_JS = r"""
() => {
  const count = document.querySelectorAll('article[data-testid="tweet"]').length;
  const busy = Array.from(document.querySelectorAll('[role="progressbar"]')).some((el) => el.getClientRects().length);
  return count === 0 || busy;
}
"""

SCROLL_TIMELINE_JS = r"""
() => {
  function findScroller() {
    const article = document.querySelector('article[data-testid="tweet"]');
    const column = document.querySelector('[data-testid="primaryColumn"]');
    const start = article || column || document.body;
    let el = start;
    while (el && el !== document.documentElement) {
      const style = getComputedStyle(el);
      const overflow = style.overflowY + " " + style.overflow;
      if (el.scrollHeight > el.clientHeight + 80 && /(auto|scroll|overlay)/.test(overflow)) return el;
      el = el.parentElement;
    }
    const root = document.scrollingElement || document.documentElement;
    if (root.scrollHeight > root.clientHeight + 80) return root;
    let best = root;
    let bestRoom = root.scrollHeight - root.clientHeight;
    el = start;
    while (el) {
      const room = el.scrollHeight - el.clientHeight;
      if (room > bestRoom) {
        best = el;
        bestRoom = room;
      }
      el = el.parentElement;
    }
    return best;
  }
  const scroller = findScroller();
  const articles = document.querySelectorAll('article[data-testid="tweet"]');
  const last = articles[articles.length - 1] || null;
  const beforeTop = scroller.scrollTop || 0;
  const delta = Math.max(420, Math.floor((scroller.clientHeight || window.innerHeight) * 0.55));
  try { scroller.scrollTop = beforeTop + delta; } catch (error) {}
  let top = scroller.scrollTop || 0;
  if (Math.abs(top - beforeTop) < 8) {
    window.scrollBy(0, delta);
    if (last && last.scrollIntoView) last.scrollIntoView({block: "end", inline: "nearest"});
    try { scroller.scrollTop = (scroller.scrollTop || 0) + Math.floor(delta * 0.35); } catch (error) {}
    top = scroller.scrollTop || window.scrollY || 0;
  }
  try {
    scroller.dispatchEvent(new WheelEvent("wheel", {deltaY: delta, bubbles: true, cancelable: true}));
  } catch (error) {}
  const height = scroller.scrollHeight || document.documentElement.scrollHeight || 0;
  const client = scroller.clientHeight || window.innerHeight || 0;
  return {
    moved: Math.abs(top - beforeTop) > 8,
    atBottom: top + client >= height - 160,
    top,
    height,
  };
}
"""

SCROLL_TOP_JS = r"""
() => {
  const article = document.querySelector('article[data-testid="tweet"]');
  let el = article || document.body;
  const seen = new Set();
  while (el && !seen.has(el)) {
    seen.add(el);
    try { el.scrollTop = 0; } catch (error) {}
    el = el.parentElement;
  }
  window.scrollTo(0, 0);
}
"""

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

ALLOWED_ACTIONS = {"repost", "like", "bookmark", "comment"}


def normalize_username(value: str) -> str:
    username = (value or "").strip()
    if username.startswith("@"):
        username = username[1:]
    return username


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


def status_owner(path: str) -> str:
    match = re.match(r"/([^/]+)/status/", path or "")
    return (match.group(1) if match else "").lower()


def comment_targets_from_posts(posts: list[dict], username: str) -> list[dict]:
    """Parent tweets a profile replied to, taken from one replies-tab viewport."""
    owner = username.lower()
    targets: list[dict] = []
    for index, post in enumerate(posts):
        if not isinstance(post, dict):
            continue
        author = str(post.get("author") or "").strip().lstrip("@").lower()
        if author != owner:
            continue
        self_path = normalize_status_path(str(post.get("href") or ""))
        parent_path = normalize_status_path(str(post.get("parent") or ""))
        excerpt = str(post.get("text") or "")[:280]
        chosen = None
        if parent_path and status_owner(parent_path) != owner:
            chosen = parent_path
        elif index > 0 and isinstance(posts[index - 1], dict):
            prev = posts[index - 1]
            prev_author = str(prev.get("author") or "").strip().lstrip("@").lower()
            prev_path = normalize_status_path(str(prev.get("href") or ""))
            if prev_path and prev_author and prev_author != owner:
                chosen = prev_path
                excerpt = str(prev.get("text") or excerpt)[:280]
        if not chosen:
            continue
        targets.append({
            "url": "https://x.com" + chosen,
            "text": excerpt,
            "needs_parent": False,
            "active": {"repost": False, "like": False, "bookmark": False, "comment": False},
        })
    return targets


def own_replies_from_posts(posts: list[dict], username: str) -> list[dict]:
    owner = username.lower()
    rows: list[dict] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        author = str(post.get("author") or "").strip().lstrip("@").lower()
        path = normalize_status_path(str(post.get("href") or ""))
        if author != owner or not path:
            continue
        rows.append({
            "url": "https://x.com" + path,
            "text": str(post.get("text") or "")[:280],
            "needs_parent": True,
            "active": {"repost": False, "like": False, "bookmark": False, "comment": False},
        })
    return rows


def read_timeline_posts(page) -> list[dict]:
    try:
        posts = page.evaluate(READ_TWEETS_JS)
    except Exception:
        return []
    if not isinstance(posts, list):
        return []
    return [post for post in posts if isinstance(post, dict)]


def timeline_is_busy(page) -> bool:
    try:
        return bool(page.evaluate(TIMELINE_BUSY_JS))
    except Exception:
        return False


def click_retry(page) -> bool:
    for label in ("Retry", "Try again", "إعادة المحاولة", "حاول مرة أخرى"):
        button = page.get_by_role("button", name=label)
        try:
            if button.count() == 0:
                continue
            button.first.click(timeout=2000)
            page.wait_for_timeout(700)
            return True
        except Exception:
            continue
    return False


def wait_for_new_posts(page, seen: set[str], timeout_s: float) -> bool:
    """Poll until a not-yet-collected status mounts, or the timeline settles."""
    deadline = time.monotonic() + timeout_s
    extended = False
    while time.monotonic() < deadline:
        page.wait_for_timeout(280)
        for post in read_timeline_posts(page):
            path = normalize_status_path(str(post.get("href") or ""))
            if path and path not in seen:
                return True
        if not extended and timeline_is_busy(page):
            deadline += 6
            extended = True
    return False


def absorb_visible_posts(page, seen: set[str], found: list[dict], requested_count: int, username: str = "", source: str = "reposts", fallback: list[dict] | None = None) -> int:
    if source == "replies":
        return absorb_reply_targets(page, seen, found, requested_count, username, fallback)
    added = 0
    for post in read_timeline_posts(page):
        path = normalize_status_path(str(post.get("href") or ""))
        if not path or path in seen:
            continue
        seen.add(path)
        found.append({
            "url": "https://x.com" + path,
            "text": str(post.get("text") or "")[:280],
            "active": {
                "repost": bool(post.get("repost")),
                "like": bool(post.get("like")),
                "bookmark": bool(post.get("bookmark")),
            },
        })
        added += 1
        if len(found) >= requested_count:
            break
    return added


def absorb_reply_targets(page, seen: set[str], found: list[dict], requested_count: int, username: str, fallback: list[dict] | None) -> int:
    posts = read_timeline_posts(page)
    added = 0
    for item in comment_targets_from_posts(posts, username):
        path = normalize_status_path(item["url"])
        if not path or path in seen:
            continue
        seen.add(path)
        found.append(item)
        added += 1
        if len(found) >= requested_count:
            break
    if fallback is not None:
        known = {normalize_status_path(item["url"]) for item in fallback}
        for item in own_replies_from_posts(posts, username):
            path = normalize_status_path(item["url"])
            if not path or path in seen or path in known:
                continue
            known.add(path)
            fallback.append(item)
    return added


def collect_repost_items(page, requested_count: int, on_progress=None, username: str = "", source: str = "reposts") -> list[dict]:
    """Scroll the virtualized timeline until enough posts mount or it ends."""
    seen: set[str] = set()
    found: list[dict] = []
    fallback: list[dict] = []

    def report() -> None:
        if on_progress:
            on_progress(len(found), requested_count, "collect")

    for _ in range(50):
        if absorb_visible_posts(page, seen, found, requested_count, username, source, fallback):
            break
        page.wait_for_timeout(200)
    report()
    if len(found) >= requested_count:
        return found[:requested_count]

    stagnant = 0
    max_passes = min(5000, max(90, requested_count * 4))
    for _ in range(max_passes):
        if len(found) >= requested_count:
            break
        try:
            metrics = page.evaluate(SCROLL_TIMELINE_JS) or {}
        except Exception:
            metrics = {}
        timeout_s = 2.4 if stagnant == 0 else min(8.0, 2.4 + stagnant * 1.3)
        if stagnant in (2, 5):
            click_retry(page)
        appeared = wait_for_new_posts(page, seen, timeout_s)
        added = absorb_visible_posts(page, seen, found, requested_count, username, source, fallback)
        if added or appeared:
            stagnant = 0
            if added:
                report()
            continue
        stagnant += 1
        at_bottom = bool(metrics.get("atBottom"))
        moved = bool(metrics.get("moved"))
        # A slow page is not a failure: stop only after repeated settled misses.
        if stagnant >= 6 and (at_bottom or not moved):
            break
        if stagnant >= 10:
            break
    if source == "replies" and len(found) < requested_count:
        for item in fallback:
            path = normalize_status_path(item["url"])
            if not path or path in seen:
                continue
            seen.add(path)
            found.append(item)
            if len(found) >= requested_count:
                break
    report()
    label = "replies" if source == "replies" else "reposts"
    print(f"X {label} collected {len(found)}/{requested_count}", flush=True)
    return found[:requested_count]


_mouse_at: dict[int, tuple[float, float]] = {}


def _mouse_start(page, target_x: float, target_y: float) -> tuple[float, float]:
    remembered = _mouse_at.get(id(page))
    if remembered:
        return remembered
    size = page.viewport_size or {"width": 1280, "height": 900}
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
    width = float(viewport.get("width") or 1280)
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


def _move_like_a_hand(page, x: float, y: float) -> None:
    viewport = page.viewport_size or {"width": 1280, "height": 900}
    start = _mouse_start(page, x, y)
    for px, py in _human_path(start, (x, y), viewport):
        page.mouse.move(px, py)
    _mouse_at[id(page)] = (x, y)


def real_click(locator) -> None:
    """Use the selector only to read the button box, then send a real mouse click."""
    locator.wait_for(state="visible", timeout=8000)
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
    x = box["x"] + local_x
    y = box["y"] + local_y
    page = locator.page
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
    _move_like_a_hand(page, x, y)
    page.mouse.down()
    page.wait_for_timeout(100)
    page.mouse.up()


def read_action_state(page, status_id: str, actions: list[str]) -> dict | None:
    payload = page.evaluate(
        """({statusId, names}) => {
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
          try { card.scrollIntoView({block: "center"}); } catch (error) {}
          const map = {repost: ["unretweet", "retweet"], like: ["unlike", "like"], bookmark: ["removeBookmark", "bookmark"]};
          const out = {};
          for (const name of names) {
            const pair = map[name];
            if (!pair) continue;
            const on = card.querySelector('[data-testid="' + pair[0] + '"]');
            const off = card.querySelector('div[role="group"] [data-testid="' + pair[1] + '"]') || card.querySelector('[data-testid="' + pair[1] + '"]');
            out[name] = {on: !!on, box: on ? null : boxOf(off)};
          }
          return out;
        }""",
        {"statusId": status_id, "names": actions},
    )
    return payload if isinstance(payload, dict) else None


ACTION_BUTTON_PROBE_JS = r"""
({statusId, testid, scroll}) => {
  const articles = [...document.querySelectorAll('article[data-testid="tweet"]')];
  let card = null;
  if (statusId) {
    card = articles.find((article) => [...article.querySelectorAll('a[href*="/status/"]')]
      .some((node) => (node.getAttribute("href") || "").includes("/status/" + statusId)));
  }
  if (!card) card = articles[0] || null;
  if (!card) return null;
  const button = card.querySelector('div[role="group"] [data-testid="' + testid + '"]')
    || card.querySelector('[data-testid="' + testid + '"]');
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
  const ok = !!hit && (button === hit || button.contains(hit));
  let blocker = "";
  if (!ok && hit) {
    const tag = hit.closest("[data-testid]");
    blocker = (tag && tag.getAttribute("data-testid")) || hit.tagName.toLowerCase();
  }
  return {box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}, ok, blocker,
          inView: y > 0 && y < window.innerHeight};
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


def press_action_button(page, path: str, status_id: str, action: str) -> str:
    """Scroll the button into view, make sure nothing covers it, then click it.

    Returns "" after a click reached the button, "missing" when the button is
    not on the page, or the name of whatever was covering the button.
    """
    testid = ACTION_CONTROLS[action][1]
    blocker = ""
    for _ in range(3):
        close_popups(page)
        try:
            page.evaluate(ACTION_BUTTON_PROBE_JS, {"statusId": status_id, "testid": testid, "scroll": True})
        except Exception:
            pass
        page.wait_for_timeout(500)
        try:
            info = page.evaluate(ACTION_BUTTON_PROBE_JS, {"statusId": status_id, "testid": testid, "scroll": False})
        except Exception:
            info = None
        if not info or not info.get("box"):
            return "missing"
        if info.get("ok") and info.get("inView"):
            click_box(page, info["box"])
            return ""
        blocker = str(info.get("blocker") or "خارج الشاشة")
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
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                again = read_action_state(page, status_id, [action]) or {}
                if (again.get(action) or {}).get("on"):
                    confirmed = True
                    break
                page.wait_for_timeout(60)
            outcome[action] = "تم التنفيذ" if confirmed else "أُرسل الطلب لكن تعذر تأكيد الحالة"
        except Exception as exc:
            if browser_closed_error(exc):
                raise BrowserClosed() from exc
            outcome[action] = "تعذر التنفيذ"
    return outcome


def act_on_visible(page, pending: dict[str, dict], actions: list[str], results: list[dict], on_item=None) -> int:
    acted = 0
    visible: list[str] = []
    for post in read_timeline_posts(page):
        path = normalize_status_path(str(post.get("href") or ""))
        if path and path in pending and path not in visible:
            visible.append(path)
    for path in visible:
        if path not in pending:
            continue
        item = pending.pop(path)
        result = results[item["_index"]]
        result["actions"] = apply_actions_on_path(page, path, actions)
        acted += 1
        if on_item:
            on_item()
    return acted


def execute_selected_actions(page, items: list[dict], actions: list[str], on_progress=None) -> list[dict]:
    """Apply confirmed actions while scrolling, before virtualization drops each post."""
    pending: dict[str, dict] = {}
    results: list[dict] = []
    for index, item in enumerate(items):
        path = normalize_status_path(item.get("url", ""))
        results.append({"url": item.get("url", ""), "text": item.get("text", ""), "actions": {}})
        if not path:
            results[-1]["actions"] = {key: "تعذر العثور على المنشور" for key in actions}
            continue
        pending[path] = {"_index": index}

    finished = len(items) - len(pending)

    def note() -> None:
        nonlocal finished
        finished += 1
        if on_progress:
            on_progress(finished, len(items), "act")

    if on_progress:
        on_progress(finished, len(items), "act")

    def run_passes(limit: int) -> None:
        stagnant = 0
        for _ in range(limit):
            if not pending:
                return
            acted = act_on_visible(page, pending, actions, results, note)
            if not pending:
                return
            try:
                metrics = page.evaluate(SCROLL_TIMELINE_JS) or {}
            except Exception:
                metrics = {}
            page.wait_for_timeout(450 if acted else 900)
            if timeline_is_busy(page):
                page.wait_for_timeout(1500)
            if acted:
                stagnant = 0
                continue
            stagnant += 1
            if stagnant in (2, 4):
                click_retry(page)
            if stagnant >= 6 and (bool(metrics.get("atBottom")) or not bool(metrics.get("moved"))):
                return
            if stagnant >= 8:
                return

    try:
        page.evaluate(SCROLL_TOP_JS)
    except Exception:
        pass
    max_passes = min(5000, max(90, len(items) * 4))
    run_passes(max_passes)
    if pending:
        try:
            page.evaluate(SCROLL_TOP_JS)
        except Exception:
            pass
        page.wait_for_timeout(600)
        run_passes(max_passes)
    for path, item in pending.items():
        results[item["_index"]]["actions"] = {key: "تعذر العثور على المنشور" for key in actions}
    return results


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
    words = [word for word in (text or "").split() if word]
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


def gemini_comment(api_key: str, model: str, image: bytes | None, tweet_text: str) -> str:
    prompt = (
        "Write one complete public reply of 10 to 20 words. "
        "Finish the sentence. Never stop mid-word or mid-phrase, and do not end with a comma. "
        "Use the same language as the tweet. Sound like a real person: specific, casual, "
        "not like a brand or an assistant. No hashtags, no links, no quotes, no labels, no introduction. "
        "Return only the finished reply."
    )
    if image:
        prompt = "Look at the tweet in the image. " + prompt
    if tweet_text:
        prompt += "\n\nTweet text:\n" + tweet_text[:280]
    parts: list[dict] = [{"text": prompt}]
    if image:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode("ascii")}})
    attempts: list[tuple[dict | None, int]] = [(gemini_thinking_config(model), 2048), (None, 4096)]
    busy_waits = (5, 12, 20, 30)
    best = ""
    saw_busy = False
    for thinking, max_tokens in attempts:
        config: dict = {"temperature": 0.7, "maxOutputTokens": max_tokens}
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
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        return best
    if saw_busy:
        raise RuntimeError("النموذج مشغول مؤقتًا بعد عدة محاولات. غيّر النموذج أو أعد التنفيذ بعد قليل.")
    return ""


def screenshot_article(card) -> bytes | None:
    try:
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
    """Wait for the tweet to settle, then open the reply box. Retry if it is late."""
    for attempt in range(3):
        page.wait_for_timeout(800 if attempt == 0 else 1400)
        if composer_visible(page):
            real_click(page.locator('[data-testid="tweetTextarea_0"]').first)
            page.wait_for_timeout(400)
            return
        card = article_locator(page, path)
        try:
            if card.count():
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


def click_reply_send(page) -> None:
    enabled = page.locator(
        '[data-testid="tweetButtonInline"]:not([aria-disabled="true"]), '
        '[data-testid="tweetButton"]:not([aria-disabled="true"])'
    )
    if enabled.count() == 0:
        raise PlaywrightTimeoutError("reply button stayed disabled")
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


def execute_comment_actions(page, items: list[dict], actions: list[str], api_key: str, model: str, on_progress=None, should_stop=None, on_snapshot=None) -> list[dict]:
    allow_media(page)
    results: list[dict] = []
    plain = [action for action in actions if action != "comment"]
    total = len(items)
    if on_progress:
        on_progress(0, total, "act")
    for index, item in enumerate(items):
        if should_stop and should_stop():
            break
        result = {"url": item.get("url", ""), "text": item.get("text", ""), "actions": {}, "comment": ""}
        path = normalize_status_path(str(item.get("url") or ""))
        if not path:
            result["actions"] = {key: "تعذر العثور على المنشور" for key in actions}
            results.append(result)
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        opened, reason = open_status_page(page, str(item.get("url") or ""))
        if not opened and reason != "gone":
            opened, reason = open_status_page(page, str(item.get("url") or ""))
        if not opened:
            note = "محذوفة — تم التخطي" if reason == "gone" else "تعذر فتح التغريدة"
            result["actions"] = {key: note for key in actions}
            results.append(result)
            if on_progress:
                on_progress(index + 1, total, "act")
            continue
        path = opened
        result["url"] = "https://x.com" + path
        try:
            page.wait_for_timeout(400)
            target_path, card = resolve_comment_target(page, path, bool(item.get("needs_parent")))
            result["url"] = "https://x.com" + target_path
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
            if "comment" in actions:
                comment = gemini_comment(api_key, model, image, str(item.get("text") or ""))
                if len(comment.split()) < 4:
                    comment = gemini_comment(api_key, model, image, str(item.get("text") or ""))
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
        results.append(result)
        if on_snapshot:
            on_snapshot(list(results))
        if on_progress:
            on_progress(index + 1, total, "act")
        page.wait_for_timeout(1400)
    return results


def launch_x_browser(playwright, auth_token: str):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    context, page = open_x_context(browser, auth_token)
    return browser, page


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


def open_x_context(browser, auth_token: str):
    context = browser.new_context(
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

    def page_for(self, auth_token: str):
        if not self._browser_alive():
            if self._playwright is None:
                self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = None
            self._page = None
            self._token = ""
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
MAX_PARALLEL_JOBS = 4


PROFILES_DIR = DATA_DIR / "profiles"
# Page height while opening the chat and collecting from the group, and for everything else.
COLLECT_VIEWPORT_HEIGHT = 50000
NORMAL_VIEWPORT_HEIGHT = 2500
# Browser profiles kept per account. Each profile can run one task at a time, so an
# account can run this many tasks at the same time.
PROFILES_PER_ACCOUNT = 3
profile_locks: dict[str, threading.Lock] = {}
profile_locks_guard = threading.Lock()


def profile_slot_dir(key: str, slot: int) -> Path:
    # Slot 1 keeps the original folder name so an existing profile is reused.
    return PROFILES_DIR / (key if slot == 1 else f"{key}-{slot}")


def acquire_profile_slot(key: str) -> tuple[int, threading.Lock]:
    """Take the first free profile of this account, waiting if all are busy."""
    while True:
        for slot in range(1, PROFILES_PER_ACCOUNT + 1):
            lock = profile_lock(f"{key}-{slot}")
            if lock.acquire(blocking=False):
                return slot, lock
        time.sleep(0.5)


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


def run_on_fresh_browser(auth_token: str, work):
    """Open the account's own permanent browser profile for one task, then close the browser.

    Each account has PROFILES_PER_ACCOUNT profile folders under data/profiles, each
    created the first time it is needed. Cookies and chat data stay in them between
    tasks. A task takes the first free profile of its account, so up to
    PROFILES_PER_ACCOUNT tasks of one account run at the same time; more wait.
    """
    key = profile_key(auth_token)
    slot, lock = acquire_profile_slot(key)
    try:
        playwright = sync_playwright().start()
        context = None
        try:
            context, page = open_x_profile(playwright, auth_token, profile_slot_dir(key, slot))
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
    finally:
        lock.release()


def set_page_height(page, height: int) -> None:
    try:
        page.set_viewport_size({"width": 1280, "height": int(height)})
        page.wait_for_timeout(500)
    except Exception:
        pass


def delete_profile(auth_token: str) -> None:
    key = profile_key(auth_token)
    for slot in range(1, PROFILES_PER_ACCOUNT + 1):
        lock = profile_lock(f"{key}-{slot}")
        if not lock.acquire(timeout=5):
            continue
        try:
            shutil.rmtree(profile_slot_dir(key, slot), ignore_errors=True)
        finally:
            lock.release()



def run_on_browser(auth_token: str, work):
    """Every action gets its own browser, closed as soon as the action ends."""
    return run_on_fresh_browser(auth_token, work)


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


def verify_x_login(auth_token: str) -> tuple[bool, str]:
    """Verify only that the X session is authenticated; do not open a profile."""
    def work(page):
        if not ensure_logged_in(page):
            return False, ""
        username = read_session_username(page)
        if not username:
            page.wait_for_timeout(1200)
            username = read_session_username(page)
        return True, username
    return run_on_browser(auth_token, work)


def open_reposts_tab(page, username: str, source: str = "reposts") -> str | None:
    leaf = "with_replies" if source == "replies" else "reposts"
    page.goto(
        "https://x.com/" + quote(username, safe="_") + "/" + leaf,
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    try:
        page.locator('article[data-testid="tweet"]').first.wait_for(state="attached", timeout=20_000)
    except PlaywrightTimeoutError:
        pass
    try:
        body_text = page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 5000)")
    except Exception:
        body_text = ""
    if re.search(r"This account doesn.t exist|Account suspended|هذا الحساب غير موجود|الحساب موقوف", body_text, re.I):
        return "profile"
    return None


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


def show_all_inbox(page) -> None:
    choose_inbox_filter(page, "dm-inbox-dropdown-all", r"^all$|الكل|all conversations|كل الرسائل")


INBOX_TOP_JS = r"""
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


def nudge_inbox(page, delta: int) -> None:
    try:
        page.evaluate(SCROLL_INBOX_JS)
    except Exception:
        pass
    try:
        panel = page.locator('[data-testid="dm-inbox-panel"]').first
        box = panel.bounding_box()
        if box:
            page.mouse.move(box["x"] + min(box["width"] / 2, 280), box["y"] + min(box["height"] * 0.55, 420))
            page.mouse.wheel(0, delta)
    except Exception:
        pass


def settle_inbox(page) -> int:
    last = -1
    stable = 0
    rows = 0
    for _ in range(22):
        try:
            payload = page.evaluate(READ_GROUPS_JS, "") or {}
        except Exception:
            payload = {}
        rows = int(payload.get("rows") or 0)
        loading = bool(payload.get("loading"))
        nudge_inbox(page, 650)
        page.wait_for_timeout(650)
        if rows > 0 and rows == last and not loading:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = rows
    try:
        page.evaluate(INBOX_TOP_JS)
    except Exception:
        pass
    page.wait_for_timeout(400)
    return rows


def prepare_chat(auth_token: str, pin: str) -> dict:
    def work(page):
        if not ensure_logged_in(page):
            return {"ok": False, "message": "الجلسة غير مسجّلة الدخول.", "groups": []}
        handle = read_session_username(page)
        if handle:
            remember_session(auth_token, handle, pin)
        error = open_chat(page, pin)
        if error:
            return {"ok": False, "message": error, "groups": []}
        show_all_inbox(page)
        rows = settle_inbox(page)
        names = collect_groups(page)
        show_all_inbox(page)
        try:
            page.evaluate(INBOX_TOP_JS)
        except Exception:
            pass
        if rows <= 0 and not names:
            return {"ok": False, "message": "فُتحت الدردشة لكن القائمة لم تظهر.", "groups": []}
        return {
            "ok": True,
            "groups": names,
            "rows": rows,
            "message": f"تمت التهيئة. ظهر {len(names)} قروب و{rows} محادثة. أُغلق المتصفح، وكل مهمة تفتح متصفحها الخاص.",
        }
    try:
        return run_on_browser(auth_token, work)
    except Exception:
        return {"ok": False, "message": "تعذّر فتح المتصفح.", "groups": []}


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


NUDGE_OLDER_JS = r"""
(delta) => {
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
  const scroller = query('[data-testid="dm-message-scroller"]')
    || query('[data-testid="dm-message-list"]')
    || query('[data-testid="dm-conversation-content"]');
  if (!scroller) return false;
  scroller.dispatchEvent(new WheelEvent("wheel", {deltaY: delta, bubbles: true, cancelable: true}));
  return true;
}
"""


def nudge_older(page, distance: int, page_up: bool = False) -> None:
    focus_group_scroller(page)
    try:
        page.mouse.wheel(0, distance)
    except Exception:
        pass
    try:
        page.evaluate(NUDGE_OLDER_JS, distance)
    except Exception:
        pass
    if page_up:
        try:
            page.keyboard.press("PageUp")
        except Exception:
            pass


SET_SCROLL_TOP_JS = r"""
(value) => {
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
  if (!scroller) return false;
  scroller.scrollTop = value;
  return true;
}
"""


def park_scroller(page, scroll_top: int) -> None:
    try:
        page.evaluate(SET_SCROLL_TOP_JS, max(0, int(scroll_top)))
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
  const card = [...document.querySelectorAll('article[data-testid="tweet"]')].find((article) =>
    [...article.querySelectorAll('a[href*="/status/"]')].some((node) => (node.getAttribute("href") || "").includes("/status/" + statusId)));
  if (!card) return false;
  const on = {repost: "unretweet", like: "unlike", bookmark: "removeBookmark"};
  return names.length > 0 && names.every((name) => on[name] && card.querySelector('[data-testid="' + on[name] + '"]'));
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
    # gives the button states before they appear on screen.
    replies: list = []
    api_flags: dict | None = None

    def on_response(response):
        address = response.url
        if "/graphql/" in address and ("TweetDetail" in address or "TweetResultByRestId" in address):
            replies.append(response)

    if quick and status_id:
        page.on("response", on_response)
    try:
        try:
            page.goto(target, wait_until="commit" if quick else "domcontentloaded", timeout=20_000)
        except Exception:
            return None, "fail"
        revealed = False
        deadline = time.monotonic() + (12 if quick else 8)
        while time.monotonic() < deadline:
            if quick and status_id:
                while replies and api_flags is None:
                    response = replies.pop(0)
                    try:
                        api_flags = tweet_flags_from_api(response.json(), status_id)
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
                page.remove_listener("response", on_response)
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


def visit_and_apply(page, items: list[dict], actions: list[str], on_progress=None, on_snapshot=None, should_stop=None) -> list[dict]:
    results: list[dict] = []
    total = len(items)
    if on_progress:
        on_progress(0, total, "act")
    for index, item in enumerate(items):
        if should_stop and should_stop():
            break
        result = {"url": item.get("url", ""), "text": item.get("text", ""), "actions": {}}
        try:
            opened, reason = open_status_page(page, str(item.get("url") or ""), actions)
            if not opened and reason != "gone":
                opened, reason = open_status_page(page, str(item.get("url") or ""), actions)
        except Exception as exc:
            if browser_closed_error(exc):
                if on_snapshot and results:
                    on_snapshot(list(results))
                raise BrowserClosed() from exc
            raise
        if not opened:
            note = "محذوفة — تم التخطي" if reason == "gone" else "تعذر فتح التغريدة"
            result["actions"] = {key: note for key in actions}
            results.append(result)
            if on_snapshot:
                on_snapshot(list(results))
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


def run_group_job(job_id: str, auth_token: str, pin: str, group_name: str, requested_count: int, actions: list[str], gemini_api_key: str = "", gemini_model: str = "", stop_id: str = "", group_wait_s: int = DEFAULT_GROUP_WAIT_SECONDS) -> None:
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
        else:
            publish_job(job_id, phase=phase, message="فتح الدردشة والقروب…")

    def work(page):
        try:
            with jobs_lock:
                saved = jobs.get(job_id) or {}
                items = list(saved["items"]) if isinstance(saved.get("items"), list) else []
                prefix = list(saved["results"]) if isinstance(saved.get("results"), list) else []
            if items and len(prefix) > len(items):
                prefix = prefix[: len(items)]
            if not items:
                publish_job(job_id, phase="login", message="التحقق من الجلسة ثم فتح الدردشة…")
                if not ensure_logged_in(page):
                    publish_job(job_id, status="done", success=False, message="فشل التحقق: الجلسة غير مسجّلة الدخول.")
                    return
                handle = read_session_username(page)
                if handle:
                    remember_session(auth_token, handle, pin)
                publish_job(job_id, phase="login", message="إدخال رمز الدردشة…")
                set_page_height(page, COLLECT_VIEWPORT_HEIGHT)
                error = open_chat(page, pin)
                if error:
                    publish_job(job_id, status="done", success=False, message=error)
                    return
                show_groups_only(page)
                publish_job(job_id, phase="login", message=f"فتح القروب: {group_name}")
                opened_name = open_named_group(page, group_name)
                if opened_name != group_name:
                    publish_job(job_id, phase="login", message=f"تغيّر اسم القروب إلى «{opened_name}» وفُتح.")
                if group_wait_s > 0:
                    publish_job(job_id, phase="login", message=f"فُتح القروب. انتظار {group_wait_s} ث قبل بدء التمرير والجمع…")
                    page.wait_for_timeout(int(group_wait_s) * 1000)
                cap = MAX_REPOSTS_REQUEST if stop_id else requested_count
                items, reached = collect_group_tweets(page, cap, on_progress, stop_id, lambda: job_cancelled(job_id))
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
                    on_progress(len(prefix) + done, len(items), "act")

                if "comment" in actions:
                    fresh = execute_comment_actions(
                        page, pending, actions, gemini_api_key, gemini_model, on_act,
                        lambda: job_cancelled(job_id), snapshot,
                    )
                else:
                    fresh = visit_and_apply(page, pending, actions, on_act, snapshot, lambda: job_cancelled(job_id))
                results = prefix + fresh
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
            publish_job(job_id, status="done", success=False, message=str(exc) or "تعذّر تنفيذ إجراءات القروب.")
            raise
    # One attempt only. If anything fails, the task stops with a message and is not retried.
    try:
        run_on_fresh_browser(auth_token, work)
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
            message = "أُغلق المتصفح أثناء العمل." if browser_closed_error(exc) else (str(exc) or "تعذّر تنفيذ إجراءات القروب.")
            publish_job(job_id, status="done", success=False, message=message)

def check_x_login_and_reposts(auth_token: str, username: str, requested_count: int, on_progress=None, source: str = "reposts") -> tuple[bool, list[dict], str | None]:
    """Verify the session, open reposts or replies, then collect links."""
    if on_progress:
        on_progress(0, requested_count, "login")
    def work(page):
        if not ensure_logged_in(page):
            return False, [], "login"
        handle = read_session_username(page)
        if handle:
            remember_session(auth_token, handle)
        issue = open_reposts_tab(page, username, source)
        if issue:
            return True, [], issue
        return True, collect_repost_items(page, requested_count, on_progress, username, source), None
    return run_on_browser(auth_token, work)


def execute_x_actions(auth_token: str, username: str, items: list[dict], actions: list[str], on_progress=None, gemini_api_key: str = "", gemini_model: str = "") -> tuple[bool, list[dict], str | None]:
    """Execute confirmed actions. Comments visit each tweet, screenshot it, and reply."""
    if on_progress:
        on_progress(0, len(items), "login")
    def work(page):
        if not ensure_logged_in(page):
            return False, [], "login"
        handle = read_session_username(page)
        if handle:
            remember_session(auth_token, handle)
        if "comment" in actions:
            return True, execute_comment_actions(page, items, actions, gemini_api_key, gemini_model, on_progress), None
        issue = open_reposts_tab(page, username, "reposts")
        if issue:
            return True, [], issue
        try:
            page.locator('article[data-testid="tweet"]').first.wait_for(state="attached", timeout=15_000)
        except PlaywrightTimeoutError:
            return True, [], "profile"
        return True, execute_selected_actions(page, items, actions, on_progress), None
    return run_on_browser(auth_token, work)


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


@app.after_request
def secure_response(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'"
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
        PAGE, csrf_token=csrf_token, api_root="/" + ACCESS_PATH,
    ))
    return response


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
        return jsonify(success=False, message="تعذّر إكمال التحقق بسبب مشكلة في الاتصال أو تشغيل المتصفح."), 502
    finally:
        auth_token = ""


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


def remember_gemini(api_key: str, model: str) -> None:
    key = (api_key or "").strip()
    normalized = normalize_gemini_model(model or "") or saved_gemini.get("model") or "gemini-3.5-flash"
    if key and not valid_gemini_key(key):
        return
    with gemini_lock:
        if key:
            saved_gemini["api_key"] = key
        saved_gemini["model"] = normalized
        write_private_json(GEMINI_PATH, {"version": 1, "api_key": saved_gemini["api_key"], "model": saved_gemini["model"]})


def load_gemini() -> None:
    global saved_gemini
    loaded = read_private_json(GEMINI_PATH) or {}
    key = str(loaded.get("api_key") or "").strip()
    model = normalize_gemini_model(str(loaded.get("model") or "")) or "gemini-3.5-flash"
    if key and not valid_gemini_key(key):
        key = ""
    saved_gemini = {"api_key": key, "model": model}


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


def persist_jobs_unlocked() -> None:
    write_private_json(JOBS_PATH, {"version": 1, "jobs": {key: job_record(job) for key, job in jobs.items()}})


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
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()
        persist_jobs_unlocked()


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
            "preview_id": None,
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


def public_job(job: dict) -> dict:
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
    }
    if job["status"] != "running":
        preview_id = job.get("preview_id")
        with preview_lock:
            preview_alive = bool(preview_id) and preview_id in pending_previews
        payload["preview_id"] = preview_id
        payload["preview_alive"] = preview_alive
    return payload


def remember_preview(owner: str, client_id: str, auth_token: str, username: str, requested_actions: list[str], reposts: list[dict]) -> str:
    preview_id = secrets.token_urlsafe(24)
    preview = {
        "owner": owner,
        "client_id": client_id,
        "token_digest": hashlib.sha256(auth_token.encode("utf-8")).hexdigest(),
        "created": time.monotonic(),
        "username": username,
        "actions": requested_actions,
        "items": reposts,
    }
    with preview_lock:
        now = time.monotonic()
        for key in [k for k, value in pending_previews.items() if now - value["created"] > PREVIEW_TTL_SECONDS]:
            pending_previews.pop(key, None)
        if len(pending_previews) >= 128:
            pending_previews.pop(next(iter(pending_previews)))
        pending_previews[preview_id] = preview
    return preview_id


def run_preview_job(job_id: str, auth_token: str, username: str, requested_count: int, requested_actions: list[str], owner: str, client_id: str) -> None:
    def on_progress(collected: int, requested: int, phase: str) -> None:
        if phase == "login":
            message = "التحقق من الجلسة قبل فتح التبويب…"
        else:
            message = f"تم جمع {collected} من {requested}. التمرير ينتظر اكتمال كل دفعة."
        publish_job(job_id, collected=collected, requested=requested, phase=phase, message=message)
    source = "replies" if "comment" in requested_actions else "reposts"
    try:
        success, reposts, issue = check_x_login_and_reposts(auth_token, username, requested_count, on_progress, source)
        if not success:
            publish_job(job_id, status="done", success=False, phase="login", message="فشل التحقق: لم يظهر الحساب مسجّل الدخول. قد يكون الرمز غير صالح أو منتهيًا.")
            return
        if issue == "profile":
            tab = "الردود" if source == "replies" else "إعادات النشر"
            publish_job(job_id, status="done", success=False, message=f"لم يتم العثور على الحساب أو تبويب {tab} غير متاح.")
            return
        if not reposts:
            missing = "تغريدات علّق عليها هذا الحساب" if source == "replies" else "إعادات نشر في هذا الحساب"
            publish_job(job_id, status="done", success=False, message=f"لم يُعثر على {missing}.")
            return
        preview_id = remember_preview(owner, client_id, auth_token, username, requested_actions, reposts)
        kind = "تبويب الردود" if source == "replies" else "إعادات النشر"
        if len(reposts) < requested_count:
            message = (
                f"تم جمع {len(reposts)} من {requested_count} المطلوبة في @{username} من {kind}. "
                "توقف ظهور منشورات جديدة، والقائمة هي كل ما تحمّل. راجعها ثم أكّد."
            )
        else:
            message = f"اكتمل جمع {len(reposts)} منشورات من @{username} عبر {kind}. راجع الحالات ثم أكّد التنفيذ."
        publish_job(
            job_id, status="done", success=True, message=message, preview_id=preview_id,
            username=username, actions=requested_actions, items=reposts,
            collected=len(reposts), requested=requested_count, phase="collect",
        )
    except Exception:
        app.logger.warning("X repost preview failed (details suppressed)")
        publish_job(job_id, status="done", success=False, message="تعذّر تجهيز المعاينة بسبب مشكلة في الاتصال أو تشغيل المتصفح.")
    finally:
        auth_token = ""


def run_execute_job(job_id: str, auth_token: str, preview: dict, gemini_api_key: str = "", gemini_model: str = "") -> None:
    total = len(preview["items"])
    commenting = "comment" in (preview.get("actions") or [])

    def on_progress(done: int, requested: int, phase: str) -> None:
        if phase == "login":
            message = "التحقق من الجلسة قبل التنفيذ…"
            publish_job(job_id, phase=phase, message=message, requested=requested, done=0)
            return
        if commenting:
            message = f"التعليق {done} من {requested}: صورة التغريدة، ثم رد من 10 إلى 20 كلمة."
        else:
            message = f"أُنجز {done} من {requested}."
        publish_job(job_id, phase="act", done=done, requested=requested, collected=done, message=message)

    try:
        login_ok, results, issue = execute_x_actions(
            auth_token, preview["username"], preview["items"], preview["actions"], on_progress,
            gemini_api_key, gemini_model,
        )
        if not login_ok:
            publish_job(job_id, status="done", success=False, message="فشل التحقق من الجلسة؛ لم تُنفذ الإجراءات.")
            return
        if issue == "profile":
            publish_job(job_id, status="done", success=False, message="تعذر فتح تبويب إعادات النشر؛ لم تُنفذ الإجراءات.")
            return
        summary = "; ".join(
            f"{name}: {sum(1 for row in results if row.get('actions', {}).get(name) == 'تم التنفيذ')} نُفّذ، "
            f"{sum(1 for row in results if 'مفعّل مسبقًا' in row.get('actions', {}).get(name, ''))} كان مفعّلًا وتم تجاوزه"
            for name in preview["actions"]
        )
        publish_job(
            job_id, status="done", success=True, phase="act", done=total, requested=total,
            message="اكتملت المحاولة. " + summary, results=results,
        )
    except Exception:
        app.logger.warning("X action execution failed (details suppressed)")
        publish_job(job_id, status="done", success=False, message="توقفت العملية بسبب خطأ؛ راجع الحالات المعروضة قبل إعادة المحاولة.")
    finally:
        auth_token = ""
        gemini_api_key = ""


SCREEN_PATH = DATA_DIR / "screen.json"
screen_lock = threading.Lock()
screen: dict = {
    "auth_token": "",
    "chat_pin": "",
    "groups": [],
    "group": "",
    "count": 25,
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
        try:
            screen["count"] = max(1, min(MAX_REPOSTS_REQUEST, int(loaded.get("count") or 25)))
        except (TypeError, ValueError):
            screen["count"] = 25
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
        row = public_job(job)
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
            "count": screen.get("count") or 25,
            "stop_tweet": screen.get("stop_tweet") or "",
            "actions": list(screen.get("actions") or []),
            "job_id": screen.get("job_id") or "",
            "job_key": screen.get("job_key") or "",
            "started_at": screen.get("started_at") or 0,
        }
    job_id = payload["job_id"]
    with jobs_lock:
        job = jobs.get(job_id)
        payload["job"] = public_job(job) if job else None
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
    requested_count = parse_requested_count(data.get("count"))
    if requested_count:
        fields["count"] = requested_count
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


@app.post("/<access_path>/reload")
def reload_browser_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    auth_token = str(data.get("auth_token", "")).strip()
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    with jobs_lock:
        busy = any(job.get("status") == "running" for job in jobs.values())
    if busy:
        return jsonify(success=False, message="هناك مهمة تعمل الآن. انتظر اكتمالها ثم حدّث."), 409
    try:
        url = browser_hub.reload_open_page()
    except Exception:
        return jsonify(success=False, message="تعذّر تحديث صفحة المتصفح."), 502
    if not url:
        return jsonify(success=False, message="لا يوجد متصفح مفتوح لتحديثه: كل مهمة تفتح متصفحها الخاص وتغلقه عند انتهائها.")
    return jsonify(success=True, message="تم تحديث صفحة متصفح السكربت.")


@app.post("/<access_path>/prepare")
def prepare_endpoint(access_path: str):
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
    with jobs_lock:
        busy = any(job.get("status") == "running" for job in jobs.values())
    if busy:
        return jsonify(success=False, message="هناك مهمة تعمل الآن. انتظر اكتمالها."), 409
    result = prepare_chat(auth_token, pin)
    if not result.get("ok"):
        return jsonify(success=False, message=result.get("message") or "تعذّرت التهيئة.", groups=[])
    names = result.get("groups") or []
    current_group = ""
    with screen_lock:
        current_group = str(screen.get("group") or "")
    if names and current_group not in names:
        current_group = ""
    save_screen(auth_token=auth_token, chat_pin=pin, groups=names or screen.get("groups") or [], group=current_group)
    return jsonify(success=True, groups=names, rows=result.get("rows") or 0, message=result.get("message") or "تمت التهيئة.")


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
    group_name = re.sub(r"\s+", " ", str(data.get("group", ""))).strip()
    stop_raw = str(data.get("stop_tweet") or "").strip()
    stop_id = extract_status_id(stop_raw) if stop_raw else ""
    requested_count = parse_requested_count(data.get("count"))
    requested_actions = data.get("actions")
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    if not valid_chat_pin(pin):
        return jsonify(success=False, message="رمز الدردشة يجب أن يكون من ٤ إلى ٨ أرقام."), 400
    if not group_name or len(group_name) > 80:
        return jsonify(success=False, message="اختر قروبًا من القائمة."), 400
    if stop_raw and not stop_id:
        return jsonify(success=False, message="رابط التغريدة غير صالح. الصق رابطها أو رقمها."), 400
    if not stop_id and (requested_count is None or not 1 <= requested_count <= MAX_REPOSTS_REQUEST):
        return jsonify(success=False, message=f"أدخل عددًا من 1 إلى {MAX_REPOSTS_REQUEST}، أو رابط تغريدة للتوقف عندها."), 400
    if stop_id:
        requested_count = 0
    if not isinstance(requested_actions, list) or not requested_actions or any(not isinstance(action, str) or action not in ALLOWED_ACTIONS for action in requested_actions):
        return jsonify(success=False, message="اختر إجراء واحدًا على الأقل."), 400
    group_wait_raw = data.get("group_wait")
    group_wait_s = DEFAULT_GROUP_WAIT_SECONDS
    if group_wait_raw is not None:
        parsed_wait = parse_requested_count(group_wait_raw)
        if parsed_wait is not None:
            group_wait_s = max(0, min(300, parsed_wait))
    requested_actions = list(dict.fromkeys(requested_actions))
    gemini_api_key = str(data.get("gemini_api_key") or "").strip()
    gemini_model = normalize_gemini_model(str(data.get("gemini_model") or ""))
    if "comment" in requested_actions:
        if not gemini_api_key:
            gemini_api_key = saved_gemini.get("api_key") or ""
        if not gemini_model:
            gemini_model = normalize_gemini_model(saved_gemini.get("model") or "")
        if not valid_gemini_key(gemini_api_key) or not gemini_model:
            return jsonify(success=False, message="أدخل مفتاح Gemini واسم النموذج قبل التعليق."), 400
        remember_gemini(gemini_api_key, gemini_model or "")
    with jobs_lock:
        running = [job for job in jobs.values() if job.get("status") == "running" and job.get("kind") == "execute"]
    if len(running) >= MAX_PARALLEL_JOBS:
        return jsonify(success=False, message=f"أقصى عدد للمهام المتزامنة هو {MAX_PARALLEL_JOBS}. أوقف مهمة أو انتظر انتهائها."), 429
    owner = session.get("csrf_token", "")
    client_id = request_client_id()
    job_id, job_key = begin_job(owner, client_id, requested_count, "login", "فتح القروب…", "execute", group_name)
    save_screen(
        auth_token=auth_token,
        chat_pin=pin,
        group=group_name,
        count=requested_count,
        stop_tweet=stop_raw,
        actions=requested_actions,
        job_id=job_id,
        job_key=job_key,
        started_at=time.time(),
    )
    threading.Thread(
        target=run_group_job,
        args=(job_id, auth_token, pin, group_name, requested_count, requested_actions, gemini_api_key, gemini_model or "", stop_id, group_wait_s),
        daemon=True,
    ).start()
    return jsonify(success=True, job_id=job_id, job_key=job_key, message="بدأ التنفيذ.")


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


@app.post("/<access_path>/reposts")
def preview_actions_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    if not allow_attempt(request.remote_addr or "unknown"):
        return jsonify(success=False, message="تجاوزت عدد المحاولات المسموح به مؤقتًا. انتظر قليلًا ثم أعد المحاولة."), 429
    auth_token = str(data.get("auth_token", "")).strip()
    username = normalize_username(str(data.get("username", "")))
    requested_count = parse_requested_count(data.get("count"))
    requested_actions = data.get("actions")
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        return jsonify(success=False, message="اسم المستخدم غير صالح. أدخل اسم X فقط، مع أو دون @."), 400
    if requested_count is None or not 1 <= requested_count <= MAX_REPOSTS_REQUEST:
        return jsonify(success=False, message=f"عدد المنشورات يجب أن يكون عددًا صحيحًا من 1 إلى {MAX_REPOSTS_REQUEST}."), 400
    if not isinstance(requested_actions, list) or not requested_actions or any(not isinstance(action, str) or action not in ALLOWED_ACTIONS for action in requested_actions):
        return jsonify(success=False, message="اختر إجراء واحدًا على الأقل من الإجراءات المتاحة."), 400
    requested_actions = list(dict.fromkeys(requested_actions))
    owner = session.get("csrf_token", "")
    client_id = request_client_id()
    job_id, job_key = begin_job(owner, client_id, requested_count, "login", "يبدأ التحقق ثم التمرير…", "preview", username)
    threading.Thread(
        target=run_preview_job,
        args=(job_id, auth_token, username, requested_count, requested_actions, owner, client_id),
        daemon=True,
    ).start()
    return jsonify(success=True, job_id=job_id, job_key=job_key, message="بدأت المعاينة.")


@app.post("/<access_path>/execute-actions")
def execute_actions_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    if not allow_attempt(request.remote_addr or "unknown"):
        return jsonify(success=False, message="تجاوزت عدد المحاولات المسموح به مؤقتًا. انتظر قليلًا ثم أعد المحاولة."), 429
    auth_token = str(data.get("auth_token", "")).strip()
    preview_id = str(data.get("preview_id", ""))
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    client_id = request_client_id()
    csrf_token = session.get("csrf_token", "")
    with preview_lock:
        preview = pending_previews.get(preview_id)
        if preview and time.monotonic() - preview["created"] > PREVIEW_TTL_SECONDS:
            pending_previews.pop(preview_id, None)
            preview = None
        elif preview and not (
            preview.get("owner") == csrf_token or (bool(client_id) and preview.get("client_id") == client_id)
        ):
            preview = None
        if preview and not hmac.compare_digest(preview["token_digest"], hashlib.sha256(auth_token.encode("utf-8")).hexdigest()):
            return jsonify(success=False, message="رمز الدخول الحالي لا يطابق الرمز المستخدم للمعاينة. أعد المعاينة بالتوكن الصحيح."), 400
        gemini_api_key = str(data.get("gemini_api_key") or "").strip()
        gemini_model = normalize_gemini_model(str(data.get("gemini_model") or ""))
        if preview and "comment" in (preview.get("actions") or []):
            if not gemini_api_key:
                gemini_api_key = saved_gemini.get("api_key") or ""
            if not gemini_model:
                gemini_model = normalize_gemini_model(saved_gemini.get("model") or "")
            if not valid_gemini_key(gemini_api_key) or not gemini_model:
                return jsonify(success=False, message="أدخل مفتاح Gemini واسم نموذج صالح قبل تنفيذ التعليقات."), 400
            remember_gemini(gemini_api_key, gemini_model or "")
        if preview:
            pending_previews.pop(preview_id, None)
    if not preview:
        return jsonify(success=False, message="انتهت صلاحية المعاينة أو استُخدمت من قبل. أعد البحث لعمل معاينة جديدة."), 400
    job_id, job_key = begin_job(csrf_token, client_id, len(preview["items"]), "login", "التحقق من الجلسة قبل التنفيذ…", "execute", preview["username"])
    threading.Thread(
        target=run_execute_job,
        args=(job_id, auth_token, preview, gemini_api_key, gemini_model or ""),
        daemon=True,
    ).start()
    return jsonify(success=True, job_id=job_id, job_key=job_key, message="بدأ التنفيذ.")


@app.get("/<access_path>/jobs/<job_id>")
def job_status(access_path: str, job_id: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(success=False, message="لا توجد عملية بهذا المعرّف."), 404
        if job_key_matches(job, request.headers.get("X-Job-Key", "")):
            return jsonify(public_job(job))
        if not csrf_matches(request.headers.get("X-CSRF-Token", "")):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        owner_ok = bool(job.get("owner")) and job.get("owner") == session.get("csrf_token", "")
        if not owner_ok:
            return jsonify(success=False, message="لا توجد عملية بهذا المعرّف."), 404
        return jsonify(public_job(job))


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
    return jsonify(success=True, sessions=rows, gemini_api_key=gemini_key, gemini_model=gemini_model)


@app.post("/<access_path>/gemini")
def save_gemini_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
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


@app.post("/<access_path>/cancel-preview")
def cancel_preview_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة."), 400
    preview_id = str(data.get("preview_id", ""))
    client_id = request_client_id()
    csrf_token = session.get("csrf_token", "")
    with preview_lock:
        preview = pending_previews.get(preview_id)
        if preview and (preview.get("owner") == csrf_token or (bool(client_id) and preview.get("client_id") == client_id)):
            pending_previews.pop(preview_id, None)
    return jsonify(success=True, message="أُلغيت المعاينة ولم تُنفّذ أي إجراءات.")


load_sessions()
load_group_ids()
load_gemini()
load_jobs()
load_screen()
atexit.register(shutdown_jobs)


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
    app.run(host=bind_host, port=port, debug=False, threaded=True)
