package app.tg.comments;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.robolectric.Shadows.shadowOf;

import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.os.Looper;
import android.view.View;
import android.widget.RadioButton;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import org.robolectric.shadows.ShadowToast;

/** The page itself, with the comments server behind it: the server's link in the settings, «إرسال», a card for
 * each tweet with its comment, and «نسخ». */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35)
public class MainActivityTest {
    static final String POSTED = "والله الزحمة صارت جزء من يومنا وما لها حل قريب أبدا";

    ServerProcess server;
    Fakes fakes;
    MainActivity page;

    @Before
    public void start() throws Exception {
        server = ServerProcess.shared();
        fakes = ServerProcess.fakes();
        page = Robolectric.buildActivity(MainActivity.class).setup().get();
    }

    static void waitFor(MainActivity on, java.util.function.BooleanSupplier done) throws InterruptedException {
        long end = System.currentTimeMillis() + 30_000;
        do {
            Thread.sleep(20);
            shadowOf(Looper.getMainLooper()).idle();
        } while (!done.getAsBoolean() && System.currentTimeMillis() < end);
        assertTrue("still waiting", done.getAsBoolean());
    }

    void waitForCards() throws InterruptedException {
        waitFor(page, () -> page.running == 0);
    }

    /** The link pasted and «حفظ» pressed, as its owner gives it; the check with the server is waited for. */
    void saveLink(MainActivity on, String link) throws InterruptedException {
        on.linkField.setText(link);
        on.voiceGroup.check(on.voiceIds[1]);
        on.saveButton.performClick();
        waitFor(on, () -> on.checking == 0);
    }

    @Test
    public void firstOpenAsksForTheLinkAndTheOldKeyIsGone() throws Exception {
        assertEquals(View.VISIBLE, page.settingsBody.getVisibility());
        assertEquals("الصق رابط السيرفر لتبدأ.", page.settingsSummary.getText().toString());
        assertEquals("لا يوجد رابط محفوظ. يعطيك إياه صاحب السيرفر، ولا يُحفظ في الهاتف أي مفتاح.", page.linkNow.getText().toString());
        // The link is one line, left to right.
        assertEquals(1, page.linkField.getMaxLines());
        assertEquals(View.TEXT_DIRECTION_LTR, page.linkField.getTextDirection());
        assertTrue(((RadioButton) page.findViewById(page.voiceIds[0])).isChecked());
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        assertEquals("الصق رابط السيرفر واضغط «حفظ» أولًا.", page.notice.getText().toString());
        assertTrue(page.cards.isEmpty());
        assertTrue(fakes.asked.isEmpty());

        // What 1.3 kept on the phone (Claude's key, the model, the proxy) is removed when 2.0 opens.
        SharedPreferences prefs = page.getSharedPreferences("settings", Context.MODE_PRIVATE);
        prefs.edit().putString("key", Fakes.CLAUDE_KEY).putString("model", "claude-sonnet-5-5")
            .putString("proxy", "http://tg:Secret123abc@1.2.3.4:8899").putInt("voice", 2).commit();
        MainActivity updated = Robolectric.buildActivity(MainActivity.class).setup().get();
        assertFalse(updated.prefs.contains("key"));
        assertFalse(updated.prefs.contains("model"));
        assertFalse(updated.prefs.contains("proxy"));
        assertEquals(2, updated.prefs.getInt("voice", -1));        // (the voice stays)
        assertTrue(updated.prefs.contains("manner"));
        for (String name : updated.prefs.getAll().keySet()) {
            assertTrue(name, name.equals("voice") || name.equals("manner"));
        }
    }

