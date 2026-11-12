

# ============================================================================
# قفل التطبيق + ربطه بخادم التحكّم (control_server.py على استضافة المالك).
# التطبيق لا يفتح إلا برمز يعرفه الخادم ولم يوقفه المالك، والتعليقات تُكتب على
# الخادم بمفاتيح المالك (لا تُرسل المفاتيح للجهاز). رابط الخادم مثبّت هنا، وبصمة
# شهادته داخل الرابط فلا يقبل التطبيق خادمًا آخر.
# ============================================================================
import http.client as _http_client
import ssl as _ssl

# رابط خادم التحكّم (المالك يضعه قبل البناء). متغيّر البيئة GG_CONTROL_LINK يغلبه (للاختبار).
CONTROL_LINK = "https://217.216.77.23:8443/yPyHqJkUQvv52HD7xxuomYeST1xigjf0mLPoFJHq#b8a830644e5ed5eb80bf2ee7005ec4c29d7a60ad7f47ab3eee899bb04ac47a46"
_CONTROL_RE = re.compile(r"https://(\[[0-9A-Fa-f:]{2,39}\]|[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?)(?::(\d{1,5}))?/([A-Za-z0-9_\-]{16,64})/?#([0-9a-fA-F]{64})")
UNLOCK_PATH = DATA_DIR / "unlock.json"
_unlock_lock = threading.Lock()
_unlock_cache = {"token": "", "code": "", "ok": False, "at": 0.0}
_CONTROL_GRACE = 120          # ثوانٍ: بين كل تحقّق وآخر مع الخادم (حتى لا يُثقل كل طلب)


class _ControlUnreachable(RuntimeError):
    pass


def control_link() -> str:
    return (os.environ.get("GG_CONTROL_LINK") or CONTROL_LINK or "").strip()


DEVICE_PATH = DATA_DIR / "device.json"
_device_lock = threading.Lock()
_device_id_cache = ""


def device_id() -> str:
    """A stable id for this install, made once and kept — so a code binds to one phone (a copy of the code on
    another phone is refused). Letters and digits only."""
    global _device_id_cache
    with _device_lock:
        if _device_id_cache:
            return _device_id_cache
        got = str((read_private_json(DEVICE_PATH) or {}).get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9]{16,64}", got):
            got = secrets.token_hex(16)
            write_private_json(DEVICE_PATH, {"id": got})
        _device_id_cache = got
        return got


def _control_parse(link: str):
    match = _CONTROL_RE.fullmatch(link or "")
    if not match:
        return None
    port = int(match.group(2) or 443)
    if not 0 < port < 65536:
        return None
    return match.group(1), port, match.group(3), match.group(4).lower()


def control_configured() -> bool:
    return _control_parse(control_link()) is not None


def _control_request(action: str, body: dict | None = None, seconds: float = 30) -> dict:
    """طلب واحد لخادم التحكّم، مع تثبيت شهادته ببصمة الرابط. يعيد ما يردّه، أو يرفع خطأ بعبارة للمستخدم."""
    parsed = _control_parse(control_link())
    if not parsed:
        raise RuntimeError("خادم التحكّم غير مضبوط في هذه النسخة. راجع صاحب البرنامج.")
    host, port, secret, pin = parsed
    context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = _ssl.CERT_NONE               # الشهادة تُعرف ببصمتها لا باسمها
    connection = _http_client.HTTPSConnection(host, port, timeout=8, context=context)
    try:
        try:
            connection.connect()
        except _ssl.SSLError:
            raise RuntimeError("على عنوان خادم التحكّم برنامج آخر. راجع الرابط.") from None
        except OSError:
            raise _ControlUnreachable("تعذّر الوصول إلى خادم التحكّم. تأكد من الإنترنت ومن أن الخادم يعمل.") from None
        seen = hashlib.sha256(connection.sock.getpeercert(binary_form=True) or b"").hexdigest()
        if not hmac.compare_digest(seen, pin):
            raise RuntimeError("شهادة خادم التحكّم لا تطابق الرابط المثبّت في التطبيق.")
        connection.sock.settimeout(seconds)
        sent = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        connection.request("POST", f"/{secret}/{action}", body=sent,
                           headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", "Connection": "close"})
        answer = connection.getresponse()
        status, raw = answer.status, answer.read(1 << 20)
    except (socket.timeout, TimeoutError):
        raise _ControlUnreachable("طال انتظار خادم التحكّم. أعد المحاولة.") from None
    except (OSError, _http_client.HTTPException):
        raise _ControlUnreachable("انقطع الاتصال بخادم التحكّم. أعد المحاولة.") from None
    finally:
        connection.close()
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise RuntimeError("ردّ غير مفهوم من خادم التحكّم." if status == 200 else f"ردّ خادم التحكّم برمز {status}.")
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "").strip() or "خطأ من خادم التحكّم.")
    return data


def _load_unlock() -> tuple[str, str]:
    data = read_private_json(UNLOCK_PATH) or {}
    return str(data.get("token") or ""), str(data.get("code") or "")


def _current_token() -> str:
    with _unlock_lock:
        if _unlock_cache["token"]:
            return _unlock_cache["token"]
    token, code = _load_unlock()
    with _unlock_lock:
        _unlock_cache["token"], _unlock_cache["code"] = token, code
    return token


def _forget_unlock() -> None:
    try:
        UNLOCK_PATH.unlink()
    except OSError:
        pass
    with _unlock_lock:
        _unlock_cache.update(token="", code="", ok=False, at=0.0)


