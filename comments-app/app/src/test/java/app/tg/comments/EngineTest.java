package app.tg.comments;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Random;

/** The tweets read without a browser, what Claude is asked, and the reply as it is posted. */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35)
public class EngineTest {
    Fakes fakes;
    final Random rng = new Random(7);

    @Before
    public void start() throws Exception {
        fakes = new Fakes();
    }

    @After
    public void stop() {
        fakes.stop();
    }

    Engine.Tweet first() throws Exception {
        return Engine.readTweet("1900000000000000001");
    }

    String comment(Engine.Tweet tweet, List<Engine.Photo> photos) throws Exception {
        return Engine.comment(Fakes.CLAUDE_KEY, "claude-haiku-5-5", 0, 2, tweet, photos, Collections.emptyList(), rng);
    }

    @Test
    public void linksInPastedText() {
        String pasted = "شوف هذي https://x.com/someone/status/1900000000000000001?s=20\n"
            + "twitter.com/i/web/status/1900000000000000002 و https://fxtwitter.com/a/status/1900000000000000003\n"
            + "1900000000000000004 مرة ثانية https://x.com/someone/status/1900000000000000001 رقم 12345 hello";
        assertEquals(Arrays.asList("1900000000000000001", "1900000000000000002", "1900000000000000003", "1900000000000000004"),
            Engine.tweetIds(pasted));
        assertTrue(Engine.tweetIds("مرحبا 12345 https://x.com/someone").isEmpty());
    }

    @Test
    public void readsTheTweetWithoutABrowser() throws Exception {
        Engine.Tweet tweet = first();
        assertEquals("someone", tweet.author);
        assertEquals("https://x.com/someone/status/1900000000000000001", tweet.url);
        assertEquals("الزحمة اليوم في طريق الملك فهد ما لها حل", tweet.text);
        assertEquals(1, tweet.photos.size());                          // (only the tweet's own photos)
        Engine.Tweet quoted = Engine.readTweet("1900000000000000002");
        assertEquals("الأسعار ارتفعت هذا الشهر", quoted.quote);
        assertTrue(quoted.photos.get(0).endsWith("/media/v.jpg"));    // a video's picture
        assertTrue(Engine.tweetWords(quoted).endsWith("(It quotes: الأسعار ارتفعت هذا الشهر)"));
        for (String gone : new String[] {"1900000000000000404", "1900000000000000003"}) {
            try {
                Engine.readTweet(gone);
                fail("read a tweet that is not there");
            } catch (Engine.CommentError e) {
                assertEquals("التغريدة غير موجودة أو حسابها خاص.", e.getMessage());
            }
        }
        try {
            Engine.readTweet("1900000000000000429");
            fail("read while FxTwitter is busy");
        } catch (Engine.CommentError e) {
            assertTrue(e.getMessage().startsWith("خدمة قراءة التغريدات مشغولة"));
        }
    }

    @Test
    public void photosInAMediumSize() throws Exception {
        List<Engine.Photo> photos = Engine.downloadPhotos(first());
        assertEquals(1, photos.size());
        assertEquals("image/jpeg", photos.get(0).mime);
        assertTrue(Arrays.equals(Fakes.JPEG, photos.get(0).data));
        assertEquals(Collections.singletonList("/media/a.jpg?name=medium"), fakes.photoPaths);
    }

    @Test
    public void claudeIsAskedLikeTheGroupsScript() throws Exception {
        Engine.Tweet tweet = first();
        String reply = comment(tweet, Engine.downloadPhotos(tweet));
        assertEquals("والله الزحمة صارت جزء من يومنا وما لها حل قريب أبدا", reply);   // no diacritics, no commas
        assertEquals(1, fakes.asked.size());
        Fakes.Asked asked = fakes.asked.get(0);
        assertEquals(Fakes.CLAUDE_KEY, asked.headers.get("x-api-key"));
        assertEquals("2026-10-21", asked.headers.get("anthropic-version"));
        assertTrue(asked.headers.get("content-type").startsWith("application/json"));
        assertEquals("claude-haiku-5-5", asked.body.getString("model"));
        assertEquals(2048, asked.body.getInt("max_tokens"));
        assertEquals("medium", asked.body.getJSONObject("output_config").getString("effort"));
        assertEquals(1, asked.images());
        String text = asked.text();
        assertTrue(text.startsWith("Look at the tweet's photos too. Write one public reply to this tweet"));
        assertTrue(text.contains("Not personal: the reply is about the tweet's topic, not about you."));
        assertTrue(text.contains("صار معي، جربت، شفت بنفسي، أخوي، عندنا في البيت، أتذكر لما، لو كنت مكانه"));
        assertTrue(text.contains("everyday spoken Saudi Arabic (عامية سعودية بيضاء)"));
        assertTrue(text.contains("At most 100 characters, spaces included"));
        assertTrue(text.contains("no diacritics (no تشكيل") && text.contains("no commas (، or ,)"));
        assertTrue(text.contains(Engine.VOICE_RULES[0]));
        assertTrue(text.contains("Your manner (always the same): " + Engine.MANNERS[2] + "."));
        assertTrue(Arrays.stream(Engine.ANGLES).anyMatch(angle -> text.contains("This time: " + angle + ".")));
        assertTrue(text.contains("The photos are the tweet's own: never mention them"));
        assertTrue(text.endsWith("\n\nTweet text:\nالزحمة اليوم في طريق الملك فهد ما لها حل"));
    }

