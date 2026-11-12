"""التحقق من الترخيص داخل التطبيق.

ضع هذين السطرين في بداية تطبيقك:

    from license_client import require_license
    require_license()

- أول مرة: يطلب من المستخدم رمز التفعيل ويتحقق منه من السيرفر.
- يحفظ الرمز على الجهاز، فلا يطلبه مرة ثانية.
- كل تشغيل يتأكد من السيرفر أن الرمز ما زال فعّالاً. لو أوقفته من اللوحة، يتوقف التطبيق.
- الرمز يرتبط بجهاز المستخدم، فلو أعطاه لغيره ما يشتغل عنده.

لا يحتاج أي مكتبة خارجية.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 👈 غيّر هذا لرابط سيرفر التراخيص على استضافتك (أو ضعه في متغير البيئة LICENSE_SERVER)
LICENSE_SERVER = os.getenv("LICENSE_SERVER", "https://your-domain.com")
APP_ID = "my-app"  # اسم مجلد حفظ الرمز على جهاز المستخدم
TIMEOUT = 15


def _config_file() -> Path:
    base = os.getenv("APPDATA") or os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_ID / "license.json"


def _load() -> dict:
    try:
        return json.loads(_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _config_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _machine_id() -> str | None:
    """معرّف ثابت للجهاز من نظام التشغيل."""
    system = platform.system()
    try:
        if system == "Windows":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
            return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        if system == "Darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            value = Path(path).read_text().strip()
            if value:
                return value
    except Exception:
        pass
    return None


def device_id(saved: dict) -> str:
    raw = _machine_id()
    if not raw:
        # احتياط: معرّف عشوائي يُحفظ مرة واحدة
        raw = saved.get("fallback_id") or uuid.uuid4().hex
        saved["fallback_id"] = raw
    return hashlib.sha256(f"{APP_ID}:{raw}".encode()).hexdigest()


def check_code(code: str, device: str) -> tuple[int, dict]:
    body = json.dumps(
        {
            "code": code,
            "device": device,
            "device_name": f"{platform.system()} · {platform.node()}"[:80],
        }
    ).encode()
    req = urllib.request.Request(
        LICENSE_SERVER.rstrip("/") + "/api/check",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:  # مثل 429 (محاولات كثيرة)
        try:
            return e.code, json.loads(e.read().decode())
        except ValueError:
            return e.code, {"ok": False, "message": f"خطأ من السيرفر ({e.code})."}


def require_license() -> str:
    """يرجع اسم المستخدم إذا الرمز صحيح، وإلا يقفل التطبيق."""
    saved = _load()
    device = device_id(saved)
    code = saved.get("code")

    for _attempt in range(3):
        if not code:
            try:
                code = input("أدخل رمز التفعيل: ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.exit("تم إيقاف التطبيق.")
            if not code:
                continue
        try:
            status, result = check_code(code, device)
        except (urllib.error.URLError, OSError, ValueError):
            sys.exit("❌ تعذر الاتصال بسيرفر التراخيص. تأكد من الإنترنت وحاول مرة أخرى.")

        if status == 200 and result.get("ok"):
            saved["code"] = code
            _save(saved)
            name = result.get("name", "")
            print(f"✅ أهلاً {name}")
            return name

        message = result.get("message") or "الرمز غير مقبول."
        if status != 200:  # مشكلة مؤقتة في السيرفر: لا نمسح الرمز المحفوظ
            sys.exit(f"❌ {message}")

        print("❌", message)
        saved.pop("code", None)
        _save(saved)
        code = None

    sys.exit("تم إيقاف التطبيق.")


if __name__ == "__main__":
    require_license()
