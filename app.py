"""
بوابة الدخول برمز الدعوة
========================

- المدير يدخل لوحة التحكم ‎/admin‎ ويُنشئ مستخدماً، فيحصل على رمز دعوة.
- يرسل المدير الرمز للمستخدم.
- المستخدم يفتح ‎/register‎ ويُدخل الرمز ويختار كلمة مرور.
- بعدها يدخل من ‎/login‎ باسم المستخدم وكلمة المرور.
- لا أحد يقدر يفتح صفحات التطبيق بدون حساب مفعّل.

التشغيل:
    python app.py create-admin     # إنشاء حساب المدير (مرة واحدة)
    python app.py                  # تشغيل للتجربة على الجهاز
    gunicorn app:app               # تشغيل على الاستضافة
"""

import functools
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# الإعدادات (كلها تُغيَّر من متغيرات البيئة بدون تعديل الكود)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "instance"))
DB_PATH = DATA_DIR / "app.db"

APP_NAME = os.environ.get("APP_NAME", "تطبيقي")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Riyadh")

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # بدون O/0 و I/1 لتجنب اللخبطة
CODE_LENGTH = 12
DEFAULT_INVITE_DAYS = 7
MAX_INVITE_DAYS = 365
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"[\w.\-]{3,32}")

# الحماية من التخمين: بعد 10 محاولات خاطئة من نفس الـ IP يُمنع 15 دقيقة
MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 15 * 60

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo(APP_TIMEZONE)
except Exception:  # لو المنطقة الزمنية غير متوفرة على السيرفر
    LOCAL_TZ = timezone.utc


# ---------------------------------------------------------------------------
# قاعدة البيانات (SQLite - ملف واحد داخل مجلد instance)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    note              TEXT    NOT NULL DEFAULT '',
    password_hash     TEXT,
    is_admin          INTEGER NOT NULL DEFAULT 0,
    is_active         INTEGER NOT NULL DEFAULT 1,
    invite_hash       TEXT    UNIQUE,
    invite_expires_at TEXT,
    session_version   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    registered_at     TEXT,
    last_login_at     TEXT
);
CREATE TABLE IF NOT EXISTS failed_attempts (
    key TEXT NOT NULL,
    ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failed_attempts ON failed_attempts (key, ts);
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat(timespec="seconds")


def parse_iso(value):
    return datetime.fromisoformat(value) if value else None


# ---------------------------------------------------------------------------
# رموز الدعوة
# ---------------------------------------------------------------------------


def generate_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + 4] for i in range(0, CODE_LENGTH, 4))


def normalize_code(code):
    return "".join(ch for ch in (code or "").upper() if ch.isascii() and ch.isalnum())


def hash_code(code):
    # الرمز لا يُحفظ كما هو في قاعدة البيانات، فقط بصمته
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def invite_is_valid(row):
    if not row["invite_hash"] or not row["is_active"]:
        return False
    expires = parse_iso(row["invite_expires_at"])
    return expires is None or expires > now_utc()


def issue_invite(db, user_id, days):
    """ينشئ رمزاً جديداً للمستخدم (ويلغي أي رمز سابق له) ويرجع الرمز."""
    code = generate_code()
    expires = now_utc() + timedelta(days=days)
    db.execute(
        "UPDATE users SET invite_hash = ?, invite_expires_at = ? WHERE id = ?",
        (hash_code(code), expires.isoformat(timespec="seconds"), user_id),
    )
    return code, expires


def registration_link(code):
    path = url_for("register", code=code)
    if PUBLIC_URL:
        return PUBLIC_URL + path
    return url_for("register", code=code, _external=True)


# ---------------------------------------------------------------------------
# الحماية من التخمين
# ---------------------------------------------------------------------------


def client_ip():
    return request.remote_addr or "unknown"


def too_many_failures(key):
    db = get_db()
    db.execute(
        "DELETE FROM failed_attempts WHERE ts < ?",
        (time.time() - FAILURE_WINDOW_SECONDS,),
    )
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) FROM failed_attempts WHERE key = ?", (key,)
    ).fetchone()[0]
    return count >= MAX_FAILURES