    @Test
    public void theVoiceTheMannerAndTheLastReplies() {
        List<String> before = Arrays.asList("أول رد كتبته قبل شوي عن الأسعار", "ثاني رد عن الدوري");
        String p = Engine.prompt(1, 5, before, false, rng);
        assertTrue(p.contains(Engine.VOICE_RULES[1]));
        assertTrue(p.contains("Your manner (always the same): " + Engine.MANNERS[5] + "."));
        assertTrue(p.contains("Never mention the post or the tweet"));
        assertTrue(p.endsWith("other words):\n- أول رد كتبته قبل شوي عن الأسعار\n- ثاني رد عن الدوري"));
        assertTrue(Engine.prompt(2, 0, new ArrayList<>(), false, rng).contains(Engine.VOICE_RULES[2]));
        assertFalse(p.contains("personal opinion"));
    }

    @Test
    public void aLongReplyIsAskedAgainThenShortened() throws Exception {
        String longer = "الزحمة في طريق الملك فهد صارت شي يومي وما أحد يحسب حسابه لأن المشاريع ما خلصت والناس كلها طالعة في نفس الوقت للدوامات";
        fakes.reply = longer;
        String reply = comment(first(), Collections.emptyList());
        assertEquals(2, fakes.asked.size());
        assertTrue(reply.length() <= Engine.MAX_CHARS);
        assertTrue(longer.startsWith(reply));
        assertFalse(Engine.HANGING_WORDS.contains(reply.substring(reply.lastIndexOf(' ') + 1)));
    }

    @Test
    public void aCutOffReplyIsAskedAgain() throws Exception {
        fakes.answer(200, Fakes.claudeText("الزحمة اليوم و"));
        assertEquals("والله الزحمة صارت جزء من يومنا وما لها حل قريب أبدا", comment(first(), Collections.emptyList()));
        assertEquals(2, fakes.asked.size());
    }

    @Test
    public void aModelWithNoEffortSettingIsAskedWithoutIt() throws Exception {
        fakes.answer(400, Fakes.claudeError("invalid_request_error", "output_config.effort: not supported by this model"));
        Engine.Tweet tweet = Engine.readTweet("1900000000000000002");
        String reply = Engine.comment(Fakes.CLAUDE_KEY, "claude-old-model", 0, 0, tweet, Collections.emptyList(),
            Collections.emptyList(), rng);
        assertFalse(reply.isEmpty());
        assertEquals(2, fakes.asked.size());
        assertTrue(fakes.asked.get(0).body.has("output_config"));
        assertFalse(fakes.asked.get(1).body.has("output_config"));
        Engine.comment(Fakes.CLAUDE_KEY, "claude-old-model", 0, 0, tweet, Collections.emptyList(), Collections.emptyList(), rng);
        assertFalse(fakes.asked.get(2).body.has("output_config"));     // (not sent it again)
    }

    @Test
    public void aPhotoClaudeCannotReadIsLeftOut() throws Exception {
        fakes.answer(400, Fakes.claudeError("invalid_request_error", "Could not process image"));
        Engine.Tweet tweet = first();
        assertFalse(comment(tweet, Engine.downloadPhotos(tweet)).isEmpty());
        assertEquals(1, fakes.asked.get(0).images());
        assertEquals(0, fakes.asked.get(1).images());
    }

