# -*- coding: utf-8 -*-
"""
مِسنّ القصص — محرّك كتابة قصص قصيرة عربية أصيلة، سكربت من ملف واحد.

التشغيل:
    pip install flask
    python story_forge.py
ثم يُفتح المتصفح تلقائيًا على منفذ 7100.

اختياري:
    pip install waitress                 # خادم أمتن
    STORY_PORT=7100                      # منفذ آخر
    STORY_LIB=/path/stories.json         # مكان المكتبة
لاستخدام مزوّد خاص بدل المجاني، من داخل الصفحة أو عبر البيئة:
    OPENAI_BASE=https://generativelanguage.googleapis.com/v1beta/openai
    OPENAI_KEY=...   OPENAI_MODEL=gemini-2.5-flash
"""

import os
import re
import json
import time
import uuid
import random
import socket
import logging
import threading
import webbrowser
import urllib.error
import urllib.request

from flask import Flask, request, jsonify, Response, abort

VERSION = "2.2"
FREE_URL = "https://text.pollinations.ai/openai"
FREE_MODEL = os.environ.get("STORY_FREE_MODEL", "openai")
FREE_TOKEN = os.environ.get("POLLINATIONS_TOKEN", "")
OPENAI_BASE = os.environ.get("OPENAI_BASE", "")
OPENAI_KEY = os.environ.get("OPENAI_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
LIB_PATH = os.environ.get("STORY_LIB",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "stories.json"))

app = Flask(__name__)
log = logging.getLogger("story_forge")

JOBS = {}
JOBS_LOCK = threading.Lock()
LIB_LOCK = threading.Lock()


# ------------------------------------------------------------------ بذرة القصة
# القصة تُبنى من تركيبة محددة، لا من طلب عام، حتى لا ينزلق النموذج لأشهر ما حفظ.
WHO = [
    "صديق عمر", "شريك عمل", "أخي الأكبر", "أختي", "زوجتي", "حماي",
    "جار العمارة", "موظف يشتغل عندي", "مديري في الدوام", "ابن عمي",
    "زميل دراسة", "سائق البيت", "صاحب المحل اللي جنبي", "محاسب الشركة",
    "شريك سكن أيام الغربة", "معلّمي القديم", "ابن خالتي", "عامل في ورشتي",
    "أبوي", "أمي", "ولدي الكبير", "صاحب العمارة اللي كنت مستأجر فيها",
]

# النواة المخفية — مجموعتان: ثقيلة وخفيفة، يُختار منها حسب نوع الموقف
SECRET_DARK = [
    "دين دفعه عني بدون ما أدري",
    "تهمة تحمّلها بدالي وسكت",
    "وظيفة أخذتها أنا وكانت من نصيبه",
    "مرض كتمه سنين وهو يشتغل",
    "وصية تغيّرت قبل لا يموت الوالد",
    "شهادة قالها ضدي وهو يعرف الحقيقة",
    "مبلغ انسحب من الحساب بتوقيع مزوّر",
    "عقد بيع مكتوب باسم غير اللي ندفع له",
    "قرض باسمي ما أخذت منه ريال",
    "فصل من الشغل سببه شخص ما يُتوقع",
    "بيت بيع وما أحد قال لي",
    "اتفاق صار بين اثنين وأنا آخر من يعلم",
]

SECRET_LIGHT = [
    "معروف قديم ما أحد يعرف مين صاحبه",
    "رسالة كُتبت وما أُرسلت",
    "شخص يتابع أخبارنا من بعيد من سنين",
    "دعوة رُفضت لسبب كريم",
    "هدية انشرت وما انعطت",
    "زيارة تتكرر كل أسبوع بلا سبب معلن",
    "اسم مكتوب على شي قديم ونُسي",
    "شخص كان يدفع عن غيره وما يقول",
    "موقف قديم فُهم بالمقلوب وطلع غير كذا",
    "عادة يومية كانت لأجل شخص ثاني",
    "وعد صغير محد تذكره إلا واحد",
    "شخص حفظ سرًا محرجًا ولا فضحه",
]

DEVICE = [
    "ظرف لقيته وأنا أرتب أوراق متوفى",
    "كشف حساب بنكي طلع بالخطأ",
    "مقطع من كاميرا مراقبة",
    "رسالة صوتية قديمة في جوال مكسور تصلّح",
    "ملف طبي في عيادة",
    "صك ملكية عند كاتب العدل",
    "صورة معلّقة في مكتب",
    "اعتراف على فراش الموت",
    "طرد وصل لعنواني بالغلط",
    "سجل زيارات في سجن أو مستشفى",
    "شيك قديم بين أوراق",
    "دفتر مواعيد فيه اسم متكرر",
    "كلام طلع من طفل بدون قصد",
    "فاتورة باسم شخص ثاني",
]

PLACE = [
    "على باب مطعم وأنا طالع", "في صالة عرس", "بممر مستشفى",
    "بمكتب محامي", "بصالة مطار", "باجتماع شغل", "بالمقبرة بعد العزاء",
    "بالبنك وأنا أراجع حسابي", "بمدرسة عيالي", "بورشة تصليح",
    "بمجلس عزاء", "بمحل الذهب", "بموقف السيارات تحت البيت",
]

COST_DARK = [
    "البنك يقص نص راتبه", "باع سيارته", "أجّل زواجه أربع سنين",
    "ترك دراسته", "اشتغل وظيفتين خمس سنين", "انقطع عن أهله",
    "أجّل عملية والدته", "طلع من بيته واستأجر غرفة",
    "تسجّل باسمه دين مو دينه", "ترك شغل يحبه",
]

COST_LIGHT = [
    "صبر سنتين وهو ساكت", "راح ورجع كل أسبوع بلا ما يقول",
    "دفع من جيبه وما ذكرها", "تنازل عن دوره وسكت",
    "غيّر طريقه كل يوم عشان يمر من مكان",
    "احتفظ بشي صغير عشر سنين", "تحمّل سوء ظن طويل بلا دفاع",
]

DILEMMA = [
    "يفضحه قدام الكل ولا يسكت ويأخذ حقه بهدوء",
    "يصارحه ويكسر كبرياءه ولا يخليه يعيش على ظنه",
    "يطالب بحقه ولا يتركه ويمشي",
    "يخبر اللي يستاهلون يعرفون ولا يدفن السر",
    "يسامح ويرجع ولا يقفل الباب للأبد",
    "يأخذ المال المعروض ولا يرده بوجهه",
    "يكمل بالشي اللي بناه على كذبة ولا يهدّه بيده",
    "يقول لأمه ولا يتركها تموت وهي مرتاحة",
]

CLOSERS = [
    "يعرف أخيرًا مين كان واقف معه ويقرر وش يسوي",
    "يرجع للشخص بعد سنين ويقول له كلمة واحدة",
    "يكتشف أن اللي ظنه إهمالًا كان خوفًا عليه",
    "يشوف الشي الصغير بعين ثانية بعد ما عرف قصته",
    "يقرر يرد المعروف بطريقته بدون ما يذكر السبب",
    "يسكت ويحتفظ بالشي عنده ويخلي الثاني على راحته",
]

