import json
import time

def execute_command(platform, action, target):
    with open("accounts.json", "r") as f:
        accounts = json.load(f)

    for acc in accounts:
        if acc["platform"] != platform:
            continue

        print(f"🔁 تنفيذ '{action}' من الحساب: {acc['username']} على {platform}")
        try:
            # هنا تضع كود Selenium حسب المنصة
            time.sleep(1)  # مؤقت فقط كمثال
            print(f"✅ تم تنفيذ {action} بنجاح على: {target}")
        except Exception as e:
            print(f"❌ فشل التنفيذ: {e}")