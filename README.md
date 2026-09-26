# أداة أتمتة المتصفح (Playwright)

سكربت بلغة Python يفتح المتصفح وينفّذ مهامك تلقائياً: فتح صفحات، تعبئة نماذج، الضغط على أزرار، استخراج بيانات إلى CSV، تنزيل ملفات، وأخذ لقطات شاشة.
تُكتب كل مهمة في ملف JSON بسيط، **بدون الحاجة لكتابة كود**.

## التثبيت

```bash
pip install -r requirements.txt
playwright install chromium
```

## الاستخدام

### 1. تشغيل مهمة

```bash
python bot.py run tasks/example_scrape.json            # مع إظهار المتصفح
python bot.py run tasks/example_scrape.json --headless # بدون إظهار المتصفح
python bot.py run tasks/example_form.json --slow 500   # ببطء لمشاهدة كل خطوة
```

تُحفظ النتائج (ملفات CSV، لقطات الشاشة، التنزيلات) في مجلد `output/`.

### 2. المواقع التي تحتاج تسجيل دخول

سجّل دخولك **مرة واحدة يدوياً** وسيحفظ السكربت الجلسة:

```bash
python bot.py login work https://example.com/login
```

بعدها أضف `"session": "work"` في ملف المهمة، وسيفتح الموقع وأنت مسجّل دخول.
الجلسات تُحفظ في مجلد `sessions/` (وهو مستثنى من git لأنه يحتوي على بيانات دخولك، **لا تشاركه مع أحد**).

## كتابة مهمة جديدة

```json
{
  "name": "اسم المهمة",
  "session": "work",
  "data": "../data/customers.csv",
  "steps": [
    { "action": "goto", "url": "https://example.com" },
    { "action": "fill", "selector": "#name", "value": "{{الاسم}}" },
    { "action": "click", "selector": "button[type=submit]" }
  ]
}
```

| الحقل | الوصف |
|---|---|
| `name` | اسم المهمة (اختياري) |
| `session` | اسم الجلسة المحفوظة لاستخدامها (اختياري) |
| `data` | ملف CSV: تتكرر الخطوات لكل صف، ويمكن استخدام `{{اسم_العمود}}` في أي قيمة (اختياري) |
| `continue_on_error` | عند فشل صف، هل يكمل بالصف التالي؟ الافتراضي `true` |
| `timeout_ms` | مدة انتظار العناصر بالمللي ثانية، الافتراضي `15000` |

المتغيران `{{date}}` و`{{time}}` متاحان دائماً (مفيدان في أسماء الملفات).

### الخطوات المتاحة

| الخطوة | الحقول | الوظيفة |
|---|---|---|
| `goto` | `url` | فتح رابط |
| `click` | `selector` | الضغط على عنصر |
| `click_text` | `text` | الضغط على عنصر حسب النص المكتوب عليه |
| `fill` | `selector`, `value` | كتابة قيمة في حقل (تستبدل الموجود) |
| `type` | `selector`, `value`, `delay` | الكتابة حرفاً حرفاً مثل الإنسان |
| `select` | `selector`, `value` | اختيار من قائمة منسدلة |
| `check` | `selector`, `value` | تحديد مربع اختيار أو زر راديو (`value: false` لإلغاء التحديد) |
| `press` | `key`, `selector` | ضغط زر من لوحة المفاتيح مثل `Enter` |
| `upload` | `selector`, `file` | رفع ملف |
| `wait_for` | `selector`, `state` | انتظار ظهور عنصر |
| `wait` | `seconds` | انتظار عدد من الثواني |
| `screenshot` | `path`, `full_page` | لقطة شاشة |
| `download` | `selector`, `path` | الضغط على زر تنزيل وحفظ الملف |
| `extract` | `selector`, `fields`, `save`, `append` | استخراج بيانات من عناصر متكررة إلى CSV |
| `print` | `selector` | طباعة نص عنصر في الشاشة |
| `pause` | `message` | إيقاف مؤقت لتكمل شيئاً يدوياً (مثل رمز التحقق) ثم تضغط Enter |

#### صيغة `fields` في خطوة `extract`

- `".title"`: نص العنصر الداخلي `.title`
- `"a@href"`: قيمة الخاصية `href` من الرابط الداخلي
- `""`: نص العنصر كاملاً

### كيف أعرف الـ selector الصحيح؟

- كلك يمين على العنصر في المتصفح ← **فحص (Inspect)** ← انسخ `id` أو `name` أو الـ class.
- أو شغّل مسجّل Playwright ليكتب لك الخطوات أثناء تصفحك:

```bash
playwright codegen https://example.com
```

## عند حدوث خطأ

يأخذ السكربت لقطة شاشة للصفحة لحظة الخطأ ويحفظها في `output/error-*.png` حتى تعرف ما الذي حدث، ثم يكمل بالصف التالي من البيانات.

## أمثلة جاهزة

- `tasks/example_scrape.json`: استخراج اقتباسات من موقع تدريبي وحفظها في CSV.
- `tasks/example_form.json`: تعبئة نموذج تلقائياً لكل عميل في `data/customers.csv`.
- `tasks/example_login_report.json`: قالب لتنزيل تقرير يومي من موقع يحتاج تسجيل دخول.
