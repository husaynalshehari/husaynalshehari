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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * One extraction task at a time, on its own thread: the original script's steps and pauses, in the app's browser,
 * with everything kept on the phone. From the start account, it opens its verified followers (scrolling down the
 * whole list), reads the country of the followers on their «About» page, and keeps the gold-verified ones of the
 * chosen countries. The search goes on from the followers of the accounts in the search list: a list of accounts of
 * those countries kept at its size (50 unless chosen otherwise). While the list is short of its size, every
 * follower's country is read to fill it; once it is full, only the gold-verified followers' «About» page is opened.
 * Each account the search takes from the list is made up for by the next one found.
 */
final class Engine {
    private Engine() {
    }

    // How far down a followers' page is read.
    private static final int MAX_PER_PAGE = 2000;
    private static final int QUIET_ROUNDS = 4;              // (scrolls in a row with no one new: the list's end)
    private static final int QUIET_ROUNDS_LOADING = 8;      // (the same while X still shows it is loading)

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
    /** The search list's size (the user's choice, 50 unless changed). */
    static volatile int pool = 50;
    /** The direct way's failures in a row (after 3 the task keeps to the page and its scrolling). */
    private static int directFails;
    /** The direct «About» check's failures in a row (after 3 the task keeps to the «About» page). */
    private static int aboutFails;
    private static boolean aboutWorks;
    /** X's direct «no country»: 0-1 being checked against the page, 2 taken as is, -1 not trusted (task long). */
    private static int noneState;
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

