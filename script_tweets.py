#!/usr/bin/env python3
"""One-file Flask page to verify an X session and act on tweets pasted into it.

Saved sessions (auth token + the account username) are written beside this
script and survive a restart. Job progress is kept too, so leaving the browser
and coming back shows whether the work is still running, where it stopped,
or that it finished. The token is not returned except to this same page.
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
from urllib.parse import quote

from flask import Flask, abort, jsonify, make_response, redirect, render_template_string, request, session
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("X_APP_SECURE_COOKIE", "0") == "1",
    MAX_CONTENT_LENGTH=512 * 1024,  # the pasted tweet list can be long
)

ACCESS_PATH = os.getenv("X_ACCESS_PATH", "desk")
ATTEMPT_WINDOW_SECONDS = 300
MAX_ATTEMPTS_PER_CLIENT = 5
MAX_REPOSTS_REQUEST = 1000
MAX_TWEETS_TEXT = 200_000
jobs: dict[str, dict] = {}
attempts: dict[str, deque[float]] = defaultdict(deque)
attempts_lock = threading.Lock()
jobs_lock = threading.Lock()
sessions_lock = threading.Lock()
gemini_lock = threading.Lock()
JOB_TTL_SECONDS = 86400
# Its own data folder, so it can run beside the group script in the same folder.
DATA_DIR = Path(__file__).resolve().with_name("data_tweets")
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
    external = Path(__file__).with_name("templates").joinpath("tweets.html")
    if external.is_file():
        return external.read_text(encoding="utf-8")
    return EMBEDDED_PAGE


EMBEDDED_PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>تنفيذ على تغريدات محددة</title>
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
    .gemini-usage { display: flex; flex-direction: column; gap: 2px; margin: 10px 0; padding: 8px 10px; border-radius: 10px;
      background: var(--paper); box-shadow: inset 0 0 0 1px var(--line); color: var(--muted); font-size: 12px; line-height: 1.5; }
    .gemini-usage b { color: var(--ink); font-size: 12px; }
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
    #tweets {
      display: block;
      width: 100%;
      min-height: 150px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--sheet);
      color: var(--ink);
      font: inherit;
      font-size: 16px;
      line-height: 1.5;
      resize: vertical;
    }
    #tweets:focus { outline: none; border-color: var(--pine); box-shadow: 0 0 0 3px rgba(15, 107, 80, 0.16); }
    #auth_token, #tweets, #gemini-model { direction: ltr; text-align: start; }
    #auth_token[readonly], #gemini-key[readonly], #gemini-model[readonly] { color: var(--muted); }
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
    .task-actions { display: flex; flex-wrap: wrap; column-gap: 6px; }
    .task-actions .btn { margin-top: 6px; }
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
          <p class="eyebrow">تغريدات تختارها</p>
          <h1>تنفيذ على تغريدات محددة</h1>
        </div>
      </div>
      <ol class="steps" id="steps">
        <li data-step="1" class="is-current"><span class="n">١</span> الجلسة</li>
        <li data-step="2"><span class="n">٢</span> التغريدات</li>
        <li data-step="3"><span class="n">٣</span> التنفيذ</li>
      </ol>
    </header>
    <p class="lede">الصق روابط التغريدات التي تريدها فقط، واختر الإجراءات، ثم اضغط تنفيذ.</p>

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
            <h2>التغريدات</h2>
            <p>الصق روابط التغريدات التي تريد التنفيذ عليها فقط، كل رابط في سطر.</p>
          </div>
          <label class="field-label" for="tweets">روابط التغريدات</label>
          <textarea id="tweets" name="tweets" rows="8" maxlength="200000" autocomplete="off" autocapitalize="off" spellcheck="false"
                    placeholder="https://x.com/user/status/123…" aria-label="روابط التغريدات"></textarea>
          <p id="tweets-count" class="hint">لا توجد روابط بعد.</p>
          <div class="group-wait-row">
            <label class="wait-groups" for="account-profiles-on">
              <input id="account-profiles-on" type="checkbox">
              عدد المهام في نفس الوقت لكل حساب
              <input id="account-profiles" class="wait-sec" type="number" min="1" max="5" step="1" value="2" inputmode="numeric" aria-label="عدد المهام لكل حساب">
            </label>
          </div>
        </section>

        <section class="block">
          <div class="block-head">
            <h2>الإجراءات</h2>
            <p>تُطبَّق على التغريدات التي في المربع فقط.</p>
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
                <span class="choice-copy">يُنشر رد على كل تغريدة.</span>
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
          <div id="gemini-usage" class="gemini-usage" aria-live="polite">
            <span id="gemini-usage-text">التعليقات (0) التكلفة (0.000)</span>
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
            <h2 id="progress-title">التنفيذ</h2>
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
        <p id="progress-note" class="progress-note"></p>
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

    <p class="footnote">إذا أغلقت المتصفح أثناء العملية فافتح الصفحة نفسها لاحقًا: يظهر التقدم من حيث بلغ، أو النتيجة إن اكتملت. الجلسات المحفوظة تبقى بعد إعادة تشغيل السكربت.</p>
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
  const tweetsField = document.getElementById("tweets");
  const tweetsCount = document.getElementById("tweets-count");
  const accountProfilesOn = document.getElementById("account-profiles-on");
  const accountProfiles = document.getElementById("account-profiles");
  function rememberWaits() {
    try {
      localStorage.setItem("x_wait_settings", JSON.stringify({
        profilesOn: accountProfilesOn.checked, profiles: accountProfiles.value
      }));
    } catch (_) {}
  }
  try {
    const savedWaits = JSON.parse(localStorage.getItem("x_wait_settings") || "null");
    if (savedWaits) {
      accountProfilesOn.checked = !!savedWaits.profilesOn;
      if (savedWaits.profiles) accountProfiles.value = savedWaits.profiles;
    }
  } catch (_) {}
  [accountProfilesOn, accountProfiles].forEach((node) => node.addEventListener("change", rememberWaits));
  // The tweet ids in the box (links or bare ids), once each, in the order pasted.
  function parseTweets(text) {
    const ids = new Set();
    String(text || "").split(/[\s,،]+/).forEach((piece) => {
      const match = piece.match(/status(?:es)?\/(\d{6,25})/) || piece.match(/^(\d{15,25})$/);
      if (match) ids.add(match[1]);
    });
    return [...ids];
  }
  function showTweetsCount() {
    const count = parseTweets(tweetsField.value).length;
    tweetsCount.textContent = count ? "عدد التغريدات: " + count : "لا توجد روابط بعد.";
  }
  function secondsFrom(field, min, max, fallback) {
    const value = Number.parseInt(field.value, 10);
    if (!Number.isInteger(value)) return fallback;
    return Math.max(min, Math.min(max, value));
  }
  const commentToggle = document.getElementById("action-comment");
  const geminiFields = document.getElementById("gemini-fields");
  const geminiKey = document.getElementById("gemini-key");
  const geminiModel = document.getElementById("gemini-model");
  const geminiUsageText = document.getElementById("gemini-usage-text");
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
  const repostResults = document.getElementById("repost-results");
  const resultStats = document.getElementById("result-stats");
  const repostList = document.getElementById("repost-list");
  const noReposts = document.getElementById("no-reposts");
  const sessionAccount = document.getElementById("session-account");
  const savedSessions = document.getElementById("saved-sessions");
  const savedSessionList = document.getElementById("saved-session-list");
  let progressStarted = 0;
  let sessionRows = [];

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
    card.innerHTML = '<div class="progress-top"><div><h3></h3><p class="task-phase"></p><p class="task-msg"></p></div><b class="task-count">0</b></div><div class="meter is-indeterminate"><span class="task-bar"></span></div><div class="task-actions"><button class="btn btn-ghost task-stop" type="button">إيقاف هذه المهمة</button><button class="btn task-retry" type="button" hidden>إعادة المحاولة</button></div>';
    card.querySelector("h3").textContent = task.title || "مهمة";
    const stopButton = card.querySelector(".task-stop");
    const retryButton = card.querySelector(".task-retry");
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
      const titles = {login: "التحقق", act: "تنفيذ"};
      card.querySelector(".task-phase").textContent = titles[phase] || "جارٍ العمل";
      card.querySelector(".task-msg").textContent = data.message || "";
      const current = phase === "act" ? (data.done || 0) : (data.collected || 0);
      const total = data.requested || 0;
      card.querySelector(".task-count").textContent = total ? current + " / " + total : String(current);
      const meter = card.querySelector(".meter");
      const indeterminate = data.status === "running" && (phase === "login" || !total);
      meter.classList.toggle("is-indeterminate", indeterminate);
      card.querySelector(".task-bar").style.width = indeterminate ? "" : Math.max(0, Math.min(100, total ? Math.round((current / total) * 100) : 100)) + "%";
      stopButton.hidden = data.status !== "running";
      retryButton.hidden = !(data.status !== "running" && data.resumable);
      retryButton.disabled = false;
    };
    const watch = async () => {
      try {
        for (;;) {
          const response = await fetch(apiRoot + "/jobs/" + encodeURIComponent(task.id) + "?light=1", {
            headers: {"X-CSRF-Token": csrf, "X-Job-Key": task.key, "X-Client-Id": clientId()},
            cache: "no-store"
          });
          const data = await response.json();
          if (response.status === 404) writeTasks(readTasks().filter((row) => row.id !== task.id));
          if (!response.ok) throw new Error(data.message || "تعذّر متابعة المهمة.");
          paint(data);
          if (data.status !== "running") {
            showNotice((task.title ? task.title + ": " : "") + (data.message || ""), Boolean(data.success));
            if (Array.isArray(data.results) && data.results.length) renderResults(data);
            // A task stopped with a retry button stays in the saved task list so it is still there after a reload.
            if (!data.resumable) writeTasks(readTasks().filter((row) => row.id !== task.id));
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
        showAccount(row.username ? "الجلسة المحفوظة: @" + row.username : "تم اختيار جلسة محفوظة.");
        showNotice(row.username ? "تم ملء جلسة @" + row.username + "." : "تم ملء الرمز المحفوظ.", true);
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

  // Total comments written by the model, and their price, read from the server.
  async function loadGeminiUsage() {
    try {
      const response = await fetch(apiRoot + "/gemini-usage", {headers: {"X-CSRF-Token": csrf}, cache: "no-store"});
      const data = await response.json();
      if (!response.ok || !data.success) return;
      const comments = Number(data.comments || 0);
      const total = Number(data.total || 0);
      geminiUsageText.textContent = "التعليقات (" + comments.toLocaleString("en-US") + ") التكلفة (" + total.toFixed(3) + ")";
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
    const titles = {login: "التحقق من الجلسة", act: "تنفيذ الإجراءات"};
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
    setStep(phase === "act" ? 3 : 2);
  }

  function finishTrackedJob(data) {
    if (!data || data.status === "running") return;
    showNotice(data.message || "", Boolean(data.success));
    renderProgress(data);
    if (data.status === "interrupted") {
      progressNote.textContent = "آخر حالة قبل توقف السكربت. لن تُستكمل العملية تلقائيًا.";
    }
    const hasResults = Array.isArray(data.results) && data.results.length > 0;
    if (data.kind === "execute") {
      if (hasResults) renderResults(data);
      else if (data.status !== "interrupted") setStep(3);
    } else if (!data.success) {
      setStep(1);
    }
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
    setStep(3);
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
  loadGeminiUsage();
  setInterval(loadGeminiUsage, 15000);

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

  document.getElementById("repost-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const authToken = tokenField.value.trim();
    const ids = parseTweets(tweetsField.value);
    const actions = selectedActions();
    if (!authToken) { showNotice("أدخل الرمز أولًا.", false); tokenField.focus(); return; }
    if (!ids.length) { showNotice("الصق رابط تغريدة واحدة على الأقل.", false); tweetsField.focus(); return; }
    if (!actions.length) { showNotice("اختر إجراءً واحدًا على الأقل.", false); return; }
    if (actions.includes("comment")) {
      if (!geminiKey.value.trim()) { lockGemini(false); showNotice("أدخل مفتاح Gemini.", false); geminiKey.focus(); return; }
      if (!geminiModel.value.trim()) { lockGemini(false); showNotice("أدخل اسم النموذج.", false); geminiModel.focus(); return; }
      rememberGeminiLocal();
    }
    repostButton.disabled = true;
    try {
      const started = await postJson("/tweets-run", {
        auth_token: authToken,
        tweets: tweetsField.value,
        actions,
        gemini_api_key: geminiKey.value.trim(),
        gemini_model: geminiModel.value.trim(),
        account_profiles: accountProfilesOn.checked ? secondsFrom(accountProfiles, 1, 5, 2) : 1
      });
      if (!started.success || !started.job_id || !started.job_key) {
        showNotice(started.message || "تعذّر بدء التنفيذ.", false);
        return;
      }
      const account = (sessionAccount.textContent || "").replace("الجلسة المحفوظة: ", "");
      followTask({
        id: started.job_id,
        key: started.job_key,
        title: started.count + " تغريدة" + (account ? " · " + account : ""),
        startedAt: Date.now()
      });
      showNotice("بدأت المهمة على " + started.count + " تغريدة.", true);
    } catch (_) {
      showNotice("تعذّر الاتصال أثناء التنفيذ.", false);
    } finally {
      repostButton.disabled = false;
    }
  });

  tokenField.addEventListener("input", () => {
    const token = tokenField.value.trim();
    const row = sessionRows.find((item) => item.token === token);
    if (row) showAccount(row.username ? "الجلسة المحفوظة: @" + row.username : "تم اختيار جلسة محفوظة.");
    else showAccount("");
    renderSessions();
  });

  let screenTimer = 0;
  function pushScreen() {
    clearTimeout(screenTimer);
    screenTimer = setTimeout(() => {
      const payload = {
        auth_token: tokenField.value.trim(),
        tweets: tweetsField.value,
        actions: selectedActions()
      };
      postJson("/screen", payload).catch(() => {});
    }, 350);
  }

  function applyScreen(data) {
    if (!data) return;
    if (data.auth_token) tokenField.value = data.auth_token;
    lockToken(true);
    if (typeof data.tweets === "string") tweetsField.value = data.tweets;
    showTweetsCount();
    if (Array.isArray(data.actions) && data.actions.length) {
      ["repost", "like", "bookmark", "comment"].forEach((key) => {
        const box = document.getElementById("action-" + key);
        if (box) box.checked = data.actions.includes(key);
      });
      syncGemini();
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
    runningJobs.slice().reverse().forEach((job) => {
      const same = screenJob && screenJob.job_id === job.id;
      followTask({
        id: job.id,
        key: same ? (screenJob.job_key || "") : "",
        title: job.username || "مهمة"
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
  tweetsField.addEventListener("input", () => {
    showTweetsCount();
    pushScreen();
  });
  document.querySelectorAll('#repost-form input[type="checkbox"]').forEach((box) => {
    box.addEventListener("change", pushScreen);
  });

  restoreScreen();

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


def _move_like_a_hand(page, x: float, y: float) -> None:
    viewport = page.viewport_size or {"width": VIEWPORT_WIDTH, "height": 900}
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


GEMINI_USAGE_PATH = DATA_DIR / "gemini_usage.json"
# Price counted for each comment the model writes (shown on the page as the running total).
GEMINI_PRICE_PER_COMMENT = 0.007
gemini_usage_lock = threading.Lock()
gemini_usage: dict = {}


def load_gemini_usage() -> None:
    loaded = read_private_json(GEMINI_USAGE_PATH) or {}
    try:
        comments = max(0, int(loaded.get("comments") or 0))
        since = float(loaded.get("since") or 0)
    except (TypeError, ValueError):
        comments, since = 0, 0.0
    with gemini_usage_lock:
        gemini_usage.clear()
        gemini_usage.update({"comments": comments, "since": since})


def record_comment_generated() -> None:
    """Count one comment written by the model."""
    try:
        with gemini_usage_lock:
            if not gemini_usage.get("since"):
                gemini_usage["since"] = time.time()
            gemini_usage["comments"] = int(gemini_usage.get("comments") or 0) + 1
            write_private_json(GEMINI_USAGE_PATH, dict(gemini_usage))
    except Exception:
        pass


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
            record_comment_generated()
            return text
        if len(text) > len(best):
            best = text
    if comment_is_complete(best):
        record_comment_generated()
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


def card_text(card) -> str:
    """The tweet's own text from its card (pasted links come without it)."""
    try:
        box = card.locator('[data-testid="tweetText"]')
        return box.first.inner_text(timeout=2000).strip()[:280] if box.count() else ""
    except Exception:
        return ""


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


