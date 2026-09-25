# -*- coding: utf-8 -*-
"""
اختبارات بلا شبكة ولا مزوّد: يُستبدل chat بمزوّد وهمي يحاكي ردود النموذج.
    python -m unittest test_story_forge -v
"""

import os
import json
import tempfile
import unittest

os.environ["STORY_LIB"] = os.path.join(tempfile.mkdtemp(), "stories.json")

import story_forge as sf  # noqa: E402  (بعد ضبط مسار المكتبة)

STORY = "\n\n".join([
    "دفعت حساب القهوة وطلعت.",
    "وقفت عند الباب.",
    "ورقة عليها اسمي مو لي.",
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


class FakeProvider:
    """مزوّد وهمي: يقرر الرد من محتوى الطلب، ويسجّل ما نُودي به."""

    def __init__(self, story=STORY, audit_text=None, polish_text=None, hooks=None,
                 ideas=IDEAS):
        self.story, self.audit_text, self.polish_text = story, audit_text, polish_text
        self.hooks, self.ideas, self.calls = hooks, ideas, []

    def __call__(self, messages, provider, creds, on_token=None, temperature=0.95, timeout=240):
        user = messages[-1]["content"]
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

    def test_third_person_leak_is_reported(self):
        fake = FakeProvider()
        sf.chat = fake
        job = new_job()
        sf.write_story(job, sf.fresh_dna("", "loss"), "short", "saudi", "loss", "third", "mid", "free", {})
        pov = [c for c in job["checks"] if c["id"] == "pov"][0]
        self.assertFalse(pov["ok"])


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
        self.assertEqual(sorted(cfg["seeds"]), sorted(sf.SEED_KEYS))
        self.assertEqual(sum(len(g["items"]) for g in cfg["cores"]), len(sf.CORES))

    def test_write_rejects_private_provider_without_key(self):
        r = self.c.post("/write", json={"provider": "openai"})
        self.assertEqual(r.status_code, 400)

    def test_write_locks_seed_and_streams_to_done(self):
        sf.chat = FakeProvider()
        r = self.c.post("/write", json={"format": "short", "core": "betrayal",
                                        "seed": {"who": "أمي", "open": "number"}})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["dna"]["who"], "أمي")
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


if __name__ == "__main__":
    unittest.main()