# حبكات صارت مستهلكة على الإنترنت — ممنوعة نصًا
TIRED_PLOTS = [
    "تبديل الأطفال في المستشفى وتربية طفل ليس ابنك",
    "الأخ الذي باع إرثه سرًا ليموّل دراسة أخيه بمنحة وهمية",
    "الصديق الذي هرب بدين وظهر بعد سنوات صاحب مطعم أو ثريًا",
    "الزوجة التي تبرعت بكليتها سرًا لزوجها",
    "السائق أو الحارس الذي تبيّن أنه مليونير",
    "الخادمة التي أنقذت الطفل ثم طُردت ظلمًا",
    "الأب الذي عمل حمّالًا سرًا ليعلّم ابنه",
    "المتسول الذي تبيّن أنه والد الرجل الغني",
    "العريس الذي اكتشف ليلة الزواج سرًا عن العروس",
    "الجار المزعج الذي تبيّن أنه يحرس البيت",
    "المعلم الذي دفع رسوم الطالب الفقير سرًا",
    "الرجل الذي تبرع بدمه فأنقذ من ظلمه",
]

DIALECTS = {
    "saudi": "سعودية بيضاء مفهومة لكل الخليج",
    "gulf": "خليجية عامة",
    "egy": "مصرية",
    "sham": "شامية",
    "fusha": "فصحى مبسّطة قريبة من المحكي",
}

FORMATS = {
    "short": ("منشور قصير", 70, 100),
    "medium": ("منشور متوسط", 120, 165),
    "long": ("منشور طويل", 200, 260),
    "thread": ("خيط مرقّم", 150, 200),
}

# نواة الموقف: كل نوع له وصفه، ونوع نهايته، ومجموعة البذرة التي تناسبه
CORES = {
    # ثقيلة
    "betrayal":  ("خيانة ثقة", "شخص قريب أخذ شيئًا وسكت", "dark", "dilemma"),
    "injustice": ("ظلم قديم", "اتُّهم أو حُرم وهو بريء", "dark", "dilemma"),
    "guilt":     ("ذنب بلا قصد", "يكتشف أنه هو من ظلم دون أن يدري", "dark", "dilemma"),
    "loss":      ("فقد متأخر", "يفهم قيمة شخص بعد ما راح", "dark", "closer"),
    "sacrifice": ("تضحية مكتومة", "أحدهم دفع الثمن بصمت ولم يُعرف إلا متأخرًا", "light", "closer"),
    "money":     ("مال بين الأقارب", "مبلغ أو ملكية قلبت العلاقة", "dark", "dilemma"),
    "secretill": ("مرض مكتوم", "شخص أخفى مرضه حتى لا يثقل أحدًا", "dark", "closer"),
    "pride":     ("كبرياء وكرامة", "شخص يرفض المساعدة ويدفع ثمن رفضه", "dark", "dilemma"),
    # متوسطة
    "misread":   ("سوء فهم طويل", "موقف فُهم بالمقلوب سنين ثم انكشف", "light", "closer"),
    "return":    ("معروف يُرد", "خدمة قديمة ترجع لصاحبها من حيث لا يحتسب", "light", "closer"),
    "gratitude": ("امتنان متأخر", "يعرف أخيرًا من كان يقف خلفه", "light", "closer"),
    "loyalty":   ("وفاء غير متوقع", "الشخص الأبعد هو الذي بقي", "light", "closer"),
    "reversal":  ("انقلاب رأي", "يغيّر نظرته لشخص حكم عليه سنين", "light", "closer"),
    # خفيفة
    "surprise":  ("مفاجأة سارّة", "شيء طيّب ينكشف بالصدفة", "light", "closer"),
    "chance":    ("صدفة غريبة", "تفصيل صغير يربط شخصين بلا تخطيط", "light", "closer"),
    "funny":     ("موقف محرج بخفّة", "سوء فهم يومي ينتهي بضحكة وعِبرة صغيرة", "light", "closer"),
    "nostalgia": ("حنين لشيء قديم", "شيء من الماضي يرجع فيفتح بابًا", "light", "closer"),
    "parenting": ("بين أب وابنه", "لحظة يكتشف فيها أحدهما الآخر", "light", "closer"),
    "work":      ("في الشغل", "موقف مع مدير أو زميل يقلب الصورة", "any", "dilemma"),
    "neighbors": ("بين الجيران", "حياة كاملة خلف باب مقابل", "light", "closer"),
}

DRAMA = {
    "small": ("عادي جدًا",
              "حدث صغير من الحياة اليومية. مبالغ بالمئات أو الآلاف القليلة، "
              "ولا مصائر تنقلب، ولا أحد يموت. الأثر نفسي لا مادي ضخم"),
    "mid":   ("متوسط",
              "حدث مؤثر لكنه ممكن يصير لأي أحد. مبالغ بعشرات الآلاف على الأكثر، "
              "ولا أحداث استثنائية"),
    "big":   ("قوي",
              "حدث قوي لكنه يبقى ضمن المعقول تمامًا: شيء تقرأ عنه وتقول "
              "«هذا صار لواحد أعرفه»، لا شيء يشبه الأفلام"),
}

POVS = {
    "self":  ("شخصي", "بضمير المتكلم، الراوي هو صاحب القصة، كأنه يحكي ما عاشه"),
    "third": ("عام", "بضمير الغائب عن شخص آخر. ممنوع منعًا تامًا استعمال «أنا» أو "
                     "«كنت» أو أي ضمير متكلم. سمّه «رجل» أو «موظف» أو «أب» ولا تعطه اسمًا"),
    "heard": ("منقول", "الراوي ينقل قصة سمعها من غيره: يبدأ بأنه سمعها، ثم يحكيها "
                       "عن صاحبها بضمير الغائب"),
}

BANNED = [
    "لن تصدق", "وهنا الصدمة", "قصة حقيقية", "شاركني رأيك", "ما رأيك أنت",
    "الدرس المستفاد", "الحياة علّمتني", "ومن يومها تعلّمت", "أيها القارئ",
    "سبحان مغيّر الأحوال", "دمعت عيناي", "شعرت بقشعريرة", "تخيّل معي",
    "في تلك اللحظة أدركت", "غيّرت حياتي", "يتبع", "انتظروا الجزء",
]


def fresh_dna(topic="", core="betrayal"):
    label, desc, pool, ending = CORES.get(core, CORES["betrayal"])
    if pool == "any":
        pool = random.choice(("dark", "light"))
    dna = {
        "who": random.choice(WHO),
        "secret": random.choice(SECRET_DARK if pool == "dark" else SECRET_LIGHT),
        "device": random.choice(DEVICE),
        "place": random.choice(PLACE),
        "cost": random.choice(COST_DARK if pool == "dark" else COST_LIGHT),
        "dilemma": random.choice(DILEMMA if ending == "dilemma" else CLOSERS),
    }
    if topic:
        dna["topic"] = topic.strip()[:300]
    return dna