def execute_comment_actions(page, items: list[dict], actions: list[str], api_key: str, model: str, on_progress=None, should_stop=None, on_snapshot=None, account: str = "") -> list[dict]:
    allow_media(page)
    results: list[dict] = []
    plain = [action for action in actions if action != "comment"]
    total = len(items)
    restarts_used = 0
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
            opened, reason = open_status_page(page, str(item.get("url") or ""))
            if not opened and reason != "gone":
                opened, reason = open_status_page(page, str(item.get("url") or ""))
            while not opened and reason != "gone" and restarts_used < MAX_BROWSER_RESTARTS:
                # Still failing: close the browser profile, reopen it on Google, go to X, try again.
                new_page = restart_browser()
                if new_page is None:
                    break
                restarts_used += 1
                page = new_page
                allow_media(page)
                opened, reason = open_status_page(page, str(item.get("url") or ""))
        except Exception as exc:
            if browser_closed_error(exc):
                if on_snapshot and results:
                    on_snapshot(list(results))
                raise BrowserClosed() from exc
            raise
        if not opened and reason != "gone":
            # Both browser restarts used and the tweet still won't open: stop the task here.
            if on_snapshot:
                on_snapshot(list(results))
            raise TweetOpenStuck(index)
        if not opened:
            note = "محذوفة — تم التخطي"
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
            if replied_before or ("comment" in actions and already_done_recently(account, tweet_status_id(target_path), ["comment"])):
                result["actions"]["comment"] = "سبق الرد خلال ٨ ساعات — تم التخطي"
            elif "comment" in actions:
                tweet_text = str(item.get("text") or "") or card_text(card)
                comment = gemini_comment(api_key, model, image, tweet_text)
                if len(comment.split()) < 4:
                    comment = gemini_comment(api_key, model, image, tweet_text)
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
        # Save what was done, for the tweet given and for the tweet actually replied to.
        for done_id in {status_id, tweet_status_id(result["url"])}:
            remember_done(account, done_id, result["actions"])
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


