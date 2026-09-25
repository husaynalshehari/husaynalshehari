# -*- coding: utf-8 -*-
"""
اختبارات بلا شبكة ولا مزوّد: يُستبدل chat بمزوّد وهمي يحاكي ردود النموذج.
    python -m unittest test_story_forge -v
"""

import io
import os
import json
import tempfile
import unittest
import urllib.error
from unittest import mock

os.environ["STORY_LIB"] = os.path.join(tempfile.mkdtemp(), "stories.json")

import story_forge as sf  # noqa: E402  (بعد ضبط مسار المكتبة)

STORY = "\n\n".join([
    "دفعت حساب القهوة وطلعت.",
    "وقفت عند الباب.",
    "ورقة مكتوب فيها «قرض شخصي» واسمي تحتها مو لي.",
    "أخوي الكبير، قبل 6 سنين، أخذ قرض 40 ألف باسمي.",
    "يسدد 900 كل شهر من راتبه.",
    "ما أدري ليش ما قال لي.",
    "وبعدين عرفت...",
    "القرض كان عشان عملية أمي.",
    "المفتاح لسا في جيبي.",
    "أروح له ولا أسكت؟",
])

IDEAS = {"ideas": [
    {"hook": "دفعت حساب القهوة وطلعت",
     "hidden": "أخوي الكبير أخذ قرض باسمي عشان عملية أمي وسدده بصمت من راتبه.",
     "why_hidden": "ما قال لأنه ما يبي أمي تعرف إن العملية كانت غالية.",
     "why_now": "البنك أرسل كشف حساب للعنوان القديم بالغلط.",
     "facts": ["القرض 40 ألف ريال", "قبل 6 سنين", "القسط 900 ريال شهريًا"]},
    {"hook": "هل تصدق ما حدث؟",
     "hidden": "تبديل الأطفال في المستشفى وتربية طفل ليس ابنك.",
     "why_hidden": "لأن.", "why_now": "الآن.", "facts": []},
    {"hook": "شي", "hidden": "فكرة عن سيارة.", "why_hidden": "", "why_now": "", "facts": []},
]}


INFO_STORY = "\n\n".join([
    "أغلب الأطفال ما يضعف تفكيرهم بالوراثة، يضعف بعادة وحدة في البيت.",
    "والسبب مو الجوال نفسه.",
    "الدماغ يتعلم من الملل: لما ما فيه شي يسوّيه يبدأ يخترع.",
    "1. خلّ نص ساعة يوميًا بلا شاشة ولا لعبة جاهزة.",
    "2. اسأله وش رأيه قبل ما تعطيه الجواب.",
    "3. خلّه يشوفك تقرأ.",
    "مثال: ولد الجيران يقعد بالسيارة بلا جوال ويسأل عن كل شي يشوفه.",
    "الطفل اللي يملّ اليوم يفكّر بكرة.",
    "جرّب نص ساعة ملل اليوم وشوف وش يطلع منه.",
])

INFO_IDEAS = {"ideas": [
    {"hook": "أغلب الأطفال ما يضعف تفكيرهم بالوراثة، يضعف بعادة وحدة في البيت",
     "claim": "الملل ضروري لنمو التفكير عند الطفل، وكثرة الترفيه الجاهز تحرمه منه.",
     "why": "الدماغ يبني حلولًا لما ما يجد شيئًا جاهزًا.",
     "points": ["نص ساعة بلا شاشة", "اسأله رأيه", "خله يشوفك تقرأ"],
     "example": "ولد الجيران في السيارة بلا جوال.", "facts": []},
    {"hook": "لن تصدق", "claim": "فكرة.", "why": "", "points": [], "example": "", "facts": []},
]}


class FakeProvider:
    """مزوّد وهمي: يقرر الرد من محتوى الطلب، ويسجّل ما نُودي به."""

    def __init__(self, story=STORY, audit_text=None, polish_text=None, hooks=None,
                 ideas=IDEAS):
        self.story, self.audit_text, self.polish_text = story, audit_text, polish_text
        self.hooks, self.ideas, self.calls, self.models = hooks, ideas, [], []

    def __call__(self, messages, provider, creds, on_token=None, temperature=0.95, timeout=240):
        user = messages[-1]["content"]
        self.models.append(creds.get("model"))
        if '"claim"' in user:
            self.calls.append("premise")
            return json.dumps(INFO_IDEAS, ensure_ascii=False)
        if '"ideas"' in user:
            self.calls.append("premise")
            return "```json\n" + json.dumps(self.ideas, ensure_ascii=False) + "\n```"
        if '"issues"' in user:
            self.calls.append("audit")
            return json.dumps({"issues": ["ملاحظة"], "text": self.audit_text or self.story},
                              ensure_ascii=False)
        if '"hooks"' in user:
            self.calls.append("hook")
            return json.dumps({"hooks": self.hooks or []}, ensure_ascii=False)
        if "لا تعد كتابته" in user:
            self.calls.append("polish")
            return self.polish_text or self.story
        if "المسودة:" in user:
            self.calls.append("edit")
        else:
            self.calls.append("draft")
        if on_token:
            for piece in ("```\n", self.story[:40], self.story[40:], "\n```"):
                on_token(piece, None)
        return "```\n" + self.story + "\n```"


def new_job():
    return {"stage": "seed", "text": "", "error": "", "at": 0, "issues": [], "checks": []}


