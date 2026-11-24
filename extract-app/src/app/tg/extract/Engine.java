package app.tg.extract;

import android.content.Context;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * One extraction task at a time, on its own thread: the original script's steps and pauses, in the app's browser,
 * with everything kept on the phone. From the start account, it opens its verified followers, reads the country of
 * each one on its «About» page, keeps the gold-verified ones of the chosen countries, and goes on from the followers
 * of every account found in those countries.
 */
final class Engine {
    private Engine() {
    }

    // The original script's numbers.
    private static final long SCROLL_DELAY = 5_000;
    private static final int NO_LOCATION_PAUSE = 3;
    private static final int NO_LOCATION_CHECK = 5;
    private static final int NO_LOCATION_STOP = 10;
    private static final int NAV_FAIL_PAUSE = 3;
    private static final int NAV_FAIL_LONG_PAUSE = 6;
    private static final long NAV_FAIL_SHORT_WAIT = 60_000;
    private static final long NAV_FAIL_LONG_WAIT = 3_600_000;

    // What opening a page gave.
    private static final int OK = 0;
    private static final int EMPTY = 1;
    private static final int FAILED = 2;
    private static final int SIGNED_OUT = 3;
    private static final int LOCKED = 4;

    private static final String SIGNED_OUT_END = "🔴 الجلسة منتهية - سجّل الدخول من جديد ثم ابدأ";
    private static final String LOCKED_END = "🔴 طلبت X التحقق من الحساب - افتح «المتصفح» وأكمل التحقق، ثم ابدأ من جديد";

    static volatile boolean running;
    static volatile boolean stopping;
    static volatile Runnable listener;
    static volatile String status = "جاهز";
    static volatile int done;
    static volatile int target;
    private static Thread thread;
    private static Context app;
    private static final ArrayDeque<String> LOG = new ArrayDeque<>();
    private static final AtomicBoolean pendingChange = new AtomicBoolean();

    /** The task was stopped by the user: it unwinds from wherever it waits. */
    private static final class Stop extends RuntimeException {
        Stop() {
            super(null, null, false, false);
        }
    }

    private static final class Cell {
        String username;
        String name;
        String bio;
        String image;
        String badge;
    }

    // ---------------------------------------------------------------- start and stop

    static synchronized boolean start(Context context, Store store, String seed, int count, Set<String> countries) {
        if (running) {
            return false;
        }
        app = context.getApplicationContext();
        running = true;
        stopping = false;
        done = 0;
        target = count;
        status = "يبدأ...";
        final Set<String> allowed = new HashSet<>(countries);
        thread = new Thread(() -> run(store, seed, count, allowed), "extract");
        thread.start();
        changed();
        return true;
    }

    static synchronized void stop() {
        if (!running || stopping) {
            return;
        }
        stopping = true;
        status = "يتوقف...";
        if (thread != null) {
            thread.interrupt();
        }
        changed();
    }

    private static void run(Store store, String seed, int count, Set<String> allowed) {
        WorkService.start(app);
        Web.onMain(() -> {
            Web.taskStarted();
            return true;
        }, 5_000, false);
        String end;
        try {
            end = extract(store, seed, count, allowed);
        } catch (Stop stop) {
            end = "⛔ أُوقفت المهمة - " + done + "/" + count;
        } catch (Throwable t) {
            end = "❌ خطأ في الاستخراج: " + t;
        }
        Thread.interrupted();                                   // (a stop's interruption is over)
        store.saveQueue();
        log(end);
        status = end;
        Web.onMain(() -> {
            Web.taskEnded();
            return true;
        }, 5_000, false);
        synchronized (Engine.class) {
            running = false;
            stopping = false;
            thread = null;
        }
        WorkService.stop(app);
        WorkService.done(app, end);
        changed();
    }

    // ---------------------------------------------------------------- the task (the script's extract_users)