def open_x_profile(playwright, auth_token: str, profile_dir: Path):
    """Open the account's permanent browser profile (created on first use)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
        viewport={"width": VIEWPORT_WIDTH, "height": NORMAL_VIEWPORT_HEIGHT},
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


# The running task's "close and reopen the browser" function (one per worker thread).
browser_restart = threading.local()


def restart_browser():
    fn = getattr(browser_restart, "fn", None)
    return fn() if fn else None


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
    key = profile_key(auth_token)
    slot_name = acquire_profile_slot(key, slots)
    try:
        playwright = sync_playwright().start()
        state = {"context": None}

        def restart():
            """Close the profile's browser, reopen it on Google, then go to X. Returns the new page."""
            try:
                if state["context"] is not None:
                    state["context"].close()
            except Exception:
                pass
            state["context"] = None
            state["context"], new_page = open_x_profile(playwright, auth_token, PROFILES_DIR / slot_name)
            try:
                new_page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=60_000)
                new_page.wait_for_timeout(2000)
            except Exception:
                pass
            try:
                new_page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
                session_is_logged_in(new_page)
            except Exception:
                pass
            return new_page

        browser_restart.fn = restart
        try:
            state["context"], page = open_x_profile(playwright, auth_token, PROFILES_DIR / slot_name)
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


