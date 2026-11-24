package com.goldextractor;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.InputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * Walks verified followers, checks each account's "Account based in" country,
 * and saves only Gulf accounts with the gold badge. Everything stays on the phone.
 */
public class Extractor {

    public interface Listener {
        void onLog(String msg);

        void onStats(String stats);

        void onFinished();
    }

    // ---------------------------------------------------------------- settings (same as the script)
    private static final int NO_LOCATION_PAUSE_THRESHOLD = 3;
    private static final int NO_LOCATION_CHECK_THRESHOLD = 5;
    private static final int NO_LOCATION_STOP_THRESHOLD = 10;

    private static final int NAV_FAIL_PAUSE_THRESHOLD = 3;       // 3 failures in a row -> 1 minute + new browser
    private static final int NAV_FAIL_LONG_PAUSE_THRESHOLD = 6;  // 6 failures in a row -> 1 hour
    private static final long NAV_FAIL_SHORT_PAUSE_MS = 60_000L;
    private static final long NAV_FAIL_LONG_PAUSE_MS = 3_600_000L;

    private static final long SCROLL_DELAY_MS = 5_000L;
    private static final long BETWEEN_USERS_MS = 3_000L;
    private static final int QUEUE_MAX = 5000;

    private static final int SESSION_OK = 1;
    private static final int SESSION_EXPIRED = 0;
    private static final int SESSION_CRASHED = -1;

    // Latin names are matched as whole words, so "Romania" does not count as "Oman"
    private static final Pattern LATIN_GULF = Pattern.compile(
            "(?<![a-z])(saudi arabia|saudi|ksa|qatar|kuwait|oman|uae|united arab emirates|emirates|dubai|abu dhabi)(?![a-z])");
    // Arabic names after normalize(): no diacritics, أ/إ/آ -> ا
    private static final String[] ARABIC_GULF = {
            "السعودية", "قطر", "كويت", "عمان", "الامارات", "دبي", "ابوظبي", "ابو ظبي"
    };

    private final Browser browser;
    private final Store store;
    private final Listener listener;
    private final String jsUsers;
    private final String jsLocation;
    private final String jsPageState;
    private final String jsSession;

    private volatile boolean stopRequested = false;
    private boolean running = false;
    private Thread thread;

    public Extractor(Context ctx, Browser browser, Store store, Listener listener) {
        this.browser = browser;
        this.store = store;
        this.listener = listener;
        this.jsUsers = asset(ctx, "extract_users.js");
        this.jsLocation = asset(ctx, "extract_location.js");
        this.jsPageState = asset(ctx, "page_state.js");
        this.jsSession = asset(ctx, "session_state.js");
    }

    private static String asset(Context ctx, String name) {
        try (InputStream in = ctx.getAssets().open(name)) {
            return Store.readStream(in);
        } catch (IOException e) {
            throw new RuntimeException("missing asset " + name, e);
        }
    }

    // ---------------------------------------------------------------- control

    public synchronized boolean isRunning() {
        return running;
    }

    public synchronized void start(String seed, int target) {
        if (running) return;
        running = true;
        stopRequested = false;
        thread = new Thread(() -> {
            try {
                runTask(seed, target);
            } catch (InterruptedException e) {
                log("⛔ تم الإيقاف");
            } catch (Throwable t) {
                log("❌ خطأ غير متوقع: " + t);
            } finally {
                synchronized (Extractor.this) {
                    running = false;
                    thread = null;
                }
                listener.onFinished();
            }
        }, "gold-extractor");
        thread.start();
    }

    public synchronized void stop() {
        stopRequested = true;
        if (thread != null) thread.interrupt();
    }

    // ---------------------------------------------------------------- the task (extract_users)