    @Test
    public void theLinkIsCheckedWithTheServerAndKept() throws Exception {
        page.linkField.setText("http://tg:Secret123abc@1.2.3.4:8899");
        page.saveButton.performClick();
        assertEquals(View.VISIBLE, page.settingsNote.getVisibility());
        assertEquals("الرابط غير صالح. الصقه كاملًا كما طبعه السيرفر: يبدأ بـ https:// وفي آخره # ثم أرقام وحروف.",
            page.settingsNote.getText().toString());
        assertEquals("", page.prefs.getString("server", ""));

        saveLink(page, " " + server.link + "\n");
        assertEquals(server.parsed().text(), page.prefs.getString("server", ""));
        assertEquals(1, page.prefs.getInt("voice", -1));
        assertEquals("", page.linkField.getText().toString());
        assertEquals("محفوظ: 127.0.0.1:" + server.port, page.linkNow.getText().toString());
        assertEquals("السيرفر 127.0.0.1:" + server.port + " · مؤنث", page.settingsSummary.getText().toString());
        assertEquals(View.GONE, page.settingsBody.getVisibility());
        assertEquals("✓ متصل بالسيرفر · النموذج claude-haiku-5-5", ShadowToast.getTextOfLatestToast());
        assertTrue(page.saveButton.isEnabled());

        // Opened again: the link is kept without pasting it again; «حفظ» alone checks it again.
        MainActivity again = Robolectric.buildActivity(MainActivity.class).setup().get();
        assertEquals(View.GONE, again.settingsBody.getVisibility());
        assertEquals("محفوظ: 127.0.0.1:" + server.port, again.linkNow.getText().toString());
        assertTrue(((RadioButton) again.findViewById(again.voiceIds[1])).isChecked());
        again.settingsToggle.performClick();
        assertEquals(View.VISIBLE, again.settingsBody.getVisibility());
        again.voiceGroup.check(again.voiceIds[2]);
        again.saveButton.performClick();
        assertEquals("حُفظت الإعدادات. يتحقق من الاتصال بالسيرفر…", again.settingsNote.getText().toString());
        assertFalse(again.saveButton.isEnabled());
        waitFor(again, () -> again.checking == 0);
        assertEquals(2, again.prefs.getInt("voice", -1));
        assertEquals(server.parsed().text(), again.prefs.getString("server", ""));
        assertEquals(View.GONE, again.settingsBody.getVisibility());

        // A link to a server that is down, or an old one: kept, and what is wrong is said.
        int closed = ServerProcess.freePort();
        Engine.ServerLink right = server.parsed();
        again.settingsToggle.performClick();
        saveLink(again, "https://127.0.0.1:" + closed + "/" + right.token + "#" + right.pin);
        assertEquals(View.VISIBLE, again.settingsBody.getVisibility());
        assertEquals("حُفظ الرابط، لكن: السيرفر رفض الاتصال على المنفذ " + closed + ": برنامج التعليقات متوقف، أو المنفذ "
            + "مغلق في جدار حمايته (على السيرفر: systemctl restart comments-server).", again.settingsNote.getText().toString());
        assertEquals("محفوظ: 127.0.0.1:" + closed, again.linkNow.getText().toString());
        saveLink(again, "https://127.0.0.1:" + server.port + "/" + "Zz".repeat(16) + "#" + right.pin);
        assertEquals("حُفظ الرابط، لكن: " + Engine.WRONG_SECRET, again.settingsNote.getText().toString());
        saveLink(again, server.link);
        assertEquals(View.GONE, again.settingsBody.getVisibility());
        assertEquals("✓ متصل بالسيرفر · النموذج claude-haiku-5-5", ShadowToast.getTextOfLatestToast());
    }

    @Test
    public void aCommentForEachTweetWithCopy() throws Exception {
        saveLink(page, server.link);
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001\nكلام بلا رابط\n"
            + "https://twitter.com/other/status/1900000000000000002?s=20\n1900000000000000404\n"
            + "https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        assertFalse(page.sendButton.isEnabled());
        assertEquals("يكتب التعليقات…", page.sendButton.getText().toString());
        assertEquals("", page.tweetsField.getText().toString());        // the box is emptied
        assertEquals(3, page.cards.size());
        assertEquals(3, page.results.getChildCount());
        waitForCards();
        assertTrue(page.sendButton.isEnabled());
        assertEquals("إرسال", page.sendButton.getText().toString());

        MainActivity.Card one = page.cards.get(0);
        assertEquals("تغريدة 1 · \u200E@someone ↗", one.title.getText().toString());
        assertEquals("الزحمة اليوم في طريق الملك فهد ما لها حل", one.words.getText().toString());
        assertEquals(POSTED, one.reply.getText().toString());
        assertTrue(one.copyButton.isEnabled());
        MainActivity.Card two = page.cards.get(1);
        assertEquals("تغريدة 2 · \u200E@other ↗", two.title.getText().toString());
        assertTrue(two.copyButton.isEnabled());
        MainActivity.Card gone = page.cards.get(2);
        assertEquals("التغريدة غير موجودة أو حسابها خاص.", gone.reply.getText().toString());
        assertFalse(gone.copyButton.isEnabled());

        assertEquals(2, fakes.asked.size());
        int manner = page.prefs.getInt("manner", -1);
        assertTrue(manner >= 0 && manner < Engine.MANNERS);
        for (Fakes.Asked asked : fakes.asked) {
            assertEquals("claude-haiku-5-5", asked.body.getString("model"));
            assertTrue(asked.text().contains(EngineTest.SHE));
            assertTrue(asked.text().contains("Your manner (always the same): " + EngineTest.MANNERS[manner] + "."));
        }

        // «نسخ»: the comment copied, then its tweet opened in X's app, to paste it there.
        one.copyButton.performClick();
        ClipboardManager clipboard = (ClipboardManager) page.getSystemService(Context.CLIPBOARD_SERVICE);
        assertEquals(POSTED, clipboard.getPrimaryClip().getItemAt(0).getText().toString());
        assertEquals("نُسخ التعليق.", ShadowToast.getTextOfLatestToast());
        assertEquals("نُسخ ✓", one.copyButton.getText().toString());
        Intent opened = shadowOf(page).getNextStartedActivity();
        assertEquals(Intent.ACTION_VIEW, opened.getAction());
        assertEquals("com.twitter.android", opened.getPackage());
        assertEquals("https://x.com/someone/status/1900000000000000001", opened.getData().toString());
        two.copyButton.performClick();
        assertEquals(POSTED, clipboard.getPrimaryClip().getItemAt(0).getText().toString());
        assertEquals("https://x.com/other/status/1900000000000000002", shadowOf(page).getNextStartedActivity().getData().toString());

        one.title.performClick();
        opened = shadowOf(page).getNextStartedActivity();
        assertEquals("com.twitter.android", opened.getPackage());
        assertEquals("https://x.com/someone/status/1900000000000000001", opened.getData().toString());

        // Sent again: the last comments give way to the new ones, and the new replies are told not to look like
        // the last ones.
        page.tweetsField.setText("https://x.com/other/status/1900000000000000002");
        page.sendButton.performClick();
        assertEquals("", page.tweetsField.getText().toString());
        assertEquals(1, page.results.getChildCount());
        waitForCards();
        assertEquals(1, page.cards.size());
        assertEquals(1, page.results.getChildCount());
        assertEquals("تغريدة 1 · \u200E@other ↗", page.cards.get(0).title.getText().toString());
        String third = fakes.asked.get(2).text();
        assertTrue(third.contains("Your own last replies to other tweets."));
        assertTrue(third.contains("\n- " + POSTED));
    }

