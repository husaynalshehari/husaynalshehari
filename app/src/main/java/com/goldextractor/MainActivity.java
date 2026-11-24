package com.goldextractor;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.PowerManager;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.List;
import java.util.Locale;

public class MainActivity extends Activity implements Extractor.Listener {

    private static final int REQ_EXPORT = 101;
    private static final int REQ_IMPORT = 102;
    private static final int MAX_LOG_LINES = 400;

    private Browser browser;
    private Store store;
    private Extractor extractor;
    private SharedPreferences prefs;
    private PowerManager.WakeLock wakeLock;

    private TextView statusText;
    private TextView statsText;
    private TextView logText;
    private ScrollView logScroll;
    private EditText seedInput;
    private EditText targetInput;
    private Button startButton;
    private Button stopButton;
    private Button loginButton;
    private Button dimButton;
    private Button logToggleButton;

    private final ArrayDeque<String> logLines = new ArrayDeque<>();
    private final SimpleDateFormat timeFormat = new SimpleDateFormat("HH:mm:ss", Locale.US);
    private boolean dimmed = false;
    private List<String> pendingImport;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = getSharedPreferences("settings", MODE_PRIVATE);

        statusText = findViewById(R.id.statusText);
        statsText = findViewById(R.id.statsText);
        logText = findViewById(R.id.logText);
        logScroll = findViewById(R.id.logScroll);
        seedInput = findViewById(R.id.seedInput);
        targetInput = findViewById(R.id.targetInput);
        startButton = findViewById(R.id.startButton);
        stopButton = findViewById(R.id.stopButton);
        loginButton = findViewById(R.id.loginButton);
        dimButton = findViewById(R.id.dimButton);
        logToggleButton = findViewById(R.id.logToggleButton);
        FrameLayout webContainer = findViewById(R.id.webContainer);

        store = new Store(getFilesDir());
        browser = new Browser(this, webContainer, prefs.getBoolean("block_images", true));
        extractor = new Extractor(this, browser, store, this);

        seedInput.setText(prefs.getString("seed", ""));
        targetInput.setText(String.valueOf(prefs.getInt("target", 100)));

        startButton.setOnClickListener(v -> startExtraction());
        stopButton.setOnClickListener(v -> stopExtraction());
        loginButton.setOnClickListener(v -> openLogin());
        findViewById(R.id.exportButton).setOnClickListener(v -> exportCsv());
        findViewById(R.id.settingsButton).setOnClickListener(v -> showSettings());
        dimButton.setOnClickListener(v -> toggleDim());
        logToggleButton.setOnClickListener(v -> toggleLog());

        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GoldExtractor:run");
        wakeLock.setReferenceCounted(false);