    private void runTask(String seedRaw, int target) throws InterruptedException {
        String seed = cleanUsername(seedRaw);
        if (seed.isEmpty()) {
            log("⚠️ يوزر البداية فارغ");
            return;
        }
        if (target <= 0) target = 100;

        log("🔍 بدء استخراج " + target + " حساب ذهبي من @" + seed);
        log("📋 عدد المستخدمين المفحوصين مسبقاً: " + store.checkedCount());

        // make sure we are logged in before burning through the queue
        int session = checkSession();
        if (session == SESSION_CRASHED) {
            browser.recreate();
            session = checkSession();
        }
        if (session == SESSION_EXPIRED) {
            log("🔴 لست مسجلاً في X - اضغط «تسجيل الدخول» وسجّل دخولك أولاً");
            return;
        }

        List<String> queue = store.loadQueue();
        if (!queue.isEmpty()) log("📂 تم تحميل " + queue.size() + " مستخدم من قائمة الانتظار");
        if (queue.isEmpty()) {
            queue.add(seed);
        } else if (!containsIgnoreCase(queue, seed)) {
            queue.add(0, seed);
        }

        int extracted = 0;
        int checked = 0;
        int duplicates = 0;
        int notGold = 0;
        int cached = 0;
        int noLocation = 0;
        int navFailures = 0;

        try {
            while (extracted < target && !queue.isEmpty()) {
                checkStop();

                String current = queue.remove(0);
                store.saveQueue(queue);
                log("   📂 استخراج متابعي @" + current + "...");
                stats("مستخرج: " + extracted + "/" + target + " | انتظار: " + queue.size()
                        + " | الحالي: @" + current);

                // ---- open the verified followers page
                String state;
                try {
                    state = openFollowers(current);
                } catch (Browser.CrashedException e) {
                    log("🔄 المتصفح انهار - إعادة تشغيله...");
                    browser.recreate();
                    log("✅ تم إعادة تشغيل المتصفح");
                    queue.add(0, current);
                    store.saveQueue(queue);
                    noLocation = 0;
                    continue;
                }

                if ("empty".equals(state)) {
                    navFailures = 0;
                    log("      ⏭️ @" + current + " - لا يوجد متابعون موثقون / حساب محمي (تخطي)");
                    continue;
                }
                if ("login".equals(state)) {
                    log("🔴 الجلسة منتهية - اضغط «تسجيل الدخول» ثم «ابدأ»");
                    queue.add(0, current);
                    store.saveQueue(queue);
                    return;
                }
                if (!"ok".equals(state)) {
                    navFailures++;
                    log("      ⚠️ تخطي @" + current + " - فشل تحميل المتابعين (" + state + ")");
                    log("      📉 فشل متتالي: " + navFailures);
                    if (navFailures >= NAV_FAIL_LONG_PAUSE_THRESHOLD) {
                        log("🔴 " + navFailures + " فشل متتالي - توقف ساعة كاملة...");
                        queue.add(0, current);
                        store.saveQueue(queue);
                        sleep(NAV_FAIL_LONG_PAUSE_MS);
                        log("🔄 فتح متصفح جديد بعد التوقف الطويل...");
                        browser.recreate();
                        log("▶️ استئناف العمل...");
                        navFailures = 0;
                    } else if (navFailures >= NAV_FAIL_PAUSE_THRESHOLD) {
                        log("⏸️ " + navFailures + " فشل متتالي - توقف دقيقة + متصفح جديد...");
                        queue.add(0, current);
                        store.saveQueue(queue);
                        sleep(NAV_FAIL_SHORT_PAUSE_MS);
                        browser.recreate();
                        log("▶️ استئناف العمل...");
                    }
                    continue;
                }
                navFailures = 0;

                // ---- read the user cells (with badge type)
                JSONArray raw;
                try {
                    Object r = browser.eval(jsUsers, 15000);
                    raw = r instanceof JSONArray ? (JSONArray) r : new JSONArray();
                } catch (Browser.CrashedException e) {
                    log("🔄 المتصفح انهار أثناء الاستخراج - إعادة تشغيله...");
                    browser.recreate();
                    queue.add(0, current);
                    store.saveQueue(queue);
                    noLocation = 0;
                    continue;
                }

                // remove duplicates inside the same page
                Map<String, JSONObject> unique = new LinkedHashMap<>();
                for (int i = 0; i < raw.length(); i++) {
                    JSONObject u = raw.optJSONObject(i);
                    if (u == null) continue;
                    String un = u.optString("username", "");
                    if (un.isEmpty()) continue;
                    String k = un.toLowerCase(Locale.ROOT);
                    if (!unique.containsKey(k)) unique.put(k, u);
                }
                int goldInPage = 0;
                for (JSONObject u : unique.values()) {
                    if ("gold".equals(u.optString("badge"))) goldInPage++;
                }
                log("      🟡 حسابات ذهبية في الصفحة: " + goldInPage + "/" + unique.size());

                int savedHere = 0;
                for (JSONObject u : unique.values()) {
                    checkStop();
                    String un = u.optString("username");

                    if (store.isExtracted(un)) {   // extracted before -> never again
                        duplicates++;
                        continue;
                    }
                    if (store.isChecked(un)) {
                        cached++;
                        continue;
                    }
                    store.addChecked(un);
                    checked++;

                    String location;
                    try {
                        location = getLocation(un);
                    } catch (Browser.CrashedException e) {
                        log("🔄 المتصفح انهار أثناء فحص الموقع - إعادة تشغيله...");
                        browser.recreate();
                        log("✅ تم إعادة تشغيل المتصفح");
                        noLocation = 0;
                        break;
                    }

                    if (location != null && isAllowedCountry(location)) {
                        noLocation = 0;
                        String badge = u.optString("badge", "none");
                        log("      ✓ @" + un + " - الموقع: " + location + " | الشارة: " + badgeName(badge));

                        // every Gulf account goes to the queue (to keep expanding)
                        if (!containsIgnoreCase(queue, un) && queue.size() < QUEUE_MAX) {
                            queue.add(un);
                            store.saveQueue(queue);
                            log("      ➕ @" + un + " - أُضيف لقائمة الانتظار");
                        }

                        // only gold accounts are saved
                        if (!"gold".equals(badge)) {
                            notGold++;
                            log("      ⏭️ @" + un + " - ليس ذهبي (تخطي)");
                        } else {
                            if (store.addExtracted(record(u, location))) {
                                extracted++;
                                savedHere++;
                                log("      ✅ @" + un + " - ذهبي خليجي - تم الحفظ (" + extracted + "/" + target + ")");
                                stats("مستخرج: " + extracted + "/" + target + " | انتظار: " + queue.size()
                                        + " | محفوظ كلياً: " + store.resultsCount());
                            } else {
                                duplicates++;
                            }
                            if (extracted >= target) break;
                        }
                    } else if (location != null) {
                        noLocation = 0;
                        log("      ❌ @" + un + " - " + location);
                    } else {
                        noLocation++;
                        log("      ❌ @" + un + " - بدون موقع (متتالي: " + noLocation + ")");

                        if (noLocation >= NO_LOCATION_STOP_THRESHOLD) {
                            log("🔴 إيقاف - " + noLocation + " بدون موقع متتالية");
                            store.saveQueue(queue);
                            return;
                        } else if (noLocation >= NO_LOCATION_CHECK_THRESHOLD) {
                            log("⚠️ " + noLocation + " بدون موقع - التحقق من الجلسة...");
                            int s = checkSession();
                            if (s == SESSION_CRASHED) {
                                log("🔄 إعادة تشغيل المتصفح...");
                                browser.recreate();
                                noLocation = 0;
                                break;
                            } else if (s == SESSION_EXPIRED) {
                                log("🔴 الجلسة منتهية - اضغط «تسجيل الدخول» ثم «ابدأ»");
                                store.saveQueue(queue);
                                return;
                            } else {
                                log("✅ الجلسة صالحة - متابعة");
                                noLocation = 0;
                            }
                        } else if (noLocation == NO_LOCATION_PAUSE_THRESHOLD) {
                            log("⏸️ توقف دقيقة (" + NO_LOCATION_PAUSE_THRESHOLD + " بدون موقع)...");
                            sleep(60_000L);
                            log("▶️ استئناف...");
                        }
                    }
                    sleep(BETWEEN_USERS_MS);
                }

                if (savedHere > 0) {
                    log("      💾 تم حفظ " + savedHere + " حساب في الجوال - الإجمالي: " + extracted + "/" + target);
                }
                String summary = "📊 انتظار: " + queue.size() + " | مكرر: " + duplicates
                        + " | غير ذهبي: " + notGold + " | مفحوص: " + store.checkedCount()
                        + " | مستخرج كلياً: " + store.extractedCount() + " | مؤقت: " + cached;
                log("   " + summary);
                stats("مستخرج: " + extracted + "/" + target + " | انتظار: " + queue.size()
                        + " | محفوظ كلياً: " + store.resultsCount());
                sleep(SCROLL_DELAY_MS);
            }

            log("✅ اكتملت - " + extracted + "/" + target + " من " + checked + " مفحوص");
            stats("✅ اكتملت: " + extracted + "/" + target + " | محفوظ كلياً: " + store.resultsCount());

        } catch (InterruptedException e) {
            store.saveQueue(queue);
            throw e;
        } catch (Exception e) {
            log("❌ خطأ في الاستخراج: " + e);
            store.saveQueue(queue);
        }
    }