def delete_profile(auth_token: str) -> None:
    key = profile_key(auth_token)
    lock = profile_lock(key)
    if not lock.acquire(timeout=5):
        return
    try:
        shutil.rmtree(PROFILES_DIR / key, ignore_errors=True)
        for extra in PROFILES_DIR.glob(f"{key}-*"):
            shutil.rmtree(extra, ignore_errors=True)
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


def tweet_url(raw_url: str, status_id: str) -> str:
    raw = (raw_url or "").strip()
    for match in re.finditer(rf"(?<![A-Za-z0-9_])/([A-Za-z0-9_]{{1,15}})/status/{status_id}(?!\d)", raw):
        owner = match.group(1)
        if owner.lower() in {"i", "web", "intent", "share"}:
            continue
        return f"https://x.com/{owner}/status/{status_id}"
    return f"https://x.com/i/web/status/{status_id}"


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


def visit_and_apply(page, items: list[dict], actions: list[str], on_progress=None, on_snapshot=None, should_stop=None, account: str = "") -> list[dict]:
    results: list[dict] = []
    total = len(items)
    restarts_used = 0
    if on_progress:
        on_progress(0, total, "act")
    for index, item in enumerate(items):
        if should_stop and should_stop():
            break
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
            opened, reason = open_status_page(page, str(item.get("url") or ""), actions)
            if not opened and reason != "gone":
                opened, reason = open_status_page(page, str(item.get("url") or ""), actions)
            while not opened and reason != "gone" and restarts_used < MAX_BROWSER_RESTARTS:
                # Still failing: close the browser profile, reopen it on Google, go to X, try again.
                new_page = restart_browser()
                if new_page is None:
                    break
                restarts_used += 1
                page = new_page
                opened, reason = open_status_page(page, str(item.get("url") or ""), actions)
            if not opened and reason != "gone":
                # Both browser restarts used and the tweet still won't open: stop the task here.
                raise TweetOpenStuck(index)
        except TweetOpenStuck:
            raise
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
        results.append(result)
        if on_snapshot:
            on_snapshot(list(results))
        if on_progress:
            on_progress(index + 1, total, "act")
    return results