class Helpers(unittest.TestCase):
    def test_numerals_normalize_arabic_digits_and_thousands(self):
        self.assertEqual(sf.numerals("قبل ٦ سنين ومبلغ 40,000 ريال"), ["6", "40000"])

    def test_stray_numbers_respects_fact_sheet(self):
        facts = ["القرض 40 ألف", "قبل 6 سنين"]
        self.assertEqual(sf.stray_numbers("دفع 40000 قبل 6 سنين", facts), [])
        self.assertEqual(sf.stray_numbers("دفع 40 ألف قبل 10 سنين و1 مرة", facts), ["10"])
        self.assertEqual(sf.stray_numbers("١. دفع 40 ألف", facts, fmt="thread"), [])
        self.assertEqual(sf.stray_numbers("أي شي", []), [])

    def test_words_of_drops_stopwords_and_tashkeel(self):
        self.assertEqual(sf.words_of("الذي كانَ مخفيًا في البيت"), {"البيت"})

    def test_overlap_flags_short_vague_and_swallowed_plots(self):
        tired = sf.TIRED_PLOTS[0]
        self.assertGreater(sf.overlap(tired + " وزيادة كلام كثير عن الحياة والناس", tired), 0.9)
        self.assertLess(sf.overlap("قرض باسم الأخ الصغير عشان عملية الأم", tired), 0.2)

    def test_pick_idea_prefers_rich_idea_over_vague_or_tired(self):
        ideas = sf.parse_ideas(json.dumps(IDEAS, ensure_ascii=False))
        best, novelty = sf.pick_idea(ideas, sf.TIRED_PLOTS)
        self.assertIn("قرض باسمي", best["hidden"])
        self.assertEqual(len(best["facts"]), 3)
        self.assertGreater(novelty, 0.5)

    def test_parse_ideas_accepts_plain_strings(self):
        ideas = sf.parse_ideas('{"ideas": ["فكرة واحدة"]}')
        self.assertEqual(ideas[0]["hidden"], "فكرة واحدة")
        with self.assertRaises(RuntimeError):
            sf.parse_ideas("كلام بلا JSON")

    def test_json_obj_tolerates_fences_and_raw_newlines(self):
        raw = '```json\n{"text": "سطر\nسطر ثاني", "issues": []}\n```'
        self.assertEqual(sf.json_obj(raw)["text"], "سطر\nسطر ثاني")

    def test_fresh_dna_locks_seed_and_avoids_used_triples(self):
        dna = sf.fresh_dna("موضوع", "loss", {"who": "أمي", "open": "quote", "bogus": "x"})
        self.assertEqual(dna["who"], "أمي")
        self.assertEqual(dna["open"], "quote")
        self.assertEqual(dna["locked"], ["who", "open"])
        self.assertIn(dna["dilemma"], sf.CLOSERS)
        self.assertIn("(ثابت)", sf.dna_text(dna))
        used = {(w, s, d) for w in sf.WHO for s in sf.SECRET_DARK for d in sf.DEVICE[:-1]}
        dna = sf.fresh_dna("", "betrayal", avoid=used)
        self.assertEqual(dna["device"], sf.DEVICE[-1])

    def test_run_checks_scores_a_good_story_high(self):
        checks = sf.run_checks(STORY, "short", "self", "dilemma",
                               ["القرض 40 ألف", "قبل 6 سنين", "900 شهريًا"])
        failed = [c["id"] for c in checks if not c["ok"]]
        self.assertEqual(failed, ["words"])           # 39 كلمة أقل من مدى «قصير»
        self.assertGreaterEqual(sf.score_of(checks), 85)

    def test_run_checks_catches_cliche_leak_long_line_and_symbols(self):
        text = "لن تصدق ما صار 😀 #قصة\n\nأنا كنت هناك وكان الرجل واقف عند الباب وهو يمسك ورقة طويلة جدًا فيها أرقام كثيرة وأسماء كثيرة ما أعرفها\n\nشاركني رأيك؟"
        ids = {c["id"]: c for c in sf.run_checks(text, "medium", "third", "closer", [])}
        for cid in ("cliches", "pov", "lines", "clean", "ending"):
            self.assertFalse(ids[cid]["ok"], cid)
            self.assertTrue(ids[cid]["fix"], cid)
        self.assertNotIn("facts", ids)

    def test_doubt_regex_covers_fusha_and_dialects(self):
        for line in ("لا أعلم كيف حصل على توقيع جده", "ما أدري ليش", "مش عارف ليه", "ما بعرف شو صار"):
            self.assertTrue(sf.DOUBT_RE.search(line), line)
        self.assertFalse(sf.DOUBT_RE.search("رحت للبنك وسحبت المبلغ"))

    def test_dialect_check_fails_on_fusha_when_dialect_requested(self):
        fusha = "كنا نراجع ملفات الشركة.\n\nلم يفتح أحد مكتبه.\n\nإما أن أقاضي ابني أو أترك الأرض."
        ids = {c["id"]: c for c in sf.run_checks(fusha, "medium", "self", "dilemma", [], dialect="saudi")}
        self.assertFalse(ids["dialect"]["ok"])
        self.assertIn("سعودية", ids["dialect"]["fix"])
        ids = {c["id"]: c for c in sf.run_checks(STORY, "short", "self", "dilemma", [], dialect="saudi")}
        self.assertTrue(ids["dialect"]["ok"])
        ids = {c["id"]: c for c in sf.run_checks(fusha, "medium", "self", "dilemma", [], dialect="fusha")}
        self.assertNotIn("dialect", ids)

    def test_quote_and_now_checks_are_advisory(self):
        plain = "رحت للبنك.\n\nقالوا لي إن القرض باسمي.\n\nأخوي أخذه قبل 6 سنين.\n\nأروح له ولا أسكت؟"
        ids = {c["id"]: c for c in sf.run_checks(plain, "short", "self", "dilemma", [])}
        self.assertFalse(ids["quote"]["ok"])
        self.assertFalse(ids["now"]["ok"])
        self.assertNotIn("quote", sf.CRITICAL)
        quoted = plain.replace("قالوا لي إن القرض باسمي.", "الموظف قال: «القرض باسمك من 2019».\n\nهو الحين قاعد برا ينتظرني.")
        ids = {c["id"]: c for c in sf.run_checks(quoted, "short", "self", "dilemma", [])}
        self.assertTrue(ids["quote"]["ok"])
        self.assertTrue(ids["now"]["ok"])

    def test_parse_ideas_reads_evidence_and_motive(self):
        raw = json.dumps({"ideas": [{"hidden": "أخوي أخذ قرض باسمي", "evidence": "«القرض باسمك»",
                                     "motive": "عشان الورث", "facts": ["قبل 6 سنين"]}]}, ensure_ascii=False)
        idea = sf.parse_ideas(raw)[0]
        self.assertEqual(idea["motive"], "عشان الورث")
        self.assertIn("الدليل بحرفه", sf.premise_text(idea))
        self.assertIn("دافع الطرف الآخر: عشان الورث", sf.premise_text(idea))

    def test_opening_styles_are_weighted_toward_acts(self):
        seen = {sf.fresh_dna()["open"] for _ in range(300)}
        self.assertIn("act", seen)
        self.assertTrue(seen <= set(sf.OPEN_STYLES))

    def test_numbers_check_wants_digits_not_words(self):
        self.assertEqual(sf.count_numbers("قبل تسع سنين ومليوني ريال"), 0)
        self.assertEqual(sf.count_numbers("قبل 9 سنين و٤٨ ساعة"), 2)
        self.assertTrue(sf.QUOTE_RE.search("لقيت ورقة مكتوب فيها بخط يده:"))
        self.assertTrue(sf.QUOTE_RE.search("قالت لي أمي بالحرف:"))
        self.assertFalse(sf.QUOTE_RE.search("كان مكتوب اسمي على الورقة"))

    def test_split_thread_honours_marker_and_limits(self):
        text = "وقعت ورقة فصل الأجهزة عن أبوي.\n\nأخوي حلف إن الدكتور قال ميئوس منه.\n\nبعد 6 شهور لقيت التقرير بخط يده: المريض يتجاوب.\n\nيحتاج..\n\n---\n\n48 ساعة ويفيق.\n\nكذب علي عشان الورث.\n\nهو الحين جالس يضحك..\n\nأوديه للشرطة ولا آخذ حقي بيدي؟"
        parts = sf.split_thread(text)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[0].endswith("يحتاج.."))
        self.assertTrue(parts[1].startswith("48 ساعة"))
        self.assertNotIn("---", "".join(parts))
        self.assertTrue(all(len(p) <= sf.THREAD_MAX for p in parts))
        # بلا علامة: يقطع عند أول سطر ينتهي بنقطتين بعد الثلث الأول
        parts = sf.split_thread(text.replace("\n\n---\n\n", "\n\n"))
        self.assertTrue(parts[0].endswith("يحتاج.."))
        # نص طويل يُجمَّع في أجزاء لا تتجاوز الحدّ
        long_text = "\n\n".join(f"سطر رقم {i} فيه كلام كافي عشان يطول شوي ويملأ المكان." for i in range(30))
        parts = sf.split_thread(long_text)
        self.assertGreaterEqual(len(parts), 5)
        self.assertTrue(all(len(p) <= sf.THREAD_MAX for p in parts))
        # سطر واحد أطول من الحدّ يُقصّ عند مسافة
        parts = sf.split_thread("كلمة " * 120)
        self.assertTrue(all(len(p) <= sf.THREAD_MAX for p in parts))
        self.assertEqual(sf.split_thread(""), [])

    def test_thread_checks_parts_and_cliff(self):
        good = "وقعت ورقة فصل الأجهزة عن أبوي.\n\nلقيت التقرير بخط يده: المريض يتجاوب.\n\nيحتاج..\n\n---\n\n48 ساعة ويفيق.\n\nكذب علي عشان الورث قبل 6 شهور.\n\nهو الحين جالس يضحك..\n\nأوديه للشرطة ولا آخذ حقي بيدي؟"
        ids = {c["id"]: c for c in sf.run_checks(good, "thread", "self", "dilemma", [])}
        self.assertTrue(ids["thread"]["ok"], ids["thread"])
        self.assertTrue(ids["cliff"]["ok"])
        ids = {c["id"]: c for c in sf.run_checks(good.replace("\n\n---", ""), "thread", "self", "dilemma", [])}
        self.assertFalse(ids["cliff"]["ok"])
        self.assertIn("cliff", sf.CRITICAL)

    def test_thread_first_part_must_be_nearly_full(self):
        short_head = "وقعت الورقة.\n\nيحتاج..\n\n---\n\n48 ساعة ويفيق.\n\nأوديه للشرطة ولا أسكت؟"
        ids = {c["id"]: c for c in sf.run_checks(short_head, "thread", "self", "dilemma", [])}
        self.assertFalse(ids["first"]["ok"])
        self.assertIn("250", ids["first"]["fix"])
        self.assertIn("first", sf.CRITICAL)
        filler = "\n\n".join("سطر خلفية فيه تفصيل محسوس ورقم 6 سنين يطوّل الجزء الأول." for _ in range(4))
        full_head = "وقعت الورقة.\n\n" + filler + "\n\nيحتاج..\n\n---\n\n48 ساعة ويفيق.\n\nأوديه للشرطة ولا أسكت؟"
        ids = {c["id"]: c for c in sf.run_checks(full_head, "thread", "self", "dilemma", [])}
        self.assertTrue(ids["first"]["ok"], ids["first"])
        self.assertTrue(ids["thread"]["ok"], ids["thread"])

    def test_polish_only_for_critical_failures(self):
        checks = [{"id": "doubt", "ok": False, "fix": "x"}, {"id": "lines", "ok": False, "fix": "y"},
                  {"id": "facts", "ok": True, "fix": None}]
        self.assertEqual(sf.polish_problems(checks), [])
        checks.append({"id": "dialect", "ok": False, "fix": "z"})
        self.assertEqual(sf.polish_problems(checks), ["z"])

    def test_provider_error_reads_google_quota_message(self):
        raw = json.dumps([{"error": {"code": 429, "message": "You exceeded your current quota.\n* Quota exceeded for metric: x, limit: 20, model: m\nPlease retry in 28s.", "details": [{"retryDelay": "28s"}]}}])
        msg = sf.provider_error(raw)
        self.assertIn("الحدّ 20", msg)
        self.assertIn("28 ثانية", msg)
        self.assertEqual(sf.provider_error("نص عادي"), "نص عادي")

    def test_seed_pools_are_large_and_free_of_duplicates(self):
        pools = {"WHO": sf.WHO, "SECRET_DARK": sf.SECRET_DARK, "SECRET_LIGHT": sf.SECRET_LIGHT,
                 "DEVICE": sf.DEVICE, "PLACE": sf.PLACE, "COST_DARK": sf.COST_DARK,
                 "COST_LIGHT": sf.COST_LIGHT, "DILEMMA": sf.DILEMMA, "CLOSERS": sf.CLOSERS,
                 "TIRED_PLOTS": sf.TIRED_PLOTS}
        for name, pool in pools.items():
            self.assertEqual(len(pool), len(set(pool)), f"تكرار في {name}")
        self.assertGreaterEqual(len(sf.WHO), 60)
        self.assertGreaterEqual(len(sf.SECRET_DARK) + len(sf.SECRET_LIGHT), 80)
        self.assertGreaterEqual(len(sf.DEVICE), 35)
        self.assertGreaterEqual(len(sf.CORES), 40)
        self.assertGreaterEqual(len(sf.OPEN_STYLES), 7)
        grouped = [k for _, keys in sf.CORE_GROUPS for k in keys]
        self.assertEqual(sorted(grouped), sorted(sf.CORES), "كل نوع موقف يجب أن يظهر في مجموعة واحدة")

    def test_best_hook_filters_questions_and_clashes(self):
        prev = ["دفعت حساب القهوة وطلعت من المطعم بسرعة"]
        hooks = ["ليش صار كذا؟", "دفعت حساب القهوة وطلعت من المطعم", "فتحت الظرف وأنا واقف عند الباب"]
        self.assertEqual(sf.best_hook(hooks, prev), "فتحت الظرف وأنا واقف عند الباب")
        self.assertIsNone(sf.best_hook(["كلمة"], prev))