    // ---------------------------------------------------------------- browser steps

    /** JS prefix telling the page scripts which address they must be on. */
    private static String setPath(String path) {
        return "window.__gePath=" + JSONObject.quote(path.toLowerCase(Locale.ROOT)) + ";";
    }

    /** True once the about page of the expected account shows the "Account based in" label. */
    private static String locationLabelJs(String path) {
        return setPath(path)
                + "(function(){if((location.pathname||'').toLowerCase()!==window.__gePath)return false;"
                + "var t=document.body?document.body.innerText:'';"
                + "return /الحساب موجود في|Account based in|Compte basé dans|Based in/.test(t);})();";
    }

    /** Opens /verified_followers and returns ok / empty / login / error / timeout. */
    private String openFollowers(String username) throws InterruptedException, Browser.CrashedException {
        String path = "/" + username + "/verified_followers";
        String stateJs = setPath(path) + jsPageState;
        browser.load("https://x.com" + path, 30000);
        sleep(2000);
        String last = "timeout";
        String pending = null;
        int stable = 0;
        long end = System.currentTimeMillis() + 60_000L;
        while (System.currentTimeMillis() < end) {
            Object r = browser.eval(stateJs, 8000);
            String s = r instanceof String ? (String) r : "loading";
            if ("ok".equals(s)) {
                sleep(3000); // let more cells render
                return "ok";
            }
            if ("empty".equals(s) || "login".equals(s) || "error".equals(s)) {
                // must stay the same for a few checks, so a page that is still loading is not misread
                stable = s.equals(pending) ? stable + 1 : 1;
                pending = s;
                last = s;
                if (stable >= 3) return s;
            } else {
                pending = null;
                stable = 0;
            }
            sleep(1500);
        }
        return last;
    }

