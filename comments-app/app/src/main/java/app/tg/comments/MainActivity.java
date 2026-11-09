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
import java.util.Random;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** One page: the settings (the comments server's link and the reply's voice), a box for the tweets' links,
 * «إرسال», and a card for each tweet with its comment and «نسخ». The Claude key is on the server: the phone keeps
 * only the server's link. */
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
    // X's app: a tweet opens there directly (in the browser when it is not on the phone).
    static final String X_APP = "com.twitter.android";
    static final int REMEMBERED = 8;
    // What the app kept before 2.0, Claude's key among them: removed from the phone when it opens.
    static final String[] OLD_SETTINGS = {"key", "model", "proxy"};

    SharedPreferences prefs;
    EditText linkField;
    EditText tweetsField;
    RadioGroup voiceGroup;
    final int[] voiceIds = new int[3];
    TextView linkNow;
    TextView settingsNote;
    TextView settingsSummary;
    TextView notice;
    LinearLayout settingsBody;
    LinearLayout results;
    Button settingsToggle;
    Button saveButton;
    Button sendButton;
    final List<Card> cards = new ArrayList<>();
    final Handler main = new Handler(Looper.getMainLooper());
    final Random rng = new SecureRandom();
    // This phone's last replies: the next one is told not to look like them.
    final List<String> lastReplies = new ArrayList<>();
    ExecutorService pool;
    ExecutorService checker;
    int running;
    int checking;                 // checks of the server's link under way
    int lastCheck;                // the last one started: an older one's answer is not shown

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("settings", MODE_PRIVATE);
        SharedPreferences.Editor edit = prefs.edit();
        for (String old : OLD_SETTINGS) {
            edit.remove(old);
        }
        if (!prefs.contains("manner")) {
            edit.putInt("manner", rng.nextInt(Engine.MANNERS));
        }
        edit.commit();                           // (written at once: no key is left on the phone)
        pool = Executors.newFixedThreadPool(PARALLEL);
        checker = Executors.newSingleThreadExecutor();
        setContentView(buildPage());
        refreshSettings();
        showSettings(link() == null);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        pool.shutdownNow();
        checker.shutdownNow();
    }

    /** The server's link kept in the settings, or null. */
    Engine.ServerLink link() {
        return Engine.parseLink(prefs.getString("server", ""));
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
        settingsBody.addView(label("رابط السيرفر"));
        linkField = field("https://…:8443/…#…");
        linkField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        leftToRight(linkField);
        settingsBody.addView(linkField);
        linkNow = text("", 12, MUTED, false);
        settingsBody.addView(linkNow, margins(0, dp(4), 0, 0));
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
        saveButton = primaryButton("حفظ");
        saveButton.setOnClickListener(v -> saveSettings());
        settingsBody.addView(saveButton, margins(0, dp(12), 0, 0));
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
        Engine.ServerLink link = link();
        int voice = Math.floorMod(prefs.getInt("voice", 0), VOICE_LABELS.length);
        voiceGroup.check(voiceIds[voice]);
        linkNow.setText(link == null ? "لا يوجد رابط محفوظ. يعطيك إياه صاحب السيرفر، ولا يُحفظ في الهاتف أي مفتاح."
            : "محفوظ: " + link.shown());
        settingsSummary.setText(link == null ? "الصق رابط السيرفر لتبدأ." : "السيرفر " + link.shown() + " · " + VOICE_LABELS[voice]);
    }

    void showSettings(boolean open) {
        settingsBody.setVisibility(open ? View.VISIBLE : View.GONE);
        settingsToggle.setText(open ? "إغلاق" : "تعديل");
        settingsNote.setVisibility(View.GONE);
    }

    /** The link (a new one pasted, or the one kept) and the voice are kept, then the server is asked whether it
     * answers with that link. */
    void saveSettings() {
        String pasted = linkField.getText().toString().trim();
        Engine.ServerLink link = pasted.isEmpty() ? link() : Engine.parseLink(pasted);
        if (link == null) {
            settingsError(pasted.isEmpty() ? "الصق رابط السيرفر."
                : "الرابط غير صالح. الصقه كاملًا كما طبعه السيرفر: يبدأ بـ https:// وفي آخره # ثم أرقام وحروف.");
            return;
        }
        int voice = 0;
        for (int i = 0; i < voiceIds.length; i++) {
            if (voiceGroup.getCheckedRadioButtonId() == voiceIds[i]) {
                voice = i;
            }
        }
        prefs.edit().putString("server", link.text()).putInt("voice", voice).apply();
        linkField.setText("");
        hideKeyboard();
        refreshSettings();
        check(link);
    }

    void check(Engine.ServerLink link) {
        int number = ++lastCheck;
        checking++;
        saveButton.setEnabled(false);
        saveButton.setAlpha(0.6f);
        settingsNote.setText("حُفظت الإعدادات. يتحقق من الاتصال بالسيرفر…");
        settingsNote.setTextColor(MUTED);
        settingsNote.setVisibility(View.VISIBLE);
        checker.execute(() -> {
            String model = null;
            String problem = null;
            try {
                model = Engine.ping(link);
            } catch (Engine.CommentError e) {
                problem = e.getMessage();
            } catch (RuntimeException e) {
                problem = "حدث خطأ غير متوقع (" + e.getClass().getSimpleName() + "). أعد المحاولة.";
            }
            String answered = model;
            String failed = problem;
            main.post(() -> checked(number, answered, failed));
        });
    }

    void checked(int number, String model, String problem) {
        checking--;
        if (number != lastCheck) {
            return;
        }
        saveButton.setEnabled(true);
        saveButton.setAlpha(1f);
        if (problem != null) {
            showSettings(true);
            settingsError("حُفظ الرابط، لكن: " + problem);
            return;
        }
        showSettings(false);
        Toast.makeText(this, "✓ متصل بالسيرفر" + (model == null || model.isEmpty() ? "" : " · النموذج " + model),
            Toast.LENGTH_LONG).show();
    }

    void settingsError(String message) {
        settingsNote.setText(message);
        settingsNote.setTextColor(CLAY);
        settingsNote.setVisibility(View.VISIBLE);
    }

    // ---------------------------------------------------------------- the comments

    void send() {
        notice.setVisibility(View.GONE);
        Engine.ServerLink link = link();
        if (link == null) {
            showSettings(true);
            say("الصق رابط السيرفر واضغط «حفظ» أولًا.");
            linkField.requestFocus();
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
        tweetsField.setText("");                 // the box is emptied, and the last comments give way to these
        results.removeAllViews();
        cards.clear();
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
            pool.execute(() -> work(card, link, voice, manner));
        }
    }

    /** One tweet, in the background: the server reads it, then writes its comment; the card shows each step. */
    void work(Card card, Engine.ServerLink link, int voice, int manner) {
        try {
            Engine.Tweet tweet = Engine.readTweet(link, card.id);
            main.post(() -> card.showTweet(tweet));
            List<String> before;
            synchronized (lastReplies) {
                before = new ArrayList<>(lastReplies);
            }
            String reply = Engine.comment(link, card.id, voice, manner, before);
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
                    copy(comment);                       // copied, then the tweet opened to paste it there
                    copyButton.setText("نُسخ ✓");
                    open();
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
            String line = tweet.text.isEmpty() ? (tweet.photos == 0 ? "(بلا نص)" : "(صورة بلا نص)") : tweet.text;
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

        /** The tweet, in X's app when it is on the phone, else wherever links open (the browser). */
        void open() {
            Intent inX = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            inX.setPackage(X_APP);
            try {
                startActivity(inX);
                return;
            } catch (ActivityNotFoundException e) {
                // (X's app is not here)
            }
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

    /** A link is written left to right (its input type already keeps it on one line). */
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