    @Test
    public void withoutXsAppTheTweetOpensInTheBrowser() throws Exception {
        saveLink(page, server.link);
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        waitForCards();
        shadowOf(RuntimeEnvironment.getApplication()).checkActivities(true);   // (only what is on this phone opens)
        page.cards.get(0).copyButton.performClick();
        assertEquals("لا يوجد تطبيق يفتح الرابط.", ShadowToast.getTextOfLatestToast());
        ComponentName browser = new ComponentName("com.example.browser", "com.example.browser.Main");
        shadowOf(page.getPackageManager()).addActivityIfNotPresent(browser);
        IntentFilter links = new IntentFilter(Intent.ACTION_VIEW);
        links.addCategory(Intent.CATEGORY_DEFAULT);
        links.addDataScheme("https");
        shadowOf(page.getPackageManager()).addIntentFilterForActivity(browser, links);
        page.cards.get(0).copyButton.performClick();
        Intent opened = shadowOf(page).getNextStartedActivity();
        assertEquals(null, opened.getPackage());
        assertEquals("https://x.com/someone/status/1900000000000000001", opened.getData().toString());
        ClipboardManager clipboard = (ClipboardManager) page.getSystemService(Context.CLIPBOARD_SERVICE);
        assertEquals(POSTED, clipboard.getPrimaryClip().getItemAt(0).getText().toString());
    }

    @Test
    public void nothingToSend() throws Exception {
        saveLink(page, server.link);
        page.tweetsField.setText("مرحبا 12345 https://x.com/someone");
        page.sendButton.performClick();
        assertEquals("لم أجد روابط تغريدات. الصق رابطًا في كل سطر.", page.notice.getText().toString());
        assertEquals("مرحبا 12345 https://x.com/someone", page.tweetsField.getText().toString());   // kept, to be put right
        assertEquals(View.VISIBLE, page.notice.getVisibility());
        assertTrue(page.cards.isEmpty());
        assertTrue(page.sendButton.isEnabled());
    }

    @Test
    public void whatGoesWrongShowsOnItsCard() throws Exception {
        saveLink(page, server.link);
        fakes.answer(401, Fakes.claudeError("authentication_error", "invalid x-api-key"));
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        waitForCards();
        MainActivity.Card card = page.cards.get(0);
        assertEquals("الزحمة اليوم في طريق الملك فهد ما لها حل", card.words.getText().toString());
        assertEquals("مفتاح Claude في السيرفر غير صالح. غيّره على السيرفر: python3 comments_server.py --key",
            card.reply.getText().toString());
        assertFalse(card.copyButton.isEnabled());

        // The server stopped since the link was saved: each card says so.
        int closed = ServerProcess.freePort();
        Engine.ServerLink right = server.parsed();
        page.prefs.edit().putString("server", "https://127.0.0.1:" + closed + "/" + right.token + "#" + right.pin).commit();
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        waitForCards();
        card = page.cards.get(0);
        assertTrue(card.reply.getText().toString().startsWith("السيرفر رفض الاتصال على المنفذ " + closed));
        assertEquals(View.GONE, card.words.getVisibility());
        assertFalse(card.copyButton.isEnabled());
    }
}