    private String getLocation(String username) throws InterruptedException, Browser.CrashedException {
        String path = "/" + username + "/about";
        String labelJs = locationLabelJs(path);
        browser.load("https://x.com" + path, 30000);
        long end = System.currentTimeMillis() + 8000;
        while (System.currentTimeMillis() < end) {
            if (Boolean.TRUE.equals(browser.eval(labelJs, 5000))) {
                sleep(500);
                break;
            }
            sleep(700);
        }
        Object r = browser.eval(setPath(path) + jsLocation, 10000);
        if (r instanceof String) {
            String s = ((String) r).trim();
            return s.isEmpty() ? null : s;
        }
        return null;
    }

    private int checkSession() throws InterruptedException {
        log("🔐 جاري التحقق من تسجيل الدخول...");
        try {
            browser.load("https://x.com/home", 30000);
            String sessionJs = setPath("/home") + jsSession;
            String state = "unknown";
            long end = System.currentTimeMillis() + 15000;
            while (System.currentTimeMillis() < end) {
                sleep(1500);
                Object r = browser.eval(sessionJs, 5000);
                state = r instanceof String ? (String) r : "unknown";
                if (!"unknown".equals(state)) break;
            }
            if ("logged_out".equals(state)) {
                log("❌ غير مسجّل الدخول");
                return SESSION_EXPIRED;
            }
            if ("logged_in".equals(state)) {
                log("✅ مسجّل الدخول");
                return SESSION_OK;
            }
            log("⚠️ حالة الدخول غير واضحة: " + browser.getUrl() + " - متابعة");
            return SESSION_OK;
        } catch (Browser.CrashedException e) {
            log("🔴 المتصفح مغلق");
            return SESSION_CRASHED;
        }
    }

    // ---------------------------------------------------------------- helpers

    private static JSONObject record(JSONObject u, String location) {
        JSONObject r = new JSONObject();
        try {
            r.put("username", u.optString("username", ""));
            r.put("name", u.optString("name", ""));
            r.put("bio", u.optString("bio", ""));
            r.put("location", location);
            r.put("profile_image", u.optString("profile_image_url", ""));
            r.put("badge", u.optString("badge", ""));
            r.put("extracted_at", new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date()));
        } catch (JSONException ignored) {
        }
        return r;
    }

    static String normalize(String s) {
        String t = s.toLowerCase(Locale.ROOT);
        t = t.replaceAll("[\\u064B-\\u0652\\u0640]", ""); // Arabic diacritics + tatweel
        t = t.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا');
        return t;
    }

    static boolean isAllowedCountry(String location) {
        if (location == null) return false;
        String t = normalize(location.trim());
        if (t.isEmpty()) return false;
        if (LATIN_GULF.matcher(t).find()) return true;
        for (String k : ARABIC_GULF) {
            if (t.contains(k)) return true;
        }
        return false;
    }

    static String cleanUsername(String s) {
        if (s == null) return "";
        String t = s.trim();
        t = t.replaceAll("^https?://(www\\.|mobile\\.)?(x|twitter)\\.com/", "");
        if (t.startsWith("@")) t = t.substring(1);
        int cut = t.indexOf('/');
        if (cut >= 0) t = t.substring(0, cut);
        cut = t.indexOf('?');
        if (cut >= 0) t = t.substring(0, cut);
        return t.trim();
    }

    private static boolean containsIgnoreCase(List<String> list, String value) {
        for (String s : list) {
            if (s.equalsIgnoreCase(value)) return true;
        }
        return false;
    }

    private static String badgeName(String badge) {
        switch (badge) {
            case "gold":
                return "🟡 ذهبية";
            case "blue":
                return "🔵 زرقاء";
            case "grey":
                return "⚪ رمادية";
            default:
                return "بدون توثيق";
        }
    }

    private void checkStop() throws InterruptedException {
        if (stopRequested || Thread.currentThread().isInterrupted()) throw new InterruptedException();
    }

    private void sleep(long ms) throws InterruptedException {
        checkStop();
        Thread.sleep(ms);
    }

    private void log(String msg) {
        listener.onLog(msg);
    }

    private void stats(String s) {
        listener.onStats(s);
    }
}