class JobStopped(Exception):
    pass


class TweetOpenStuck(Exception):
    """A tweet still failed to open after both browser restarts; the task stops and can be retried."""


# How many times one task may close and reopen the browser when tweets fail to open.
MAX_BROWSER_RESTARTS = 2
# Arguments of each task, kept in memory so the retry button can continue it.
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


def parse_tweet_list(raw: str) -> list[dict]:
    """The tweets pasted in the box (links or bare ids), once each, in the order pasted."""
    items: list[dict] = []
    seen: set[str] = set()
    for piece in re.split(r"[\s,،]+", raw or ""):
        match = re.search(r"status(?:es)?/(\d{6,25})", piece) or re.fullmatch(r"(\d{15,25})", piece)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            items.append({"url": tweet_url(piece, match.group(1)), "text": ""})
    return items


def run_tweets_job(job_id: str, auth_token: str, actions: list[str], gemini_api_key: str = "", gemini_model: str = "", profiles_per_account: int = 1) -> None:
    def on_progress(done: int, requested: int, phase: str) -> None:
        if job_cancelled(job_id):
            raise JobStopped
        publish_job(job_id, phase="act", done=done, collected=requested, requested=requested, message=f"تنفيذ {done} من {requested}.")

    job_run_args[job_id] = (auth_token, actions, gemini_api_key, gemini_model, profiles_per_account)

    def work(page):
        try:
            with jobs_lock:
                saved = jobs.get(job_id) or {}
                items = list(saved["items"]) if isinstance(saved.get("items"), list) else []
                prefix = list(saved["results"]) if isinstance(saved.get("results"), list) else []
            if len(prefix) > len(items):
                prefix = prefix[: len(items)]
            publish_job(job_id, phase="login", message="التحقق من الجلسة…")
            if not ensure_logged_in(page):
                publish_job(job_id, status="done", success=False, message="فشل التحقق: الجلسة غير مسجّلة الدخول.")
                return
            if prefix:
                publish_job(job_id, phase="act", requested=len(items), collected=len(items), done=len(prefix), message=f"استئناف التنفيذ من {len(prefix)} من {len(items)}.")
            else:
                handle = read_session_username(page)
                if handle:
                    remember_session(auth_token, handle)
                publish_job(job_id, phase="act", requested=len(items), collected=len(items), done=0, message=f"بدأ تنفيذ الإجراءات على {len(items)} تغريدة.")
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
                        lambda: job_cancelled(job_id), snapshot, profile_key(auth_token),
                    )
                else:
                    fresh = visit_and_apply(page, pending, actions, on_act, snapshot, lambda: job_cancelled(job_id), profile_key(auth_token))
                results = prefix + fresh
            if job_cancelled(job_id):
                publish_job(
                    job_id, status="done", success=False, phase="act", done=len(results), requested=len(items),
                    collected=len(items), message="تم إيقاف التنفيذ.",
                    results=results, items=items, actions=actions,
                )
                return
            publish_job(
                job_id, status="done", success=True, phase="act", done=len(results), requested=len(items),
                collected=len(items), message=f"اكتمل التنفيذ على {len(results)} تغريدة.",
                results=results, items=items, actions=actions,
            )
        except JobStopped:
            publish_job(job_id, status="done", success=False, message="تم إيقاف التنفيذ.")
            return
        except TweetOpenStuck:
            with jobs_lock:
                done_count = len((jobs.get(job_id) or {}).get("results") or [])
            publish_job(
                job_id, status="done", success=False, resumable=True, phase="act", done=done_count,
                message=f"توقفت المهمة: تعذّر فتح التغريدة {done_count + 1} بعد إغلاق المتصفح وفتحه مرتين. اضغط «إعادة المحاولة» للمتابعة من حيث توقفت.",
            )
            return
        except BrowserClosed:
            raise
        except Exception as exc:
            if browser_closed_error(exc):
                raise BrowserClosed() from exc
            app.logger.warning("tweets run failed")
            publish_job(job_id, status="done", success=False, message=str(exc) or "تعذّر تنفيذ الإجراءات.")
            raise
    # One attempt only. If anything fails, the task stops with a message and is not retried.
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
            app.logger.warning("tweets run failed")
            message = "أُغلق المتصفح أثناء العمل." if browser_closed_error(exc) else (str(exc) or "تعذّر تنفيذ الإجراءات.")
            publish_job(job_id, status="done", success=False, message=message)


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
            "verified_at": float(row.get("verified_at") or 0),
        })
    cleaned.sort(key=lambda item: item["verified_at"], reverse=True)
    saved_sessions = cleaned[:50]


