"""أداة أتمتة المتصفح باستخدام Playwright.

الاستخدام:
    python bot.py login <اسم_الجلسة> <رابط_صفحة_الدخول>
    python bot.py run <ملف_المهمة.json> [--headless] [--slow 300]

المهمة ملف JSON يحتوي على قائمة خطوات (steps) تُنفَّذ بالترتيب.
راجع README.md للاطلاع على جميع الخطوات المتاحة.
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_TIMEOUT_MS = 15_000


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fill_template(value, row):
    """يستبدل {{اسم_العمود}} بقيمته من صف البيانات، و{{date}} و{{time}} بالتاريخ والوقت."""
    if isinstance(value, dict):
        return {k: fill_template(v, row) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_template(v, row) for v in value]
    if not isinstance(value, str):
        return value

    now = datetime.now()
    extra = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H-%M-%S")}

    def repl(match):
        key = match.group(1).strip()
        if key in row:
            return str(row[key])
        if key in extra:
            return extra[key]
        raise KeyError(f"المتغير {{{{{key}}}}} غير موجود في البيانات")

    return re.sub(r"\{\{(.*?)\}\}", repl, value)


def output_path(name):
    path = Path(name)
    if not path.is_absolute():
        path = OUTPUT_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_field(element, spec):
    """spec: "" = نص العنصر نفسه، "css" = نص عنصر داخلي، "css@attr" أو "@attr" = قيمة خاصية."""
    selector, _, attr = spec.partition("@")
    target = element.locator(selector).first if selector else element
    if target.count() == 0:
        return ""
    if attr:
        return target.get_attribute(attr) or ""
    return target.inner_text().strip()


def step_extract(page, step):
    items = page.locator(step["selector"]).all()
    fields = step.get("fields", {"text": ""})
    rows = [{name: read_field(item, spec) for name, spec in fields.items()} for item in items]

    save = step.get("save")
    if save:
        path = output_path(save)
        append = step.get("append", False) and path.exists()
        with open(path, "a" if append else "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(fields))
            if not append:
                writer.writeheader()
            writer.writerows(rows)
        log(f"   📄 تم استخراج {len(rows)} صف وحفظها في {path}")
    else:
        for r in rows:
            log(f"   {r}")
    return rows


def run_step(page, step):
    action = step["action"]
    sel = step.get("selector")

    if action == "goto":
        page.goto(step["url"], wait_until=step.get("wait_until", "load"))
    elif action == "click":
        page.locator(sel).first.click()
    elif action == "click_text":
        page.get_by_text(step["text"], exact=step.get("exact", False)).first.click()
    elif action == "fill":
        page.locator(sel).first.fill(str(step["value"]))
    elif action == "type":
        page.locator(sel).first.press_sequentially(str(step["value"]), delay=step.get("delay", 50))
    elif action == "select":
        page.locator(sel).first.select_option(str(step["value"]))
    elif action == "check":
        page.locator(sel).first.set_checked(step.get("value", True))
    elif action == "press":
        if sel:
            page.locator(sel).first.press(step["key"])
        else:
            page.keyboard.press(step["key"])
    elif action == "upload":
        page.locator(sel).first.set_input_files(step["file"])
    elif action == "wait_for":
        page.locator(sel).first.wait_for(state=step.get("state", "visible"))
    elif action == "wait":
        time.sleep(float(step.get("seconds", 1)))
    elif action == "screenshot":
        path = output_path(step.get("path", "screenshot-{{date}}_{{time}}.png"))
        page.screenshot(path=str(path), full_page=step.get("full_page", True))
        log(f"   📸 لقطة شاشة: {path}")
    elif action == "download":
        with page.expect_download() as info:
            page.locator(sel).first.click()
        download = info.value
        path = output_path(step.get("path", download.suggested_filename))
        download.save_as(path)
        log(f"   ⬇️  تم التنزيل: {path}")
    elif action == "extract":
        step_extract(page, step)
    elif action == "print":
        log(f"   📝 {page.locator(sel).first.inner_text().strip()}")
    elif action == "pause":
        input(f"   ⏸️  {step.get('message', 'أكمل يدوياً في المتصفح ثم اضغط Enter للمتابعة...')} ")
    else:
        raise ValueError(f"خطوة غير معروفة: {action}")


def load_rows(task, task_file):
    data = task.get("data")
    if not data:
        return [{}]
    path = Path(data)
    if not path.is_absolute():
        path = task_file.parent / path
        if not path.exists():
            path = BASE_DIR / data
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def cmd_run(args):
    task_file = Path(args.task).resolve()
    task = json.loads(task_file.read_text(encoding="utf-8"))
    rows = load_rows(task, task_file)
    steps = task["steps"]
    log(f"🚀 بدء المهمة: {task.get('name', task_file.stem)} ({len(rows)} تكرار، {len(steps)} خطوة)")

    session_file = None
    if task.get("session"):
        session_file = SESSIONS_DIR / f"{task['session']}.json"
        if not session_file.exists():
            sys.exit(f"❌ الجلسة '{task['session']}' غير موجودة. سجّل الدخول أولاً:\n"
                     f"   python bot.py login {task['session']} <رابط_الدخول>")

    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slow)
        context = browser.new_context(
            storage_state=str(session_file) if session_file else None,
            accept_downloads=True,
            locale=task.get("locale", "ar-SA"),
        )
        context.set_default_timeout(task.get("timeout_ms", DEFAULT_TIMEOUT_MS))
        page = context.new_page()

        for i, row in enumerate(rows, 1):
            if len(rows) > 1:
                log(f"── التكرار {i}/{len(rows)}: {row}")
            try:
                for n, raw_step in enumerate(steps, 1):
                    step = fill_template(raw_step, row)
                    log(f"  {n}. {step['action']} {step.get('selector') or step.get('url') or ''}")
                    run_step(page, step)
            except (PlaywrightError, KeyError, ValueError, OSError) as e:
                failures += 1
                shot = output_path(f"error-{datetime.now():%Y%m%d-%H%M%S}.png")
                page.screenshot(path=str(shot), full_page=True)
                log(f"❌ فشلت الخطوة {n}: {str(e).splitlines()[0]}")
                log(f"   لقطة الخطأ محفوظة في {shot}")
                if not task.get("continue_on_error", True):
                    break

        # تحديث الجلسة (الكوكيز) بعد التنفيذ حتى تبقى صالحة لأطول مدة
        if session_file:
            context.storage_state(path=str(session_file))
        browser.close()

    if failures:
        log(f"⚠️ انتهت المهمة مع {failures} خطأ")
        sys.exit(1)
    log("✅ انتهت المهمة بنجاح")


def cmd_login(args):
    SESSIONS_DIR.mkdir(exist_ok=True)
    session_file = SESSIONS_DIR / f"{args.name}.json"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="ar-SA")
        page = context.new_page()
        page.goto(args.url)
        input("🔐 سجّل دخولك في نافذة المتصفح، ثم ارجع هنا واضغط Enter لحفظ الجلسة... ")
        context.storage_state(path=str(session_file))
        browser.close()
    log(f"✅ تم حفظ الجلسة في {session_file}")
    log(f'   أضف "session": "{args.name}" في ملف المهمة لاستخدامها.')


def main():
    parser = argparse.ArgumentParser(description="أتمتة المتصفح باستخدام Playwright")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="فتح المتصفح لتسجيل الدخول يدوياً وحفظ الجلسة")
    p_login.add_argument("name", help="اسم الجلسة، مثل: work")
    p_login.add_argument("url", help="رابط صفحة تسجيل الدخول")
    p_login.set_defaults(func=cmd_login)

    p_run = sub.add_parser("run", help="تشغيل مهمة من ملف JSON")
    p_run.add_argument("task", help="مسار ملف المهمة")
    p_run.add_argument("--headless", action="store_true", help="تشغيل بدون إظهار نافذة المتصفح")
    p_run.add_argument("--slow", type=int, default=0, help="تأخير بالمللي ثانية بين كل إجراء (لمشاهدة التنفيذ)")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
