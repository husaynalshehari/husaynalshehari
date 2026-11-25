package app.tg.extract;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.MediaStore;
import android.provider.Settings;
import android.text.InputType;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/** The app's one page: the account, the task, the results and the log; and, on its other tab, the browser. */
public final class MainActivity extends Activity {
    private static final int BLUE = 0xFF1E3A8A;
    private static final int INK = 0xFF111827;
    private static final int MUTED = 0xFF6B7280;
    private static final int PAPER = 0xFFF3F4F6;
    private static final int LINE = 0xFFE5E7EB;
    private static final int RED = 0xFFB91C1C;
    private static final int GREEN = 0xFF047857;
    private static final int AMBER = 0xFFD97706;
    private static final int MATCH = ViewGroup.LayoutParams.MATCH_PARENT;
    private static final int WRAP = ViewGroup.LayoutParams.WRAP_CONTENT;
    private static final int ASK_NOTIFICATIONS = 7;
    private static final int ASK_STORAGE = 11;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final Runnable refresher = this::refresh;
    private final Runnable loginWatch = this::checkLogin;
    private final Map<String, CheckBox> countryBoxes = new LinkedHashMap<>();

    private Store store;
    private SharedPreferences prefs;
    private FrameLayout stage;
    private ScrollView panel;
    private View blocker;
    private TextView banner;
    private Button tabPanel;
    private Button tabBrowser;
    private TextView accountText;
    private Button loginButton;
    private Button tokenButton;
    private Button logoutButton;
    private EditText seedInput;
    private EditText targetInput;
    private EditText poolInput;
    private EditText perListInput;
    private RadioButton fromFollowers;
    private RadioButton fromFollowing;
    private Button startButton;
    private Button stopButton;
    private TextView progressText;
    private LinearLayout backgroundRow;
    private TextView backgroundText;
    private Button resetButton;
    private Button listButton;
    private Button editListButton;
    private TextView resultsText;
    private Button saveButton;
    private Button shareButton;
    private Button copyButton;
    private Button linksButton;
    private Button editResultsButton;
    private Button clearButton;
    private TextView collectedText;
    private Button collectedButton;
    private TextView logView;

    private boolean onBrowserTab;
    private boolean waitingLogin;
    private boolean waitingToken;
    private int homeTicks;
    private boolean askedBackground;
    private boolean batteryNext;
    private Runnable afterStoragePermission;

