"""طبقة التعامل مع إكس عبر Playwright.

مبدأ العمل: الدخول يتم من جهاز المستخدم (يفتح متصفحاً ويسجّل دخوله بنفسه)،
ويُحفظ في ملف جلسة storage_state. هذا الملف يُرفع إلى السيرفر ويُستخدم هنا.
لا يوجد أي حقن لرمز جلسة (توكن) في خانة نصية.

يُنشئ ملف الجلسة على جهازك بالأمر:
    python bot.py login x https://x.com/login
ثم ارفع الملف الناتج sessions/x.json إلى نفس المسار على السيرفر.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
DEFAULT_TIMEOUT_MS = 20_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def session_path(name):
    return SESSIONS_DIR / f"{name}.json"


def session_exists(name):
    return session_path(name).exists()


class XSession:
    """يفتح سياق متصفح من ملف جلسة محفوظ. يُستخدم داخل جملة with."""

    def __init__(self, name="x", headless=True):
        self.name = name
        self.headless = headless
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        sess = session_path(self.name)
        if not sess.exists():
            raise FileNotFoundError(
                f"ملف الجلسة غير موجود: {sess}\n"
                f"سجّل الدخول على جهازك بـ: python bot.py login {self.name} https://x.com/login\n"
                f"ثم ارفع الملف إلى {sess}"
            )
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.context = self._browser.new_context(
            storage_state=str(sess),
            locale="ar",
            viewport={"width": 1360, "height": 900},
            user_agent=USER_AGENT,
        )
        self.context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        self.page = self.context.new_page()
        return self

    def __exit__(self, *exc):
        # نحفظ الجلسة المحدّثة حتى تبقى الكوكيز صالحة لأطول مدة
        try:
            if self.context:
                self.context.storage_state(path=str(session_path(self.name)))
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()


def verify_login(name="x", headless=True):
    """يفتح صفحة الرسائل ويتحقق أن الجلسة صالحة ومسجّلة الدخول.

    يرجع dict: {ok, url, handle}. handle هو اسم المستخدم إن أمكن قراءته.
    """
    with XSession(name, headless) as x:
        page = x.page
        page.goto("https://x.com/messages", wait_until="domcontentloaded")
        # صفحة الدخول تعيد التوجيه إلى /login أو /i/flow/login عند انتهاء الجلسة
        page.wait_for_timeout(4000)
        url = page.url
        logged_out = "/login" in url or "/i/flow/login" in url or "/account/access" in url
        handle = None
        if not logged_out:
            link = page.locator('[data-testid="AppTabBar_Profile_Link"]').first
            if link.count():
                href = link.get_attribute("href") or ""
                handle = href.strip("/") or None
        return {"ok": not logged_out, "url": url, "handle": handle}


def list_conversations(name="x", headless=True, max_scrolls=40):
    """يفتح صفحة الرسائل ويجمع القروبات/المحادثات مع التمرير (القائمة افتراضية).

    يرجع قائمة dicts: [{id, title, snippet}] حيث id مأخوذ من رابط المحادثة.
    """
    with XSession(name, headless) as x:
        page = x.page
        page.goto("https://x.com/messages", wait_until="domcontentloaded")
        page.wait_for_selector('[data-testid="conversation"]', timeout=DEFAULT_TIMEOUT_MS)

        found = {}
        stable_rounds = 0
        for _ in range(max_scrolls):
            cells = page.locator('[data-testid="conversation"]').all()
            before = len(found)
            for cell in cells:
                conv_id = None
                link = cell.locator('a[href*="/messages/"]').first
                if link.count():
                    href = link.get_attribute("href") or ""
                    conv_id = href.rsplit("/", 1)[-1] or href
                try:
                    text = cell.inner_text().strip()
                except Exception:
                    text = ""
                lines = [ln for ln in text.splitlines() if ln.strip()]
                title = lines[0] if lines else "(بدون عنوان)"
                snippet = lines[-1] if len(lines) > 1 else ""
                key = conv_id or title
                found[key] = {"id": conv_id, "title": title, "snippet": snippet}

            # لا جديد بعد التمرير مرتين متتاليتين => وصلنا النهاية
            stable_rounds = stable_rounds + 1 if len(found) == before else 0
            if stable_rounds >= 2:
                break
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(700)

        return list(found.values())


def open_conversation(conv_id, name="x", headless=True):
    """يفتح قروباً محدداً بمعرّفه ويرجع عنوانه وعدد الرسائل الظاهرة حالياً."""
    with XSession(name, headless) as x:
        page = x.page
        page.goto(f"https://x.com/messages/{conv_id}", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        title_el = page.locator('[data-testid="DMDrawerHeader"], header').first
        title = title_el.inner_text().splitlines()[0] if title_el.count() else conv_id
        visible = page.locator('[data-testid="messageEntry"]').count()
        return {"id": conv_id, "title": title.strip(), "visible_messages": visible, "url": page.url}