def control_activate(code: str) -> str:
    """يُفعّل بالرمز عبر الخادم (مربوطًا بجهاز هذا التطبيق)، ويحفظ رمز الجلسة. يعيد اسم المحرّك، أو يرفع خطأ."""
    data = _control_request("activate", {"code": code, "device": device_id()})
    token = str(data.get("token") or "")
    if not token:
        raise RuntimeError("لم يصل رمز الجلسة من الخادم. أعد المحاولة.")
    write_private_json(UNLOCK_PATH, {"token": token, "code": code})
    with _unlock_lock:
        _unlock_cache.update(token=token, code=code, ok=True, at=time.monotonic())
    return str(data.get("engine") or "")


def is_unlocked() -> bool:
    """هل التطبيق مفتوح: يوجد رمز جلسة محفوظ والخادم لم يوقف الرمز (يُتحقّق كل فترة). عند تعذّر الوصول
    للخادم يبقى على آخر حالة معروفة، فلا يُقفل المستخدم بانقطاع مؤقت للإنترنت."""
    token = _current_token()
    if not token:
        return False
    now = time.monotonic()
    with _unlock_lock:
        if _unlock_cache["ok"] and now - _unlock_cache["at"] < _CONTROL_GRACE:
            return True
    try:
        active = bool(_control_request("check", {"token": token}, seconds=8).get("active"))
    except _ControlUnreachable:
        with _unlock_lock:
            _unlock_cache.update(ok=True, at=now)       # إمهال: يبقى مفتوحًا برمز محفوظ ما دام الخادم بعيدًا
        return True
    except RuntimeError:
        active = False
    with _unlock_lock:
        _unlock_cache.update(ok=active, at=now)
    if not active:
        _forget_unlock()
    return active


def control_comment(tweet_text: str, image: bytes | None, voice: str, account: str, tweet_id: str) -> str:
    """تعليق واحد يُكتب على خادم التحكّم بمفتاح المالك المُفعّل (لا يُرسل المفتاح للجهاز)."""
    token = _current_token()
    if not token:
        raise RuntimeError("التطبيق غير مُفعّل. أدخل رمز الدخول.")
    if account:
        manner = int(hashlib.sha256(("reply-manner:" + account).encode("utf-8")).hexdigest()[:8], 16) % len(REPLY_MANNERS)
    else:
        manner = random.randrange(len(REPLY_MANNERS))
    with replies_memory_lock:
        own = list(replies_by_account.get(account) or []) if account else []
        others = [text for text in (replies_by_tweet.get(tweet_id) or []) if text not in own] if tweet_id else []
    before = (own[-6:] + others[-4:])[-10:]
    body = {"token": token, "text": tweet_text or "", "voice": voice, "manner": manner, "before": before,
            "account": account or "", "name": account_names.get(account, "")}
    if image:
        body["image"] = base64.b64encode(image).decode("ascii")
    text = str(_control_request("comment", body, seconds=90).get("comment") or "")
    if not text:
        raise RuntimeError("لم يصل تعليق من خادم التحكّم. أعد المحاولة.")
    record_comment_generated()
    remember_reply(account, tweet_id, text)
    return text


UNLOCK_PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>الدخول</title>
<style>body{font:16px/1.7 system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;display:grid;place-items:center;min-height:100vh}
.box{background:#1e293b;padding:26px;border-radius:16px;width:min(92vw,340px);box-shadow:0 12px 44px #0007;text-align:center}
h1{font-size:19px;margin:0 0 4px}p{color:#94a3b8;font-size:14px;margin:0 0 18px}
input{width:100%;box-sizing:border-box;padding:13px;border-radius:11px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;text-align:center;font-size:18px;letter-spacing:2px}
button{width:100%;padding:13px;border:0;border-radius:11px;background:#2563eb;color:#fff;font-weight:700;font-size:16px;cursor:pointer;margin-top:12px}
button:disabled{opacity:.6}.msg{min-height:20px;color:#f87171;font-size:14px;margin-top:12px}</style></head>
<body><form class="box" onsubmit="go(event)"><h1>قروبات</h1><p>أدخل رمز الدخول الذي أعطاك إياه صاحب البرنامج.</p>
<input id="code" placeholder="XXXX-XXXX-XXXX" autocomplete="off" autocapitalize="characters" autofocus>
<button id="btn" type="submit">دخول</button><div class="msg" id="msg"></div></form>
<script>
async function go(e){e.preventDefault();var b=document.getElementById("btn"),m=document.getElementById("msg");
var code=document.getElementById("code").value.trim();if(!code)return;b.disabled=true;m.textContent="جارٍ التحقّق…";m.style.color="#94a3b8";
try{var r=await fetch("/unlock",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:code})});
var d=await r.json();if(d.ok){location.href="/";}else{m.style.color="#f87171";m.textContent=d.error||"الرمز غير صحيح.";b.disabled=false;}}
catch(x){m.style.color="#f87171";m.textContent="تعذّر الاتصال. أعد المحاولة.";b.disabled=false;}}
</script></body></html>"""


@app.get("/unlock")
def unlock_screen():
    if not control_configured() or is_unlocked():
        return redirect("/" + ACCESS_PATH, code=302)
    return make_response(render_template_string(UNLOCK_PAGE))


@app.post("/unlock")
def unlock_submit():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    if not code:
        return jsonify(ok=False, error="أدخل الرمز."), 200
    try:
        control_activate(code)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 200
    return jsonify(ok=True)


@app.before_request
def _unlock_gate():
    # لا قفل إن لم يُضبط خادم تحكّم (نسخة بلا رابط). وإلا: لا يمرّ شيء قبل إدخال رمز صالح.
    if not control_configured():
        return None
    path = request.path
    if path == "/healthz" or path == "/unlock":
        return None
    if is_unlocked():
        return None
    if request.method == "GET" and "text/html" in (request.headers.get("Accept") or ""):
        return redirect("/unlock", code=302)
    return jsonify(ok=False, error="التطبيق مقفل. أدخل رمز الدخول."), 423
