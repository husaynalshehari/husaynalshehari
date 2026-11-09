package app.tg.comments;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.robolectric.Shadows.shadowOf;

import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Looper;
import android.view.View;
import android.widget.RadioButton;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;
import org.robolectric.shadows.ShadowToast;

/** The page itself: the settings, «إرسال», a card for each tweet with its comment, and «نسخ». */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35)
public class MainActivityTest {
    Fakes fakes;
    MainActivity page;

    @Before
    public void start() throws Exception {
        fakes = new Fakes();
        page = Robolectric.buildActivity(MainActivity.class).setup().get();
    }

    @After
    public void stop() {
        fakes.stop();
    }

    void waitForCards() throws InterruptedException {
        long end = System.currentTimeMillis() + 20_000;
        do {
            Thread.sleep(20);
            shadowOf(Looper.getMainLooper()).idle();
        } while (page.running > 0 && System.currentTimeMillis() < end);
        assertEquals(0, page.running);
    }

    void saveKey() {
        page.keyField.setText(Fakes.CLAUDE_KEY);
        page.modelField.setText("claude-sonnet-5-5");
        page.voiceGroup.check(page.voiceIds[1]);
        clickSave();
    }

    void clickSave() {
        // «حفظ» is the first button inside the settings.
        for (int i = 0; i < page.settingsBody.getChildCount(); i++) {
            View child = page.settingsBody.getChildAt(i);
            if (child instanceof android.widget.Button) {
                child.performClick();
                return;
            }
        }
        throw new AssertionError("no «حفظ»");
    }

    @Test
    public void firstOpenAsksForTheKey() {
        assertEquals(View.VISIBLE, page.settingsBody.getVisibility());
        assertEquals("أدخل مفتاح Claude لتبدأ.", page.settingsSummary.getText().toString());
        assertEquals("لا يوجد مفتاح محفوظ.", page.keyNow.getText().toString());
        assertEquals("claude-haiku-5-5", page.modelField.getText().toString());
        // The key stays hidden while it is typed, on one line, left to right.
        assertTrue(page.keyField.getTransformationMethod() instanceof android.text.method.PasswordTransformationMethod);
        assertEquals(1, page.keyField.getMaxLines());
        assertEquals(View.TEXT_DIRECTION_LTR, page.keyField.getTextDirection());
        assertTrue(((RadioButton) page.findViewById(page.voiceIds[0])).isChecked());
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        assertEquals("أدخل مفتاح Claude واضغط «حفظ» أولًا.", page.notice.getText().toString());
        assertTrue(page.cards.isEmpty());
        assertTrue(fakes.asked.isEmpty());
    }

    @Test
    public void settingsAreCheckedAndKept() {
        page.keyField.setText("xai-123456789012345678901234567890");
        clickSave();
        assertEquals(View.VISIBLE, page.settingsNote.getVisibility());
        assertEquals("صيغة مفتاح Claude غير صالحة. يبدأ بـ sk-ant-", page.settingsNote.getText().toString());
        saveKey();
        assertEquals(Fakes.CLAUDE_KEY, page.prefs.getString("key", ""));
        assertEquals("claude-sonnet-5-5", page.prefs.getString("model", ""));
        assertEquals(1, page.prefs.getInt("voice", -1));
        assertEquals("", page.keyField.getText().toString());
        assertEquals("محفوظ: sk-ant-…Zx_w", page.keyNow.getText().toString());
        assertEquals("مفتاح Claude محفوظ · claude-sonnet-5-5 · مؤنث", page.settingsSummary.getText().toString());
        assertEquals(View.GONE, page.settingsBody.getVisibility());
        assertEquals("حُفظت الإعدادات.", ShadowToast.getTextOfLatestToast());
        // Opened again: the key is kept without typing it again.
        MainActivity again = Robolectric.buildActivity(MainActivity.class).setup().get();
        assertEquals(View.GONE, again.settingsBody.getVisibility());
        assertEquals("محفوظ: sk-ant-…Zx_w", again.keyNow.getText().toString());
        assertTrue(((RadioButton) again.findViewById(again.voiceIds[1])).isChecked());
        again.modelField.setText("claude-haiku-5-5");
        again.settingsToggle.performClick();
        assertEquals(View.VISIBLE, again.settingsBody.getVisibility());
        for (int i = 0; i < again.settingsBody.getChildCount(); i++) {
            if (again.settingsBody.getChildAt(i) instanceof android.widget.Button) {
                again.settingsBody.getChildAt(i).performClick();
                break;
            }
        }
        assertEquals(Fakes.CLAUDE_KEY, again.prefs.getString("key", ""));
        assertEquals("claude-haiku-5-5", again.prefs.getString("model", ""));
    }