    private static String extract(Store store, String seed, int count, Set<String> allowed) {
        log("🔍 بدء استخراج " + count + " حساب ذهبي من @" + seed);
        log("📋 عدد المستخدمين المفحوصين مسبقًا: " + store.checkedCount());
        if (!Web.onMain(Web::loggedIn, 5_000, false)) {
            return "🔴 لم تسجّل الدخول - اضغط «تسجيل الدخول» أولًا";
        }
        store.prepare(seed);
        int checkedNow = 0;
        int duplicates = 0;
        int notGold = 0;
        int cached = 0;
        int noLocation = 0;
        int navFails = 0;

        while (done < count) {
            checkStop();
            String current = store.poll();
            if (current == null) {
                break;
            }
            log("   📂 استخراج متابعي @" + current + "...");
            status("يفتح متابعي @" + current);
            int generation = Web.generation;
            int page = openFollowers(current);
            if (Web.generation != generation) {
                log("🔄 المتصفح انهار - إعادة تشغيله...");
                log("✅ تم إعادة تشغيل المتصفح");
                store.pushFront(current);
                noLocation = 0;
                continue;
            }
            if (page == SIGNED_OUT || page == LOCKED) {
                store.pushFront(current);
                return page == LOCKED ? LOCKED_END : SIGNED_OUT_END;
            }
            if (page == EMPTY) {
                navFails = 0;
                log("      ⚪ لا يوجد متابعون موثّقون ظاهرون لـ @" + current + " (تخطي)");
                sleep(SCROLL_DELAY);
                continue;
            }
            if (page == FAILED) {
                navFails++;
                log("      ⚠️ تخطي @" + current + " - فشل التحميل");
                log("      📉 فشل متتالي: " + navFails);
                if (navFails >= NAV_FAIL_LONG_PAUSE) {
                    log("🔴 " + navFails + " فشل متتالي - توقف ساعة كاملة...");
                    store.pushFront(current);
                    pause(NAV_FAIL_LONG_WAIT);
                    log("🔄 فتح متصفح جديد بعد التوقف الطويل...");
                    freshBrowser();
                    log("▶️ استئناف العمل...");
                    navFails = 0;
                } else if (navFails >= NAV_FAIL_PAUSE) {
                    log("⏸️ " + navFails + " فشل متتالي - توقف دقيقة + متصفح جديد...");
                    store.pushFront(current);
                    pause(NAV_FAIL_SHORT_WAIT);
                    freshBrowser();
                    log("▶️ استئناف العمل...");
                }
                continue;
            }
            navFails = 0;
            sleep(4_000);

            List<Cell> cells = collect();
            if (Web.generation != generation) {
                log("🔄 المتصفح انهار أثناء الاستخراج - إعادة تشغيله...");
                store.pushFront(current);
                noLocation = 0;
                continue;
            }
            if (cells == null) {
                log("      ⚠️ تعذّر قراءة صفحة @" + current + " (تخطي)");
                continue;
            }
            int gold = 0;
            for (Cell cell : cells) {
                if ("gold".equals(cell.badge)) {
                    gold++;
                }
            }
            log("      🟡 حسابات ذهبية في الصفحة: " + gold + "/" + cells.size());

            int found = 0;
            for (Cell user : cells) {
                checkStop();
                if (store.isExtracted(user.username)) {
                    duplicates++;
                    continue;
                }
                if (store.isChecked(user.username)) {
                    cached++;
                    continue;
                }
                store.addChecked(user.username);
                checkedNow++;
                status("يفحص @" + user.username);

                int before = Web.generation;
                String location = locationOf(user.username);
                if (Web.generation != before) {
                    log("🔄 المتصفح انهار أثناء فحص الموقع - إعادة تشغيله...");
                    log("✅ تم إعادة تشغيل المتصفح");
                    noLocation = 0;
                    break;
                }
                String country = location == null ? null : Text.countryOf(location, allowed);

                if (country != null) {
                    noLocation = 0;
                    log("      ✓ @" + user.username + " - الموقع: " + location + " | الشارة: " + badgeName(user.badge));
                    // Every account of the chosen countries widens the search (as in the script).
                    if (store.addToQueue(user.username)) {
                        log("      ➕ @" + user.username + " - أُضيف لقائمة الانتظار");
                    }
                    if (!"gold".equals(user.badge)) {
                        notGold++;
                        log("      ⏭️ @" + user.username + " - ليس ذهبي (تخطي)");
                        sleep(3_000);
                        continue;
                    }
                    if (done >= count) {
                        break;
                    }
                    store.addResult(user.username, user.name, user.bio, location, Text.nameOf(country), user.image, current);
                    done++;
                    found++;
                    log("      ✅ @" + user.username + " - ذهبي من " + Text.nameOf(country) + " - تم الاستخراج (" + done + "/" + count + ")");
                } else if (location != null) {
                    noLocation = 0;
                    log("      ❌ @" + user.username + " - " + location);
                } else {
                    noLocation++;
                    log("      ❌ @" + user.username + " - بدون موقع (متتالي: " + noLocation + ")");
                    if (noLocation >= NO_LOCATION_STOP) {
                        log("🔴 إيقاف - " + noLocation + " بدون موقع متتالية");
                        return "🔴 توقفت المهمة: " + noLocation + " حسابات متتالية بلا موقع - " + done + "/" + count;
                    } else if (noLocation >= NO_LOCATION_CHECK) {
                        log("⚠️ " + noLocation + " بدون موقع - التحقق من الجلسة...");
                        int session = checkSession();
                        if (session == FAILED) {
                            log("🔄 إعادة تشغيل المتصفح...");
                            freshBrowser();
                            noLocation = 0;
                            break;
                        }
                        if (session == SIGNED_OUT || session == LOCKED) {
                            log("🔴 الجلسة منتهية - إيقاف");
                            return session == LOCKED ? LOCKED_END : SIGNED_OUT_END;
                        }
                        log("✅ الجلسة صالحة - متابعة");
                        noLocation = 0;
                    } else if (noLocation == NO_LOCATION_PAUSE) {
                        log("⏸️ توقف دقيقة (" + NO_LOCATION_PAUSE + " بدون موقع)...");
                        pause(60_000);
                        log("▶️ استئناف...");
                    }
                }
                sleep(3_000);
            }

            if (found > 0) {
                log("      ✅ الإجمالي: " + done + "/" + count);
            }
            log("   📊 انتظار: " + store.queueSize() + " | مكرر: " + duplicates + " | غير ذهبي: " + notGold
                + " | مفحوص: " + store.checkedCount() + " | مستخرج كليًا: " + store.extractedCount() + " | مؤقت: " + cached);
            sleep(SCROLL_DELAY);
        }
        if (done >= count) {
            return "✅ اكتملت - " + done + "/" + count + " من " + checkedNow + " مفحوص";
        }
        return "✅ انتهت قائمة الانتظار - " + done + "/" + count + " من " + checkedNow + " مفحوص";
    }