def remember_session(auth_token: str, username: str) -> None:
    token = (auth_token or "").strip()
    if not valid_auth_token(token):
        return
    username = (username or "").strip().lstrip("@")
    if username and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        username = ""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with sessions_lock:
        found = next((row for row in saved_sessions if hmac.compare_digest(row["digest"], digest)), None)
        if found:
            if username:
                found["username"] = username
            found["token"] = token
            found["verified_at"] = now
        else:
            saved_sessions.append({
                "id": secrets.token_urlsafe(12),
                "digest": digest,
                "token": token,
                "username": username,
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


def public_job(job: dict, light: bool = False) -> dict:
    """light: while the task runs, leave out its tweet and result lists (the page only
    needs the counts and message then; the full lists are sent once it finishes)."""
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
    return payload


SCREEN_PATH = DATA_DIR / "screen.json"
screen_lock = threading.Lock()
screen: dict = {
    "auth_token": "",
    "tweets": "",
    "actions": ["repost", "like", "bookmark"],
    "job_id": "",
    "job_key": "",
    "started_at": 0,
}


def load_screen() -> None:
    loaded = read_private_json(SCREEN_PATH) or {}
    with screen_lock:
        for key in ("auth_token", "job_id", "job_key"):
            value = str(loaded.get(key) or "").strip()
            if value:
                screen[key] = value
        actions = loaded.get("actions")
        if isinstance(actions, list):
            picked = [action for action in actions if isinstance(action, str) and action in ALLOWED_ACTIONS]
            if picked:
                screen["actions"] = list(dict.fromkeys(picked))
        screen["tweets"] = str(loaded.get("tweets") or "")[:MAX_TWEETS_TEXT]
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
            "tweets": screen.get("tweets") or "",
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
    if "tweets" in data:
        fields["tweets"] = str(data.get("tweets") or "")[:MAX_TWEETS_TEXT]
    actions = data.get("actions")
    if isinstance(actions, list):
        picked = [action for action in actions if isinstance(action, str) and action in ALLOWED_ACTIONS]
        if picked:
            fields["actions"] = list(dict.fromkeys(picked))
    if fields:
        save_screen(**fields)
    return jsonify(success=True)


@app.post("/<access_path>/tweets-run")
def tweets_run_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    data = request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    auth_token = str(data.get("auth_token", "")).strip()
    raw_tweets = str(data.get("tweets") or "")
    items = parse_tweet_list(raw_tweets)
    requested_actions = data.get("actions")
    if not valid_auth_token(auth_token):
        return jsonify(success=False, message="أدخل auth_token صالحًا."), 400
    if not items:
        return jsonify(success=False, message="الصق رابط تغريدة واحدة على الأقل."), 400
    if len(items) > MAX_REPOSTS_REQUEST:
        return jsonify(success=False, message=f"الحد الأقصى {MAX_REPOSTS_REQUEST} تغريدة في المهمة الواحدة."), 400
    if not isinstance(requested_actions, list) or not requested_actions or any(not isinstance(action, str) or action not in ALLOWED_ACTIONS for action in requested_actions):
        return jsonify(success=False, message="اختر إجراء واحدًا على الأقل."), 400
    profiles_per_account = parse_requested_count(data.get("account_profiles")) or 1
    profiles_per_account = max(1, min(MAX_PROFILES_PER_ACCOUNT, profiles_per_account))
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
    job_id, job_key = begin_job(owner, client_id, len(items), "login", "التحقق من الجلسة…", "execute", f"{len(items)} تغريدة")
    publish_job(job_id, items=items, collected=len(items))
    save_screen(
        auth_token=auth_token,
        tweets=raw_tweets[:MAX_TWEETS_TEXT],
        actions=requested_actions,
        job_id=job_id,
        job_key=job_key,
        started_at=time.time(),
    )
    threading.Thread(
        target=run_tweets_job,
        args=(job_id, auth_token, requested_actions, gemini_api_key, gemini_model or "", profiles_per_account),
        daemon=True,
    ).start()
    return jsonify(success=True, job_id=job_id, job_key=job_key, count=len(items), message="بدأ التنفيذ.")


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
    threading.Thread(target=run_tweets_job, args=(job_id, *args), daemon=True).start()
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


@app.get("/<access_path>/jobs/<job_id>")
def job_status(access_path: str, job_id: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    light = request.args.get("light") == "1"
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(success=False, message="لا توجد عملية بهذا المعرّف."), 404
        if job_key_matches(job, request.headers.get("X-Job-Key", "")):
            return jsonify(public_job(job, light=light))
        if not csrf_matches(request.headers.get("X-CSRF-Token", "")):
            return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
        owner_ok = bool(job.get("owner")) and job.get("owner") == session.get("csrf_token", "")
        if not owner_ok:
            return jsonify(success=False, message="لا توجد عملية بهذا المعرّف."), 404
        return jsonify(public_job(job, light=light))


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


@app.get("/<access_path>/gemini-usage")
def gemini_usage_endpoint(access_path: str):
    if not hmac.compare_digest(access_path, ACCESS_PATH):
        abort(404)
    if not csrf_matches(request.headers.get("X-CSRF-Token", "")):
        return jsonify(success=False, message="انتهت صلاحية الصفحة. أعد تحميلها ثم حاول مجددًا."), 400
    with gemini_usage_lock:
        comments = int(gemini_usage.get("comments") or 0)
        since = gemini_usage.get("since") or 0
    return jsonify(
        success=True,
        since=since,
        comments=comments,
        price_per_comment=GEMINI_PRICE_PER_COMMENT,
        total=round(comments * GEMINI_PRICE_PER_COMMENT, 6),
    )


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


load_sessions()
load_done_history()
load_gemini()
load_gemini_usage()
load_jobs()
threading.Thread(target=save_jobs_in_background, name="jobs-saver", daemon=True).start()
load_screen()
atexit.register(shutdown_jobs)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
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