    @Test
    public void aCommentForEachTweetWithCopy() throws Exception {
        saveKey();
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001\nكلام بلا رابط\n"
            + "https://twitter.com/other/status/1900000000000000002?s=20\n1900000000000000404\n"
            + "https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        assertFalse(page.sendButton.isEnabled());
        assertEquals("يكتب التعليقات…", page.sendButton.getText().toString());
        assertEquals(3, page.cards.size());
        assertEquals(3, page.results.getChildCount());
        waitForCards();
        assertTrue(page.sendButton.isEnabled());
        assertEquals("إرسال", page.sendButton.getText().toString());

        MainActivity.Card one = page.cards.get(0);
        assertEquals("تغريدة 1 · \u200E@someone ↗", one.title.getText().toString());
        assertEquals("الزحمة اليوم في طريق الملك فهد ما لها حل", one.words.getText().toString());
        assertEquals("والله الزحمة صارت جزء من يومنا وما لها حل قريب أبدا", one.reply.getText().toString());
        assertTrue(one.copyButton.isEnabled());
        MainActivity.Card two = page.cards.get(1);
        assertEquals("تغريدة 2 · \u200E@other ↗", two.title.getText().toString());
        assertTrue(two.copyButton.isEnabled());
        MainActivity.Card gone = page.cards.get(2);
        assertEquals("التغريدة غير موجودة أو حسابها خاص.", gone.reply.getText().toString());
        assertFalse(gone.copyButton.isEnabled());

        assertEquals(2, fakes.asked.size());
        for (Fakes.Asked asked : fakes.asked) {
            assertEquals("claude-sonnet-5-5", asked.body.getString("model"));
            assertTrue(asked.text().contains(Engine.VOICE_RULES[1]));
            assertTrue(asked.text().contains("Your manner (always the same): " + Engine.MANNERS[page.prefs.getInt("manner", -1)] + "."));
        }

        one.copyButton.performClick();
        ClipboardManager clipboard = (ClipboardManager) page.getSystemService(Context.CLIPBOARD_SERVICE);
        assertEquals("والله الزحمة صارت جزء من يومنا وما لها حل قريب أبدا", clipboard.getPrimaryClip().getItemAt(0).getText().toString());
        assertEquals("نُسخ التعليق.", ShadowToast.getTextOfLatestToast());
        assertEquals("نُسخ ✓", one.copyButton.getText().toString());

        one.title.performClick();
        Intent opened = shadowOf(page).getNextStartedActivity();
        assertEquals(Intent.ACTION_VIEW, opened.getAction());
        assertEquals("https://x.com/someone/status/1900000000000000001", opened.getData().toString());

        // Sent again: the cards start over, and the next replies are told not to look like these.
        page.tweetsField.setText("https://x.com/other/status/1900000000000000002");
        page.sendButton.performClick();
        waitForCards();
        assertEquals(1, page.cards.size());
        assertTrue(fakes.asked.get(2).text().contains("Your own last replies to other tweets."));
    }

    @Test
    public void nothingToSend() {
        saveKey();
        page.tweetsField.setText("مرحبا 12345 https://x.com/someone");
        page.sendButton.performClick();
        assertEquals("لم أجد روابط تغريدات. الصق رابطًا في كل سطر.", page.notice.getText().toString());
        assertEquals(View.VISIBLE, page.notice.getVisibility());
        assertTrue(page.cards.isEmpty());
        assertTrue(page.sendButton.isEnabled());
    }

    @Test
    public void anErrorFromClaudeShowsOnItsCard() throws Exception {
        saveKey();
        fakes.answer(401, Fakes.claudeError("authentication_error", "invalid x-api-key"));
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001");
        page.sendButton.performClick();
        waitForCards();
        MainActivity.Card card = page.cards.get(0);
        assertEquals("الزحمة اليوم في طريق الملك فهد ما لها حل", card.words.getText().toString());
        assertEquals("مفتاح Claude غير صالح أو بلا صلاحية. راجعه في «الإعدادات».", card.reply.getText().toString());
        assertFalse(card.copyButton.isEnabled());
    }
}