    // ---------------------------------------------------------------- pages

    /** The account's verified followers (the script: load, 7 s, then up to 60 s for the first account cell). */
    private static int openFollowers(String user) {
        String path = "/" + user + "/verified_followers";
        int opened = navigate("https://x.com" + path, path, 30_000);
        if (opened != OK) {
            return opened;
        }
        sleep(7_000);
        long end = SystemClock.elapsedRealtime() + 60_000;
        while (true) {
            Object value = evaluate(Scripts.CELLS, 5_000);
            if (value instanceof JSONObject) {
                JSONObject page = (JSONObject) value;
                int moved = signInState(page.optString("h"));
                if (moved != OK) {
                    return moved;
                }
                if (page.optInt("cells") > 0) {
                    return OK;
                }
                if (page.optBoolean("empty")) {
                    return EMPTY;
                }
            }
            if (SystemClock.elapsedRealtime() >= end) {
                return FAILED;
            }
            sleep(1_000);
        }
    }

    /** The country on the account's «About» page; null when the page has none (or did not open). */
    private static String locationOf(String user) {
        String path = "/" + user + "/about";
        if (navigate("https://x.com" + path, path, 30_000) != OK) {
            return null;
        }
        long start = SystemClock.elapsedRealtime();
        sleep(5_000);                                            // (the script reads after 5 s)
        while (true) {
            Object value = evaluate(Scripts.LOCATION, 5_000);
            if (value instanceof JSONObject && !((JSONObject) value).isNull("loc")) {
                String location = ((JSONObject) value).optString("loc").trim();
                if (!location.isEmpty()) {
                    return location;
                }
            }
            if (SystemClock.elapsedRealtime() - start >= 12_000) {    // (a slow phone gets a few seconds more)
                return null;
            }
            sleep(1_500);
        }
    }

    /** The script's check_session: X's home page, and whether X sends it to signing in. */
    private static int checkSession() {
        log("🔐 جاري التحقق من صلاحية الجلسة...");
        int generation = Web.generation;
        if (!Web.onMain(Web::loggedIn, 5_000, false)) {
            log("❌ الجلسة منتهية");
            return SIGNED_OUT;
        }
        int opened = navigate("https://x.com/home", "/home", 30_000);
        if (Web.generation != generation) {
            log("🔴 المتصفح مغلق");
            return FAILED;
        }
        if (opened == SIGNED_OUT || opened == LOCKED) {
            log("❌ الجلسة منتهية");
            return opened;
        }
        sleep(5_000);
        Object value = evaluate(Scripts.READY, 3_000);
        String href = value instanceof JSONObject ? ((JSONObject) value).optString("h") : "";
        int state = signInState(href);
        if (state != OK) {
            log("❌ الجلسة منتهية");
            return state;
        }
        log("✅ الجلسة صالحة - " + href);
        return OK;
    }

    /** Loads the address and waits until the page there has loaded (or X sent it to signing in). */
    private static int navigate(String url, String path, long timeoutMs) {
        Web.load(url);
        String wanted = path.toLowerCase(Locale.ROOT);
        long end = SystemClock.elapsedRealtime() + timeoutMs;
        sleep(500);
        while (true) {
            Object value = evaluate(Scripts.READY, 3_000);
            if (value instanceof JSONObject) {
                JSONObject page = (JSONObject) value;
                String href = page.optString("h");
                int moved = signInState(href);
                if (moved != OK) {
                    return moved;
                }
                String at = Web.pathOf(href);
                if (at != null && at.toLowerCase(Locale.ROOT).startsWith(wanted) && !"loading".equals(page.optString("s"))) {
                    return OK;
                }
            }
            if (SystemClock.elapsedRealtime() >= end) {
                return FAILED;
            }
            sleep(500);
        }
    }