def dna_text(dna):
    lines = [
        f"- الطرف الآخر: {dna['who']}",
        f"- نوع السر: {dna['secret']}",
        f"- أداة الكشف: {dna['device']}",
        f"- مكان لحظة الكشف: {dna['place']}",
        f"- الأثر الذي تركه ذلك: {dna['cost']}",
        f"- ما تقف عنده النهاية: {dna['dilemma']}",
    ]
    if dna.get("topic"):
        lines.insert(0, f"- الموضوع اللي طلبه الكاتب: {dna['topic']}")
    return "\n".join(lines)


# ------------------------------------------------------------------- المكتبة
def lib_read():
    try:
        with open(LIB_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def lib_write(items):
    with LIB_LOCK:
        tmp = LIB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items[-200:], fh, ensure_ascii=False, indent=1)
        os.replace(tmp, LIB_PATH)


def recent_plots(limit=25):
    out = []
    for item in reversed(lib_read()):
        plot = (item.get("plot") or "").strip()
        if plot:
            out.append(plot[:160])
        if len(out) >= limit:
            break
    return out


def recent_openings(limit=14):
    out = []
    for item in reversed(lib_read()):
        head = (item.get("text") or "").strip().split("\n")[0][:90]
        if head:
            out.append(head)
        if len(out) >= limit:
            break
    return out


def opening_clash(text, previous):
    """هل تبدأ القصة الجديدة كسابقاتها؟"""
    first = set(re.findall(r"[\w؀-ۿ]+", text.split("\n")[0].lower())[:9])
    if len(first) < 4:
        return False
    for old in previous:
        prev = set(re.findall(r"[\w؀-ۿ]+", old.lower())[:9])
        if prev and len(first & prev) / len(first) > 0.55:
            return True
    return False