class Pipeline(unittest.TestCase):
    def setUp(self):
        self._chat = sf.chat
        sf.lib_write([])

    def tearDown(self):
        sf.chat = self._chat

    def test_full_pipeline_reaches_done_with_facts_and_scorecard(self):
        fake = FakeProvider()
        sf.chat = fake
        job = new_job()
        dna = sf.fresh_dna("", "betrayal")
        sf.write_story(job, dna, "short", "saudi", "betrayal", "self", "mid", "free", {})
        self.assertEqual(job["stage"], "done")
        self.assertTrue(job["text"].startswith("دفعت"))
        self.assertEqual(job["facts"], ["القرض 40 ألف ريال", "قبل 6 سنين", "القسط 900 ريال شهريًا"])
        self.assertEqual(job["plot"], IDEAS["ideas"][0]["hidden"])
        self.assertIn("لماذا بقي مخفيًا", job["premise"])
        self.assertIsInstance(job["score"], int)
        self.assertTrue(any(c["id"] == "facts" and c["ok"] for c in job["checks"]))
        self.assertEqual(fake.calls[:4], ["premise", "draft", "edit", "audit"])
        # الكلمات أقل من المدى → جولة صقل واحدة لا أكثر
        self.assertEqual(fake.calls.count("polish"), 1)

    def test_polish_fixes_a_surviving_cliche(self):
        bad = STORY.replace("وقفت عند الباب.", "وهنا الصدمة.")
        fake = FakeProvider(story=bad, polish_text=STORY)
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna(), "short", "saudi", "betrayal", "self", "mid", "free", {})
        self.assertTrue(job.get("polished"))
        self.assertEqual(job["cliches"], [])
        self.assertNotIn("وهنا الصدمة", job["text"])

    def test_polish_is_discarded_when_it_makes_things_worse(self):
        fake = FakeProvider(polish_text="نص قصير جدًا")
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna(), "short", "saudi", "betrayal", "self", "mid", "free", {})
        self.assertFalse(job.get("polished"))
        self.assertTrue(job["text"].startswith("دفعت"))

    def test_audit_result_is_kept_only_when_not_truncated(self):
        fake = FakeProvider(audit_text="مبتور")
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna(), "short", "saudi", "betrayal", "self", "mid", "free", {})
        self.assertTrue(job["text"].startswith("دفعت"))
        self.assertEqual(job["issues"], ["ملاحظة"])

    def test_opening_clash_triggers_hook_replacement(self):
        sf.lib_write([{"id": "a", "text": STORY, "plot": "شي آخر", "dna": {}}])
        fake = FakeProvider(hooks=["دفعت حساب القهوة وطلعت.", "فتحت الظرف وأنا واقف عند الباب."])
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna(), "short", "saudi", "betrayal", "self", "mid", "free", {})
        self.assertTrue(job.get("rehooked"))
        self.assertTrue(job["text"].startswith("فتحت الظرف"))
        self.assertIn("hook", fake.calls)
        self.assertTrue(all(c["ok"] for c in job["checks"] if c["id"] == "opening"))

    def test_cancel_stops_between_stages(self):
        fake = FakeProvider()
        sf.chat = fake
        job = new_job()
        job["cancel"] = True
        with self.assertRaises(sf.Cancelled):
            sf.write_story(job, sf.fresh_dna(), "short", "saudi", "betrayal", "self", "mid", "free", {})
        self.assertEqual(fake.calls, ["premise"])

    def test_plan_model_is_used_for_the_premise_only(self):
        fake = FakeProvider()
        sf.chat = fake
        job = new_job()
        creds = {"base": "https://x", "key": "k", "model": "flash", "plan_model": "pro", "think": ""}
        sf.write_story(job, sf.fresh_dna(), "short", "saudi", "betrayal", "self", "big", "openai", creds)
        self.assertEqual(job["stage"], "done")
        self.assertEqual(fake.models[0], "pro")
        self.assertTrue(all(m == "flash" for m in fake.models[1:]), fake.models)
        self.assertEqual(sf.plan_creds({"model": "flash", "plan_model": ""})["model"], "flash")

    def test_thread_format_yields_parts(self):
        thread = STORY.replace("وبعدين عرفت...", "وبعدين عرفت..\n\n---")
        fake = FakeProvider(story=thread, polish_text=thread)
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna(), "thread", "saudi", "betrayal", "self", "big", "free", {})
        self.assertEqual(job["stage"], "done")
        self.assertEqual(len(job["parts"]), 2)
        self.assertTrue(job["parts"][0].endswith("عرفت.."))
        self.assertEqual(job["text"], sf.thread_text(job["parts"]))
        self.assertNotIn("---", "".join(job["parts"]))

    def test_fast_mode_skips_audit(self):
        fake = FakeProvider()
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna(), "short", "saudi", "betrayal", "self", "mid", "free", {}, mode="fast")
        self.assertEqual(job["stage"], "done")
        self.assertNotIn("audit", fake.calls)
        self.assertEqual(job["issues"], [])

    def test_third_person_leak_is_reported(self):
        fake = FakeProvider()
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna("", "loss"), "short", "saudi", "loss", "third", "mid", "free", {})
        pov = [c for c in job["checks"] if c["id"] == "pov"][0]
        self.assertFalse(pov["ok"])


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _http_error(code, body):
    return urllib.error.HTTPError("https://x/chat/completions", code, "err", {}, io.BytesIO(body.encode("utf-8")))