        setRunning(false);
        refreshIdleStats();
        appendLog("👋 جاهز. أول مرة: اضغط «تسجيل الدخول» وسجّل دخولك في X. الدخول يبقى محفوظاً.");
        browser.loadAsync("https://x.com/home");
    }

    // ---------------------------------------------------------------- login / start / stop

    private void openLogin() {
        if (logScroll.getVisibility() == View.VISIBLE) toggleLog(); // more room for the login page
        browser.loadAsync("https://x.com/i/flow/login");
        toast("سجّل دخولك في المتصفح بالأسفل. بعد ظهور الصفحة الرئيسية اضغط «ابدأ».");
    }

    private void startExtraction() {
        if (extractor.isRunning()) return;

        String seed = seedInput.getText().toString().trim();
        int target;
        try {
            target = Integer.parseInt(targetInput.getText().toString().trim());
        } catch (NumberFormatException e) {
            target = 0;
        }
        if (Extractor.cleanUsername(seed).isEmpty()) {
            toast("اكتب يوزر البداية");
            return;
        }
        if (target <= 0) {
            toast("اكتب العدد المطلوب");
            return;
        }

        prefs.edit().putString("seed", seed).putInt("target", target).apply();
        browser.saveSession();
        if (logScroll.getVisibility() != View.VISIBLE) toggleLog();
        setRunning(true);
        extractor.start(seed, target);
    }

    private void stopExtraction() {
        extractor.stop();
        statusText.setText("⏳ جاري الإيقاف...");
        stopButton.setEnabled(false);
    }

    private void setRunning(boolean running) {
        startButton.setEnabled(!running);
        stopButton.setEnabled(running);
        loginButton.setEnabled(!running);
        seedInput.setEnabled(!running);
        targetInput.setEnabled(!running);
        statusText.setText(running ? "🟢 يعمل - اترك التطبيق مفتوحاً" : "⚪ متوقف");
        if (running) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (!wakeLock.isHeld()) wakeLock.acquire(24L * 60 * 60 * 1000);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (wakeLock.isHeld()) wakeLock.release();
            if (dimmed) toggleDim();
        }
    }

    // ---------------------------------------------------------------- Extractor.Listener (background thread)

    @Override
    public void onLog(String msg) {
        runOnUiThread(() -> appendLog(msg));
    }

    @Override
    public void onStats(String stats) {
        runOnUiThread(() -> statsText.setText(stats));
    }

    @Override
    public void onFinished() {
        runOnUiThread(() -> {
            setRunning(false);
            refreshIdleStats();
            browser.saveSession();
        });
    }

    // ---------------------------------------------------------------- log / stats

    private void appendLog(String msg) {
        logLines.addLast("[" + timeFormat.format(new Date()) + "] " + msg);
        while (logLines.size() > MAX_LOG_LINES) logLines.removeFirst();
        StringBuilder sb = new StringBuilder();
        for (String line : logLines) sb.append(line).append('\n');
        logText.setText(sb);
        logScroll.post(() -> logScroll.scrollTo(0, logText.getHeight()));
    }

    private void refreshIdleStats() {
        statsText.setText("محفوظ في الجوال: " + store.resultsCount()
                + " | مفحوص: " + store.checkedCount()
                + " | انتظار: " + store.loadQueue().size());
    }

    private void toggleDim() {
        dimmed = !dimmed;
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = dimmed ? 0.01f : WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
        getWindow().setAttributes(lp);
        dimButton.setText(dimmed ? "إضاءة" : "تعتيم");
    }

    private void toggleLog() {
        boolean visible = logScroll.getVisibility() == View.VISIBLE;
        logScroll.setVisibility(visible ? View.GONE : View.VISIBLE);
        logToggleButton.setText(visible ? "إظهار السجل" : "إخفاء السجل");
    }

    // ---------------------------------------------------------------- export / import

    private void exportCsv() {
        if (store.resultsCount() == 0) {
            toast("لا توجد حسابات مستخرجة بعد");
            return;
        }
        Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("text/csv");
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmm", Locale.US).format(new Date());
        i.putExtra(Intent.EXTRA_TITLE, "gold_accounts_" + stamp + ".csv");
        try {
            startActivityForResult(i, REQ_EXPORT);
        } catch (Exception e) {
            toast("تعذر فتح نافذة الحفظ");
        }
    }

    private void pickImportFile() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        try {
            startActivityForResult(i, REQ_IMPORT);
        } catch (Exception e) {
            toast("تعذر فتح الملفات");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();

        if (requestCode == REQ_EXPORT) {
            try (OutputStream out = getContentResolver().openOutputStream(uri)) {
                if (out == null) throw new IOException("no output stream");
                out.write(store.resultsCsv().getBytes(StandardCharsets.UTF_8));
                toast("✅ تم التصدير: " + store.resultsCount() + " حساب");
            } catch (Exception e) {
                toast("❌ فشل التصدير: " + e.getMessage());
            }
        } else if (requestCode == REQ_IMPORT) {
            try (InputStream in = getContentResolver().openInputStream(uri)) {
                if (in == null) throw new IOException("no input stream");
                List<String> names = Store.parseUsernames(Store.readStream(in));
                if (names.isEmpty()) {
                    toast("الملف لا يحتوي يوزرات");
                    return;
                }
                pendingImport = names;
                chooseImportTarget();
            } catch (Exception e) {
                toast("❌ فشل قراءة الملف: " + e.getMessage());
            }
        }
    }

    private void chooseImportTarget() {
        String[] labels = {
                "قائمة الانتظار (queue.json)",
                "المفحوصين (checked_users.json)",
                "المستخرجين - لمنع التكرار (extracted_users.json)"
        };
        String[] keys = {"queue", "checked", "extracted"};
        new AlertDialog.Builder(this)
                .setTitle("استيراد " + pendingImport.size() + " يوزر إلى:")
                .setItems(labels, (dialog, which) -> {
                    int added = store.importUsernames(keys[which], pendingImport);
                    pendingImport = null;
                    toast("✅ أُضيف " + added + " يوزر جديد");
                    refreshIdleStats();
                })
                .setNegativeButton("إلغاء", null)
                .show();
    }

    // ---------------------------------------------------------------- settings

    private void showSettings() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(18), dp(8), dp(18), dp(8));

        CheckBox blockImages = new CheckBox(this);
        blockImages.setText("توفير البيانات (عدم تحميل الصور)");
        blockImages.setChecked(prefs.getBoolean("block_images", true));
        box.addView(blockImages);

        boolean running = extractor.isRunning();
        addButton(box, "استيراد ملف (queue / checked_users)", !running, v -> pickImportFile());
        addButton(box, "مسح قائمة الانتظار", !running, v -> confirm("مسح قائمة الانتظار؟", () -> {
            store.clearQueue();
            refreshIdleStats();
            toast("تم المسح");
        }));
        addButton(box, "مسح المفحوصين", !running, v -> confirm("مسح كل المفحوصين؟ سيُعاد فحصهم من جديد.", () -> {
            store.clearChecked();
            refreshIdleStats();
            toast("تم المسح");
        }));
        addButton(box, "تسجيل الخروج من X", !running, v -> confirm("تسجيل الخروج من X؟ ستحتاج تسجيل الدخول من جديد.", () -> {
            browser.clearCookies();
            browser.loadAsync("https://x.com/");
            toast("تم تسجيل الخروج");
        }));

        ScrollView scroll = new ScrollView(this);
        scroll.addView(box);

        new AlertDialog.Builder(this)
                .setTitle("الإعدادات")
                .setView(scroll)
                .setPositiveButton("حفظ", (dialog, which) -> {
                    prefs.edit().putBoolean("block_images", blockImages.isChecked()).apply();
                    browser.setBlockImages(blockImages.isChecked());
                    toast("تم الحفظ");
                })
                .setNegativeButton("إلغاء", null)
                .show();
    }

    private void addButton(LinearLayout box, String text, boolean enabled, View.OnClickListener listener) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setEnabled(enabled);
        b.setOnClickListener(listener);
        box.addView(b);
    }

    private void confirm(String message, Runnable action) {
        new AlertDialog.Builder(this)
                .setMessage(message)
                .setPositiveButton("نعم", (dialog, which) -> action.run())
                .setNegativeButton("لا", null)
                .show();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
    }

    // ---------------------------------------------------------------- lifecycle

    @Override
    protected void onPause() {
        super.onPause();
        browser.saveSession(); // keep the login even if Android closes the app
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (extractor.isRunning()) {
            toast("الاستخراج يعمل - اضغط «إيقاف» أولاً");
            return;
        }
        if (browser.goBack()) return;
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        extractor.stop();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        browser.destroy();
        super.onDestroy();
    }
}
