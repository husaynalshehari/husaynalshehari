package app.tg.groups;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.app.PictureInPictureParams;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.Rational;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

/**
 * The app's one page. «اللوحة» is the groups script's own page (the script runs inside the app, see Engine), and
 * «المتصفح» shows the browsers the script's tasks open (see BrowserHost). A task's browser works only while the
 * app is on the screen, so the screen is kept on while a browser is open, and leaving the app then shrinks it to a
 * small window over other apps instead of hiding it.
 */
public class MainActivity extends Activity {
    // The groups script's colors: light paper, pine green, brick red.
    static final int PAPER = 0xFFF3EFE6;
    static final int SHEET = 0xFFFFFDF8;
    static final int INK = 0xFF1C1915;
    static final int MUTED = 0xFF6E675E;
    static final int LINE = 0xFFE4DCD0;
    static final int PINE = 0xFF0F6B50;
    static final int ON_PINE = 0xFFF4FBF7;
    static final int CLAY = 0xFF8D3A32;

    final Handler main = new Handler(Looper.getMainLooper());
    LinearLayout root;
    LinearLayout bar;
    FrameLayout content;
    ScrollView stageScroll;
    BrowserStage stage;
    TextView emptyNote;
    WebView panel;
    TextView status;
    LinearLayout report;
    TextView reportTitle;
    TextView reportText;
    Button tabPanel;
    Button tabBrowser;
    boolean onBrowserTab;
    boolean panelLoaded;
    boolean panelFailed;
    boolean alive;
    boolean small;                        // shrunk to the small window over other apps
    String reportCopy = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        alive = true;
        // The script reaches the tasks' browsers through their DevTools socket, which Android opens to the app itself.
        WebView.setWebContentsDebuggingEnabled(true);
        setContentView(buildPage());
        BrowserHost.attach(this);
        browsersChanged();
        Engine.start(this);
        waitForScript();
    }

    @Override
    protected void onStart() {
        super.onStart();
        BrowserHost.setVisible(true);
    }

    @Override
    protected void onStop() {
        super.onStop();
        BrowserHost.setVisible(false);
    }

    @Override
    protected void onDestroy() {
        alive = false;
        BrowserHost.detach(this);
        if (panel != null) {
            content.removeView(panel);
            panel.destroy();
            panel = null;
        }
        super.onDestroy();
    }

    // ---------------------------------------------------------------- the page

    View buildPage() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(PAPER);
        root.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);

        bar = new LinearLayout(this);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(12), dp(6), dp(8), dp(6));
        TextView title = text("قروبات", 18, INK, true);
        bar.addView(title, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        tabPanel = tab("اللوحة");
        tabPanel.setOnClickListener(v -> showTab(false));
        bar.addView(tabPanel, gap(dp(6)));
        tabBrowser = tab("المتصفح");
        tabBrowser.setOnClickListener(v -> showTab(true));
        bar.addView(tabBrowser, gap(dp(6)));
        Button more = tab("⋮");
        more.setMinWidth(dp(40));
        more.setMinimumWidth(dp(40));
        more.setOnClickListener(this::showMenu);
        bar.addView(more, gap(0));
        root.addView(bar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        content = new FrameLayout(this);
        root.addView(content, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));

        // Underneath: the browsers. They stay there (and keep working) while the panel is in front of them.
        stageScroll = new ScrollView(this);
        stageScroll.setBackgroundColor(0xFFFFFFFF);
        stageScroll.setFillViewport(false);
        stage = new BrowserStage(this);
        stageScroll.addView(stage, new ScrollView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        content.addView(stageScroll, fill());
        emptyNote = text("لا يوجد متصفح مفتوح الآن.\nيفتح هنا عند بدء مهمة من «اللوحة»، ويمكنك مشاهدته وهو يعمل.", 15, MUTED, false);
        emptyNote.setGravity(Gravity.CENTER);
        emptyNote.setPadding(dp(24), dp(24), dp(24), dp(24));
        emptyNote.setBackgroundColor(PAPER);
        content.addView(emptyNote, fill());

        panel = buildPanel();
        content.addView(panel, fill());

        status = text("يشغّل السكربت…", 16, MUTED, false);
        status.setGravity(Gravity.CENTER);
        status.setPadding(dp(24), dp(24), dp(24), dp(24));
        status.setBackgroundColor(PAPER);
        content.addView(status, fill());

        report = buildReport();
        report.setVisibility(View.GONE);
        content.addView(report, fill());

        fitToScreen(root);
        showTab(false);
        return root;
    }

    /** From Android 15 the page goes under the status bar, the navigation bar and the keyboard: it keeps clear. */
    void fitToScreen(View view) {
        if (Build.VERSION.SDK_INT < 35) {
            return;
        }
        view.setOnApplyWindowInsetsListener((target, insets) -> {
            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                | WindowInsets.Type.ime());
            target.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return insets;
        });
    }

    @SuppressLint("SetJavaScriptEnabled")
    WebView buildPanel() {
        WebView view = new WebView(this);
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setSupportMultipleWindows(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        view.setBackgroundColor(PAPER);
        view.setWebChromeClient(new WebChromeClient());        // (the page's own «هل أنت متأكد؟» questions show)
        view.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView web, WebResourceRequest request) {
                Uri link = request.getUrl();
                if ("127.0.0.1".equals(link.getHost())) {
                    return false;
                }
                openOutside(link);                              // a tweet's link, and any other site: in its own app
                return true;
            }

            @Override
            public void onPageStarted(WebView web, String url, android.graphics.Bitmap icon) {
                panelFailed = false;
            }

            @Override
            public void onPageFinished(WebView web, String url) {
                if (!panelFailed && url != null && url.startsWith("http://127.0.0.1")) {
                    panelLoaded = true;
                    status.setVisibility(View.GONE);
                }
            }

            @Override
            public void onReceivedError(WebView web, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame() && alive) {
                    // (the script was not answering yet, or was restarting: asked again in a moment)
                    panelFailed = true;
                    panelLoaded = false;
                    if (!onBrowserTab && !small) {
                        status.setVisibility(View.VISIBLE);
                    }
                    main.postDelayed(() -> {
                        if (alive && panel != null && Engine.phase.equals("running")) {
                            panel.loadUrl(Engine.url());
                        }
                    }, 1200);
                }
            }
        });
        view.setDownloadListener((url, agent, disposition, mime, length) -> download(url, agent, disposition, mime));
        return view;
    }

    LinearLayout buildReport() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(PAPER);
        box.setPadding(dp(14), dp(10), dp(14), dp(10));
        LinearLayout head = new LinearLayout(this);
        head.setGravity(Gravity.CENTER_VERTICAL);
        reportTitle = text("", 17, INK, true);
        head.addView(reportTitle, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        Button copy = tab("نسخ");
        copy.setOnClickListener(v -> {
            ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            clipboard.setPrimaryClip(ClipData.newPlainText("قروبات", reportCopy.isEmpty() ? reportText.getText() : reportCopy));
            Toast.makeText(this, "نُسخ.", Toast.LENGTH_SHORT).show();
        });
        head.addView(copy, gap(dp(6)));
        Button close = tab("إغلاق");
        close.setOnClickListener(v -> report.setVisibility(View.GONE));
        head.addView(close, gap(0));
        box.addView(head);
        ScrollView scroll = new ScrollView(this);
        reportText = text("", 14, INK, false);
        reportText.setTextIsSelectable(true);
        reportText.setPadding(0, dp(10), 0, dp(20));
        scroll.addView(reportText);
        box.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        return box;
    }

    void showTab(boolean browser) {
        onBrowserTab = browser;
        // The panel is in front of the browsers on its tab, and out of the way on theirs.
        panel.setVisibility(browser ? View.INVISIBLE : View.VISIBLE);
        status.setVisibility(!browser && !panelLoaded ? View.VISIBLE : View.GONE);
        emptyNote.setVisibility(browser && BrowserHost.count() == 0 ? View.VISIBLE : View.GONE);
        paintTab(tabPanel, !browser);
        paintTab(tabBrowser, browser);
    }

    /** A browser was opened or closed. */
    void browsersChanged() {
        int open = BrowserHost.count();
        tabBrowser.setText(open > 0 ? "المتصفح ●" : "المتصفح");
        emptyNote.setVisibility(onBrowserTab && open == 0 ? View.VISIBLE : View.GONE);
        if (open > 0) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        if (Build.VERSION.SDK_INT >= 31 && canShrink()) {
            try {
                setPictureInPictureParams(new PictureInPictureParams.Builder().setAspectRatio(new Rational(9, 16))
                    .setAutoEnterEnabled(open > 0).build());
            } catch (RuntimeException e) {
                // (this phone does not shrink apps)
            }
        }
    }

    // ---------------------------------------------------------------- the script's page

    void waitForScript() {
        new Thread(() -> {
            long end = System.currentTimeMillis() + 180_000;
            while (alive && System.currentTimeMillis() < end) {
                if (Engine.phase.equals("failed")) {
                    break;
                }
                if (Engine.answers()) {
                    runOnUiThread(() -> {
                        if (alive && panel != null) {
                            panel.loadUrl(Engine.url());
                        }
                    });
                    return;
                }
                try {
                    Thread.sleep(300);
                } catch (InterruptedException e) {
                    return;
                }
            }
            runOnUiThread(() -> {
                if (alive) {
                    status.setTextColor(CLAY);
                    status.setText("لم يبدأ السكربت. افتح القائمة ⋮ ثم «السجل» وأرسل ما فيه.\n\n"
                        + (Engine.error.length() > 600 ? Engine.error.substring(Engine.error.length() - 600) : Engine.error));
                }
            });
        }, "wait-for-script").start();
    }

    void openOutside(Uri link) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, link));
        } catch (ActivityNotFoundException | SecurityException e) {
            Toast.makeText(this, "لا يوجد تطبيق يفتح الرابط.", Toast.LENGTH_SHORT).show();
        }
    }

    /** A file the panel offers (a picture made in «إنشاء محتوى»): saved in the phone's downloads. */
    void download(String url, String agent, String disposition, String mime) {
        try {
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
            String cookies = CookieManager.getInstance().getCookie(url);
            if (cookies != null) {
                request.addRequestHeader("Cookie", cookies);
            }
            String name = URLUtil.guessFileName(url, disposition, mime);
            request.setTitle(name);
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(request);
            Toast.makeText(this, "يُحفظ في التنزيلات: " + name, Toast.LENGTH_SHORT).show();
        } catch (RuntimeException e) {
            Toast.makeText(this, "تعذّر حفظ الملف.", Toast.LENGTH_SHORT).show();
        }
    }

    // ---------------------------------------------------------------- the menu: check, log, update

    void showMenu(View anchor) {
        PopupMenu menu = new PopupMenu(this, anchor);
        menu.getMenu().add(0, 1, 0, "فحص المتصفح");
        menu.getMenu().add(0, 2, 1, "فحص المتصفح والوصول إلى X");
        menu.getMenu().add(0, 3, 2, "السجل");
        menu.getMenu().add(0, 6, 3, BrowserHost.manualTyping ? "منع الكتابة في المتصفح بيدي" : "السماح بالكتابة في المتصفح بيدي");
        menu.getMenu().add(0, 4, 4, "تحديث السكربت");
        menu.getMenu().add(0, 5, 5, "إعادة تشغيل التطبيق");
        menu.setOnMenuItemClickListener(item -> {
            switch (item.getItemId()) {
                case 1:
                    runCheck(false);
                    return true;
                case 2:
                    runCheck(true);
                    return true;
                case 3:
                    showLog();
                    return true;
                case 4:
                    confirmUpdate();
                    return true;
                case 5:
                    restart();
                    return true;
                case 6:
                    BrowserHost.setManualTyping(!BrowserHost.manualTyping);
                    Toast.makeText(this, BrowserHost.manualTyping ? "يمكنك الآن الكتابة في المتصفح من تبويب «المتصفح»."
                        : "المتصفح لا يأخذ لوحة المفاتيح.", Toast.LENGTH_SHORT).show();
                    return true;
                default:
                    return false;
            }
        });
        menu.show();
    }

    void openReport(String title, String body) {
        reportTitle.setText(title);
        reportText.setText(body);
        reportCopy = "";
        report.setVisibility(View.VISIBLE);
    }

    void reportAdd(String line) {
        if (alive) {
            reportText.append(line + "\n");
        }
    }

    /** «فحص المتصفح»: everything the script asks of its browser, tried on a small page; a line for each. */
    void runCheck(boolean online) {
        if (!Engine.phase.equals("running")) {
            Toast.makeText(this, "السكربت لم يبدأ بعد.", Toast.LENGTH_SHORT).show();
            return;
        }
        openReport("فحص المتصفح", "");
        Lines lines = new Lines(this);
        new Thread(() -> {
            String whole = Engine.call("check", online, lines);
            runOnUiThread(() -> {
                if (alive) {
                    reportCopy = whole;
                    reportTitle.setText("فحص المتصفح: انتهى");
                }
            });
        }, "browser-check").start();
    }

    void showLog() {
        if (!Engine.phase.equals("running") && !Engine.phase.equals("failed")) {
            openReport("السجل", "السكربت لم يبدأ بعد.");
            return;
        }
        openReport("السجل", "…");
        new Thread(() -> {
            String log = com.chaquo.python.Python.isStarted() ? Engine.call("log_text", 600) : "";
            String all = Engine.phase.equals("failed") ? (log + "\n\n" + Engine.error).trim() : log;
            runOnUiThread(() -> {
                if (alive) {
                    reportText.setText(all);
                    reportCopy = all;
                }
            });
        }, "log").start();
    }

    void confirmUpdate() {
        new AlertDialog.Builder(this)
            .setMessage("يأخذ التطبيق أحدث نسخة من السكربت من الإنترنت ثم يعيد تشغيل نفسه. المهام العاملة الآن تتوقف. هل تتابع؟")
            .setPositiveButton("تحديث", (dialog, which) -> update())
            .setNegativeButton("إلغاء", null)
            .show();
    }

    void update() {
        if (!Engine.phase.equals("running")) {
            Toast.makeText(this, "السكربت لم يبدأ بعد.", Toast.LENGTH_SHORT).show();
            return;
        }
        Toast.makeText(this, "يأخذ أحدث نسخة…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            String answer = Engine.call("update_script");
            runOnUiThread(() -> {
                if (!alive) {
                    return;
                }
                if (answer.equals("OK")) {
                    Toast.makeText(this, "تم التحديث. يُعاد تشغيل التطبيق…", Toast.LENGTH_LONG).show();
                    main.postDelayed(this::restart, 900);
                } else {
                    new AlertDialog.Builder(this).setMessage(answer).setPositiveButton("حسنًا", null).show();
                }
            });
        }, "script-update").start();
    }

    /** Start the app afresh: the script runs once for the app's whole life, so a new script needs a new start. */
    void restart() {
        Intent launch = getPackageManager().getLaunchIntentForPackage(getPackageName());
        if (launch == null || launch.getComponent() == null) {
            return;
        }
        startActivity(Intent.makeRestartActivityTask(launch.getComponent()));
        Runtime.getRuntime().exit(0);
    }

    // ---------------------------------------------------------------- leaving the app while tasks run

    boolean canShrink() {
        return Build.VERSION.SDK_INT >= 26 && getPackageManager().hasSystemFeature(PackageManager.FEATURE_PICTURE_IN_PICTURE);
    }

    @Override
    protected void onUserLeaveHint() {
        super.onUserLeaveHint();
        // With a task's browser open, leaving the app shrinks it to a small window over the other apps, so that the
        // browser stays on the screen and the task goes on. (From Android 12 the phone does this by itself.)
        if (BrowserHost.count() > 0 && canShrink() && Build.VERSION.SDK_INT < 31) {
            try {
                enterPictureInPictureMode(new PictureInPictureParams.Builder().setAspectRatio(new Rational(9, 16)).build());
            } catch (RuntimeException e) {
                // (not allowed on this phone: the task rests until the app is opened again)
            }
        }
    }

    @Override
    public void onPictureInPictureModeChanged(boolean inSmallWindow, Configuration newConfig) {
        super.onPictureInPictureModeChanged(inSmallWindow, newConfig);
        small = inSmallWindow;
        bar.setVisibility(inSmallWindow ? View.GONE : View.VISIBLE);
        if (inSmallWindow) {
            // The small window shows the browser at work.
            report.setVisibility(View.GONE);
            panel.setVisibility(View.INVISIBLE);
            status.setVisibility(View.GONE);
            emptyNote.setVisibility(BrowserHost.count() == 0 ? View.VISIBLE : View.GONE);
        } else {
            showTab(onBrowserTab);
        }
    }

    @Override
    public void onBackPressed() {
        if (report.getVisibility() == View.VISIBLE) {
            report.setVisibility(View.GONE);
        } else if (onBrowserTab) {
            showTab(false);
        } else if (panel != null && panel.canGoBack()) {
            panel.goBack();
        } else {
            moveTaskToBack(true);                 // (the app is left running, with its tasks)
        }
    }

    // ---------------------------------------------------------------- small view helpers

    int dp(float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }

    FrameLayout.LayoutParams fill() {
        return new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
    }

    LinearLayout.LayoutParams gap(int end) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.setMarginEnd(end);
        return params;
    }

    TextView text(String value, float size, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, size);
        view.setTextColor(color);
        view.setLineSpacing(0, 1.2f);
        if (bold) {
            view.setTypeface(Typeface.DEFAULT_BOLD);
        }
        return view;
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

    Button tab(String value) {
        Button button = new Button(this);
        button.setText(value);
        button.setAllCaps(false);
        button.setSingleLine(true);
        button.setEllipsize(TextUtils.TruncateAt.END);
        button.setTextColor(INK);
        button.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        button.setBackground(shape(SHEET, LINE, 10));
        button.setStateListAnimator(null);
        button.setMinHeight(dp(38));
        button.setMinimumHeight(dp(38));
        button.setMinWidth(dp(64));
        button.setMinimumWidth(dp(64));
        button.setPadding(dp(12), 0, dp(12), 0);
        return button;
    }

    void paintTab(Button button, boolean chosen) {
        button.setBackground(shape(chosen ? PINE : SHEET, chosen ? 0 : LINE, 10));
        button.setTextColor(chosen ? ON_PINE : INK);
        button.setTypeface(chosen ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
    }
}