    // ---------------------------------------------------------------- life

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        store = Store.get(this);
        prefs = getSharedPreferences("extract", MODE_PRIVATE);
        drawBehindBars();
        setContentView(buildPage());
        Web.showIn(stage, this);
        Engine.listener = refresher;
        restoreInputs();
        showTab(false);
        refresh();
    }

    @Override
    protected void onStart() {
        super.onStart();
        Web.showIn(stage, this);
        refresh();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (batteryNext && Web.canOverlay(this)) {
            batteryNext = false;
            askBattery();                                       // (back from the overlay setting)
        }
        refresh();
    }

    @Override
    protected void onStop() {
        super.onStop();
        if (Engine.running) {
            Web.moveToOverlay(this);                            // (the task goes on at full speed meanwhile)
        }
    }

    @Override
    protected void onDestroy() {
        if (Engine.listener == refresher) {
            Engine.listener = null;
        }
        main.removeCallbacksAndMessages(null);
        if (!isChangingConfigurations()) {
            Web.release(this);
        }
        super.onDestroy();
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (onBrowserTab) {
            if (!Engine.running && Web.canGoBack()) {
                Web.goBack();
            } else {
                showTab(false);
            }
            return;
        }
        if (Engine.running) {
            moveTaskToBack(true);                               // (the task keeps going: the page is not closed)
            return;
        }
        super.onBackPressed();
    }

    // ---------------------------------------------------------------- the page

    /** The page draws under the status and navigation bars, and keeps its content clear of them and the keyboard. */
    @SuppressWarnings("deprecation")
    private void drawBehindBars() {
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
        } else {
            getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
    }

    @SuppressWarnings("deprecation")
    private View buildPage() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BLUE);
        root.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars()
                    | WindowInsets.Type.ime() | WindowInsets.Type.displayCutout());
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return WindowInsets.CONSUMED;
            }
            v.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets.consumeSystemWindowInsets();
        });

        LinearLayout bar = new LinearLayout(this);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(16), dp(8), dp(10), dp(8));
        bar.addView(text("المستخرج", 19, Color.WHITE, true), new LinearLayout.LayoutParams(0, WRAP, 1));
        tabPanel = tab("اللوحة");
        tabPanel.setOnClickListener(v -> showTab(false));
        tabBrowser = tab("المتصفح");
        tabBrowser.setOnClickListener(v -> showTab(true));
        LinearLayout.LayoutParams tabSize = new LinearLayout.LayoutParams(WRAP, dp(36));
        tabSize.setMarginStart(dp(6));
        bar.addView(tabPanel, tabSize);
        bar.addView(tabBrowser, new LinearLayout.LayoutParams(tabSize));
        root.addView(bar, new LinearLayout.LayoutParams(MATCH, WRAP));

        FrameLayout content = new FrameLayout(this);
        content.setBackgroundColor(PAPER);
        root.addView(content, new LinearLayout.LayoutParams(MATCH, 0, 1));

        // The browser's place (Web puts it first), with what covers it while a task works.
        stage = new FrameLayout(this);
        stage.setBackgroundColor(Color.WHITE);
        content.addView(stage, new FrameLayout.LayoutParams(MATCH, MATCH));
        blocker = new View(this);
        blocker.setClickable(true);
        blocker.setVisibility(View.GONE);
        stage.addView(blocker, new FrameLayout.LayoutParams(MATCH, MATCH));
        banner = text("⏳ المهمة تعمل الآن - المتصفح للمشاهدة فقط", 13, Color.WHITE, true);
        banner.setBackgroundColor(0xCC111827);
        banner.setGravity(Gravity.CENTER);
        banner.setPadding(dp(8), dp(6), dp(8), dp(6));
        banner.setVisibility(View.GONE);
        stage.addView(banner, new FrameLayout.LayoutParams(MATCH, WRAP, Gravity.TOP));

        panel = new ScrollView(this);
        panel.setBackgroundColor(PAPER);
        panel.setFillViewport(true);
        LinearLayout list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        list.setPadding(dp(12), dp(12), dp(12), dp(24));
        panel.addView(list, new FrameLayout.LayoutParams(MATCH, WRAP));
        content.addView(panel, new FrameLayout.LayoutParams(MATCH, MATCH));

        list.addView(accountCard());
        list.addView(taskCard());
        list.addView(resultsCard());
        list.addView(logCard());
        return root;
    }

    private LinearLayout accountCard() {
        LinearLayout card = card("الحساب");
        accountText = text("", 14, INK, true);
        add(card, accountText, 6);
        loginButton = primary("تسجيل الدخول");
        loginButton.setOnClickListener(v -> login());
        tokenButton = secondary("دخول بالتوكن");
        tokenButton.setOnClickListener(v -> askToken());
        logoutButton = danger("خروج");
        logoutButton.setOnClickListener(v -> logout());
        add(card, row(loginButton, tokenButton, logoutButton), 10);
        add(card, text("سجّل الدخول بالحساب الذي تريد أن يعمل منه الاستخراج. يبقى الدخول محفوظًا في التطبيق.", 12, MUTED, false), 8);
        return card;
    }

    private LinearLayout taskCard() {
        LinearLayout card = card("مهمة الاستخراج");
        add(card, text("ابدأ من حساب", 13, INK, true), 8);
        seedInput = input("يوزر أو رابط، مثل: vextrand",
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS | InputType.TYPE_TEXT_VARIATION_URI);
        add(card, seedInput, 4);
        add(card, text("اجمع من", 13, INK, true), 10);
        RadioGroup source = new RadioGroup(this);
        source.setOrientation(RadioGroup.VERTICAL);
        fromFollowers = radio("متابعيه الموثّقين (الذين يتابعون الحساب)");
        fromFollowing = radio("الذين يتابعهم الحساب (Following)");
        source.addView(fromFollowers);
        source.addView(fromFollowing);
        add(card, source, 2);
        add(card, text("عدد الحسابات الذهبية المطلوبة", 13, INK, true), 10);
        targetInput = input("100", InputType.TYPE_CLASS_NUMBER);
        add(card, targetInput, 4);
        add(card, text("حجم قائمة البحث (حسابات خليجية يُبحث في متابعيها)", 13, INK, true), 10);
        poolInput = input("50", InputType.TYPE_CLASS_NUMBER);
        add(card, poolInput, 4);
        add(card, text("الحد الأقصى للجمع من كل قائمة", 13, INK, true), 10);
        perListInput = input("2000", InputType.TYPE_CLASS_NUMBER);
        add(card, perListInput, 4);
        add(card, text("الدول", 13, INK, true), 10);
        LinearLayout grid = new LinearLayout(this);
        grid.setOrientation(LinearLayout.VERTICAL);
        LinearLayout line = null;
        for (int i = 0; i < Text.CODES.length; i++) {
            if (i % 3 == 0) {
                line = new LinearLayout(this);
                grid.addView(line, new LinearLayout.LayoutParams(MATCH, WRAP));
            }
            CheckBox box = new CheckBox(this);
            box.setText(Text.NAMES[i]);
            box.setTextColor(INK);
            box.setTextSize(14);
            box.setButtonTintList(ColorStateList.valueOf(BLUE));
            line.addView(box, new LinearLayout.LayoutParams(0, WRAP, 1));
            countryBoxes.put(Text.CODES[i], box);
        }
        add(card, grid, 2);
        add(card, text("يمرّر القائمة حتى نهايتها. يُفحص موقع كل الحسابات حتى تكتمل قائمة البحث، ثم يُفتح موقع "
            + "الحسابات الذهبية فقط، وكل حساب يُبحث فيه يُعوَّض بغيره فتبقى القائمة مكتملة. تُحفظ الحسابات الذهبية "
            + "فقط (الشركات والمنظمات).", 12, MUTED, false), 6);
        startButton = primary("▶  بدء");
        startButton.setOnClickListener(v -> startTask());
        stopButton = danger("■  إيقاف");
        stopButton.setOnClickListener(v -> Engine.stop());
        add(card, row(startButton, stopButton), 12);
        progressText = text("", 13, INK, false);
        progressText.setLineSpacing(dp(2), 1f);
        add(card, progressText, 10);

        backgroundRow = new LinearLayout(this);
        backgroundRow.setGravity(Gravity.CENTER_VERTICAL);
        backgroundRow.setBackground(round(0xFFFFFBEB, 0xFFF59E0B, 10));
        backgroundRow.setPadding(dp(10), dp(8), dp(10), dp(8));
        backgroundText = text("", 12, 0xFF92400E, false);
        backgroundRow.addView(backgroundText, new LinearLayout.LayoutParams(0, WRAP, 1));
        Button fix = button("ضبط", AMBER, Color.WHITE, 0);
        fix.setOnClickListener(v -> askBackground(true));
        LinearLayout.LayoutParams fixSize = new LinearLayout.LayoutParams(WRAP, WRAP);
        fixSize.setMarginStart(dp(8));
        backgroundRow.addView(fix, fixSize);
        add(card, backgroundRow, 10);

        editListButton = secondary("✏️  تعديل قائمة البحث");
        editListButton.setOnClickListener(v -> editSearchList());
        add(card, editListButton, 10);
        listButton = secondary("⬇  تنزيل يوزرات قائمة البحث");
        listButton.setOnClickListener(v -> saveSearchList());
        add(card, listButton, 10);
        resetButton = secondary("تصفير البحث (قائمة البحث والمفحوصين)");
        resetButton.setOnClickListener(v -> resetSearch());
        add(card, resetButton, 10);
        return card;
    }

    private LinearLayout resultsCard() {
        LinearLayout card = card("النتائج");
        resultsText = text("", 14, INK, true);
        add(card, resultsText, 6);
        saveButton = primary("حفظ في التنزيلات");
        saveButton.setOnClickListener(v -> saveToDownloads());
        shareButton = secondary("مشاركة الملف");
        shareButton.setOnClickListener(v -> shareResults());
        add(card, row(saveButton, shareButton), 10);
        copyButton = secondary("نسخ الروابط");
        copyButton.setOnClickListener(v -> copyUsernames());
        linksButton = secondary("⬇  تنزيل الروابط");
        linksButton.setOnClickListener(v -> saveLinks());
        add(card, row(copyButton, linksButton), 8);
        editResultsButton = secondary("✏️  تعديل الحسابات الذهبية");
        editResultsButton.setOnClickListener(v -> editResults());
        add(card, editResultsButton, 8);
        clearButton = danger("مسح النتائج");
        clearButton.setOnClickListener(v -> clearResults());
        add(card, clearButton, 8);
        collectedText = text("", 13, INK, false);
        add(card, collectedText, 14);
        collectedButton = secondary("⬇  تنزيل كل اليوزرات المجموعة");
        collectedButton.setOnClickListener(v -> saveCollected());
        add(card, collectedButton, 6);
        add(card, text("ملف CSV يفتح في Excel و Google Sheets. لا يُرسل شيء إلى أي خادم: كل شيء على جوالك.", 12, MUTED, false), 8);
        return card;
    }

    private LinearLayout logCard() {
        LinearLayout card = card("السجل (الأحدث أولًا)");
        logView = text("", 12, 0xFF374151, false);
        logView.setLineSpacing(dp(2), 1f);
        add(card, logView, 8);
        Button copyLog = secondary("نسخ السجل");
        copyLog.setOnClickListener(v -> copy("السجل", Engine.logText(600, false)));
        add(card, copyLog, 10);
        return card;
    }

    private void showTab(boolean browser) {
        onBrowserTab = browser;
        panel.setVisibility(browser ? View.GONE : View.VISIBLE);
        paintTab(tabPanel, !browser);
        paintTab(tabBrowser, browser);
        if (browser) {
            hideKeyboard();
        }
    }

    /** Everything on the page from the current state (called after each change of the task too). */
    private void refresh() {
        if (accountText == null) {
            return;
        }
        boolean running = Engine.running;
        boolean signedIn = Web.loggedIn();
        String handle = prefs.getString("handle", "");
        accountText.setText(signedIn ? "✅ مسجّل الدخول" + (handle.isEmpty() ? "" : "  -  @" + handle) : "❌ لم تسجّل الدخول بعد");
        accountText.setTextColor(signedIn ? GREEN : RED);
        loginButton.setText(signedIn ? "تبديل الحساب" : "تسجيل الدخول");
        enable(loginButton, !running);
        enable(tokenButton, !running);
        enable(logoutButton, !running && signedIn);
        enable(startButton, !running);
        enable(stopButton, running && !Engine.stopping);
        seedInput.setEnabled(!running);
        targetInput.setEnabled(!running);
        poolInput.setEnabled(!running);
        perListInput.setEnabled(!running);
        fromFollowers.setEnabled(!running);
        fromFollowing.setEnabled(!running);
        for (CheckBox box : countryBoxes.values()) {
            box.setEnabled(!running);
        }
        enable(resetButton, !running);
        enable(listButton, store.queueSize() > 0);
        enable(editListButton, !running);
        progressText.setText(Engine.progress(store));

        int count = store.resultCount();
        resultsText.setText("محفوظ: " + count + " حساب ذهبي");
        enable(saveButton, count > 0);
        enable(shareButton, count > 0);
        enable(copyButton, count > 0);
        enable(linksButton, count > 0);
        enable(editResultsButton, !running);
        enable(clearButton, count > 0 && !running);
        int everyone = store.collectedCount();
        collectedText.setText("كل اليوزرات المجموعة من صفحات المتابعين (بلا تكرار): " + everyone);
        enable(collectedButton, everyone > 0);

        String log = Engine.logText(150, true);
        logView.setText(log.isEmpty() ? "لا شيء بعد." : log);
        blocker.setVisibility(running ? View.VISIBLE : View.GONE);
        banner.setVisibility(running ? View.VISIBLE : View.GONE);

        boolean overlay = Web.canOverlay(this);
        boolean battery = batteryExempt();
        backgroundRow.setVisibility(overlay && battery ? View.GONE : View.VISIBLE);
        StringBuilder missing = new StringBuilder("ليعمل بكامل سرعته وأنت خارج التطبيق: ");
        if (!overlay) {
            missing.append("فعّل «الظهور فوق التطبيقات»");
        }
        if (!overlay && !battery) {
            missing.append("، و");
        }
        if (!battery) {
            missing.append("استثنِ التطبيق من توفير البطارية");
        }
        backgroundText.setText(missing.append('.').toString());

        if (running) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }

    // ---------------------------------------------------------------- the account

    private void login() {
        if (Engine.running) {
            return;
        }
        if (Web.loggedIn()) {
            confirm("تبديل الحساب", "سيُسجَّل الخروج من الحساب الحالي لتدخل بحساب آخر. النتائج تبقى محفوظة.",
                () -> Web.clearSession(() -> {
                    prefs.edit().remove("handle").apply();
                    openLogin();
                }));
            return;
        }
        openLogin();
    }

    private void openLogin() {
        waitingLogin = true;
        waitingToken = false;
        homeTicks = 0;
        showTab(true);
        Web.load("https://x.com/i/flow/login");
        toast("سجّل الدخول في الصفحة، وسيعود التطبيق إلى اللوحة وحده");
        refresh();
        main.removeCallbacks(loginWatch);
        main.postDelayed(loginWatch, 1500);
    }

    private void askToken() {
        if (Engine.running) {
            return;
        }
        EditText field = input("auth_token",
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        FrameLayout box = new FrameLayout(this);
        box.setPadding(dp(20), dp(8), dp(20), 0);
        box.addView(field);
        new AlertDialog.Builder(this)
            .setTitle("الدخول بالتوكن")
            .setMessage("الصق قيمة auth_token لحسابك (من كوكيز X في متصفح مسجّل فيه).")
            .setView(box)
            .setPositiveButton("دخول", (dialog, which) -> {
                String token = field.getText().toString().trim()
                    .replaceFirst("(?i)^auth_token\\s*=\\s*", "").replace(";", "").replace("\"", "").trim();
                if (!token.matches("[A-Za-z0-9]{20,100}")) {
                    toast("التوكن غير صحيح");
                    return;
                }
                Web.setToken(token, () -> {
                    prefs.edit().remove("handle").apply();
                    waitingToken = true;
                    waitingLogin = false;
                    homeTicks = 0;
                    showTab(true);
                    Web.load("https://x.com/home");
                    refresh();
                    main.removeCallbacks(loginWatch);
                    main.postDelayed(loginWatch, 1500);
                });
            })
            .setNegativeButton("إلغاء", null)
            .show();
    }

    /**
     * While signing in: done once X stays on its home page with the session cookie (a few seconds for a pasted token,
     * which X may still refuse and send to its sign-in page).
     */
    private void checkLogin() {
        main.removeCallbacks(loginWatch);
        if (!waitingLogin && !waitingToken) {
            return;
        }
        String path = Web.pathOf(Web.currentUrl());
        if (waitingToken && Web.isSignInPath(path)) {
            waitingToken = false;
            Web.clearSession(() -> {
                toast("❌ التوكن غير صالح أو منتهي");
                showTab(false);
                refresh();
            });
            return;
        }
        homeTicks = path != null && path.startsWith("/home") && Web.loggedIn() ? homeTicks + 1 : 0;
        if (homeTicks >= (waitingToken ? 4 : 2)) {
            waitingLogin = false;
            waitingToken = false;
            Web.flushCookies();
            toast("✅ تم تسجيل الدخول");
            findHandle(0);
            showTab(false);
            refresh();
            return;
        }
        main.postDelayed(loginWatch, 1500);
    }

    /** The signed-in account's name, to show it (X's page may take a moment to show it). */
    private void findHandle(int attempt) {
        Web.evaluate(Scripts.HANDLE, json -> {
            String handle = Web.jsonString(json);
            if (handle != null && handle.matches("[A-Za-z0-9_]{1,15}")) {
                prefs.edit().putString("handle", handle).apply();
                refresh();
            } else if (attempt < 6 && !isDestroyed()) {
                main.postDelayed(() -> findHandle(attempt + 1), 1500);
            }
        });
    }

    private void logout() {
        confirm("تسجيل الخروج", "سيُحذف دخول الحساب من التطبيق. النتائج تبقى محفوظة.",
            () -> Web.clearSession(() -> {
                prefs.edit().remove("handle").apply();
                toast("تم تسجيل الخروج");
                refresh();
            }));
    }

    // ---------------------------------------------------------------- the task

    private void restoreInputs() {
        seedInput.setText(prefs.getString("seed", ""));
        (prefs.getBoolean("following", false) ? fromFollowing : fromFollowers).setChecked(true);
        targetInput.setText(String.valueOf(prefs.getInt("target", 100)));
        poolInput.setText(String.valueOf(prefs.getInt("pool", 50)));
        perListInput.setText(String.valueOf(prefs.getInt("perList", 2000)));
        if (!Engine.running) {
            Engine.pool = prefs.getInt("pool", 50);
        }
        String saved = prefs.getString("countries", TextUtils.join(",", Text.DEFAULT_CODES));
        Set<String> chosen = new LinkedHashSet<>(Arrays.asList(saved.split(",")));
        for (Map.Entry<String, CheckBox> box : countryBoxes.entrySet()) {
            box.getValue().setChecked(chosen.contains(box.getKey()));
        }
    }

    private void startTask() {
        if (Engine.running) {
            return;
        }
        if (!Web.loggedIn()) {
            toast("سجّل الدخول أولًا");
            return;
        }
        String seed = Text.cleanHandle(seedInput.getText().toString());
        if (seed == null) {
            seedInput.setError("اكتب يوزر صحيح، مثل: vextrand");
            seedInput.requestFocus();
            return;
        }
        int count;
        try {
            count = Integer.parseInt(targetInput.getText().toString().trim());
        } catch (NumberFormatException e) {
            count = -1;
        }
        if (count < 1 || count > 100_000) {
            targetInput.setError("اكتب عددًا من 1 إلى 100000");
            targetInput.requestFocus();
            return;
        }
        int listSize;
        try {
            listSize = Integer.parseInt(poolInput.getText().toString().trim());
        } catch (NumberFormatException e) {
            listSize = -1;
        }
        if (listSize < 1 || listSize > Store.QUEUE_MAX) {
            poolInput.setError("اكتب عددًا من 1 إلى " + Store.QUEUE_MAX);
            poolInput.requestFocus();
            return;
        }
        int perList;
        try {
            perList = Integer.parseInt(perListInput.getText().toString().trim());
        } catch (NumberFormatException e) {
            perList = -1;
        }
        if (perList < 1 || perList > 20_000) {
            perListInput.setError("اكتب عددًا من 1 إلى 20000");
            perListInput.requestFocus();
            return;
        }
        Set<String> countries = new LinkedHashSet<>();
        for (Map.Entry<String, CheckBox> box : countryBoxes.entrySet()) {
            if (box.getValue().isChecked()) {
                countries.add(box.getKey());
            }
        }
        if (countries.isEmpty()) {
            toast("اختر دولة واحدة على الأقل");
            return;
        }
        seedInput.setText(seed);
        prefs.edit().putString("seed", seed).putInt("target", count).putInt("pool", listSize)
            .putBoolean("following", fromFollowing.isChecked()).putInt("perList", perList)
            .putString("countries", TextUtils.join(",", countries)).apply();
        hideKeyboard();
        if (Engine.start(this, store, seed, count, listSize, countries, fromFollowing.isChecked(), perList)) {
            if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, ASK_NOTIFICATIONS);
            } else {
                askBackground(false);
            }
        }
        refresh();
    }

    private void resetSearch() {
        confirm("تصفير البحث", "تُمسح قائمة البحث (" + store.queueSize() + ") والحسابات المفحوصة ("
                + store.checkedCount() + ")، فيبدأ البحث القادم من جديد. النتائج لا تُمسح.",
            () -> {
                store.clearSearch();
                refresh();
            });
    }

    // ---------------------------------------------------------------- working in the background

    /** What keeps the task at full speed off the screen: the invisible window first, then the battery. */
    private void askBackground(boolean always) {
        if (!always && askedBackground) {
            return;
        }
        askedBackground = true;
        if (!Web.canOverlay(this)) {
            new AlertDialog.Builder(this)
                .setTitle("العمل في الخلفية")
                .setMessage("ليكمل الاستخراج بكامل سرعته وأنت خارج التطبيق، فعّل «الظهور فوق التطبيقات الأخرى» "
                    + "لـ«المستخرج» في الصفحة التالية.\n\nلا يظهر شيء على الشاشة، ويُستخدم فقط أثناء المهمة.")
                .setPositiveButton("فتح الإعداد", (dialog, which) -> {
                    batteryNext = true;
                    openSetting(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:" + getPackageName())),
                        new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION));
                })
                .setNegativeButton("لاحقًا", (dialog, which) -> askBattery())
                .show();
            return;
        }
        askBattery();
    }

    private void askBattery() {
        if (batteryExempt() || isFinishing()) {
            return;
        }
        new AlertDialog.Builder(this)
            .setTitle("استثناء من توفير البطارية")
            .setMessage("ليبقى الاستخراج يعمل دون أن يوقفه الجوال بعد الخروج من التطبيق، استثنِ «المستخرج» من توفير البطارية.\n\n"
                + "اضغط «السماح» ثم اختر «السماح» أو «غير مقيّد».")
            .setPositiveButton("السماح", (dialog, which) -> openSetting(
                new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName())),
                new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)))
            .setNegativeButton("لاحقًا", null)
            .show();
    }

    private boolean batteryExempt() {
        try {
            PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
            return power != null && power.isIgnoringBatteryOptimizations(getPackageName());
        } catch (RuntimeException e) {
            return true;
        }
    }

    private void openSetting(Intent first, Intent fallback) {
        try {
            startActivity(first);
        } catch (ActivityNotFoundException | SecurityException e) {
            try {
                startActivity(fallback);
            } catch (ActivityNotFoundException | SecurityException again) {
                toast("افتح إعدادات الجوال ← التطبيقات ← المستخرج");
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(code, permissions, results);
        if (code == ASK_NOTIFICATIONS) {
            askBackground(false);
        } else if (code == ASK_STORAGE && afterStoragePermission != null) {
            Runnable retry = afterStoragePermission;
            afterStoragePermission = null;
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                retry.run();
            } else {
                toast("بدون إذن التخزين لا يمكن الحفظ في التنزيلات.");
            }
        }
    }

    // ---------------------------------------------------------------- results

    /** What a saved file holds. */
    private interface Content {
        void writeTo(OutputStream out) throws IOException;
    }

    private void saveToDownloads() {
        if (store.resultCount() == 0) {
            return;
        }
        saveDownload("extract-" + stamp() + ".csv", "text/csv", store::copyResults, this::saveToDownloads);
    }

    /** The search list in a box to edit: one username (or @name, or link) a line, in the order of the search. */
    private void editSearchList() {
        if (Engine.running) {
            toast("أوقف المهمة أولًا لتعديل قائمة البحث");
            return;
        }
        List<String> names = store.queueSnapshot();
        listEditor("تعديل قائمة البحث (" + names.size() + ")",
            "يوزر أو رابط حساب في كل سطر. الترتيب هو ترتيب البحث، والمكرر يُحذف.", names, this::saveEditedList);
    }

    /** The gold accounts extracted, to edit: a removed line takes the account out, a new line adds one. */
    private void editResults() {
        if (Engine.running) {
            toast("أوقف المهمة أولًا لتعديل الحسابات الذهبية");
            return;
        }
        List<String> names = store.resultUsernames();
        listEditor("تعديل الحسابات الذهبية (" + names.size() + ")",
            "يوزر أو رابط حساب في كل سطر. احذف السطر لحذف الحساب من النتائج (ولن يُستخرج مرة أخرى)، وأضف سطرًا "
                + "لإضافة حساب. الترتيب هو ترتيب الملف، والمكرر يُحذف.", names, this::saveEditedResults);
    }

    private void saveEditedResults(String text) {
        if (Engine.running) {
            toast("أوقف المهمة أولًا لتعديل الحسابات الذهبية");
            return;
        }
        int before = store.resultCount();
        int[] bad = new int[1];
        List<String> names = Text.handlesIn(text, bad);
        int[] done = store.replaceResults(names);
        int removed = Math.max(0, before - done[0]);
        toast("✅ حُفظت الحسابات الذهبية: " + names.size()
            + (removed > 0 ? "، حُذف " + removed : "")
            + (done[1] > 0 ? "، أُضيف " + done[1] : "")
            + (bad[0] > 0 ? "، وتُرك " + bad[0] + " غير صالح" : ""));
        refresh();
    }

    /** A box with one username a line, to edit; save gets the text when the user saves. */
    private void listEditor(String title, String message, List<String> names, java.util.function.Consumer<String> save) {
        EditText box = new EditText(this);
        box.setText(TextUtils.join("\n", names));
        box.setHint("يوزر أو رابط حساب في كل سطر");
        box.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        box.setSingleLine(false);
        box.setHorizontallyScrolling(false);
        box.setMinLines(8);
        box.setMaxLines(14);
        box.setVerticalScrollBarEnabled(true);
        box.setGravity(Gravity.TOP | Gravity.START);
        box.setTextDirection(View.TEXT_DIRECTION_LTR);
        box.setTextSize(14);
        box.setTextColor(INK);
        box.setBackground(round(0xFFF9FAFB, LINE, 10));
        box.setPadding(dp(12), dp(10), dp(12), dp(10));
        FrameLayout holder = new FrameLayout(this);
        holder.setPadding(dp(20), dp(8), dp(20), 0);
        holder.addView(box);
        new AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(message)
            .setView(holder)
            .setPositiveButton("حفظ", (dialog, which) -> save.accept(box.getText().toString()))
            .setNegativeButton("إلغاء", null)
            .show();
    }

    private void saveEditedList(String text) {
        if (Engine.running) {
            toast("أوقف المهمة أولًا لتعديل قائمة البحث");
            return;
        }
        int[] bad = new int[1];
        List<String> names = Text.handlesIn(text, bad);
        int over = Math.max(0, names.size() - Store.QUEUE_MAX);
        if (over > 0) {
            names = names.subList(0, Store.QUEUE_MAX);
        }
        store.replaceQueue(names);
        toast("✅ حُفظت قائمة البحث: " + names.size() + " حساب"
            + (bad[0] > 0 ? "، وتُرك " + bad[0] + " غير صالح" : "")
            + (over > 0 ? "، وحُذف " + over + " فوق الحد " + Store.QUEUE_MAX : ""));
        refresh();
    }

    /** The search list's usernames, one a line, in a text file in «Downloads». */
    private void saveSearchList() {
        List<String> names = store.queueSnapshot();
        if (names.isEmpty()) {
            toast("قائمة البحث فارغة");
            return;
        }
        StringBuilder text = new StringBuilder();
        for (String name : names) {
            text.append('@').append(name).append('\n');
        }
        byte[] data = text.toString().getBytes(StandardCharsets.UTF_8);
        saveDownload("search-list-" + stamp() + ".txt", "text/plain", out -> out.write(data), this::saveSearchList);
    }

    /** Every username read from the followers' pages, once each, in a text file in «Downloads». */
    private void saveCollected() {
        if (store.collectedCount() == 0) {
            return;
        }
        saveDownload("all-usernames-" + stamp() + ".txt", "text/plain", store::copyCollected, this::saveCollected);
    }

    private String stamp() {
        return new SimpleDateFormat("yyyyMMdd-HHmm", Locale.US).format(new Date());
    }

    /** Writes the file to «Downloads» (on Android 8 and 9 after the storage permission: then retry runs again). */
    @SuppressWarnings("deprecation")
    private void saveDownload(String name, String type, Content content, Runnable retry) {
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues values = new ContentValues();
                values.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                values.put(MediaStore.MediaColumns.MIME_TYPE, type);
                values.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                Uri saved = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                if (saved == null) {
                    throw new IOException("no place");
                }
                try (OutputStream out = getContentResolver().openOutputStream(saved)) {
                    if (out == null) {
                        throw new IOException("no file");
                    }
                    content.writeTo(out);
                }
            } else {
                if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                    afterStoragePermission = retry;
                    requestPermissions(new String[] {Manifest.permission.WRITE_EXTERNAL_STORAGE}, ASK_STORAGE);
                    return;
                }
                File folder = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                folder.mkdirs();
                try (OutputStream out = new FileOutputStream(new File(folder, name))) {
                    content.writeTo(out);
                }
            }
            toast("✅ حُفظ في التنزيلات: " + name);
        } catch (IOException | RuntimeException e) {
            toast("تعذّر الحفظ: " + e.getMessage());
        }
    }

    private void shareResults() {
        if (store.resultCount() == 0) {
            return;
        }
        try {
            File copy = store.shareCopy();
            Uri uri = ResultsProvider.uriOf(copy);
            Intent send = new Intent(Intent.ACTION_SEND)
                .setType("text/csv")
                .putExtra(Intent.EXTRA_STREAM, uri)
                .putExtra(Intent.EXTRA_SUBJECT, copy.getName())
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            send.setClipData(ClipData.newRawUri(copy.getName(), uri));
            startActivity(Intent.createChooser(send, "مشاركة النتائج").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
        } catch (IOException | RuntimeException e) {
            toast("تعذّرت المشاركة: " + e.getMessage());
        }
    }

    /** The gold accounts' profile links, copied one a line. */
    private void copyUsernames() {
        List<String> names = store.resultUsernames();
        copy("روابط الحسابات", resultLinks(names).trim());
        toast("نُسخ " + names.size() + " رابط");
    }

    /** The gold accounts' profile links in a text file in «Downloads». */
    private void saveLinks() {
        List<String> names = store.resultUsernames();
        if (names.isEmpty()) {
            return;
        }
        byte[] data = resultLinks(names).getBytes(StandardCharsets.UTF_8);
        saveDownload("gold-links-" + stamp() + ".txt", "text/plain", out -> out.write(data), this::saveLinks);
    }

    private static String resultLinks(List<String> names) {
        StringBuilder text = new StringBuilder();
        for (String name : names) {
            text.append("https://x.com/").append(name).append('\n');
        }
        return text.toString();
    }

    private void clearResults() {
        confirm("مسح النتائج", "تُحذف النتائج المحفوظة (" + store.resultCount() + " حساب) من التطبيق، "
                + "ويمكن استخراج هذه الحسابات مرة أخرى لاحقًا. احفظها أو شاركها أولًا إن كنت تحتاجها.",
            () -> {
                store.clearResults();
                refresh();
            });
    }

    // ---------------------------------------------------------------- small things

    private void copy(String label, String text) {
        ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        if (clipboard != null) {
            clipboard.setPrimaryClip(ClipData.newPlainText(label, text));
        }
    }

    private void confirm(String title, String message, Runnable yes) {
        new AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(message)
            .setPositiveButton("نعم", (dialog, which) -> yes.run())
            .setNegativeButton("إلغاء", null)
            .show();
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show();
    }

    private void hideKeyboard() {
        InputMethodManager keyboard = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        if (keyboard != null) {
            keyboard.hideSoftInputFromWindow(getWindow().getDecorView().getWindowToken(), 0);
        }
    }

    private int dp(float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }

    private GradientDrawable round(int fill, int stroke, float radius) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(fill);
        shape.setCornerRadius(dp(radius));
        if (stroke != 0) {
            shape.setStroke(dp(1), stroke);
        }
        return shape;
    }

    private TextView text(String value, float size, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        if (bold) {
            view.setTypeface(Typeface.DEFAULT_BOLD);
        }
        return view;
    }

    private EditText input(String hint, int type) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setInputType(type);
        field.setSingleLine(true);
        field.setTextSize(15);
        field.setTextColor(INK);
        field.setHintTextColor(MUTED);
        field.setBackground(round(0xFFF9FAFB, LINE, 10));
        field.setPadding(dp(12), dp(10), dp(12), dp(10));
        return field;
    }

    private Button button(String label, int fill, int textColor, int stroke) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(14);
        b.setTextColor(textColor);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setBackground(round(fill, stroke, 10));
        b.setStateListAnimator(null);
        b.setMinHeight(dp(44));
        b.setMinimumHeight(dp(44));
        b.setPadding(dp(10), 0, dp(10), 0);
        return b;
    }

    private RadioButton radio(String label) {
        RadioButton choice = new RadioButton(this);
        choice.setText(label);
        choice.setTextColor(INK);
        choice.setTextSize(14);
        choice.setButtonTintList(ColorStateList.valueOf(BLUE));
        return choice;
    }

    private Button primary(String label) {
        return button(label, BLUE, Color.WHITE, 0);
    }

    private Button secondary(String label) {
        return button(label, Color.WHITE, BLUE, BLUE);
    }

    private Button danger(String label) {
        return button(label, Color.WHITE, RED, RED);
    }

    private Button tab(String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(13);
        b.setTextColor(Color.WHITE);
        b.setStateListAnimator(null);
        b.setMinHeight(0);
        b.setMinimumHeight(0);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        b.setPadding(dp(14), 0, dp(14), 0);
        return b;
    }

    private void paintTab(Button b, boolean on) {
        b.setBackground(round(on ? 0x33FFFFFF : 0x00000000, 0x66FFFFFF, 18));
        b.setTypeface(on ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
    }

    private void enable(View v, boolean on) {
        v.setEnabled(on);
        v.setAlpha(on ? 1f : 0.45f);
    }

    private LinearLayout card(String title) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(round(Color.WHITE, LINE, 14));
        card.setPadding(dp(14), dp(12), dp(14), dp(14));
        card.addView(text(title, 16, INK, true));
        LinearLayout.LayoutParams size = new LinearLayout.LayoutParams(MATCH, WRAP);
        size.bottomMargin = dp(12);
        card.setLayoutParams(size);
        return card;
    }

    private LinearLayout row(View... views) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        for (int i = 0; i < views.length; i++) {
            LinearLayout.LayoutParams size = new LinearLayout.LayoutParams(0, WRAP, 1);
            if (i > 0) {
                size.setMarginStart(dp(8));
            }
            row.addView(views[i], size);
        }
        return row;
    }

    private void add(LinearLayout parent, View child, int topMargin) {
        LinearLayout.LayoutParams size = new LinearLayout.LayoutParams(MATCH, WRAP);
        size.topMargin = dp(topMargin);
        parent.addView(child, size);
    }
}