def record_failure(key):
    db = get_db()
    db.execute(
        "INSERT INTO failed_attempts (key, ts) VALUES (?, ?)", (key, time.time())
    )
    db.commit()


# ---------------------------------------------------------------------------
# التطبيق
# ---------------------------------------------------------------------------


def load_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "secret_key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "COOKIE_SECURE", "1" if PUBLIC_URL.startswith("https://") else "0"
    )
    == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)

if os.environ.get("TRUST_PROXY") == "1":
    # فعّلها فقط إذا كان التطبيق خلف Nginx أو بروكسي الاستضافة
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# بصمة وهمية حتى يأخذ فحص اسم مستخدم غير موجود نفس الوقت تقريباً
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.template_filter("localtime")
def localtime_filter(value):
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return "—"
    text = dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    return Markup('<bdi dir="ltr">%s</bdi>') % text


@app.context_processor
def inject_globals():
    return {"app_name": APP_NAME}


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["invite_is_valid"] = invite_is_valid


@app.before_request
def load_current_user():
    g.user = None
    if request.endpoint == "static":
        return
    user_id = session.get("uid")
    if user_id is None:
        return
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    # أي تغيير على الحساب (إيقاف/حذف/إعادة تعيين) يُخرج المستخدم فوراً
    if (
        row is None
        or not row["is_active"]
        or not row["password_hash"]
        or row["session_version"] != session.get("sv")
    ):
        session.clear()
        return
    g.user = row


@app.before_request
def csrf_protect():
    if request.method != "POST":
        return
    expected = session.get("csrf")
    sent = request.form.get("csrf_token", "")
    if expected and hmac.compare_digest(expected, sent):
        return
    flash("انتهت صلاحية الصفحة، حاول مرة أخرى.", "error")
    endpoint = request.endpoint or ""
    if endpoint in ("login", "register"):
        return redirect(current_path())
    if endpoint.startswith("admin"):
        return redirect(url_for("admin"))
    return redirect(url_for("index"))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "form-action 'self'; base-uri 'none'",
    )
    if request.endpoint != "static":
        resp.headers["Cache-Control"] = "no-store"
    return resp


def current_path():
    return request.full_path if request.query_string else request.path


def login_required(view):
    """ضعها فوق أي صفحة تبي تحميها: لازم المستخدم يكون مسجّل دخول."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=current_path()))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    """للصفحات الخاصة بالمدير فقط."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=current_path()))
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def safe_next(target):
    # نمنع التحويل لمواقع خارجية
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def start_session(user_row):
    session.clear()
    session.permanent = True
    session["uid"] = user_row["id"]
    session["sv"] = user_row["session_version"]
    db = get_db()
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso(), user_row["id"]))
    db.commit()


# ---------------------------------------------------------------------------
# صفحات المستخدم
# ---------------------------------------------------------------------------