    private static int signInState(String href) {
        String path = Web.pathOf(href);
        if (Web.isLockedPath(path)) {
            return LOCKED;
        }
        return Web.isSignInPath(path) ? SIGNED_OUT : OK;
    }

    private static List<Cell> collect() {
        Object value = evaluate(Scripts.COLLECT, 10_000);
        if (!(value instanceof JSONArray)) {
            return null;
        }
        JSONArray found = (JSONArray) value;
        List<Cell> cells = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < found.length(); i++) {
            JSONObject one = found.optJSONObject(i);
            if (one == null) {
                continue;
            }
            String username = one.optString("username");
            if (username.isEmpty() || !seen.add(username.toLowerCase(Locale.ROOT))) {
                continue;                                       // (once per page)
            }
            Cell cell = new Cell();
            cell.username = username;
            cell.name = one.optString("name", username);
            cell.bio = one.optString("bio");
            cell.image = one.optString("image");
            cell.badge = one.optString("badge", "none");
            cells.add(cell);
        }
        return cells;
    }

    private static Object evaluate(String script, long timeoutMs) {
        String json = Web.js(script, timeoutMs);
        checkStop();
        if (json == null) {
            return null;
        }
        try {
            return new JSONTokener(json).nextValue();
        } catch (JSONException e) {
            return null;
        }
    }

    private static void freshBrowser() {
        Web.onMain(() -> {
            Web.recreate();
            Web.taskStarted();
            return true;
        }, 10_000, false);
        sleep(3_000);
    }

    // ---------------------------------------------------------------- waiting

    private static void checkStop() {
        if (stopping) {
            throw new Stop();
        }
    }

    /** Waits, and ends the task at once if the user stops it meanwhile. */
    private static void sleep(long ms) {
        long end = SystemClock.elapsedRealtime() + ms;
        while (true) {
            checkStop();
            long left = end - SystemClock.elapsedRealtime();
            if (left <= 0) {
                return;
            }
            try {
                Thread.sleep(Math.min(left, 250));
            } catch (InterruptedException e) {
                // (stop() interrupts: the check above ends the task)
            }
        }
    }

    private static void pause(long ms) {
        String until = new SimpleDateFormat("HH:mm", Locale.US).format(new Date(System.currentTimeMillis() + ms));
        status("⏸️ توقف مؤقت حتى " + until);
        sleep(ms);
    }

    // ---------------------------------------------------------------- what the page and the notification show

    private static String badgeName(String badge) {
        if ("gold".equals(badge)) {
            return "🟡 ذهبية";
        }
        if ("blue".equals(badge)) {
            return "🔵 زرقاء";
        }
        if ("grey".equals(badge)) {
            return "⚪ رمادية";
        }
        return "بدون توثيق";
    }

    private static void status(String now) {
        status = now;
        changed();
    }

    static void log(String line) {
        String stamped = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date()) + "  " + line;
        synchronized (LOG) {
            LOG.addLast(stamped);
            while (LOG.size() > 600) {
                LOG.removeFirst();
            }
        }
        changed();
    }

    /** The last lines of the log, the newest first or last. */
    static String logText(int lines, boolean newestFirst) {
        List<String> copy;
        synchronized (LOG) {
            copy = new ArrayList<>(LOG);
        }
        int from = Math.max(0, copy.size() - lines);
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < copy.size() - from; i++) {
            String line = copy.get(newestFirst ? copy.size() - 1 - i : from + i);
            if (out.length() > 0) {
                out.append('\n');
            }
            out.append(line);
        }
        return out.toString();
    }

    static String progress(Store store) {
        String head;
        if (running) {
            head = (stopping ? "⏳ يتوقف..." : "🟢 يعمل - هذه المهمة: " + done + "/" + target) + "\n" + status;
        } else {
            head = status;
        }
        return head + "\nفي الانتظار: " + store.queueSize() + "  |  مفحوص: " + store.checkedCount();
    }

    /** The page and the notification are refreshed (at most a few times a second). */
    static void changed() {
        if (!pendingChange.compareAndSet(false, true)) {
            return;
        }
        Web.MAIN.postDelayed(() -> {
            pendingChange.set(false);
            Runnable now = listener;
            if (now != null) {
                now.run();
            }
            if (app != null) {
                WorkService.update(app);
            }
        }, 250);
    }
}