    @Test
    public void aBusyClaudeIsAskedAgain() throws Exception {
        fakes.answer(429, Fakes.claudeError("rate_limit_error", "Too many requests"));
        fakes.answer(529, Fakes.claudeError("overloaded_error", "Overloaded"));
        assertFalse(comment(first(), Collections.emptyList()).isEmpty());
        assertEquals(3, fakes.asked.size());
    }

    @Test
    public void whatStopsAComment() throws Exception {
        Object[][] cases = {
            {401, Fakes.claudeError("authentication_error", "invalid x-api-key"), "مفتاح Claude غير صالح أو بلا صلاحية. راجعه في «الإعدادات»."},
            {400, Fakes.claudeError("invalid_request_error", "Your credit balance is too low to access the Anthropic API."),
                "بلغ حساب Claude حد الصرف أو نفد رصيده."},
            {404, Fakes.claudeError("not_found_error", "model: claude-nope"), "النموذج غير موجود. راجع اسمه في «الإعدادات»."},
            {200, "{\"content\":[],\"stop_reason\":\"refusal\"}", "امتنع Claude عن التعليق على هذه التغريدة."},
        };
        Engine.Tweet tweet = first();
        for (Object[] c : cases) {
            fakes.answer((Integer) c[0], (String) c[1]);
            try {
                comment(tweet, Collections.emptyList());
                fail("no error for " + c[1]);
            } catch (Engine.CommentError e) {
                assertEquals(c[2], e.getMessage());
            }
        }
        fakes.answer(400, Fakes.claudeError("invalid_request_error", "bad key " + Fakes.CLAUDE_KEY + " here"));
        try {
            comment(tweet, Collections.emptyList());
            fail("no error");
        } catch (Engine.CommentError e) {
            assertEquals("رفض Claude الطلب: bad key … here", e.getMessage());    // (the key is never shown)
        }
        Engine.claudeUrl = "http://127.0.0.1:1/v1/messages";
        try {
            comment(tweet, Collections.emptyList());
            fail("no error");
        } catch (Engine.CommentError e) {
            assertEquals("تعذّر الاتصال بـ Claude. تأكد من الاتصال بالإنترنت.", e.getMessage());
        }
    }

    /** The tests run on the computer's Java, whose regex takes more than Android's (ICU): what Android refuses
     * (it throws IllegalArgumentException on a phone) is kept out of the app's code. */
    @Test
    public void noRegexFlagAndroidRefuses() throws Exception {
        for (String name : new String[] {"Engine.java", "MainActivity.java"}) {
            String source = new String(java.nio.file.Files.readAllBytes(
                java.nio.file.Paths.get("src/main/java/app/tg/comments/" + name)), java.nio.charset.StandardCharsets.UTF_8);
            for (String refused : new String[] {"UNICODE_CHARACTER_CLASS", "CANON_EQ", "(?U)"}) {
                assertFalse(name + " uses " + refused, source.contains(refused));
            }
        }
        assertEquals("ما أتوقع إنها بتخلص قريب", Engine.clean("Reply: ما أتوقع إنها بتخلص قريب"));
        assertEquals("ما أتوقع إنها بتخلص قريب", Engine.clean("تعليقي: ما أتوقع إنها بتخلص قريب"));
    }

    @Test
    public void theReplyAsItIsPosted() {
        assertEquals("الزحمة حلوة بس مو للكل", Engine.polish("الزحمةُ حلوةٌ، بس مو للكل"));
        assertEquals("السعر 1500 ريال وهذا كثير", Engine.polish("السعر 1,500 ريال, وهذا كثير"));
        assertEquals("ما أتوقع إنها بتخلص قريب", Engine.clean("الرد: «ما أتوقع إنها بتخلص قريب»"));
        assertTrue(Engine.isComplete("ما أتوقع إنها بتخلص قريب صراحة 😂"));
        assertFalse(Engine.isComplete("ما أتوقع إنها بتخلص قريب و"));
        assertFalse(Engine.isComplete("قصير جدا"));
        assertEquals("الزحمة صارت يومية", Engine.fit("الزحمة صارت يومية في الرياض", 22));
        assertTrue(Engine.validKey(Fakes.CLAUDE_KEY));
        assertFalse(Engine.validKey("xai-123456789012345678901234567890"));
        assertTrue(Engine.validModel("claude-sonnet-5-5"));
        assertFalse(Engine.validModel("Claude 5!"));
        assertEquals("sk-ant-…Zx_w", Engine.shown(Fakes.CLAUDE_KEY));
    }
}
