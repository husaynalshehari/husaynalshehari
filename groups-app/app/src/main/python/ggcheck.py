"""«فحص المتصفح»: every kind of thing the groups script asks of its browser, tried on a small page served from this
machine, with a line for each: ✓ or ✗ and why. The same check runs in the app (on the phone's WebView) and on a
computer (on Chromium), so what passes here and fails there is the phone's browser, not the script."""
from __future__ import annotations

import json
import re
import struct
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PIXEL = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                      "0000000d49444154789c6360f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>check</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{margin:0;font:16px sans-serif} #b{position:absolute;left:100px;top:300px;width:120px;height:60px}
#pin{position:absolute;left:20px;top:400px;width:200px;height:30px}
#box{position:absolute;left:20px;top:460px;width:400px;min-height:40px;border:1px solid #888}
#scroller{position:absolute;left:20px;top:560px;width:400px;height:300px;overflow:auto;border:1px solid #888}
#menu{position:absolute;left:20px;top:900px} #hidden{display:none} #far{position:absolute;left:20px;top:1500px;width:100px;height:40px}
</style></head><body>
<button id="b" data-testid="like">tap</button>
<input id="pin" type="password" inputmode="numeric">
<div id="box" data-testid="tweetTextarea_0"><div contenteditable="true" style="min-height:40px"></div></div>
<div id="scroller"><div style="height:3000px">scroll</div></div>
<div id="menu"><div role="menuitem">Everything</div><div role="menuitem">القروبات</div><div role="menuitem" id="hidden">hidden</div></div>
<article data-testid="tweet"><a href="/someone/status/111">one</a><div data-testid="tweetText">first</div></article>
<article data-testid="tweet"><a href="/other/status/222">two</a><div data-testid="tweetText">second</div></article>
<div id="host"></div>
<button id="far">far</button>
<script>
window.events = [];
const note = (name) => (e) => events.push(name + ":" + (e.pointerType || "") + ":" + e.isTrusted);
for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) document.getElementById("b").addEventListener(type, note(type));
document.getElementById("far").addEventListener("click", note("far"));
window.wheels = 0;
document.getElementById("scroller").addEventListener("wheel", () => { wheels++; });
window.keys = [];
document.addEventListener("keydown", (e) => keys.push(e.key + ":" + e.isTrusted));
const shadow = document.getElementById("host").attachShadow({mode: "open"});
shadow.innerHTML = '<span data-testid="dm-conversation-item-1">in shadow</span><span data-testid="dm-conversation-item-2">again</span>';
window.loadPixel = (n) => new Promise((done) => { const img = new Image(); img.onload = () => done("loaded"); img.onerror = () => done("blocked"); img.src = "/pixel.png?n=" + n; });
window.askData = () => fetch("/data.json?x=1").then((r) => r.json());
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        type(self).seen.append((path, {key.lower(): value for key, value in self.headers.items()}))
        if path == "/pixel.png":
            body, kind = PIXEL, "image/png"
        elif path == "/data.json":
            body, kind = json.dumps({"data": {"rest_id": "111", "legacy": {"favorited": True}}}).encode(), "application/json"
        elif path == "/second":
            body, kind = b"<!doctype html><title>second</title><p id=x>second page</p>", "text/html; charset=utf-8"
        else:
            body, kind = PAGE.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _jpeg_size(data: bytes):
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return width, height
        index += 2 + struct.unpack(">H", data[index + 2:index + 4])[0]
    return None