# -------------------------------------------------------------- نداء المزوّد
def chat(messages, provider, creds, on_token=None, temperature=0.95, timeout=240):
    """نداء دردشة متوافق مع OpenAI، مع بثّ اختياري حرفًا حرفًا."""
    if provider == "openai":
        base = (creds.get("base") or OPENAI_BASE).rstrip("/")
        key = creds.get("key") or OPENAI_KEY
        model = creds.get("model") or OPENAI_MODEL
        if not base or not key:
            raise RuntimeError("أدخل عنوان المزوّد ومفتاحه، أو اختر المزوّد المجاني.")
        url = base + "/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    else:
        url, model = FREE_URL, FREE_MODEL
        headers = {"Content-Type": "application/json"}
        if FREE_TOKEN:
            headers["Authorization"] = "Bearer " + FREE_TOKEN

    body = {"model": model, "messages": messages, "temperature": temperature,
            "stream": bool(on_token)}
    data = json.dumps(body).encode("utf-8")

    backoff = [3, 9, 20]
    for attempt in range(len(backoff) + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                if not on_token:
                    payload = json.loads(res.read().decode("utf-8"))
                    return payload["choices"][0]["message"]["content"]
                return _read_stream(res, on_token)

        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < len(backoff):
                if on_token:
                    on_token(None, f"الخدمة مشغولة، إعادة المحاولة بعد {backoff[attempt]} ثانية")
                time.sleep(backoff[attempt])
                continue
            if e.code in (401, 403):
                raise RuntimeError("المفتاح مرفوض: " + detail)
            raise RuntimeError(f"فشل الطلب ({e.code}): {detail}")

        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < len(backoff):
                time.sleep(backoff[attempt])
                continue
            raise RuntimeError(f"تعذّر الاتصال: {e}")

    raise RuntimeError("تعذّر إتمام الطلب.")


def _read_stream(res, on_token):
    out = []
    for raw in res:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk in ("[DONE]", ""):
            continue
        try:
            delta = json.loads(chunk)["choices"][0].get("delta", {}).get("content")
        except (ValueError, KeyError, IndexError):
            continue
        if delta:
            out.append(delta)
            on_token(delta, None)
    text = "".join(out)
    if not text.strip():
        raise RuntimeError("وصل ردّ فارغ من المزوّد.")
    return text


# ----------------------------------------------------------------- الكتابة
CRAFT = (
    "أنت كاتب منشورات قصصية عربية تُقرأ إلى آخر سطر. القصة مؤلّفة بالكامل "
    "لكنها تبدو كأن صاحبها عاشها وكتبها على جواله.\n"
    "طريقتك في الكتابة، التزم بها حرفيًا:\n"
    "- كل جملة في سطر مستقل، وبين السطور سطر فارغ. لا فقرات طويلة.\n"
    "- جُمل قصيرة ومباشرة. لا وصف أدبي ولا استعارات ولا سجع.\n"
    "- أرقام محددة: كم سنة، كم مبلغ، كم نسبة. الأرقام هي ما يصدّق القارئ.\n"
    "- لا تسمِّ المشاعر. بدل «حزنت» اكتب الفعل أو التفصيل اللي يدل عليها.\n"
    "- لا رموز تعبيرية، لا عناوين، لا وسوم، لا أقواس شارحة، لا تمهيد قبل القصة."
)

REALISM = (
    "شروط الواقعية، وهي الأهم، وأي إخلال بها يفسد المنشور:\n"
    "- الكشف يجي من شيء ملموس يمكن أن يوجد فعلًا: ورقة، رقم، فاتورة، رسالة، "
    "كلام عابر من طفل أو موظف. ممنوع الاعتراف على فراش الموت، وممنوع اللقاء "
    "بالصدفة في اللحظة المناسبة، وممنوع أن يكون الشخص الغامض شخصية معروفة.\n"
    "- لازم يكون واضحًا من النص لماذا بقي الأمر مخفيًا كل هذي المدة، "
    "ولماذا ظهر الآن بالذات. بلا هذين السببين القصة تبدو ملفّقة.\n"
    "- الأرقام متسقة: إذا قلت ست سنين فلا تقل بعدها عشر. راجع كل رقم مع ما قبله.\n"
    "- المبالغ من الواقع، لا أرقام خيالية.\n"
    "- صاحب القصة لا يعرف كل شيء: لازم سطر واحد على الأقل فيه شك أو جهل، "
    "مثل «ما أدري ليش» أو «يمكن».\n"
    "- لازم تفصيل واحد على الأقل لا وظيفة له في الحبكة، لأن الحياة فيها تفاصيل "
    "زائدة والقصة المحبوكة بإحكام كامل تنكشف أنها مؤلفة.\n"
    "- صاحب القصة ليس ملاكًا: لازم يظهر منه تقصير أو تصرّف ناقص.\n"
    "- لا تُغلق كل الأسئلة في النهاية. اترك شيئًا معلّقًا كما في الواقع."
)


def beats(pov, ending):
    closing = ("آخر سطر أو سطرين: معضلة بخيارين واضحين يواجهها صاحب القصة الآن، "
               "كحيرة شخصية لا كسؤال للقارئ ولا كدعوة للتعليق."
               if ending == "dilemma" else
               "آخر سطر أو سطرين: لحظة تقف عندها القصة، قرار صغير أو صورة تغيّر "
               "معنى كل ما سبق. بلا خلاصة ولا عبرة مكتوبة ولا سؤال للقارئ.")
    return (
        f"منظور السرد: {POVS.get(pov, POVS['self'])[1]}.\n"
        "البناء الإلزامي، بهذا الترتيب:\n"
        "1. سطر أول: موقف عادي جدًا من يوم عادي، من ٦ إلى ١٢ كلمة، بلا أي تشويق "
        "مصنوع وبلا سؤال. لا يُفهم منه أن شيئًا سيحدث.\n"
        "2. سطر قصير جدًا لردّة فعل جسدية توقف المشهد (٣ إلى ٧ كلمات).\n"
        "3. ما الذي رآه أو سمعه: تفصيل واحد محسوس، بلا تفسير.\n"
        "4. سطران خلفية: من هذا الشخص، وما الذي جرى قبل سنوات، بأرقام محددة، "
        "وما الأثر الذي تركه.\n"
        "5. الكشف على دفعتين: سطر تمهيد ينتهي بنقاط حذف، ثم الجملة المفصلية في "
        "سطر مستقل. الكشف يقلب الصورة التي بناها القارئ.\n"
        "6. تفصيل صغير من الحاضر يترك أثرًا: شيء مادي أو حركة، بلا شرح.\n"
        f"7. {closing}"
    )


def premise_prompt(dna, core, drama, avoid_plots):
    label, desc, _, _ = CORES.get(core, CORES["betrayal"])
    dlabel, ddesc = DRAMA.get(drama, DRAMA["mid"])
    return "\n".join([
        "اقترح خمس أفكار مختلفة لحبكة منشور قصصي قصير.",
        f"نوع الموقف المطلوب: {label} — {desc}.",
        f"حجم الحدث: {dlabel} — {ddesc}.",
        "",
        "ابنِ كل فكرة على هذه العناصر:",
        dna_text(dna),
        "",
        "كل فكرة في جملتين: الأولى ما المخفي وكيف انكشف، والثانية تجيب على "
        "سؤالين بالتحديد: لماذا بقي مخفيًا كل هذه المدة، ولماذا ظهر الآن.",
        "أي فكرة لا تجيب على السؤالين تُرفض. وأي فكرة تعتمد على مصادفة كبيرة "
        "أو اعتراف على فراش الموت أو شخصية مشهورة تُرفض أيضًا.",
        "",
        "ممنوع منعًا تامًا أي فكرة تشبه هذه الحبكات المستهلكة أو تكون نسخة منها "
        "بتغيير الأسماء:",
        "\n".join("• " + p for p in TIRED_PLOTS),
        *(["", "وممنوع كذلك أي فكرة تشبه ما كُتب سابقًا:",
           "\n".join("• " + p for p in avoid_plots)] if avoid_plots else []),
        "",
        'أعد JSON فقط: {"ideas":["...","...","...","...","..."]}',
    ])


def write_prompt(dna, premise, fmt, dialect, core, pov, drama):
    label, low, high = FORMATS.get(fmt, FORMATS["medium"])
    core_label, core_desc, _, ending = CORES.get(core, CORES["betrayal"])
    dlabel, ddesc = DRAMA.get(drama, DRAMA["mid"])
    shape = ("رقّم المقاطع ١، ٢، ٣ كخيط منشورات، وكل مقطع مقطع قائم بذاته."
             if fmt == "thread" else "اكتبه منشورًا واحدًا متصلًا.")
    return "\n".join([
        f"اكتب {label} بلهجة {DIALECTS.get(dialect, DIALECTS['saudi'])}، "
        f"من {low} إلى {high} كلمة.",
        shape,
        "",
        "الحبكة التي ستكتبها:",
        premise,
        "",
        "العناصر التي يجب أن تظهر داخل القصة بلا ذكرها كقائمة:",
        dna_text(dna),
        f"- نوع الموقف: {core_label} — {core_desc}",
        f"- حجم الحدث: {dlabel} — {ddesc}",
        "",
        REALISM,
        "",
        beats(pov, ending),
        "",
        "ممنوع استعمال هذه العبارات أو ما يشبهها:",
        "، ".join(BANNED),
        "",
        "اكتب النص وحده.",
    ])


EDIT_PROMPT = (
    "أنت محرّر منشورات. أمامك مسودة. أعد كتابتها أقوى بنفس الحكاية ونفس اللهجة.\n"
    "افعل هذا بالترتيب:\n"
    "1. السطر الأول: اجعله موقفًا عاديًا قصيرًا، بلا تشويق مصنوع وبلا سؤال. "
    "إن كان فيه أي تلميح لما سيحدث، اكسره.\n"
    "2. احذف كل سطر لا يضيف واقعة أو رقمًا أو تفصيلًا محسوسًا.\n"
    "3. استبدل كل جملة تسمّي شعورًا بفعل أو تفصيل مادي.\n"
    "4. تأكد أن الكشف جاء في النصف الثاني لا في البداية، وأنه على دفعتين: "
    "تمهيد ثم جملة قاصمة في سطر مستقل.\n"
    "5. تأكد من وجود رقمين محددين على الأقل في النص.\n"
    "6. آخر سطرين: معضلة بخيارين واضحين، بلا سؤال موجّه للقارئ وبلا دعوة تعليق.\n"
    "7. كل جملة في سطر مستقل وبينها سطر فارغ.\n"
    "8. احذف أي عبارة جاهزة أو مألوفة واستبدلها بتفصيل محدد.\n"
    "9. تأكد أن النص يوضّح لماذا بقي الأمر مخفيًا ولماذا ظهر الآن.\n"
    "10. أبقِ سطرًا فيه شك أو جهل من صاحب القصة، وتفصيلًا واحدًا زائدًا "
    "لا علاقة له بالحبكة.\n"
    "أعد النص النهائي وحده، بلا أي تعليق."
)


AUDIT_PROMPT = (
    "أنت مدقّق. أمامك منشور قصصي يُفترض أنه واقعي. مهمتك أن تجد ما يجعل القارئ "
    "يقول «هذي ما تصير».\n"
    "افحص بالترتيب:\n"
    "1. الأرقام والمُدد: هل تتناقض؟ (سنوات، مبالغ، أعمار، تواريخ).\n"
    "2. المنطق: هل يوجد تصرّف لا يفعله عاقل في هذا الموقف؟\n"
    "3. الإخفاء: هل يُفهم لماذا بقي الأمر مخفيًا، ولماذا انكشف الآن؟\n"
    "4. المصادفة: هل تعتمد القصة على صدفة كبيرة يصعب تصديقها؟\n"
    "5. المبالغة: هل فيها حدث أقرب للأفلام منه للحياة؟\n"
    "ثم أصلح كل خلل وجدته بأقل تغيير ممكن، مع الحفاظ على اللهجة والبناء "
    "وتقسيم السطور كما هي، ودون إضافة عبارات جاهزة.\n"
    'أعد JSON فقط بهذا الشكل: {"issues":["وصف مختصر لكل خلل وجدته"],'
    '"text":"النص بعد الإصلاح كاملًا"}\n'
    "إن لم تجد أي خلل، أعد القائمة فارغة والنص كما هو."
)


def numbers_in(text):
    return re.findall(r"[0-9٠-٩]+", text)


def word_count(text):
    return len(re.findall(r"[\w؀-ۿ]+", text))


def clean(text):
    text = re.sub(r"^```.*?$|^```$", "", text.strip(), flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def words_of(text):
    return set(re.findall(r"[\w؀-ۿ]{3,}", text.lower()))


def overlap(a, b):
    wa, wb = words_of(a), words_of(b)
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


def pick_premise(ideas, blocked):
    """يختار الفكرة الأبعد عن الحبكات المستهلكة وعن قصصك السابقة."""
    best, best_score = None, 2.0
    for idea in ideas:
        score = max([overlap(idea, b) for b in blocked] or [0])
        if score < best_score:
            best, best_score = idea, score
    return best, best_score


def json_list(raw, field):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise RuntimeError("ردّ غير مفهوم من المزوّد.")
        data = json.loads(m.group())
    items = [str(x).strip() for x in data.get(field, []) if str(x).strip()]
    if not items:
        raise RuntimeError("ردّ فارغ من المزوّد.")
    return items


def write_story(job, dna, fmt, dialect, core, pov, drama, provider, creds):
    seen_plots = recent_plots()
    blocked = TIRED_PLOTS + seen_plots
    ending = CORES.get(core, CORES["betrayal"])[3]

    job["stage"] = "premise"
    ideas = json_list(chat(
        [{"role": "system", "content": CRAFT},
         {"role": "user", "content": premise_prompt(dna, core, drama, seen_plots)}],
        provider, creds, temperature=1.05), "ideas")
    premise, score = pick_premise(ideas, blocked)
    job["premise"] = premise
    job["novelty"] = round(1 - score, 2)

    job["stage"] = "draft"
    draft = clean(chat(
        [{"role": "system", "content": CRAFT},
         {"role": "user", "content": write_prompt(dna, premise, fmt, dialect,
                                                   core, pov, drama)}],
        provider, creds))

    job["stage"] = "edit"
    job["text"] = ""

    def token(piece, notice):
        if notice:
            job["notice"] = notice
        elif piece:
            job.pop("notice", None)
            job["text"] += piece

    guard = (f"\nمنظور السرد يجب أن يبقى: {POVS.get(pov, POVS['self'])[1]}.\n"
             + ("النهاية معضلة بخيارين." if ending == "dilemma"
                else "النهاية لحظة أو قرار صغير، لا معضلة ولا عبرة."))
    final = clean(chat(
        [{"role": "system", "content": CRAFT},
         {"role": "user", "content": EDIT_PROMPT + guard + "\n\nالمسودة:\n" + draft}],
        provider, creds, on_token=token, temperature=0.75))

    job["stage"] = "audit"
    try:
        checked = json.loads(re.sub(r"^```(?:json)?|```$", "",
                                    chat([{"role": "system", "content": CRAFT},
                                          {"role": "user",
                                           "content": AUDIT_PROMPT + "\n\nالنص:\n"
                                           + final + "\n\nالأرقام الواردة فيه: "
                                           + "، ".join(numbers_in(final))}],
                                         provider, creds, temperature=0.3).strip(),
                                    flags=re.M).strip())
        fixed = clean(str(checked.get("text") or ""))
        issues = [str(x) for x in checked.get("issues", [])][:6]
        if word_count(fixed) >= word_count(final) * 0.6:
            final, job["issues"] = fixed, issues
        else:                                   # ردّ مبتور: نُبقي النص الأصلي
            job["issues"] = issues
    except Exception as exc:
        log.warning("تعذّر التدقيق: %s", exc)
        job["issues"] = []

    job["text"] = final
    job["words"] = word_count(final)
    job["cliches"] = [b for b in BANNED if b in final]
    job["numbers"] = len(re.findall(r"[0-9٠-٩]+|\b(?:سنة|سنين|سنوات|شهر|ألف|مليون|نص|ربع)\b", final))
    if pov != "self":
        leaks = len(re.findall(r"(?:^|\s)(?:أنا|كنت|لي|عندي|أنّي|إني)(?=\s|$|[،.])", final))
        job["leak"] = leaks if leaks > 1 else 0
    job["plot"] = premise
    job["stage"] = "done"


def _worker(job_id, dna, fmt, dialect, core, pov, drama, provider, creds):
    job = JOBS[job_id]
    try:
        write_story(job, dna, fmt, dialect, core, pov, drama, provider, creds)
    except Exception as exc:
        log.exception("write failed")
        job["stage"] = "error"
        job["error"] = str(exc)


# ------------------------------------------------------------------ المسارات
@app.route("/")
def index():
    html = PAGE.replace("__VER__", VERSION)
    resp = Response(html, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/config")
def config():
    groups = [
        ("مواقف ثقيلة", ["betrayal", "injustice", "guilt", "money", "pride",
                          "secretill", "loss"]),
        ("وفاء وامتنان", ["sacrifice", "gratitude", "loyalty", "return", "misread",
                           "reversal"]),
        ("مواقف يومية", ["surprise", "chance", "funny", "nostalgia", "parenting",
                          "work", "neighbors"]),
    ]
    cores = [{"group": g, "items": [{"id": k, "label": CORES[k][0],
                                     "desc": CORES[k][1]} for k in keys]}
             for g, keys in groups]
    return jsonify(version=VERSION, openai_ready=bool(OPENAI_BASE and OPENAI_KEY),
                   model=OPENAI_MODEL, saved=len(lib_read()), cores=cores,
                   povs=[{"id": k, "label": v[0]} for k, v in POVS.items()])


@app.route("/write", methods=["POST"])
def write():
    data = request.get_json(silent=True) or {}
    fmt = data.get("format", "medium")
    dialect = data.get("dialect", "saudi")
    core = data.get("core", "betrayal")
    core = core if core in CORES else "betrayal"
    pov = data.get("pov", "self")
    pov = pov if pov in POVS else "self"
    drama = data.get("drama", "mid")
    drama = drama if drama in DRAMA else "mid"
    provider = "openai" if data.get("provider") == "openai" else "free"
    creds = {"base": (data.get("base") or "").strip(),
             "key": (data.get("key") or "").strip(),
             "model": (data.get("model") or "").strip()}
    if provider == "openai" and not ((creds["base"] or OPENAI_BASE) and (creds["key"] or OPENAI_KEY)):
        return jsonify(error="أدخل عنوان المزوّد ومفتاحه، أو اختر المحرّك المجاني."), 400

    dna = fresh_dna(data.get("topic", ""), core)

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        for old, j in list(JOBS.items()):
            if time.time() - j.get("at", 0) > 3600:
                JOBS.pop(old, None)
        JOBS[job_id] = {"stage": "seed", "text": "", "error": "", "dna": dna,
                        "words": 0, "cliches": [], "premise": "", "issues": [],
                        "at": time.time()}

    threading.Thread(target=_worker,
                     args=(job_id, dna, fmt, dialect, core, pov, drama,
                           provider, creds),
                     daemon=True).start()
    return jsonify(job=job_id, dna=dna)


@app.route("/write/<job_id>")
def write_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="المهمة غير موجودة."), 404
    return jsonify({k: v for k, v in job.items() if k != "at"})


@app.route("/hooks", methods=["POST"])
def hooks():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if len(text) < 40:
        return jsonify(error="لا توجد قصة بعد."), 400
    provider = "openai" if data.get("provider") == "openai" else "free"
    creds = {"base": (data.get("base") or "").strip(),
             "key": (data.get("key") or "").strip(),
             "model": (data.get("model") or "").strip()}

    ask = ("أمامك قصة. اكتب أربع بدائل للسطر الأول فقط، كل بديل واقعة محسوسة "
           "لا تتجاوز اثنتي عشرة كلمة، متسقة مع بقية القصة، ومختلفة عن بعضها "
           "في زاوية الدخول. ممنوع الأسئلة والتمهيد. "
           'أعد JSON فقط بالشكل: {"hooks":["...","...","...","..."]}\n\n' + text[:4000])
    try:
        raw = chat([{"role": "system", "content": CRAFT},
                    {"role": "user", "content": ask}], provider, creds, temperature=1.0)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            items = json.loads(raw)["hooks"]
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            items = json.loads(m.group())["hooks"] if m else []
        items = [str(x).strip() for x in items if str(x).strip()][:4]
        if not items:
            raise RuntimeError("لم تصل بدائل صالحة.")
        return jsonify(hooks=items)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.route("/library", methods=["GET", "POST"])
def library():
    if request.method == "GET":
        return jsonify(items=list(reversed(lib_read()))[:60])

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if len(text) < 40:
        return jsonify(error="لا يوجد نص لحفظه."), 400
    items = lib_read()
    items.append({"id": uuid.uuid4().hex[:12], "text": text,
                  "dna": data.get("dna") or {}, "plot": data.get("plot") or "",
                  "at": time.strftime("%Y-%m-%d %H:%M")})
    lib_write(items)
    return jsonify(ok=True, count=len(items))


@app.route("/library/<item_id>", methods=["DELETE"])
def library_delete(item_id):
    items = [i for i in lib_read() if i.get("id") != item_id]
    lib_write(items)
    return jsonify(ok=True, count=len(items))


# --------------------------------------------------------------- الواجهة
PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>مِسنّ — منشورات قصصية</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Amiri:ital,wght@0,400;0,700;1,400&family=IBM+Plex+Sans+Arabic:wght@300;400;600&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#141C31; --panel:#1C2745; --rule:#2C3A61; --dim:#8C9BC4;
    --paper:#F6F5F3; --graphite:#1A1F2B; --rose:#D4577C; --sage:#7FC7C2;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    background:var(--ink); color:#E6EAF5; padding:0 20px 90px;
    font-family:"IBM Plex Sans Arabic",system-ui,sans-serif; font-weight:300;
    line-height:1.75; -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:720px; margin:0 auto}

  header{padding:54px 0 30px}
  .mark{font-size:13px; color:var(--dim); letter-spacing:.04em}
  .hero{
    font-family:"Amiri",serif; font-size:clamp(26px,5.4vw,40px); line-height:1.55;
    margin:14px 0 0; min-height:2.6em; color:#F3F1EC;
  }
  .caret{display:inline-block; width:2px; height:.95em; background:var(--rose);
         vertical-align:-.1em; margin-inline-start:2px; animation:blink 1s step-end infinite}
  @keyframes blink{50%{opacity:0}}
  .sub{color:var(--dim); margin:18px 0 0; font-size:15px; max-width:52ch}

  .desk{border-top:1px solid var(--rule); border-bottom:1px solid var(--rule);
        padding:22px 0; margin:30px 0 0;
        display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:18px 20px}
  label{display:block; font-size:13px; color:var(--dim); margin-bottom:7px}
  select,input,textarea{
    width:100%; background:var(--panel); color:#E6EAF5; border:1px solid var(--rule);
    border-radius:7px; padding:10px 12px; font-family:inherit; font-size:15px; font-weight:300;
  }
  textarea{resize:vertical; line-height:1.8}
  select:focus,input:focus,textarea:focus{outline:2px solid var(--rose); outline-offset:1px; border-color:transparent}
  .full{grid-column:1/-1}

  .run{
    width:100%; margin-top:24px; background:var(--rose); color:#fff; border:0;
    border-radius:8px; padding:16px; font-family:inherit; font-size:17px; font-weight:600;
    cursor:pointer;
  }
  .run:disabled{background:#43304A; color:#9A8FA4; cursor:not-allowed}
  .state{margin:14px 0 0; font-size:14px; color:var(--sage); min-height:1.4em}
  .state.bad{color:#FF9DAF}

  .sheet{
    background:var(--paper); color:var(--graphite); border-radius:3px;
    padding:46px 40px 34px; margin:30px 0 0; display:none;
    box-shadow:0 26px 60px rgba(0,0,0,.42);
  }
  .sheet.on{display:block}
  .sheet .body{
    font-family:"Amiri",serif; font-size:21px; line-height:2.05; white-space:pre-wrap;
    border-inline-start:2px solid transparent;
  }
  .sheet .body::first-line{font-weight:700}
  .foot{display:flex; flex-wrap:wrap; gap:14px; align-items:center;
        border-top:1px solid #DDD9D2; margin-top:28px; padding-top:16px;
        font-size:13px; color:#6B6F7A}
  .foot .grow{flex:1}
  .act{background:none; border:1px solid #CFCAC2; color:var(--graphite); border-radius:6px;
       padding:8px 14px; font-family:inherit; font-size:14px; cursor:pointer}
  .act:hover{border-color:var(--rose); color:var(--rose)}
  .act.solid{background:var(--graphite); color:var(--paper); border-color:var(--graphite)}
  .act.solid:hover{background:var(--rose); border-color:var(--rose); color:#fff}

  .hooks{margin-top:18px; display:none}
  .hooks.on{display:block}
  .hooks p{font-size:13px; color:#6B6F7A; margin:0 0 10px}
  .hook{display:block; width:100%; text-align:start; background:#EDEAE4; border:0;
        border-inline-start:3px solid transparent; padding:11px 14px; margin-bottom:7px;
        font-family:"Amiri",serif; font-size:18px; color:var(--graphite); cursor:pointer}
  .hook:hover{border-inline-start-color:var(--rose); background:#E7E3DC}

  .seedbox{margin-top:26px; font-size:13px; color:var(--dim)}
  .seedbox summary{cursor:pointer; color:var(--sage)}
  .seedbox ul{margin:10px 0 0; padding-inline-start:18px; line-height:1.9}

  .shelf{margin-top:56px; border-top:1px solid var(--rule); padding-top:26px}
  .shelf h2{font-family:"Amiri",serif; font-weight:400; font-size:22px; margin:0 0 16px}
  .saved{border-bottom:1px solid var(--rule); padding:14px 0; display:flex; gap:14px; align-items:flex-start}
  .saved .txt{flex:1; font-family:"Amiri",serif; font-size:17px; line-height:1.8; color:#D8DEEE;
              max-height:3.6em; overflow:hidden}
  .saved time{font-size:12px; color:var(--dim); white-space:nowrap}
  .saved button{background:none; border:0; color:var(--dim); cursor:pointer; font-family:inherit; font-size:13px}
  .saved button:hover{color:#FF9DAF}
  .empty{color:var(--dim); font-size:14px}

  @media (max-width:560px){ .sheet{padding:30px 22px 26px} .sheet .body{font-size:19px} }
  @media (prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important}}
  :focus-visible{outline:2px solid var(--rose); outline-offset:2px}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="mark">مِسنّ القصص · الإصدار __VER__</div>
    <p class="hero" id="hero"><span class="caret"></span></p>
    <p class="sub">منشورات قصصية مؤلَّفة، بأسلوب من عاشها وكتبها على جواله: سطر أول عادي،
       كشف في النصف الثاني، ومعضلة في الآخر. المحرّك يولّد خمس حبكات ويستبعد المتداول
       منها قبل أن يكتب، حتى لا تخرج قصة قرأها الناس ألف مرة. عشرون نوع موقف،
       من الخيانة إلى الطرفة اليومية، وثلاثة منظورات للسرد. وبعد الكتابة يمرّ النص
       على مدقّق يفحص الأرقام والمنطق وسبب بقاء السر مخفيًا، ويصلح ما لا يُصدَّق.</p>
  </header>

  <div class="desk">
    <div class="full">
      <label for="topic">موضوع أو موقف تريد البناء عليه (اتركه فارغًا ليختار المحرّك)</label>
      <textarea id="topic" rows="2" placeholder="مثال: شي صار في مكتب محامي، أو سر طلع من كشف حساب"></textarea>
    </div>
    <div>
      <label for="format">الشكل</label>
      <select id="format">
        <option value="short">قصير · نحو 85 كلمة</option>
        <option value="medium" selected>متوسط · نحو 140 كلمة</option>
        <option value="long">طويل · نحو 230 كلمة</option>
        <option value="thread">خيط مرقّم</option>
      </select>
    </div>
    <div>
      <label for="dialect">اللهجة</label>
      <select id="dialect">
        <option value="saudi" selected>سعودية بيضاء</option>
        <option value="gulf">خليجية</option>
        <option value="egy">مصرية</option>
        <option value="sham">شامية</option>
        <option value="fusha">فصحى مبسّطة</option>
      </select>
    </div>
    <div>
      <label for="core">نوع الموقف</label>
      <select id="core"></select>
    </div>
    <div>
      <label for="drama">حجم الحدث</label>
      <select id="drama">
        <option value="small">عادي جدًا · أقرب للتصديق</option>
        <option value="mid" selected>متوسط</option>
        <option value="big">قوي · مع بقائه معقولًا</option>
      </select>
    </div>
    <div>
      <label for="pov">المنشور</label>
      <select id="pov">
        <option value="self" selected>شخصي · بضمير المتكلم</option>
        <option value="third">عام · عن شخص آخر</option>
        <option value="heard">منقول · سمعتها من أحدهم</option>
      </select>
    </div>
    <div>
      <label for="provider">المحرّك</label>
      <select id="provider">
        <option value="free" selected>مجاني بلا مفتاح</option>
        <option value="openai">مزوّد خاص</option>
      </select>
    </div>
    <div class="full" id="creds" style="display:none">
      <label for="base">عنوان المزوّد ومفتاحه واسم النموذج</label>
      <input id="base" placeholder="https://api.openai.com/v1">
      <input id="key" type="password" placeholder="sk-…" style="margin-top:8px">
      <input id="model" placeholder="gpt-4o-mini" style="margin-top:8px">
    </div>
  </div>

  <button class="run" id="run">اكتب قصة</button>
  <p class="state" id="state"></p>

  <article class="sheet" id="sheet">
    <div class="body" id="story"></div>
    <div class="hooks" id="hooks">
      <p>اختر افتتاحية بديلة لتحلّ محل السطر الأول</p>
      <div id="hooklist"></div>
    </div>
    <div class="foot">
      <span id="meta" class="grow"></span>
      <button class="act" id="rehook">بدائل للافتتاحية</button>
      <button class="act" id="save">احفظ</button>
      <button class="act solid" id="copy">انسخ النص</button>
    </div>
  </article>

  <details class="seedbox" id="seedbox" style="display:none">
    <summary>بذرة هذه القصة وما أصلحه المدقّق</summary>
    <ul id="seedlist"></ul>
    <ul id="auditbox"></ul>
  </details>

  <section class="shelf">
    <h2>المحفوظات</h2>
    <div id="shelf"><p class="empty">لا شيء محفوظ بعد.</p></div>
  </section>

</div>

<script>
const $ = id => document.getElementById(id);
const OPENING = 'دفعت حساب القهوة وطلعت. عند الباب شفت اسمي مكتوب على ورقة مو لي.';

/* لحظة واحدة متحركة: السطر الأول يُكتب أمام القارئ */
(function type(i = 0) {
  const hero = $('hero');
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    hero.textContent = OPENING; return;
  }
  hero.textContent = OPENING.slice(0, i);
  hero.insertAdjacentHTML('beforeend', '<span class="caret"></span>');
  if (i <= OPENING.length) setTimeout(() => type(i + 1), i < 2 ? 500 : 38);
})();

$('provider').onchange = () => {
  $('creds').style.display = $('provider').value === 'openai' ? '' : 'none';
};

const creds = () => ({
  provider: $('provider').value,
  base: $('base').value, key: $('key').value, model: $('model').value
});

const SEED_LABEL = { who:'الطرف الآخر', secret:'السر', device:'أداة الكشف',
                     place:'مكان الكشف', cost:'الثمن', dilemma:'المعضلة',
                     topic:'موضوعك' };

let current = { text: '', dna: null, plot: '' };
let poll = null;

function state(msg, bad) {
  $('state').textContent = msg || '';
  $('state').classList.toggle('bad', !!bad);
}

$('run').onclick = async () => {
  clearInterval(poll);
  $('run').disabled = true;
  $('run').textContent = 'يكتب…';
  $('hooks').classList.remove('on');
  state('يختار بذرة لم تُستعمل من قبل');

  let job;
  try {
    const res = await fetch('/write', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topic: $('topic').value.trim(), format: $('format').value,
        dialect: $('dialect').value, core: $('core').value,
        pov: $('pov').value, drama: $('drama').value, ...creds()
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'تعذّر البدء.');
    job = data.job;
    current.dna = data.dna;
    showSeed(data.dna);
  } catch (e) { finish(e.message); return; }

  const STAGE = { seed: 'يركّب بذرة جديدة',
                  premise: 'يولّد خمس حبكات ويستبعد المستهلك منها',
                  draft: 'يكتب المسودة',
                  edit: 'يشدّ السطر الأول ويضبط الكشف والنهاية',
                  audit: 'يدقّق الأرقام والمنطق ويصلح ما لا يُصدَّق' };

  poll = setInterval(async () => {
    let j;
    try { j = await (await fetch('/write/' + job)).json(); } catch (e) { return; }

    if (j.text) {
      $('sheet').classList.add('on');
      $('story').textContent = j.text;
    }
    state(j.notice || STAGE[j.stage] || '');

    if (j.stage === 'done') {
      clearInterval(poll);
      current.text = j.text;
      $('story').textContent = j.text;
      $('sheet').classList.add('on');
      $('meta').textContent = j.words + ' كلمة · ' + (j.numbers || 0) + ' رقم محدد'
        + (j.novelty ? ' · جِدّة الحبكة ' + Math.round(j.novelty * 100) + '٪' : '')
        + (j.cliches && j.cliches.length ? ' · عبارة مألوفة نجت: ' + j.cliches[0] : '')
        + (j.leak ? ' · تسرّب ضمير متكلم في منشور عام' : '');
      showAudit(j.issues || []);
      current.plot = j.premise || '';
      finish('');
    }
    if (j.stage === 'error') { clearInterval(poll); finish(j.error, true); }
  }, 350);
};

function finish(msg, bad) {
  $('run').disabled = false;
  $('run').textContent = 'اكتب قصة أخرى';
  state(msg, bad);
}

function showAudit(issues) {
  const box = $('auditbox');
  if (!issues.length) {
    box.innerHTML = '<li>لم يجد المدقّق أي تناقض.</li>';
    return;
  }
  box.innerHTML = issues.map(i => '<li>' + i + '</li>').join('');
}

function showSeed(dna) {
  $('seedbox').style.display = '';
  $('seedlist').innerHTML = Object.keys(dna)
    .map(k => '<li>' + (SEED_LABEL[k] || k) + ': ' + dna[k] + '</li>').join('');
}

$('copy').onclick = async () => {
  try {
    await navigator.clipboard.writeText(current.text || $('story').textContent);
    $('copy').textContent = 'نُسخ';
    setTimeout(() => $('copy').textContent = 'انسخ النص', 1600);
  } catch (e) { state('المتصفح منع النسخ. حدّد النص وانسخه يدويًا.', true); }
};

$('save').onclick = async () => {
  const res = await fetch('/library', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: current.text || $('story').textContent, dna: current.dna, plot: current.plot })
  });
  if (res.ok) { $('save').textContent = 'حُفظ'; setTimeout(() => $('save').textContent = 'احفظ', 1600); shelf(); }
};

$('rehook').onclick = async () => {
  const text = current.text || $('story').textContent;
  if (!text) return;
  $('rehook').textContent = 'يبحث…';
  try {
    const res = await fetch('/hooks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, ...creds() })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    $('hooklist').innerHTML = '';
    data.hooks.forEach(h => {
      const b = document.createElement('button');
      b.className = 'hook'; b.type = 'button'; b.textContent = h;
      b.onclick = () => {
        const rest = (current.text || $('story').textContent).split('\n').slice(1).join('\n');
        current.text = h + '\n' + rest;
        $('story').textContent = current.text;
        $('hooks').classList.remove('on');
      };
      $('hooklist').appendChild(b);
    });
    $('hooks').classList.add('on');
  } catch (e) { state(e.message, true); }
  $('rehook').textContent = 'بدائل للافتتاحية';
};

async function shelf() {
  const data = await (await fetch('/library')).json();
  const box = $('shelf');
  if (!data.items.length) { box.innerHTML = '<p class="empty">لا شيء محفوظ بعد.</p>'; return; }
  box.innerHTML = '';
  data.items.forEach(it => {
    const row = document.createElement('div');
    row.className = 'saved';
    row.innerHTML = '<div class="txt"></div><time></time><button type="button">حذف</button>';
    row.querySelector('.txt').textContent = it.text;
    row.querySelector('time').textContent = it.at || '';
    row.querySelector('.txt').onclick = () => {
      current = { text: it.text, dna: it.dna, plot: it.plot || '' };
      $('story').textContent = it.text;
      $('sheet').classList.add('on');
      if (it.dna) showSeed(it.dna);
      window.scrollTo({ top: $('sheet').offsetTop - 40, behavior: 'smooth' });
    };
    row.querySelector('button').onclick = async () => {
      await fetch('/library/' + it.id, { method: 'DELETE' });
      shelf();
    };
    box.appendChild(row);
  });
}

fetch('/config').then(r => r.json()).then(c => {
  if (c.openai_ready) { $('provider').value = 'openai'; $('model').value = c.model; }
  $('provider').onchange();
  const sel = $('core');
  (c.cores || []).forEach(g => {
    const grp = document.createElement('optgroup');
    grp.label = g.group;
    g.items.forEach(it => {
      const o = document.createElement('option');
      o.value = it.id; o.textContent = it.label; o.title = it.desc;
      grp.appendChild(o);
    });
    sel.appendChild(grp);
  });
  sel.value = 'betrayal';
}).catch(() => {});
shelf();
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ التشغيل
def server_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def pick_port(host, port, tries=10):
    for p in range(port, port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return port


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    HOST = os.environ.get("STORY_HOST", "0.0.0.0")
    PORT = pick_port(HOST, int(os.environ.get("STORY_PORT", 7100)))
    URL = f"http://{server_ip()}:{PORT}"

    print(f"مِسنّ القصص — الإصدار {VERSION}")
    print("المحرّك: " + ("مزوّد خاص مضبوط" if (OPENAI_BASE and OPENAI_KEY)
                        else "مجاني بلا مفتاح"))
    print("المكتبة: " + LIB_PATH)
    print("العنوان:        " + URL)
    print("من نفس الجهاز:  " + f"http://localhost:{PORT}")

    if os.environ.get("STORY_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(URL)).start()

    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True, debug=False)