@app.route("/")
@login_required
def index():
    # 👈 هنا واجهة تطبيقك. أي صفحة جديدة ضع فوقها @login_required
    return render_template("dashboard.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        ip_key = "login:" + client_ip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if too_many_failures(ip_key):
            flash("محاولات خاطئة كثيرة. انتظر ربع ساعة ثم حاول مرة أخرى.", "error")
            return render_template("login.html", username=username), 429

        row = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is not None and row["password_hash"]:
            ok = check_password_hash(row["password_hash"], password)
        else:
            check_password_hash(DUMMY_HASH, password)
            ok = False

        if not ok:
            record_failure(ip_key)
            flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
            return render_template("login.html", username=username), 401

        if not row["is_active"]:
            flash("هذا الحساب موقوف. تواصل مع المدير.", "error")
            return render_template("login.html", username=username), 403

        start_session(row)
        return redirect(safe_next(request.args.get("next")) or url_for("index"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", code=request.args.get("code", ""))

    ip_key = "register:" + client_ip()
    code = request.form.get("code", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    if too_many_failures(ip_key):
        flash("محاولات خاطئة كثيرة. انتظر ربع ساعة ثم حاول مرة أخرى.", "error")
        return render_template("register.html", code=code), 429

    db = get_db()
    row = None
    if normalize_code(code):
        row = db.execute(
            "SELECT * FROM users WHERE invite_hash = ?", (hash_code(code),)
        ).fetchone()

    if row is None or not invite_is_valid(row):
        record_failure(ip_key)
        flash("الرمز غير صحيح أو انتهت صلاحيته. اطلب رمزاً جديداً من المدير.", "error")
        return render_template("register.html", code=code), 400

    if len(password) < MIN_PASSWORD_LENGTH:
        flash(f"كلمة المرور لازم تكون {MIN_PASSWORD_LENGTH} أحرف على الأقل.", "error")
        return render_template("register.html", code=code), 400

    if password != confirm:
        flash("كلمتا المرور غير متطابقتين.", "error")
        return render_template("register.html", code=code), 400

    cur = db.execute(
        """
        UPDATE users
           SET password_hash = ?,
               invite_hash = NULL,
               invite_expires_at = NULL,
               registered_at = COALESCE(registered_at, ?),
               session_version = session_version + 1
         WHERE id = ? AND invite_hash = ?
        """,
        (generate_password_hash(password), now_iso(), row["id"], row["invite_hash"]),
    )
    db.commit()
    if cur.rowcount != 1:  # الرمز استُخدم في نفس اللحظة من مكان آخر
        flash("الرمز غير صحيح أو انتهت صلاحيته. اطلب رمزاً جديداً من المدير.", "error")
        return render_template("register.html", code=code), 400

    user = db.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    start_session(user)
    flash(
        f"تم التسجيل بنجاح! اسم المستخدم الخاص بك: {user['username']} — احفظه مع كلمة المرور.",
        "success",
    )
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    flash("تم تسجيل الخروج.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# لوحة تحكم المدير
# ---------------------------------------------------------------------------


def parse_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_INVITE_DAYS
    return max(1, min(days, MAX_INVITE_DAYS))


def get_user_or_404(user_id):
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def remember_new_invite(username, code, expires):
    # يُعرض مرة واحدة فقط في الصفحة التالية
    session["new_invite"] = {
        "username": username,
        "code": code,
        "link": registration_link(code),
        "expires": expires.isoformat(timespec="seconds"),
    }


@app.get("/admin")
@admin_required
def admin():
    users = get_db().execute(
        "SELECT * FROM users ORDER BY is_admin DESC, created_at DESC, id DESC"
    ).fetchall()
    return render_template(
        "admin.html",
        users=users,
        new_invite=session.pop("new_invite", None),
        default_days=DEFAULT_INVITE_DAYS,
        max_days=MAX_INVITE_DAYS,
    )


@app.post("/admin/users")
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    note = request.form.get("note", "").strip()[:200]
    days = parse_days(request.form.get("days"))

    if not USERNAME_RE.fullmatch(username):
        flash("اسم المستخدم من 3 إلى 32 حرفاً (حروف أو أرقام أو _ . -) بدون مسافات.", "error")
        return redirect(url_for("admin"))

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, note, created_at) VALUES (?, ?, ?)",
            (username, note, now_iso()),
        )
    except sqlite3.IntegrityError:
        flash(f"اسم المستخدم «{username}» موجود مسبقاً.", "error")
        return redirect(url_for("admin"))

    code, expires = issue_invite(db, cur.lastrowid, days)
    db.commit()
    remember_new_invite(username, code, expires)
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/invite")
@admin_required
def admin_new_invite(user_id):
    """رمز جديد: لو المستخدم ما سجّل أو نسي كلمة المرور."""
    row = get_user_or_404(user_id)
    db = get_db()
    code, expires = issue_invite(db, user_id, parse_days(request.form.get("days")))
    db.commit()
    remember_new_invite(row["username"], code, expires)
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/cancel-invite")
@admin_required
def admin_cancel_invite(user_id):
    row = get_user_or_404(user_id)
    db = get_db()
    db.execute(
        "UPDATE users SET invite_hash = NULL, invite_expires_at = NULL WHERE id = ?",
        (user_id,),
    )
    db.commit()
    flash(f"تم إلغاء رمز «{row['username']}».", "success")
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/toggle")
@admin_required
def admin_toggle_user(user_id):
    row = get_user_or_404(user_id)
    if row["id"] == g.user["id"]:
        flash("ما تقدر توقف حسابك أنت.", "error")
        return redirect(url_for("admin"))
    db = get_db()
    db.execute(
        "UPDATE users SET is_active = 1 - is_active, session_version = session_version + 1 WHERE id = ?",
        (user_id,),
    )
    db.commit()
    if row["is_active"]:
        flash(f"تم إيقاف «{row['username']}» وإخراجه من التطبيق.", "success")
    else:
        flash(f"تم تفعيل «{row['username']}».", "success")
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/delete")
@admin_required
def admin_delete_user(user_id):
    row = get_user_or_404(user_id)
    if row["id"] == g.user["id"]:
        flash("ما تقدر تحذف حسابك أنت.", "error")
        return redirect(url_for("admin"))
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"تم حذف «{row['username']}».", "success")
    return redirect(url_for("admin"))


# ---------------------------------------------------------------------------
# صفحات الأخطاء
# ---------------------------------------------------------------------------


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", title="غير مسموح", message="هذه الصفحة للمدير فقط."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", title="غير موجودة", message="الصفحة المطلوبة غير موجودة."), 404


# ---------------------------------------------------------------------------
# إنشاء حساب المدير
# ---------------------------------------------------------------------------


def upsert_admin(username, password):
    """ينشئ مديراً جديداً، أو يغيّر كلمة مرور مستخدم موجود ويجعله مديراً."""
    with closing(connect()) as conn:
        pw_hash = generate_password_hash(password)
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE users
                      SET password_hash = ?, is_admin = 1, is_active = 1,
                          invite_hash = NULL, invite_expires_at = NULL,
                          registered_at = COALESCE(registered_at, ?),
                          session_version = session_version + 1
                    WHERE id = ?""",
                (pw_hash, now_iso(), existing["id"]),
            )
            created = False
        else:
            conn.execute(
                """INSERT INTO users (username, password_hash, is_admin, created_at, registered_at)
                   VALUES (?, ?, 1, ?, ?)""",
                (username, pw_hash, now_iso(), now_iso()),
            )
            created = True
        conn.commit()
        return created


def bootstrap_admin_from_env():
    """لو الاستضافة ما فيها Terminal: ضع ADMIN_USERNAME و ADMIN_PASSWORD في متغيرات البيئة."""
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not username or not password:
        return
    with closing(connect()) as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
    if has_admin:
        return
    try:
        upsert_admin(username, password)
    except sqlite3.IntegrityError:
        pass  # عامل (worker) آخر أنشأه في نفس اللحظة


def cli_create_admin():
    print("=== إنشاء حساب المدير ===")
    username = input("اسم المستخدم: ").strip()
    if not USERNAME_RE.fullmatch(username):
        sys.exit("اسم المستخدم من 3 إلى 32 حرفاً (حروف أو أرقام أو _ . -) بدون مسافات.")
    password = getpass("كلمة المرور: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"كلمة المرور لازم تكون {MIN_PASSWORD_LENGTH} أحرف على الأقل.")
    if getpass("أعد كتابة كلمة المرور: ") != password:
        sys.exit("كلمتا المرور غير متطابقتين.")
    created = upsert_admin(username, password)
    print("تم إنشاء المدير ✅" if created else "تم تحديث كلمة المرور وصلاحية المدير ✅")


init_db()
bootstrap_admin_from_env()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        cli_create_admin()
    elif len(sys.argv) > 1:
        sys.exit("الأوامر المتاحة: python app.py  |  python app.py create-admin")
    else:
        app.run(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1",
        )