def run(profile_root: str | None = None, online: bool = False, say=None) -> str:
    """Run the whole check; the report (one line a step). say(line), when given, gets each line as it is known."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    lines: list[str] = []
    failed: list[str] = []

    def tell(line: str) -> None:
        lines.append(line)
        if say:
            try:
                say(line)
            except Exception:
                pass

    def step(name: str, work) -> bool:
        try:
            detail = work()
            if detail is False:
                raise AssertionError("لم يتحقق")
            tell(f"✓ {name}" + (f": {detail}" if isinstance(detail, str) and detail else ""))
            return True
        except Exception as exc:
            failed.append(name)
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:220]
            tell(f"✗ {name}: {type(exc).__name__}: {first}")
            return False

    def expect(condition, detail: str = "") -> None:
        if not condition:
            raise AssertionError(detail or "لم يتحقق")

    _Handler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, name="check-page", daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    root = Path(profile_root or tempfile.mkdtemp(prefix="gg-check-"))
    profile = root / "check-profile"
    playwright = sync_playwright().start()
    context = None
    started = time.monotonic()

    def open_browser():
        return playwright.chromium.launch_persistent_context(
            str(profile), headless=True, viewport={"width": 600, "height": 1800}, user_agent=USER_AGENT, locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"])

    try:
        holder: dict = {}

        def opened():
            holder["context"] = open_browser()
            holder["page"] = holder["context"].pages[0]
            holder["page"].set_default_timeout(20_000)
            holder["page"].set_default_navigation_timeout(60_000)
        if not step("فتح المتصفح والاتصال به", opened):
            return "\n".join(lines + ["", "النتيجة: المتصفح لم يُفتح، فلم يُجرَّب غيره."])
        context, page = holder["context"], holder["page"]

        def loaded():
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page.goto(base + "/", wait_until="domcontentloaded", timeout=30_000)
            expect(page.url.startswith(base), page.url)
            expect(page.title() == "check", page.title())
            return page.url
        step("فتح صفحة", loaded)

        def sized():
            seen = page.evaluate("() => ({w: innerWidth, h: innerHeight, ratio: devicePixelRatio})")
            expect(abs(seen["w"] - 600) <= 2 and abs(seen["h"] - 1800) <= 2, json.dumps(seen))
            expect(page.viewport_size == {"width": 600, "height": 1800}, str(page.viewport_size))
            return f"{seen['w']}×{seen['h']} (كثافة {seen['ratio']})"
        step("مقاس الصفحة 600×1800", sized)

        def evaluated():
            got = page.evaluate("({a, b}) => ({sum: a + b, list: [1, 'x', null], nothing: undefined})", {"a": 2, "b": 3})
            expect(got == {"sum": 5, "list": [1, "x", None], "nothing": None}, json.dumps(got))
            expect(page.evaluate("1 + 1") == 2 and page.evaluate("() => null") is None)
            expect(page.evaluate("async () => { await new Promise((r) => setTimeout(r, 50)); return 'later'; }") == "later")
            return ""
        step("تشغيل JavaScript في الصفحة", evaluated)

        def identity():
            seen = page.evaluate("() => ({ua: navigator.userAgent, webdriver: navigator.webdriver, lang: navigator.language,"
                                 " platform: navigator.userAgentData ? navigator.userAgentData.platform : '',"
                                 " brands: navigator.userAgentData ? navigator.userAgentData.brands.map((b) => b.brand) : []})")
            expect(seen["ua"] == USER_AGENT, seen["ua"])
            header = next((headers.get("user-agent", "") for path, headers in reversed(_Handler.seen) if path == "/"), "")
            expect(header == USER_AGENT, "في الطلب: " + header)
            notes = ""
            if seen["webdriver"]:
                notes += "، تنبيه: navigator.webdriver ظاهر"
            wv = next((headers.get("x-requested-with", "") for path, headers in reversed(_Handler.seen) if path == "/"), "")
            if wv:
                notes += "، ومع كل طلب اسم التطبيق كما في كل WebView (X-Requested-With: " + wv + ")"
            return f"لغة {seen['lang']}، منصة {seen['platform'] or '؟'}، {', '.join(seen['brands']) or 'بلا علامات'}{notes}"
        step("هوية المتصفح كما يطلبها السكربت", identity)

        def located():
            tweets = page.locator('article[data-testid="tweet"]')
            expect(tweets.count() == 2, f"tweets {tweets.count()}")
            second = tweets.filter(has=page.locator('a[href*="/status/222"]'))
            expect(second.count() == 1 and second.first.locator('[data-testid="tweetText"]').first.inner_text() == "second")
            expect(tweets.last.locator('[data-testid="tweetText"]').inner_text(timeout=2000) == "second")
            expect(page.locator('[data-testid^="dm-conversation-item-"]').count() == 2, "shadow")
            expect(page.locator('[data-testid^="dm-conversation-item-"]').nth(1).inner_text() == "again")
            expect(page.locator("#hidden, #b").count() == 2 and page.locator("#hidden:visible, #b:visible").count() == 1, ":visible")
            expect(page.locator("#b").first.is_visible() and not page.locator("#hidden").first.is_visible())
            expect(page.locator("#nothing").count() == 0 and not page.locator("#nothing").first.is_visible(timeout=500))
            menu = page.get_by_role("menuitem", name=re.compile(r"groups|قروب|مجموع", re.I))
            expect(menu.count() == 1 and menu.first.inner_text() == "القروبات", f"role {menu.count()}")
            box = page.locator("#b").bounding_box()
            expect(box and abs(box["x"] - 100) < 1 and abs(box["y"] - 300) < 1 and abs(box["width"] - 120) < 1, json.dumps(box))
            try:
                page.locator("#nothing").first.wait_for(state="visible", timeout=300)
                expect(False, "wait_for did not time out")
            except PlaywrightTimeoutError as exc:
                expect("#nothing" in str(exc), str(exc))
            return ""
        step("العثور على العناصر (ومنها ما داخل shadow)", located)

        def clicked():
            page.evaluate("events.splice(0)")
            box = page.locator("#b").bounding_box()
            page.mouse.move(box["x"] + 30, box["y"] + 20)
            page.mouse.move(box["x"] + 60, box["y"] + 30)
            page.mouse.down()
            page.wait_for_timeout(100)
            page.mouse.up()
            page.wait_for_timeout(200)
            events = page.evaluate("events.splice(0)")
            expect(any(event.startswith("click:") and event.endswith(":true") for event in events), str(events))
            page.locator("#far").click(timeout=4000, delay=100)
            page.wait_for_timeout(150)
            far = page.evaluate("events.splice(0)")
            expect(any(event.startswith("far:") for event in far), "locator.click: " + str(far))
            page.evaluate("window.scrollTo(0, 0)")
            return ", ".join(events)
        step("نقر حقيقي بالمؤشر", clicked)

        def wheeled():
            page.evaluate("() => { document.getElementById('scroller').scrollTop = 0; wheels = 0; }")
            page.mouse.move(200, 700)
            page.mouse.wheel(0, 700)
            deadline = time.monotonic() + 3
            top = 0
            while time.monotonic() < deadline and top < 100:
                page.wait_for_timeout(100)
                top = page.evaluate("document.getElementById('scroller').scrollTop")
            expect(top >= 100, f"scrollTop {top}")
            page.mouse.wheel(0, -2000)
            page.wait_for_timeout(600)
            back = page.evaluate("document.getElementById('scroller').scrollTop")
            expect(back < top, f"scrollTop {top} → {back}")
            return f"نزل {round(top)} ثم عاد إلى {round(back)}، أحداث العجلة {page.evaluate('wheels')}"
        step("التمرير بعجلة المؤشر", wheeled)

        def typed():
            page.locator("#pin").first.click(timeout=4000)
            page.keyboard.type("1234", delay=30)
            expect(page.evaluate("document.getElementById('pin').value") == "1234", "pin: " + str(page.evaluate("document.getElementById('pin').value")))
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            expect(page.evaluate("document.getElementById('pin').value") == "", "after delete: " + str(page.evaluate("document.getElementById('pin').value")))
            box = page.locator('[data-testid="tweetTextarea_0"]').last
            box.click(timeout=4000)
            text = "والله الزحمة صارت جزء من يومنا 😂"
            page.keyboard.insert_text(text)
            page.wait_for_timeout(150)
            got = (box.inner_text() or "").strip()
            expect(got == text, "insert_text: " + got)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type("hello كلام", delay=5)
            page.wait_for_timeout(150)
            got = (box.inner_text() or "").strip()
            expect(got == "hello كلام", "type: " + got)
            page.evaluate("keys.splice(0)")
            page.keyboard.press("Escape")
            keys = page.evaluate("keys.splice(0)")
            expect("Escape:true" in keys, str(keys))
            return ""
        step("الكتابة (أرقام الرمز، نص عربي، مسح، Escape)", typed)

        def pictured():
            shot = page.screenshot(type="jpeg", quality=40, timeout=8000, clip={"x": 0, "y": 0, "width": 600, "height": 1000})
            expect(shot[:2] == b"\xff\xd8", "ليست JPEG")
            size = _jpeg_size(shot)
            expect(size and abs(size[0] - 600) <= 2 and abs(size[1] - 1000) <= 2, f"مقاس الصورة {size}")
            card = page.locator("#b").first.screenshot(type="jpeg", quality=70, timeout=12000)
            small = _jpeg_size(card)
            expect(small and abs(small[0] - 120) <= 2 and abs(small[1] - 60) <= 2, f"صورة الزر {small}")
            png = page.screenshot(timeout=8000)
            expect(png[:4] == b"\x89PNG", "ليست PNG")
            return f"الصفحة {size[0]}×{size[1]} ({len(shot) // 1024} ك.ب)، الزر {small[0]}×{small[1]}"
        step("تصوير الصفحة وعنصر منها", pictured)

        def resized():
            page.set_viewport_size({"width": 600, "height": 9000})
            tall = page.evaluate("[innerWidth, innerHeight]")
            expect(abs(tall[0] - 600) <= 2 and abs(tall[1] - 9000) <= 2, f"طويلة: {tall}")
            page.set_viewport_size({"width": 412, "height": 900})
            phone = page.evaluate("[innerWidth, innerHeight]")
            expect(abs(phone[0] - 412) <= 2 and abs(phone[1] - 900) <= 2, f"جوال: {phone}")
            page.set_viewport_size({"width": 600, "height": 1800})
            normal = page.evaluate("[innerWidth, innerHeight]")
            expect(abs(normal[0] - 600) <= 2 and abs(normal[1] - 1800) <= 2, f"عادية: {normal}")
            return f"{tall[0]}×{tall[1]} ثم {phone[0]}×{phone[1]} ثم {normal[0]}×{normal[1]}"
        step("تغيير مقاس الصفحة أثناء العمل", resized)

        def cookied():
            context.add_cookies([
                {"name": "auth_token", "value": "secret-token", "domain": "127.0.0.1", "path": "/", "httpOnly": True, "secure": False, "sameSite": "Lax"},
                {"name": "plain", "value": "seen", "domain": "127.0.0.1", "path": "/", "httpOnly": False, "secure": False, "sameSite": "Lax"},
            ])
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            in_page = page.evaluate("document.cookie")
            expect("plain=seen" in in_page and "auth_token" not in in_page, "في الصفحة: " + in_page)
            sent = next((headers.get("cookie", "") for path, headers in reversed(_Handler.seen) if path == "/"), "")
            expect("auth_token=secret-token" in sent, "في الطلب: " + sent)
            # (Taken away again: where accounts share one profile, the app's own page is at this address too.)
            context.add_cookies([{"name": name, "value": "", "domain": "127.0.0.1", "path": "/", "expires": 1, "httpOnly": name == "auth_token",
                                  "secure": False, "sameSite": "Lax"} for name in ("auth_token", "plain")])
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            left = next((headers.get("cookie", "") for path, headers in reversed(_Handler.seen) if path == "/"), "")
            expect("auth_token" not in left and "plain" not in left, "بعد الحذف: " + left)
            return ""
        step("الكوكيز (auth_token لا تراه الصفحة ويصل للموقع)", cookied)

        def routed():
            state = {"allow": False}

            def handle(route):
                request = route.request
                if not state["allow"] and (request.resource_type in {"image", "media"} or "video.twimg.com" in request.url):
                    route.abort()
                else:
                    route.continue_()
            context.route("**/*", handle)
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            expect(page.evaluate("loadPixel(1)") == "blocked", "الصورة لم تُمنع")
            state["allow"] = True
            expect(page.evaluate("loadPixel(2)") == "loaded", "الصورة لم تُحمَّل بعد السماح")
            expect(page.evaluate("askData()") == {"data": {"rest_id": "111", "legacy": {"favorited": True}}}, "طلب عادي تعطّل")
            return ""
        step("منع الصور ثم السماح بها", routed)

        def listened():
            finished: list = []

            def on_finished(request):
                if "/data.json" in request.url:
                    finished.append(request)
            page.on("requestfinished", on_finished)
            try:
                page.evaluate("askData()")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not finished:
                    page.wait_for_timeout(100)
                expect(finished, "لم يصل حدث انتهاء الطلب")
                response = finished[0].response()
                expect(response is not None and response.json()["data"]["rest_id"] == "111", "رد الطلب")
            finally:
                page.remove_listener("requestfinished", on_finished)
            cdp = context.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send("Network.setBypassServiceWorker", {"bypass": True})
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            cdp.send("Network.setBypassServiceWorker", {"bypass": False})
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
            cdp.detach()
            return ""
        step("قراءة ردود الموقع وأوامر DevTools", listened)

        def travelled():
            page.evaluate("localStorage.setItem('kept', 'yes')")
            page.goto(base + "/second", wait_until="domcontentloaded", timeout=30_000)
            expect(page.url.endswith("/second") and page.locator("#x").first.inner_text() == "second page", page.url)
            page.go_back(wait_until="domcontentloaded", timeout=15_000)
            expect(page.url.rstrip("/") == base, "بعد الرجوع: " + page.url)
            page.evaluate("history.pushState({}, '', '/i/chat/g123')")
            page.wait_for_timeout(200)
            expect(page.url.endswith("/i/chat/g123"), "عنوان داخل الصفحة: " + page.url)
            page.wait_for_function("() => document.readyState === 'complete'", timeout=8000)
            try:
                page.wait_for_function("(word) => document.title === word", arg="never", timeout=400)
                expect(False, "wait_for_function did not time out")
            except PlaywrightTimeoutError:
                pass
            return ""
        step("التنقل والرجوع وانتظار الصفحة", travelled)

        def reopened():
            nonlocal context, page
            context.close()
            try:
                page.evaluate("1")
                expect(False, "الصفحة ما زالت تجيب بعد الإغلاق")
            except Exception as exc:
                expect("has been closed" in str(exc), str(exc))
            context = open_browser()
            page = context.pages[0]
            page.goto(base + "/", wait_until="domcontentloaded", timeout=30_000)
            kept = page.evaluate("localStorage.getItem('kept')")
            expect(kept == "yes", "ملف الحساب لم يُحفظ بين الفتحتين: " + str(kept))
            return ""
        step("إغلاق المتصفح وفتحه بملف الحساب نفسه", reopened)

        if online:
            def reached():
                page.goto("https://x.com/", wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2500)
                return f"{page.url} «{page.title()[:40]}»"
            step("الوصول إلى X", reached)
    except Exception:
        tell("✗ خطأ غير متوقع أثناء الفحص:")
        for line in traceback.format_exc().strip().splitlines()[-6:]:
            tell("   " + line)
        failed.append("unexpected")
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
        server.shutdown()
        server.server_close()
    tell("")
    seconds = round(time.monotonic() - started)
    tell(f"النتيجة: كل شيء يعمل ({seconds} ث)." if not failed else f"النتيجة: {len(failed)} لم ينجح ({seconds} ث). أرسل هذا التقرير كما هو.")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(run(online="--online" in sys.argv))
