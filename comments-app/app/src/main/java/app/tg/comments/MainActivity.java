package app.tg.comments;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Random;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** One page: the settings (Claude's key, the model, the reply's voice), a box for the tweets' links, «إرسال»,
 * and a card for each tweet with its comment and «نسخ». */
public class MainActivity extends Activity {
    // The groups script's colors: light paper, pine green, brick red.
    static final int PAPER = 0xFFF3EFE6;
    static final int SHEET = 0xFFFFFDF8;
    static final int SUNK = 0xFFF8F4EC;
    static final int INK = 0xFF1C1915;
    static final int MUTED = 0xFF6E675E;
    static final int FAINT = 0xFFA59D91;
    static final int LINE = 0xFFE4DCD0;
    static final int PINE = 0xFF0F6B50;
    static final int ON_PINE = 0xFFF4FBF7;
    static final int CLAY = 0xFF8D3A32;
    static final String[] VOICE_LABELS = {"مذكر", "مؤنث", "عام"};
    static final int PARALLEL = 3;
    static final int REMEMBERED = 8;

    SharedPreferences prefs;
    EditText keyField;
    EditText modelField;
    EditText tweetsField;
    RadioGroup voiceGroup;
    final int[] voiceIds = new int[3];
    TextView keyNow;
    TextView settingsNote;
    TextView settingsSummary;
    TextView notice;
    LinearLayout settingsBody;
    LinearLayout results;
    Button settingsToggle;
    Button sendButton;
    final List<Card> cards = new ArrayList<>();
    final Handler main = new Handler(Looper.getMainLooper());
    final Random rng = new SecureRandom();
    // This phone's last replies: the next one is told not to look like them.
    final List<String> lastReplies = new ArrayList<>();
    ExecutorService pool;
    int running;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("settings", MODE_PRIVATE);
        if (!prefs.contains("manner")) {
            prefs.edit().putInt("manner", rng.nextInt(Engine.MANNERS.length)).apply();
        }
        pool = Executors.newFixedThreadPool(PARALLEL);
        setContentView(buildPage());
        refreshSettings();
        showSettings(!Engine.validKey(prefs.getString("key", "")));
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        pool.shutdownNow();
    }

    // ---------------------------------------------------------------- the page

    View buildPage() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(PAPER);
        scroll.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        LinearLayout page = column();
        page.setPadding(dp(16), dp(18), dp(16), dp(28));
        scroll.addView(page, new ScrollView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        page.addView(text("تعليقات", 26, INK, true));
        page.addView(text("الصق روابط التغريدات، ويكتب Claude تعليقًا لكل تغريدة.", 14, MUTED, false), margins(0, dp(2), 0, dp(14)));

        LinearLayout settings = card();
        page.addView(settings, margins(0, 0, 0, dp(12)));
        LinearLayout head = new LinearLayout(this);
        head.setGravity(Gravity.CENTER_VERTICAL);
        settings.addView(head);
        head.addView(text("الإعدادات", 17, INK, true), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        settingsToggle = ghostButton("تعديل");
        settingsToggle.setOnClickListener(v -> showSettings(settingsBody.getVisibility() != View.VISIBLE));
        head.addView(settingsToggle);
        settingsSummary = text("", 13, MUTED, false);
        settings.addView(settingsSummary, margins(0, dp(2), 0, 0));
        settingsBody = column();
        settings.addView(settingsBody);
        settingsBody.addView(label("مفتاح Claude"));
        keyField = field("sk-ant-…");
        keyField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        leftToRight(keyField);
        settingsBody.addView(keyField);
        keyNow = text("", 12, MUTED, false);
        settingsBody.addView(keyNow, margins(0, dp(4), 0, 0));
        settingsBody.addView(label("النموذج"));
        modelField = field(Engine.DEFAULT_MODEL);
        modelField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        leftToRight(modelField);
        settingsBody.addView(modelField);
        settingsBody.addView(label("صيغة الرد"));
        voiceGroup = new RadioGroup(this);
        voiceGroup.setOrientation(LinearLayout.HORIZONTAL);
        for (int i = 0; i < VOICE_LABELS.length; i++) {
            RadioButton choice = new RadioButton(this);
            voiceIds[i] = View.generateViewId();
            choice.setId(voiceIds[i]);
            choice.setText(VOICE_LABELS[i]);
            choice.setTextColor(INK);
            choice.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
            RadioGroup.LayoutParams place = new RadioGroup.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            place.setMarginEnd(dp(18));
            voiceGroup.addView(choice, place);
        }
        settingsBody.addView(voiceGroup);
        Button save = primaryButton("حفظ");
        save.setOnClickListener(v -> saveSettings());
        settingsBody.addView(save, margins(0, dp(12), 0, 0));
        settingsNote = text("", 13, CLAY, false);
        settingsNote.setVisibility(View.GONE);
        settingsBody.addView(settingsNote, margins(0, dp(6), 0, 0));

        LinearLayout tweets = card();
        page.addView(tweets, margins(0, 0, 0, dp(12)));
        tweets.addView(text("التغريدات", 17, INK, true));
        tweetsField = field("الصق روابط التغريدات هنا، رابط في كل سطر");
        tweetsField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        tweetsField.setSingleLine(false);
        tweetsField.setMinLines(4);
        tweetsField.setMaxLines(10);
        tweetsField.setGravity(Gravity.TOP | Gravity.START);
        tweets.addView(tweetsField, margins(0, dp(8), 0, 0));
        sendButton = primaryButton("إرسال");
        sendButton.setOnClickListener(v -> send());
        tweets.addView(sendButton, margins(0, dp(12), 0, 0));
        notice = text("", 13, CLAY, false);
        notice.setVisibility(View.GONE);
        tweets.addView(notice, margins(0, dp(6), 0, 0));

        results = column();
        page.addView(results);
        fitToScreen(scroll);
        return scroll;
    }

    /** From Android 15 the page goes under the status bar, the navigation bar and the keyboard: it keeps clear of them. */
    void fitToScreen(View root) {
        if (Build.VERSION.SDK_INT < 35) {
            return;
        }
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                | WindowInsets.Type.ime());
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return insets;
        });
    }

    // ---------------------------------------------------------------- the settings

    void refreshSettings() {
        String key = prefs.getString("key", "");
        String model = prefs.getString("model", Engine.DEFAULT_MODEL);
        int voice = Math.floorMod(prefs.getInt("voice", 0), VOICE_LABELS.length);
        boolean ready = Engine.validKey(key);
        keyNow.setText(ready ? "محفوظ: " + Engine.shown(key) : "لا يوجد مفتاح محفوظ.");
        modelField.setText(model);
        voiceGroup.check(voiceIds[voice]);
        settingsSummary.setText(ready ? "مفتاح Claude محفوظ · " + model + " · " + VOICE_LABELS[voice] : "أدخل مفتاح Claude لتبدأ.");
    }

    void showSettings(boolean open) {
        settingsBody.setVisibility(open ? View.VISIBLE : View.GONE);
        settingsToggle.setText(open ? "إغلاق" : "تعديل");
        settingsNote.setVisibility(View.GONE);
    }

    void saveSettings() {
        String key = keyField.getText().toString().trim();
        String model = modelField.getText().toString().trim().toLowerCase(Locale.ROOT);
        String current = prefs.getString("key", "");
        if (model.isEmpty()) {
            model = Engine.DEFAULT_MODEL;
        }
        if (key.isEmpty() && !Engine.validKey(current)) {
            settingsError("الصق مفتاح Claude.");
            return;
        }
        if (!key.isEmpty() && !Engine.validKey(key)) {
            settingsError("صيغة مفتاح Claude غير صالحة. يبدأ بـ sk-ant-");
            return;
        }
        if (!Engine.validModel(model)) {
            settingsError("اسم النموذج غير صالح.");
            return;
        }
        int voice = 0;
        for (int i = 0; i < voiceIds.length; i++) {
            if (voiceGroup.getCheckedRadioButtonId() == voiceIds[i]) {
                voice = i;
            }
        }
        prefs.edit().putString("key", key.isEmpty() ? current : key).putString("model", model).putInt("voice", voice).apply();
        keyField.setText("");
        hideKeyboard();
        refreshSettings();
        showSettings(false);
        Toast.makeText(this, "حُفظت الإعدادات.", Toast.LENGTH_SHORT).show();
    }

    void settingsError(String message) {
        settingsNote.setText(message);
        settingsNote.setVisibility(View.VISIBLE);
    }

    // ---------------------------------------------------------------- the comments

    void send() {
        notice.setVisibility(View.GONE);
        String key = prefs.getString("key", "");
        if (!Engine.validKey(key)) {
            showSettings(true);
            say("أدخل مفتاح Claude واضغط «حفظ» أولًا.");
            keyField.requestFocus();
            return;
        }
        List<String> ids = Engine.tweetIds(tweetsField.getText().toString());
        if (ids.isEmpty()) {
            say("لم أجد روابط تغريدات. الصق رابطًا في كل سطر.");
            return;
        }
        if (ids.size() > Engine.MAX_TWEETS) {
            ids = new ArrayList<>(ids.subList(0, Engine.MAX_TWEETS));
            say("تُكتب تعليقات لأول " + Engine.MAX_TWEETS + " تغريدة فقط.");
        }
        hideKeyboard();
        results.removeAllViews();
        cards.clear();
        String model = prefs.getString("model", Engine.DEFAULT_MODEL);
        int voice = prefs.getInt("voice", 0);
        int manner = prefs.getInt("manner", 0);
        running = ids.size();
        sendButton.setEnabled(false);
        sendButton.setAlpha(0.6f);
        sendButton.setText("يكتب التعليقات…");
        for (int i = 0; i < ids.size(); i++) {
            Card card = new Card(i + 1, ids.get(i));
            cards.add(card);
            results.addView(card.view, margins(0, 0, 0, dp(10)));
            pool.execute(() -> work(card, key, model, voice, manner));
        }
    }

    /** One tweet, in the background: read it, its photos, then its comment; the card shows each step. */
    void work(Card card, String key, String model, int voice, int manner) {
        try {
            Engine.Tweet tweet = Engine.readTweet(card.id);
            main.post(() -> card.showTweet(tweet));
            List<Engine.Photo> photos = Engine.downloadPhotos(tweet);
            List<String> before;
            synchronized (lastReplies) {
                before = new ArrayList<>(lastReplies);
            }
            String reply = Engine.comment(key, model, voice, manner, tweet, photos, before, rng);
            synchronized (lastReplies) {
                lastReplies.add(reply);
                while (lastReplies.size() > REMEMBERED) {
                    lastReplies.remove(0);
                }
            }
            main.post(() -> card.showReply(reply));
        } catch (Engine.CommentError e) {
            String message = e.getMessage();
            main.post(() -> card.showError(message));
        } catch (RuntimeException e) {
            String kind = e.getClass().getSimpleName();      // (named, so that it can be found and fixed)
            main.post(() -> card.showError("حدث خطأ غير متوقع (" + kind + "). أعد المحاولة."));
        } finally {
            main.post(this::oneDone);
        }
    }

    void oneDone() {
        running--;
        if (running <= 0) {
            running = 0;
            sendButton.setEnabled(true);
            sendButton.setAlpha(1f);
            sendButton.setText("إرسال");
        }
    }

    void say(String message) {
        notice.setText(message);
        notice.setVisibility(View.VISIBLE);
    }

    void copy(String text) {
        ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        clipboard.setPrimaryClip(ClipData.newPlainText("تعليق", text));
        Toast.makeText(this, "نُسخ التعليق.", Toast.LENGTH_SHORT).show();
    }

    /** A tweet's card: its number and author (opens the tweet), a line of its text, the comment and «نسخ». */
    final class Card {
        final int number;
        final String id;
        final LinearLayout view;
        final TextView title;
        final TextView words;
        final TextView reply;
        final Button copyButton;
        String url;
        String comment = "";

        Card(int number, String id) {
            this.number = number;
            this.id = id;
            this.url = "https://x.com/i/status/" + id;
            view = card();
            title = text("تغريدة " + number, 14, PINE, true);
            title.setOnClickListener(v -> open());
            view.addView(title);
            words = text("يقرأ التغريدة…", 13, MUTED, false);
            words.setMaxLines(2);
            words.setEllipsize(TextUtils.TruncateAt.END);
            view.addView(words, margins(0, dp(2), 0, 0));
            reply = text("…", 16, FAINT, false);
            reply.setTextIsSelectable(true);
            view.addView(reply, margins(0, dp(10), 0, 0));
            copyButton = ghostButton("نسخ");
            copyButton.setEnabled(false);
            copyButton.setAlpha(0.5f);
            copyButton.setOnClickListener(v -> {
                if (!comment.isEmpty()) {
                    copy(comment);
                    copyButton.setText("نُسخ ✓");
                }
            });
            LinearLayout actions = new LinearLayout(MainActivity.this);
            actions.addView(copyButton);
            view.addView(actions, margins(0, dp(10), 0, 0));
        }

        void showTweet(Engine.Tweet tweet) {
            url = tweet.url;
            // (\u200E: the account's name reads left to right, with its @ in front, inside the Arabic line)
            title.setText("تغريدة " + number + (tweet.author.isEmpty() ? "" : " · \u200E@" + tweet.author) + " ↗");
            String line = tweet.text.isEmpty() ? (tweet.photos.isEmpty() ? "(بلا نص)" : "(صورة بلا نص)") : tweet.text;
            words.setText(line.replaceAll("\\s+", " "));
            reply.setText("يكتب التعليق…");
        }

        void showReply(String text) {
            comment = text;
            reply.setText(text);
            reply.setTextColor(INK);
            copyButton.setEnabled(true);
            copyButton.setAlpha(1f);
        }

        void showError(String message) {
            if ("يقرأ التغريدة…".contentEquals(words.getText())) {
                words.setText("");
                words.setVisibility(View.GONE);
            }
            reply.setText(message);
            reply.setTextColor(CLAY);
            reply.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        }

        void open() {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
            } catch (ActivityNotFoundException e) {
                Toast.makeText(MainActivity.this, "لا يوجد تطبيق يفتح الرابط.", Toast.LENGTH_SHORT).show();
            }
        }
    }

    // ---------------------------------------------------------------- small view helpers

    int dp(float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }

    LinearLayout.LayoutParams margins(int left, int top, int right, int bottom) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.setMargins(left, top, right, bottom);
        return params;
    }

    LinearLayout column() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        return layout;
    }

    GradientDrawable shape(int fill, int stroke, int radius) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(radius));
        if (stroke != 0) {
            drawable.setStroke(dp(1), stroke);
        }
        return drawable;
    }

    LinearLayout card() {
        LinearLayout layout = column();
        layout.setBackground(shape(SHEET, LINE, 16));
        layout.setPadding(dp(14), dp(14), dp(14), dp(14));
        return layout;
    }

    TextView text(String value, float size, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, size);
        view.setTextColor(color);
        view.setLineSpacing(0, 1.15f);
        if (bold) {
            view.setTypeface(Typeface.DEFAULT_BOLD);
        }
        return view;
    }

    TextView label(String value) {
        TextView view = text(value, 13, MUTED, true);
        view.setPadding(0, dp(12), 0, dp(6));
        return view;
    }

    EditText field(String hint) {
        EditText view = new EditText(this);
        view.setHint(hint);
        view.setHintTextColor(FAINT);
        view.setTextColor(INK);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        view.setBackground(shape(SUNK, LINE, 10));
        view.setPadding(dp(12), dp(10), dp(12), dp(10));
        return view;
    }

    /** A key or a model name is written left to right (its input type already keeps it on one line, and the
     * key hidden). */
    void leftToRight(EditText view) {
        view.setTextDirection(View.TEXT_DIRECTION_LTR);
    }

    Button primaryButton(String value) {
        Button button = new Button(this);
        button.setText(value);
        button.setAllCaps(false);
        button.setTextColor(ON_PINE);
        button.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        button.setTypeface(Typeface.DEFAULT_BOLD);
        button.setBackground(shape(PINE, 0, 12));
        button.setStateListAnimator(null);
        button.setMinHeight(dp(48));
        return button;
    }

    Button ghostButton(String value) {
        Button button = new Button(this);
        button.setText(value);
        button.setAllCaps(false);
        button.setTextColor(INK);
        button.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        button.setBackground(shape(SHEET, LINE, 10));
        button.setStateListAnimator(null);
        button.setMinHeight(dp(38));
        button.setMinimumHeight(dp(38));
        button.setMinWidth(dp(76));
        button.setMinimumWidth(dp(76));
        button.setPadding(dp(14), 0, dp(14), 0);
        return button;
    }

    void hideKeyboard() {
        View focus = getCurrentFocus();
        if (focus != null) {
            InputMethodManager keyboard = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
            keyboard.hideSoftInputFromWindow(focus.getWindowToken(), 0);
            focus.clearFocus();
        }
    }
}