class Transport(unittest.TestCase):
    """سلوك chat() أمام المزوّد: التفكير المرفوض، والحصة اليومية، بلا شبكة."""
    CREDS = {"base": "https://x", "key": "k", "model": "m", "think": "low"}

    def setUp(self):
        sf.THINK_OK[0] = True

    def tearDown(self):
        sf.THINK_OK[0] = True

    def test_reasoning_effort_is_sent_then_dropped_when_provider_rejects_it(self):
        seen = []

        def fake_urlopen(req, timeout=0):
            body = json.loads(req.data)
            seen.append(body)
            if "reasoning_effort" in body:
                raise _http_error(400, '{"error":{"message":"Unrecognized request argument supplied: reasoning_effort"}}')
            return _Resp({"choices": [{"message": {"content": "ok"}}]})

        with mock.patch.object(sf.urllib.request, "urlopen", fake_urlopen):
            out = sf.chat([{"role": "user", "content": "x"}], "openai", self.CREDS)
            self.assertEqual(out, "ok")
            self.assertEqual(seen[0]["reasoning_effort"], "low")
            self.assertNotIn("reasoning_effort", seen[1])
            self.assertFalse(sf.THINK_OK[0])
            sf.chat([{"role": "user", "content": "y"}], "openai", self.CREDS)
            self.assertEqual(len(seen), 3)          # لا محاولة ثانية بعد ما عرفنا أنه مرفوض

    def test_daily_quota_fails_fast_with_clear_message(self):
        body = json.dumps([{"error": {"code": 429, "message": "You exceeded your current quota.\n* Quota exceeded for metric: x, limit: 20", "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}])

        def fake_urlopen(req, timeout=0):
            raise _http_error(429, body)

        with mock.patch.object(sf.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(sf.time, "sleep", side_effect=AssertionError("لا انتظار على حصة يومية")):
            with self.assertRaises(RuntimeError) as ctx:
                sf.chat([{"role": "user", "content": "x"}], "openai", self.CREDS)
        self.assertIn("انتهت حصة اليوم", str(ctx.exception))
        self.assertIn("الحدّ 20", str(ctx.exception))

    def test_temporary_429_waits_for_retry_delay_then_succeeds(self):
        calls, slept = [], []

        def fake_urlopen(req, timeout=0):
            calls.append(1)
            if len(calls) == 1:
                raise _http_error(429, '{"error":{"message":"busy","details":[{"retryDelay":"7s"}]}}')
            return _Resp({"choices": [{"message": {"content": "ok"}}]})

        with mock.patch.object(sf.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(sf.time, "sleep", lambda s: slept.append(s)):
            self.assertEqual(sf.chat([{"role": "user", "content": "x"}], "openai", self.CREDS), "ok")
        self.assertEqual(slept, [8])


class Info(unittest.TestCase):
    """منشورات المعلومات: خط إنتاج مستقل لا يتداخل مع القصص."""

    def setUp(self):
        self._chat = sf.chat
        sf.lib_write([])

    def tearDown(self):
        sf.chat = self._chat

    def test_fresh_info_validates_and_locks(self):
        spec = sf.fresh_info("food", "myth", "السكر البني", {"frame": sf.INFO_FRAMES[2]})
        self.assertEqual((spec["kind"], spec["domain"], spec["angle"]), ("info", "food", "myth"))
        self.assertEqual(spec["frame"], sf.INFO_FRAMES[2])
        self.assertEqual(spec["locked"], ["frame"])
        self.assertEqual(spec["topic"], "السكر البني")
        spec = sf.fresh_info("bogus", "bogus")
        self.assertEqual((spec["domain"], spec["angle"]), ("kids", "why"))
        self.assertIn(spec["frame"], sf.INFO_FRAMES)

    def test_info_checks_skip_story_checks_and_add_info_ones(self):
        ids = {c["id"]: c for c in sf.run_checks(INFO_STORY, "short", "self", "closer", [], dialect="saudi", kind="info")}
        for cid in ("doubt", "quote", "now", "ending", "pov", "numbers"):
            self.assertNotIn(cid, ids, cid)
        self.assertTrue(ids["hook"]["ok"], ids["hook"])
        self.assertTrue(ids["points"]["ok"], ids["points"])
        self.assertTrue(ids["hedge"]["ok"])
        self.assertTrue(ids["dialect"]["ok"])
        bad = INFO_STORY.replace("الطفل اللي يملّ اليوم يفكّر بكرة.", "هذا يشفي الطفل من كل شي 100%.")
        ids = {c["id"]: c for c in sf.run_checks(bad, "short", "self", "closer", [], kind="info")}
        self.assertFalse(ids["hedge"]["ok"])
        self.assertIn("hedge", sf.CRITICAL)

    def test_info_pipeline_runs_and_uses_plan_model(self):
        fake = FakeProvider(story=INFO_STORY, audit_text=INFO_STORY, polish_text=INFO_STORY)
        sf.chat = fake
        job = {"stage": "seed", "text": "", "kind": "info", "issues": [], "checks": []}
        spec = sf.fresh_info("kids", "why")
        sf.write_info(job, spec, "short", "saudi", "openai",
                      {"base": "https://x", "key": "k", "model": "flash", "plan_model": "pro", "think": ""})
        self.assertEqual(job["stage"], "done")
        self.assertEqual(fake.models[0], "pro")
        self.assertEqual(fake.calls[:4], ["premise", "draft", "edit", "audit"])
        self.assertIn("الملل ضروري", job["plot"])
        self.assertTrue(job["text"].startswith("أغلب الأطفال"))
        self.assertGreaterEqual(job["score"], 70)

    def test_info_thread_ends_part_one_with_cut_shocking_bait(self):
        bait = "الشي اللي تسويه كل يوم عشان يسكت هو بالضبط اللي بيخليه بعد 10 سنين.."
        hook = "ليش عقل طفلك ما يصير عميق؟"
        lines = []                                   # تمهيد بقدر ما يوصل الجزء الأول إلى 250–280
        while len("\n\n".join([hook, *lines, bait])) < sf.THREAD_FIRST_MIN:
            lines.append("تفصيل يومي يعرفه كل بيت.")
        filler = "\n\n".join(lines)
        thread = (hook + "\n\n" + filler + "\n\n" + bait +
                  "\n\n---\n\n..ينهار أول ما يواجه مشكلة بدون شاشة.\n\nالمقصود: الملل يدفع الدماغ يخترع.\n\n"
                  "1. نص ساعة بلا شاشة.\n\n2. اسأله رأيه.\n\n3. خله يشوفك تقرأ.\n\nجرّبها اليوم.")
        ids = {c["id"]: c for c in sf.run_checks(thread, "thread", "self", "closer", [], kind="info")}
        self.assertTrue(ids["cliff"]["ok"], ids["cliff"])
        self.assertEqual(ids["cliff"]["label"], "طُعم مقطوع صادم")
        self.assertTrue(ids["first"]["ok"], ids["first"])
        # جملة كاملة هادئة بلا قطع ولا صدمة → يرسب
        calm = thread.replace(bait, "إسكات طفلك الباكي فورًا يؤثر على نمو مشاعره.")
        ids = {c["id"]: c for c in sf.run_checks(calm, "thread", "self", "closer", [], kind="info")}
        self.assertFalse(ids["cliff"]["ok"])
        self.assertIn("مقطوع", ids["cliff"]["fix"])
        # مقطوعة لكن بلا صدمة → يرسب
        mild = thread.replace(bait, "وهذا الشي له سبب واضح عند الأطفال ووالديهم..")
        ids = {c["id"]: c for c in sf.run_checks(mild, "thread", "self", "closer", [], kind="info")}
        self.assertFalse(ids["cliff"]["ok"])
        # جزء أول أطول من الحدّ ينقسم وتضيع الفجوة → يرسب «امتلاء» و«طُعم» معًا
        long_head = thread.replace(filler, filler + "\n\n" + filler)
        ids = {c["id"]: c for c in sf.run_checks(long_head, "thread", "self", "closer", [], kind="info")}
        self.assertFalse(ids["first"]["ok"])
        self.assertFalse(ids["cliff"]["ok"])
        self.assertIn("الطُعم", sf.info_write_prompt(sf.fresh_info("kids", "why"), sf.parse_info_ideas(
            json.dumps({"ideas": [{"claim": "x", "bait": "ب", "resolve": "ر"}]}, ensure_ascii=False))[0], "thread", "saudi"))
        self.assertIn("«---»", sf.info_audit_prompt([], sf.fresh_info("kids", "why")))
        fake = FakeProvider(story=thread, audit_text=thread, polish_text=thread)
        sf.chat = fake
        job = {"stage": "seed", "text": "", "kind": "info", "issues": [], "checks": []}
        sf.write_info(job, sf.fresh_info("kids", "why"), "thread", "saudi", "free", {})
        self.assertEqual(job["stage"], "done")
        self.assertTrue(job["parts"][0].endswith("بعد 10 سنين.."))
        self.assertTrue(job["parts"][1].startswith("..ينهار"))

    def test_critique_and_satire_kinds_share_pipeline_with_own_rules(self):
        for kind, first_domain in (("critique", "habit"), ("satire", "types")):
            cfg = sf.topic_cfg(kind)
            self.assertGreaterEqual(len(cfg["domains"]), 15, kind)
            self.assertGreaterEqual(len(cfg["angles"]), 8, kind)
            spec = sf.fresh_info("bogus", "bogus", "", None, kind)
            self.assertEqual((spec["kind"], spec["domain"]), (kind, first_domain))
            self.assertIn(spec["frame"], cfg["frames"])
            self.assertIn(cfg["noun"], sf.info_premise_prompt(spec, []))
            self.assertIn(cfg["hook"], sf.info_write_prompt(spec, {"hook": "", "claim": "x", "why": "", "points": [],
                                                                    "example": "", "facts": [], "bait": "", "resolve": ""}, "short", "saudi"))
            self.assertIn("اسم شخص", sf.info_audit_prompt([], spec))
            self.assertIn(cfg["craft"][:20], sf.info_system_prompt("saudi", kind))
        self.assertIn("لا سخرية من دين", sf.info_system_prompt("saudi", "satire"))
        self.assertIn("لا أشخاصًا بأسمائهم", sf.info_system_prompt("saudi", "critique"))
        # النقاط المرقّمة إرشادية للانتقاد والسخرية، جوهرية للمعلومات فقط
        text = INFO_STORY.replace("1. ", "").replace("2. ", "").replace("3. ", "")
        for kind in ("critique", "satire"):
            ids = {c["id"]: c for c in sf.run_checks(text, "short", "self", "closer", [], kind=kind)}
            self.assertFalse(ids["points"]["ok"]); self.assertIsNone(ids["points"]["fix"])
        ids = {c["id"]: c for c in sf.run_checks(text, "short", "self", "closer", [], kind="info")}
        self.assertTrue(ids["points"]["fix"])
        self.assertNotIn("faith", ids)
        holy = INFO_STORY.replace("خلّه يشوفك تقرأ.", "اللي يرسل آية كل صباح يبغى يثبت إنه حي.")
        ids = {c["id"]: c for c in sf.run_checks(holy, "short", "self", "closer", [], kind="satire")}
        self.assertFalse(ids["faith"]["ok"]); self.assertIn("faith", sf.CRITICAL)
        # الانتقاد والسخرية يُدقَّقان حتى في الوضع السريع
        fake2 = FakeProvider(story=INFO_STORY, audit_text=INFO_STORY, polish_text=INFO_STORY)
        sf.chat = fake2
        job = {"stage": "seed", "text": "", "kind": "critique", "issues": [], "checks": []}
        sf.write_info(job, sf.fresh_info("habit", "cost", "", None, "critique"), "short", "saudi", "free", {}, mode="fast")
        self.assertIn("audit", fake2.calls)
        # خط الإنتاج والمكتبة مفصولان
        fake = FakeProvider(story=INFO_STORY, audit_text=INFO_STORY, polish_text=INFO_STORY)
        sf.chat = fake
        job = {"stage": "seed", "text": "", "kind": "satire", "issues": [], "checks": []}
        sf.write_info(job, sf.fresh_info("types", "types", "", None, "satire"), "short", "saudi", "free", {})
        self.assertEqual(job["stage"], "done")
        self.assertIn("سخرية", fake.calls and sf.KIND_LABELS["satire"])
        c = sf.app.test_client(); sf.JOBS.clear()
        r = c.post("/write", json={"kind": "critique", "domain": "wedding", "angle": "cost", "mode": "fast"})
        self.assertEqual(r.get_json()["dna"]["kind"], "critique")
        self.assertEqual(sf.JOBS[r.get_json()["job"]]["kind"], "critique")
        r = c.post("/library", json={"text": INFO_STORY, "kind": "satire"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/library").get_json()["items"][0]["kind"], "satire")
        self.assertEqual(sf.recent_plots(kind="critique"), [])
        cfg = c.get("/config").get_json()["topics"]
        self.assertEqual(sorted(cfg), ["critique", "fire", "info", "satire"])
        self.assertEqual(len(cfg["satire"]["domains"]), len(sf.SAT_DOMAINS))

    def test_fire_kind_provokes_by_scene_not_by_gender_or_harm(self):
        cfg = sf.topic_cfg("fire")
        self.assertEqual(len(cfg["domains"]), 20); self.assertEqual(len(cfg["angles"]), 10)
        self.assertNotIn("اغتصاب", " ".join(v[0] + v[1] for v in cfg["domains"].values()))
        self.assertIn("ممنوع «كل النساء»", sf.info_system_prompt("saudi", "fire"))
        ok_text = INFO_STORY.replace("خلّه يشوفك تقرأ.", "طلّقها بعد 11 سنة لأنها قالت له شكرًا.")
        ids = {c["id"]: c for c in sf.run_checks(ok_text, "short", "self", "closer", [], kind="fire")}
        self.assertTrue(ids["gender"]["ok"]); self.assertTrue(ids["harm"]["ok"]); self.assertTrue(ids["faith"]["ok"])
        bad = INFO_STORY.replace("خلّه يشوفك تقرأ.", "كل النساء كذا، اضربها عشان تتأدب.")
        ids = {c["id"]: c for c in sf.run_checks(bad, "short", "self", "closer", [], kind="fire")}
        self.assertFalse(ids["gender"]["ok"]); self.assertFalse(ids["harm"]["ok"])
        self.assertTrue({"gender", "harm"} <= sf.CRITICAL)
        assault = INFO_STORY.replace("خلّه يشوفك تقرأ.", "قصة اغتصاب في الحي.")
        ids = {c["id"]: c for c in sf.run_checks(assault, "short", "self", "closer", [], kind="fire")}
        self.assertFalse(ids["harm"]["ok"])
        fake = FakeProvider(story=INFO_STORY, audit_text=INFO_STORY, polish_text=INFO_STORY)
        sf.chat = fake
        job = {"stage": "seed", "text": "", "kind": "fire", "issues": [], "checks": []}
        sf.write_info(job, sf.fresh_info("husband", "both", "", None, "fire"), "short", "saudi", "free", {}, mode="fast")
        self.assertEqual(job["stage"], "done"); self.assertIn("audit", fake.calls)
        c = sf.app.test_client(); sf.JOBS.clear()
        r = c.post("/write", json={"kind": "fire", "domain": "divorce", "angle": "scene", "mode": "fast"})
        self.assertEqual(r.get_json()["dna"]["kind"], "fire")
        self.assertIn("fire", c.get("/config").get_json()["topics"])

    def test_library_keeps_kinds_apart(self):
        sf.lib_write([{"id": "s", "text": STORY, "plot": "قرض", "dna": {"who": "أمي", "secret": "س", "device": "د"}},
                      {"id": "i", "text": INFO_STORY, "plot": "الملل ضروري", "kind": "info", "dna": {"kind": "info"}}])
        self.assertEqual(sf.recent_plots(), ["قرض"])
        self.assertEqual(sf.recent_plots(kind="info"), ["الملل ضروري"])
        self.assertTrue(sf.recent_openings()[0].startswith("دفعت"))
        self.assertTrue(sf.recent_openings(kind="info")[0].startswith("أغلب"))
        self.assertEqual(sf.recent_dna(), {("أمي", "س", "د")})
        c = sf.app.test_client()
        self.assertEqual(len(c.get("/library?kind=info").get_json()["items"]), 1)
        self.assertEqual(len(c.get("/library").get_json()["items"]), 2)

    def test_write_route_accepts_info_kind(self):
        sf.chat = FakeProvider(story=INFO_STORY, audit_text=INFO_STORY, polish_text=INFO_STORY)
        c = sf.app.test_client()
        sf.JOBS.clear()
        r = c.post("/write", json={"kind": "info", "domain": "sleep", "angle": "signs", "format": "short", "mode": "fast"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["dna"]["kind"], "info")
        self.assertEqual(data["dna"]["domain"], "sleep")
        import time
        for _ in range(200):
            j = c.get("/write/" + data["job"]).get_json()
            if j["stage"] in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual(j["stage"], "done", j.get("error"))
        self.assertEqual(j["kind"], "info")
        cfg = c.get("/config").get_json()
        self.assertEqual(len(cfg["domains"]), len(sf.INFO_DOMAINS))
        self.assertEqual(len(cfg["angles"]), len(sf.INFO_ANGLES))


class Routes(unittest.TestCase):
    def setUp(self):
        self.c = sf.app.test_client()
        self._chat = sf.chat
        sf.lib_write([])
        sf.JOBS.clear()

    def tearDown(self):
        sf.chat = self._chat

    def test_index_and_config(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("مِسنّ", r.get_data(as_text=True))
        cfg = self.c.get("/config").get_json()
        self.assertEqual(cfg["version"], sf.VERSION)
        self.assertEqual(cfg["think"], sf.THINK)
        self.assertEqual(sorted(cfg["seeds"]), sorted(sf.SEED_KEYS))
        self.assertEqual(sum(len(g["items"]) for g in cfg["cores"]), len(sf.CORES))

    def test_write_rejects_private_provider_without_key(self):
        r = self.c.post("/write", json={"provider": "openai"})
        self.assertEqual(r.status_code, 400)

    def test_write_locks_seed_and_streams_to_done(self):
        sf.chat = FakeProvider()
        r = self.c.post("/write", json={"format": "short", "core": "betrayal", "mode": "fast",
                                        "seed": {"who": "أمي", "open": "number"}})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["dna"]["who"], "أمي")
        self.assertEqual(sf.JOBS[data["job"]]["mode"], "fast")
        self.assertEqual(data["dna"]["locked"], ["who", "open"])
        job = data["job"]
        for _ in range(200):                     # العامل في خيط آخر
            j = self.c.get("/write/" + job).get_json()
            if j["stage"] in ("done", "error"):
                break
            import time; time.sleep(0.02)
        self.assertEqual(j["stage"], "done", j.get("error"))
        self.assertNotIn("at", j)
        stream = self.c.get("/write/" + job + "/stream").get_data(as_text=True)
        events = [json.loads(ln[5:]) for ln in stream.split("\n") if ln.startswith("data:")]
        self.assertTrue(events[-1].get("final"))
        self.assertEqual(events[-1]["state"]["stage"], "done")
        self.assertTrue(events[-1]["text"].startswith("دفعت"))
        self.assertEqual(self.c.get("/write/nope/stream").status_code, 404)

    def test_cancel_route(self):
        sf.JOBS["j1"] = {"stage": "draft", "text": "", "at": 0}
        self.assertEqual(self.c.post("/write/j1/cancel").get_json(), {"ok": True})
        self.assertTrue(sf.JOBS["j1"]["cancel"])
        self.assertEqual(self.c.post("/write/zz/cancel").status_code, 404)

    def test_library_roundtrip_and_export(self):
        r = self.c.post("/library", json={"text": STORY, "dna": {"who": "أمي"}, "plot": "قرض",
                                          "facts": ["القرض 40 ألف"], "score": 90,
                                          "core": "betrayal", "format": "short"})
        self.assertEqual(r.get_json()["count"], 1)
        items = self.c.get("/library").get_json()["items"]
        self.assertEqual(items[0]["score"], 90)
        self.assertEqual(items[0]["meta"]["core"], "betrayal")
        md = self.c.get("/library/export?fmt=md").get_data(as_text=True)
        self.assertIn("خيانة ثقة", md)
        self.assertIn("القرض 40 ألف", md)
        txt = self.c.get("/library/export").get_data(as_text=True)
        self.assertTrue(txt.startswith("دفعت"))
        self.assertEqual(self.c.post("/library", json={"text": "x"}).status_code, 400)
        r = self.c.delete("/library/" + items[0]["id"])
        self.assertEqual(r.get_json()["count"], 0)

    def test_hooks_route(self):
        sf.chat = FakeProvider(hooks=["أ ب ج د", "هـ و ز ح"])
        r = self.c.post("/hooks", json={"text": STORY})
        self.assertEqual(r.get_json()["hooks"], ["أ ب ج د", "هـ و ز ح"])
        self.assertEqual(self.c.post("/hooks", json={"text": "قصير"}).status_code, 400)




class CoreRelevance(unittest.TestCase):
    def test_every_core_maps_to_valid_who_tags(self):
        for core in sf.CORES:
            self.assertIn(core, sf.CORE_WHO, core)
            for tag in sf.CORE_WHO[core]:
                self.assertIn(tag, sf.WHO_TAGS, (core, tag))

    def test_counterpart_follows_the_core(self):
        for _ in range(40):
            self.assertIn(sf.fresh_dna("", "neighbors")["who"], sf.WHO_TAGS["neighbors"])
            self.assertIn(sf.fresh_dna("", "work")["who"], sf.WHO_TAGS["work"])
            self.assertIn(sf.fresh_dna("", "siblings")["who"], sf.WHO_TAGS["family"])
        self.assertEqual(sf.fresh_dna("", "work", {"who": "أمي"})["who"], "أمي")   # المثبّت يبقى

    def test_prompts_put_the_core_first(self):
        dna = sf.fresh_dna("", "neighbors")
        p = sf.premise_prompt(dna, "neighbors", "big", [])
        self.assertTrue(p.startswith("نوع الموقف المطلوب، وهو الحاكم"))
        self.assertIn("بين الجيران", p)
        idea = {"hook": "", "hidden": "x", "why_hidden": "", "why_now": "", "evidence": "", "motive": "", "facts": []}
        w = sf.write_prompt(dna, idea, "short", "saudi", "neighbors", "self", "big")
        self.assertIn("نوع الموقف الحاكم: بين الجيران", w)
        self.assertIn("بين الجيران", sf.audit_prompt([], "closer", "neighbors"))


if __name__ == "__main__":
    unittest.main()