    static synchronized boolean start(Context context, Store store, String seed, int count, int listSize,
                                      Set<String> countries) {
        if (running) {
            return false;
        }
        app = context.getApplicationContext();
        running = true;
        stopping = false;
        done = 0;
        target = count;
        pool = listSize;
        status = "يبدأ...";
        final Set<String> allowed = new HashSet<>(countries);
        thread = new Thread(() -> run(store, seed, count, listSize, allowed), "extract");
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

    private static void run(Store store, String seed, int count, int listSize, Set<String> allowed) {
        WorkService.start(app);
        Web.onMain(() -> {
            Web.taskStarted();
            return true;
        }, 5_000, false);
        String end;
        try {
            end = extract(store, seed, count, listSize, allowed);
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

    private static String extract(Store store, String seed, int count, int listSize, Set<String> allowed) {
        log("🔍 بدء استخراج " + count + " حساب ذهبي من @" + seed);
        log("📋 المفحوصون مسبقًا: " + store.checkedCount() + " | قائمة البحث: " + store.queueSize() + "/" + listSize);
        if (!Web.onMain(Web::loggedIn, 5_000, false)) {
            return "🔴 لم تسجّل الدخول - اضغط «تسجيل الدخول» أولًا";
        }
        store.prepare(seed);
        directFails = 0;
        aboutFails = 0;
        aboutWorks = false;
        noneState = 0;
        int checkedNow = 0;
        int duplicates = 0;
        int notGold = 0;
        int cached = 0;
        int unopened = 0;
        int noLocation = 0;
        int navFails = 0;
        Boolean listFull = null;                                // (the search list's state last told in the log)

        while (done < count) {
            checkStop();
            String current = store.poll();
            if (current == null) {
                break;
            }
            log("   📂 استخراج متابعي @" + current + " (مباشر)...");
            status("يجلب متابعي @" + current + " مباشرة");
            List<Cell> cells = directFails >= 3 ? null : collectAllDirect(current);
            if (cells != null) {
                directFails = 0;
            } else if (directFails < 3 && ++directFails == 3) {
                log("   ↩️ الطريقة المباشرة فشلت 3 مرات متتالية - تُستخدم الصفحة والتمرير لبقية المهمة");
            }
            if (cells == null) {
                // Fall back to traditional page + scroll
                log("   ↩️ العودة لطريقة التحميل والتمرير لـ @" + current);
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
                sleep(1_500);
                cells = collectAll(current, generation);
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
            } else if (cells.isEmpty()) {
                log("      ⚪ لا يوجد متابعون موثّقون لـ @" + current + " (تخطي)");
                continue;
            }
            int gold = 0;
            int goldDone = 0;
            int goldSeen = 0;
            for (Cell cell : cells) {
                if ("gold".equals(cell.badge)) {
                    gold++;
                    if (store.isExtracted(cell.username)) {
                        goldDone++;
                    } else if (store.isGoldChecked(cell.username)) {
                        goldSeen++;
                    }
                }
            }
            log("      📜 جُمع " + cells.size() + " حساب من صفحة المتابعين | 🟡 الذهبية: " + gold
                + (gold > 0 ? " (جديدة: " + (gold - goldDone - goldSeen) + "، مستخرجة سابقًا: " + goldDone
                + "، فُحص موقعها سابقًا: " + goldSeen + ")" : ""));
            List<String> names = new ArrayList<>();
            for (Cell cell : cells) {
                names.add(cell.username);
            }
            int fresh = store.addCollected(names);
            log("      🗂️ ملف كل اليوزرات: +" + fresh + " جديد (المجموع " + store.collectedCount() + " بلا تكرار)");

            int found = 0;
            int unopenedHere = 0;
            for (Cell user : cells) {
                checkStop();
                boolean filling = store.queueSize() < listSize;
                listFull = tellList(listFull, !filling, store.queueSize(), listSize);
                if (store.isExtracted(user.username)) {
                    duplicates++;
                    continue;
                }
                boolean isGold = "gold".equals(user.badge);
                if (isGold ? store.isGoldChecked(user.username) : store.isChecked(user.username)) {
                    cached++;
                    continue;
                }
                if (!filling && !isGold) {
                    unopenedHere++;                             // (the list is full: only gold accounts are opened)
                    continue;
                }
                checkedNow++;
                status("يفحص @" + user.username);

                int before = Web.generation;
                String location = locationOf(user.username, isGold);
                if (Web.generation != before) {
                    log("🔄 المتصفح انهار أثناء فحص الموقع - إعادة تشغيله...");
                    log("✅ تم إعادة تشغيل المتصفح");
                    noLocation = 0;
                    break;
                }
                if (location != null) {                         // (read for sure: not asked again; else retried later)
                    store.addChecked(user.username);
                    if (isGold) {
                        store.addGoldChecked(user.username);
                    }
                }
                String country = location == null || location.isEmpty() ? null : Text.countryOf(location, allowed);

                if (country != null) {
                    noLocation = 0;
                    log("      ✓ @" + user.username + " - الموقع: " + location + " | الشارة: " + badgeName(user.badge));
                    // An account of the chosen countries joins the search list while the list is short of its size.
                    if (store.addToQueue(user.username, listSize)) {
                        log("      ➕ @" + user.username + " - أُضيف لقائمة البحث (" + store.queueSize() + "/" + listSize + ")");
                    }
                    if (!isGold) {
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
                } else if (location != null && location.isEmpty()) {
                    noLocation = 0;                             // (X answered for it: the session is fine)
                    log("      ⚪ @" + user.username + " - لا يوجد موقع مذكور");
                } else if (location != null) {
                    noLocation = 0;
                    log("      ❌ @" + user.username + " - " + location);
                } else {
                    noLocation++;
                    log("      ❌ @" + user.username + " - تعذّر قراءة الموقع (متتالي: " + noLocation + ")");
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

            if (unopenedHere > 0) {
                unopened += unopenedHere;
                log("      ⏭️ " + unopenedHere + " حساب غير ذهبي لم يُفتح موقعه (قائمة البحث مكتملة)");
            }
            if (found > 0) {
                log("      ✅ الإجمالي: " + done + "/" + count);
            }
            log("   📊 قائمة البحث: " + store.queueSize() + "/" + listSize + " | مكرر: " + duplicates
                + " | غير ذهبي: " + notGold + " | بلا فتح: " + unopened + " | مفحوص: " + store.checkedCount()
                + " | مستخرج كليًا: " + store.extractedCount() + " | مؤقت: " + cached);
            sleep(SCROLL_DELAY);
        }
        if (done >= count) {
            return "✅ اكتملت - " + done + "/" + count + " من " + checkedNow + " مفحوص";
        }
        return "✅ انتهت قائمة البحث - " + done + "/" + count + " من " + checkedNow + " مفحوص";
    }

    /** The log says when the search list fills up and when it is short again (and what that changes). */
    private static Boolean tellList(Boolean before, boolean full, int size, int listSize) {
        if (before == null || before != full) {
            log(full
                ? "      📋 قائمة البحث مكتملة (" + size + "/" + listSize + ") - يُفتح موقع الحسابات الذهبية فقط"
                : "      🔎 قائمة البحث " + size + "/" + listSize + " - يُفحص موقع كل الحسابات حتى تكتمل");
        }
        return full;
    }

    // ---------------------------------------------------------------- pages

    /** The account's verified followers (the script: load, 7 s, then up to 60 s for the first account cell). */
    private static int openFollowers(String user) {
        String path = "/" + user + "/verified_followers";
        int opened = navigate("https://x.com" + path, path, 30_000);
        if (opened != OK) {
            return opened;
        }
        sleep(2_000);
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

    /**
     * The account's country, asked straight from X first and read on its «About» page if that does not tell. "" when
     * X shows no country for the account (nothing to wait for), null when it could not be told.
     */
    private static String locationOf(String user, boolean gold) {
        if (aboutFails < 3) {
            String direct = aboutDirect(user);
            if (direct != null && !direct.isEmpty()) {
                return direct;
            }
            if (direct != null && gold) {
                String page = locationFromPage(user);           // (what the task looks for: the page is read too)
                return page != null ? page : "";
            }
            if (direct != null) {                               // (X's data: no country for this account)
                if (noneState >= 2) {
                    return "";
                }
                if (noneState < 0) {
                    return locationFromPage(user);
                }
                // The task's first two such answers are checked against the page: if it shows a country after all,
                // a missing country is always checked on the page from then on.
                String page = locationFromPage(user);
                if (page != null && !page.isEmpty()) {
                    noneState = -1;
                    log("   ⚠️ صفحة «حول» أظهرت موقعًا لم يُظهره الفحص المباشر - تُفتح «حول» لكل حساب بلا موقع");
                    return page;
                }
                if (page != null && ++noneState == 2) {
                    log("   ✅ «لا يوجد موقع» من الفحص المباشر مطابق للصفحة - لا تُفتح «حول» لهذه الحسابات بعد الآن");
                }
                return "";
            }
        }
        return locationFromPage(user);
    }

    /**
     * The country from X's «About» data, asked with the page's session (no page load). Null when it gave none (then
     * the «About» page is read). After 3 failures in a row, it is not asked again in the task.
     */
    private static String aboutDirect(String user) {
        evaluate(Scripts.START_FETCH_ABOUT.replace("%USERNAME%", user), 3_000);
        String error = "لا رد";
        for (int i = 0; i < 30; i++) {                          // (up to about 15 s)
            sleep(500);
            Object value = evaluate(Scripts.POLL_ABOUT_RESULT, 2_000);
            if (!(value instanceof String)) {
                continue;
            }
            try {
                JSONObject result = new JSONObject((String) value);
                if (result.isNull("error")) {
                    aboutFails = 0;
                    if (!aboutWorks) {
                        aboutWorks = true;
                        log("      ⚡ فحص الموقع المباشر يعمل");
                    }
                    // (Android gives "null" as text for a JSON null: a missing country is never taken as a place)
                    String country = result.isNull("country") ? "" : result.optString("country", "").trim();
                    if (!country.isEmpty()) {
                        return country;
                    }
                    // X's «About» data without a country: none is shown for the account (""). Without the data at
                    // all, the page is read instead (null).
                    return result.optBoolean("about") ? "" : null;
                }
                error = result.optString("error", "خطأ");
            } catch (JSONException e) {
                error = "رد غير مفهوم";
            }
            break;
        }
        aboutFails++;
        log("      ⚠️ فحص الموقع المباشر لـ @" + user + " فشل (" + error + ") - تُفتح صفحة «حول»");
        if (aboutFails == 3) {
            log("   ↩️ فحص الموقع المباشر فشل 3 مرات متتالية - تُستخدم صفحة «حول» لبقية المهمة");
        }
        return null;
    }

    /**
     * The country on the account's «About» page, read as soon as the page shows it. "" when the page is there with
     * its details (the date joined...) but no country; null when it did not open or show its details in time.
     */
    private static String locationFromPage(String user) {
        String path = "/" + user + "/about";
        if (navigate("https://x.com" + path, path, 30_000) != OK) {
            return null;
        }
        long start = SystemClock.elapsedRealtime();
        int lastLength = -1;
        sleep(1_500);
        while (true) {
            Object value = evaluate(Scripts.LOCATION, 5_000);
            if (value instanceof JSONObject) {
                JSONObject page = (JSONObject) value;
                String location = page.isNull("loc") ? "" : page.optString("loc").trim();
                if (!location.isEmpty()) {
                    return location;
                }
                int length = page.optInt("len", -1);
                if (page.optBoolean("loaded") && length == lastLength) {
                    return "";                                  // (its details are there, no country: no use waiting)
                }
                lastLength = length;
            }
            if (SystemClock.elapsedRealtime() - start >= 8_000) {
                return null;
            }
            sleep(1_000);
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

    /**
     * Every follower the page shows, scrolling down step by step until the list ends (no one new for a few steps) or
     * MAX_PER_PAGE are read. X keeps only the accounts near the screen in the page, so they are read at each step.
     * Null when the page could not be read at all (or the browser was made anew meanwhile).
     */

    /** Direct extraction of verified followers via GraphQL (no page load / scroll). Falls back if it fails. */
    private static List<Cell> collectAllDirect(String user) {
        String userId = fetchUserId(user);
        if (userId == null || userId.isEmpty()) {
            log("      ⚠️ تعذّر جلب معرف المستخدم لـ @" + user + " - سيتم استخدام الطريقة التقليدية");
            return null;
        }
        Map<String, Cell> all = new LinkedHashMap<>();
        String cursor = null;
        for (int page = 0; page < 120 && all.size() < MAX_PER_PAGE; page++) {  // safety limit
            checkStop();
            JSONObject pageResult = fetchVerifiedPage(userId, cursor);
            if (pageResult == null || (pageResult.has("error") && !pageResult.isNull("error"))) {
                String why = pageResult == null ? "لا رد" : pageResult.optString("error");
                if (all.isEmpty()) {
                    log("      ⚠️ فشل الجلب المباشر (" + why + ") - العودة للطريقة التقليدية");
                    return null;
                }
                log("      ⚠️ توقف الجلب المباشر عند " + all.size() + " (" + why + ") - يُكمل بما جُمع");
                break;
            }
            int before = all.size();
            JSONArray users = pageResult.optJSONArray("users");
            if (users != null) {
                for (int i = 0; i < users.length(); i++) {
                    JSONObject u = users.optJSONObject(i);
                    if (u == null) continue;
                    Cell cell = new Cell();
                    cell.username = u.optString("username");
                    cell.name = u.optString("name");
                    cell.bio = u.optString("bio");
                    cell.image = u.optString("image");
                    cell.badge = u.optString("badge");
                    if (cell.username != null && !cell.username.isEmpty()) {
                        all.put(cell.username.toLowerCase(Locale.ROOT), cell);
                    }
                }
            }
            String next = pageResult.optString("cursor", null);
            if (next == null || next.isEmpty() || "null".equals(next) || next.equals(cursor)) {
                break;
            }
            if (all.size() == before) {
                break;                                          // (no one new: the list's end, no use asking on)
            }
            cursor = next;
            status("يجلب متابعي @" + user + " مباشرة - جُمع " + all.size());
            sleep(800);  // small delay between API pages to avoid rate limit
        }
        if (all.isEmpty()) {
            log("      ⚠️ لم يُقرأ أحد بالطريقة المباشرة - العودة للطريقة التقليدية");
            return null;
        }
        List<Cell> cells = new ArrayList<>(all.values());
        return cells.size() > MAX_PER_PAGE ? new ArrayList<>(cells.subList(0, MAX_PER_PAGE)) : cells;
    }

    private static String fetchUserId(String screenName) {
        String script = Scripts.START_FETCH_USER_ID.replace("%SCREEN%", screenName.replace("'", "\'"));
        evaluate(script, 3_000);
        for (int i = 0; i < 20; i++) {  // poll up to ~10s
            sleep(500);
            Object val = evaluate(Scripts.POLL_GQL_RESULT, 2_000);
            if (val instanceof String) {
                try {
                    JSONObject obj = new JSONObject((String) val);
                    String id = obj.isNull("id") ? null : obj.optString("id", null);
                    return id == null || id.isEmpty() || "null".equals(id) ? null : id;
                } catch (JSONException e) {
                    return null;
                }
            }
        }
        return null;
    }

    private static JSONObject fetchVerifiedPage(String userId, String cursor) {
        String cur = (cursor == null) ? "null" : cursor.replace("'", "\'");
        String script = Scripts.START_FETCH_VERIFIED
            .replace("%USER_ID%", userId)
            .replace("%CURSOR%", cur);
        evaluate(script, 3_000);
        for (int i = 0; i < 30; i++) {  // poll up to ~15s
            sleep(500);
            Object val = evaluate(Scripts.POLL_GQL_RESULT, 2_000);
            if (val instanceof String) {
                try {
                    return new JSONObject((String) val);
                } catch (JSONException e) {
                    return null;
                }
            }
        }
        return null;
    }

    private static List<Cell> collectAll(String user, int generation) {
        Map<String, Cell> all = new LinkedHashMap<>();
        boolean readOnce = false;
        int quiet = 0;
        evaluate(Scripts.TOP, 5_000);
        sleep(800);
        for (int round = 0; round < 600; round++) {
            List<Cell> seen = collect();
            if (Web.generation != generation) {
                return null;
            }
            int before = all.size();
            if (seen != null) {
                readOnce = true;
                for (Cell cell : seen) {
                    String key = cell.username.toLowerCase(Locale.ROOT);
                    if (!all.containsKey(key)) {
                        all.put(key, cell);
                    }
                }
            }
            if (all.size() >= MAX_PER_PAGE) {
                break;
            }
            quiet = all.size() > before ? 0 : quiet + 1;
            Object scrolled = evaluate(Scripts.SCROLL, 5_000);
            boolean loading = scrolled instanceof JSONObject && ((JSONObject) scrolled).optBoolean("loading");
            boolean bottom = scrolled instanceof JSONObject && ((JSONObject) scrolled).optBoolean("bottom");
            if (quiet >= (loading ? QUIET_ROUNDS_LOADING : bottom ? 2 : QUIET_ROUNDS)) {
                break;                                          // (the list's end: no use waiting on)
            }
            status("يمرّر متابعي @" + user + " - جُمع " + all.size());
            sleep(1_500);
        }
        if (!readOnce) {
            return null;
        }
        List<Cell> cells = new ArrayList<>(all.values());
        return cells.size() > MAX_PER_PAGE ? new ArrayList<>(cells.subList(0, MAX_PER_PAGE)) : cells;
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
        return head + "\nقائمة البحث: " + store.queueSize() + "/" + pool + "  |  مفحوص: " + store.checkedCount();
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
