"""واجهة ويب (Flask) لأدوات الإشراف على قروبات إكس.

التشغيل:
    python app.py            # ثم افتح http://127.0.0.1:5000
    HOST=0.0.0.0 PORT=8080 python app.py   # على VPS خلف عكس بروكسي/جدار ناري

مبدأ الدخول: لا تُدخل رمز جلسة هنا. سجّل دخولك على جهازك بـ:
    python bot.py login x https://x.com/login
وارفع الملف الناتج sessions/x.json إلى مجلد sessions/ بجانب هذا الملف.
"""

import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import xdriver

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
SESSION_NAME = "x"
HEADLESS = os.environ.get("HEADLESS", "1") != "0"


@app.get("/")
def index():
    return render_template("index.html", session_ready=xdriver.session_exists(SESSION_NAME))


@app.get("/api/session-status")
def session_status():
    return jsonify({"ready": xdriver.session_exists(SESSION_NAME),
                    "path": str(xdriver.session_path(SESSION_NAME))})


@app.post("/api/verify")
def verify():
    if not xdriver.session_exists(SESSION_NAME):
        return jsonify({"ok": False, "error": "ملف الجلسة غير موجود. ارفع sessions/x.json أولاً."}), 400
    try:
        result = xdriver.verify_login(SESSION_NAME, headless=HEADLESS)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/conversations")
def conversations():
    if not xdriver.session_exists(SESSION_NAME):
        return jsonify({"error": "ملف الجلسة غير موجود."}), 400
    try:
        items = xdriver.list_conversations(SESSION_NAME, headless=HEADLESS)
        return jsonify({"count": len(items), "conversations": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/open")
def open_group():
    conv_id = (request.json or {}).get("id")
    if not conv_id:
        return jsonify({"error": "لم يُحدَّد القروب."}), 400
    try:
        result = xdriver.open_conversation(conv_id, SESSION_NAME, headless=HEADLESS)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=True, use_reloader=False)
