package app.tg.groups;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.net.http.SslError;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Base64;
import android.util.SparseArray;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.webkit.ProfileStore;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import com.chaquo.python.PyObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The tasks' browsers. The groups script asks for a browser the way it asks Playwright for one; in the app that
 * request comes here (from playwright/_host.py), and the browser is a WebView on the «المتصفح» tab, with the
 * account's own profile (its cookies and storage, kept apart from other accounts where this phone's WebView can).
 * The script then drives the WebView through its DevTools socket; what DevTools does not do the same on every
 * phone is done here with Android's own calls: the page's size, cookies, stopping requests, and a picture.
 * Every method the script calls is static and safe from any thread.
 */
public final class BrowserHost {
    static final Handler MAIN = new Handler(Looper.getMainLooper());
    static final Object VISIBLE_LOCK = new Object();
    // On the main thread only:
    static final SparseArray<Browser> browsers = new SparseArray<>();
    static MainActivity activity;
    // A page that is not on the screen is slowed by the browser itself almost to a stop (its timers run once a
    // second, then once a minute, and nothing is drawn), so a task hardly moves while the app is in the background.
    // While the app's page is not on the screen its browsers are therefore moved into a window of one pixel that
    // Android keeps over the other apps (the permission «الظهور فوق التطبيقات»): as far as the browser knows they
    // are still on the screen, and they work at their full speed. Nothing shows to the user.
    static BrowserStage backstage;
    static boolean away;                       // the app's page is not on the screen
    static volatile boolean offScreen;         // the browsers are in the one-pixel window
    static int lastId;
    // Whether the user may type in the browsers by hand («الكتابة في المتصفح بيدي» in the menu). Otherwise they never
    // take the keyboard: the script types through DevTools, and the panel keeps the keyboard for itself.
    static boolean manualTyping;
    // From any thread:
    static volatile String lastError = "";
    static volatile boolean visible;

    /** One open browser. */
    static final class Browser {
        int id;
        String profile = "";
        boolean ownProfile;
        WebView view;
        int cssHeight = 1800;
        volatile PyObject router;
        volatile boolean download;
        volatile boolean gone;
    }

    private BrowserHost() {
    }

    private static <T> T onMain(Callable<T> work, long timeoutMs, T fallback) {
        try {
            if (Looper.myLooper() == Looper.getMainLooper()) {
                return work.call();
            }
            FutureTask<T> task = new FutureTask<>(work);
            MAIN.post(task);
            return task.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            lastError = cause.getClass().getSimpleName() + ": " + cause.getMessage();
            AppLog.error("تعذّر أمر لمتصفح التطبيق", cause);
            return fallback;
        }
    }

    static boolean supported(String feature) {
        try {
            return WebViewFeature.isFeatureSupported(feature);
        } catch (RuntimeException e) {
            return false;
        }
    }

    // ---------------------------------------------------------------- what the script calls

    /** Whether each account can have a profile of its own on this phone (a WebView of late 2023 or newer). */
    public static boolean profilesSupported() {
        return onMain(() -> supported(WebViewFeature.MULTI_PROFILE), 10_000, false);
    }

    public static String lastError() {
        return lastError;
    }

    /** A new browser for this profile: its number (0 or less when it could not be made, and lastError says why).
     * It is the phone's own browser as it is: its user agent, its language and its width are left alone, and only
     * the page's height (in CSS pixels) is the script's. Its first page is titled gg-&lt;number&gt;, which is how
     * the script finds it among the WebViews DevTools lists. */
    public static int open(String profile, int height) {
        lastError = "";
        return onMain(() -> {
            MainActivity host = activity;
            if (host == null) {
                lastError = "التطبيق ليس مفتوحًا على الشاشة. افتحه ثم أعد المحاولة.";
                AppLog.error("لم يُفتح متصفح: " + lastError);
                return -1;
            }
            Browser b = new Browser();
            b.id = ++lastId;
            b.cssHeight = Math.max(200, height);
            b.profile = "gg-" + String.valueOf(profile).replaceAll("[^A-Za-z0-9_-]", "_");
            WebView view = new WebView(host);
            if (supported(WebViewFeature.MULTI_PROFILE)) {
                try {
                    ProfileStore.getInstance().getOrCreateProfile(b.profile);
                    WebViewCompat.setProfile(view, b.profile);
                    b.ownProfile = true;
                } catch (RuntimeException e) {
                    b.ownProfile = false;
                }
            }
            if (!b.ownProfile) {
                sharedProfileFor(host, b.profile);
            }
            b.view = view;
            configure(b);
            browsers.put(b.id, b);
            (away && openBackstage(host) ? backstage : host.stage).add(b);
            view.loadDataWithBaseURL(null, "<html><head><title>gg-" + b.id + "</title></head><body></body></html>",
                "text/html", "utf-8", null);
            host.browsersChanged();
            AppLog.add("متصفح " + b.id + " فُتح (ملف الحساب " + (b.ownProfile ? "مستقل" : "مشترك") + "، طول الصفحة " + b.cssHeight + ")");
            return b.id;
        }, 30_000, -1);
    }

    public static void close(int id) {
        onMain(() -> {
            Browser b = browsers.get(id);
            if (b != null) {
                try {
                    cookiesOf(b).flush();
                } catch (RuntimeException e) {
                    // (the profile still keeps what the page already stored)
                }
                browsers.remove(id);
                destroy(b);
                AppLog.add("متصفح " + id + " أُغلق");
                if (activity != null) {
                    activity.browsersChanged();
                }
            }
            return true;
        }, 15_000, false);
    }

    /** The page's height in CSS pixels: the WebView itself is made that tall. Its width stays the phone's. */
    public static void resize(int id, int height) {
        onMain(() -> {
            Browser b = browsers.get(id);
            if (b == null) {
                return false;
            }
            b.cssHeight = Math.max(200, height);
            BrowserStage where = stageOf(b);
            if (where != null) {
                where.sized(b);
            }
            return true;
        }, 10_000, false);
    }

    public static void setCookie(int id, String url, String cookie) {
        onMain(() -> {
            Browser b = browsers.get(id);
            if (b == null) {
                return false;
            }
            CookieManager cookies = cookiesOf(b);
            cookies.setCookie(url, cookie);
            cookies.flush();
            return true;
        }, 10_000, false);
    }

    /** decide(url, accept, hasRange, mainFrame, method) is asked for each request of this browser: true stops it.
     * (null: every request goes through.) */
    public static void setRouter(int id, PyObject decide) {
        onMain(() -> {
            Browser b = browsers.get(id);
            if (b != null) {
                b.router = decide;
            }
            return true;
        }, 10_000, false);
    }

    /** Whether the browser was handed a file to download since this was last asked. */
    public static boolean downloadStarted(int id) {
        return onMain(() -> {
            Browser b = browsers.get(id);
            boolean was = b != null && b.download;
            if (b != null) {
                b.download = false;
            }
            return was;
        }, 5_000, false);
    }

    public static void show(int id) {
        onMain(() -> {
            Browser b = browsers.get(id);
            BrowserStage where = b == null ? null : stageOf(b);
            if (where != null) {
                where.front(b);
            }
            return true;
        }, 5_000, false);
    }

    /** A picture of one box of the page (CSS pixels, from the top left of what shows), drawn by Android itself: for
     * a phone whose DevTools gives none. Base64 of a JPEG or PNG, or "" when it could not be drawn. */
    public static String capture(int id, float x, float y, float width, float height, boolean jpeg, int quality) {
        return onMain(() -> {
            Browser b = browsers.get(id);
            if (b == null || b.view == null || width < 1 || height < 1 || width * height > 40_000_000f) {
                return "";
            }
            float density = b.view.getResources().getDisplayMetrics().density;
            Bitmap bitmap = Bitmap.createBitmap(Math.round(width), Math.round(height), Bitmap.Config.ARGB_8888);
            bitmap.eraseColor(Color.WHITE);
            Canvas canvas = new Canvas(bitmap);
            canvas.scale(1f / density, 1f / density);
            canvas.translate(-x * density, -y * density);
            b.view.draw(canvas);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            bitmap.compress(jpeg ? Bitmap.CompressFormat.JPEG : Bitmap.CompressFormat.PNG, Math.max(1, Math.min(100, quality)), out);
            bitmap.recycle();
            return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);
        }, 20_000, "");
    }

    /** The page is in the background with a task open: keep its browsers running instead of pausing them. */
    public static void keepAwake() {
        onMain(() -> {
            for (int i = 0; i < browsers.size(); i++) {
                WebView view = browsers.valueAt(i).view;
                if (view != null) {
                    view.onResume();
                    view.resumeTimers();
                }
            }
            return true;
        }, 5_000, false);
    }

    /** Wait (at most this long) until the app is on the screen: a WebView that is not shown draws nothing and takes
     * no clicks, so a task rests here while the app is in the background. Whether it is on the screen. */
    public static boolean awaitVisible(long maxMs) {
        long end = System.currentTimeMillis() + Math.max(0, maxMs);
        synchronized (VISIBLE_LOCK) {
            while (!visible) {
                long left = end - System.currentTimeMillis();
                if (left <= 0) {
                    break;
                }
                try {
                    VISIBLE_LOCK.wait(Math.min(left, 1000));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
            return visible;
        }
    }

    // ---------------------------------------------------------------- the page that shows them

    static void attach(MainActivity host) {
        activity = host;
    }

    /** The page is going away for good: its browsers go with it (the script sees them closed). */
    static void detach(MainActivity host) {
        if (activity != host) {
            return;
        }
        for (int i = browsers.size() - 1; i >= 0; i--) {
            destroy(browsers.valueAt(i));
        }
        browsers.clear();
        closeBackstage(host);
        away = false;
        activity = null;
    }

    private static BrowserStage stageOf(Browser b) {
        return b.view != null && b.view.getParent() instanceof BrowserStage ? (BrowserStage) b.view.getParent() : null;
    }

    /** Whether Android lets the app keep its one-pixel window over other apps («الظهور فوق التطبيقات»). */
    static boolean mayStayOnScreen(Context context) {
        try {
            return Settings.canDrawOverlays(context);
        } catch (RuntimeException e) {
            return false;
        }
    }

    /** The script's side asks: a picture is then drawn by the app itself (the screen has one pixel of the page). */
    public static boolean offScreen() {
        return offScreen;
    }

    /** The app's page left the screen: its browsers go on working in the one-pixel window, where Android allows
     * it; where it does not, they stay in the page, slowed by the browser until the app is back. */
    static void leaveScreen(MainActivity host) {
        away = true;
        if (!mayStayOnScreen(host)) {
            if (browsers.size() > 0) {
                AppLog.error("التطبيق في الخلفية بلا إذن «الظهور فوق التطبيقات»: المتصفح يبطؤ كثيرًا إلى أن تعود إلى التطبيق");
                setVisible(true);
                keepAwake();
            } else {
                AppLog.add("التطبيق في الخلفية");
                setVisible(false);
            }
            return;
        }
        if (browsers.size() > 0 && openBackstage(host)) {
            for (View view : childrenOf(host.stage)) {
                move(view, host.stage, backstage);
            }
            AppLog.add("التطبيق في الخلفية: المتصفحات (" + browsers.size() + ") تكمل عملها بسرعتها في نافذة الخلفية");
        } else {
            AppLog.add("التطبيق في الخلفية");
        }
        setVisible(true);                  // (a task that starts now opens its browser in that window)
    }

    /** The app's page is on the screen again: the browsers are back in its «المتصفح» tab. */
    static void returnToScreen(MainActivity host) {
        away = false;
        if (backstage != null) {
            for (View view : childrenOf(backstage)) {
                move(view, backstage, host.stage);
            }
            closeBackstage(host);
        }
        AppLog.add("التطبيق في الواجهة");
        setVisible(true);
    }

    private static boolean openBackstage(MainActivity host) {
        if (backstage != null) {
            return true;
        }
        if (!mayStayOnScreen(host)) {
            return false;
        }
        try {
            Context app = host.getApplicationContext();
            BrowserStage made = new BrowserStage(app);
            int width = host.stage.getWidth();
            made.fixedWidth = width > 0 ? width : host.getResources().getDisplayMetrics().widthPixels;
            WindowManager.LayoutParams params = new WindowManager.LayoutParams(1, 1,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                    | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS | WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
                PixelFormat.TRANSLUCENT);
            params.gravity = Gravity.TOP | Gravity.START;
            params.alpha = 0.01f;                                   // (its one pixel does not show)
            params.setTitle("gg-background");
            ((WindowManager) app.getSystemService(Context.WINDOW_SERVICE)).addView(made, params);
            backstage = made;
            offScreen = true;
            return true;
        } catch (RuntimeException e) {
            AppLog.error("تعذّر فتح نافذة الخلفية، فالمتصفح يبطؤ إلى أن تعود إلى التطبيق", e);
            return false;
        }
    }

    private static void closeBackstage(Context context) {
        BrowserStage old = backstage;
        backstage = null;
        offScreen = false;
        if (old == null) {
            return;
        }
        try {
            ((WindowManager) context.getApplicationContext().getSystemService(Context.WINDOW_SERVICE)).removeViewImmediate(old);
        } catch (RuntimeException e) {
            // (already gone)
        }
    }

    private static java.util.List<View> childrenOf(ViewGroup group) {
        java.util.List<View> views = new java.util.ArrayList<>();
        for (int i = 0; i < group.getChildCount(); i++) {
            views.add(group.getChildAt(i));
        }
        return views;
    }

    /** A browser's WebView from one holder to the other, as it is (its page goes on). */
    private static void move(View view, ViewGroup from, BrowserStage to) {
        from.removeView(view);
        if (view.getTag() instanceof Browser) {
            to.add((Browser) view.getTag());
        } else {
            to.addView(view);
        }
    }

    static void setVisible(boolean now) {
        synchronized (VISIBLE_LOCK) {
            visible = now;
            VISIBLE_LOCK.notifyAll();
        }
    }

    static int count() {
        return browsers.size();
    }

    /** The login button: let the user type in the browser, and show that tab. View changes stay on the main thread. */
    public static void prepareManualLogin() {
        onMain(() -> {
            setManualTyping(true);
            if (activity != null) {
                activity.showTab(true);
                if (browsers.size() > 0) {
                    WebView view = browsers.valueAt(browsers.size() - 1).view;
                    if (view != null) {
                        view.requestFocus();
                    }
                }
            }
            return true;
        }, 8_000, false);
    }

    static void setManualTyping(boolean allowed) {
        manualTyping = allowed;
        onMain(() -> {
            for (int i = 0; i < browsers.size(); i++) {
                WebView view = browsers.valueAt(i).view;
                if (view != null) {
                    view.setFocusable(allowed);
                    view.setFocusableInTouchMode(allowed);
                    if (!allowed) {
                        view.clearFocus();
                    }
                }
            }
            return true;
        }, 5_000, false);
    }

    // ---------------------------------------------------------------- one browser

    private static void destroy(Browser b) {
        b.gone = true;
        b.router = null;
        WebView view = b.view;
        b.view = null;
        if (view == null) {
            return;
        }
        try {
            if (view.getParent() instanceof ViewGroup) {
                ((ViewGroup) view.getParent()).removeView(view);
            }
            view.stopLoading();
            view.setWebViewClient(new WebViewClient());
            view.setWebChromeClient(null);
            view.destroy();
        } catch (RuntimeException e) {
            // (already gone)
        }
    }

    private static CookieManager cookiesOf(Browser b) {
        if (b.ownProfile) {
            try {
                return ProfileStore.getInstance().getProfile(b.profile).getCookieManager();
            } catch (RuntimeException e) {
                // (falls back to the shared ones below)
            }
        }
        return CookieManager.getInstance();
    }

    /** Where accounts cannot have profiles of their own, they take turns on the app's one profile: when another
     * account's turn comes, what X kept there for the one before (its cookies and storage) is removed first. */
    private static void sharedProfileFor(Context context, String profile) {
        SharedPreferences prefs = context.getSharedPreferences("browser", Context.MODE_PRIVATE);
        if (profile.equals(prefs.getString("shared", ""))) {
            return;
        }
        CookieManager cookies = CookieManager.getInstance();
        for (String site : new String[] {"x.com", "twitter.com"}) {
            for (String host : new String[] {site, "www." + site, "api." + site, "mobile." + site}) {
                String kept = cookies.getCookie("https://" + host + "/");
                if (kept == null) {
                    continue;
                }
                for (String pair : kept.split(";")) {
                    String name = pair.split("=", 2)[0].trim();
                    if (name.isEmpty()) {
                        continue;
                    }
                    cookies.setCookie("https://" + host + "/", name + "=; Max-Age=0; Path=/; Domain=." + site);
                    cookies.setCookie("https://" + host + "/", name + "=; Max-Age=0; Path=/");
                }
            }
            WebStorage.getInstance().deleteOrigin("https://" + site);
            WebStorage.getInstance().deleteOrigin("https://www." + site);
        }
        cookies.flush();
        prefs.edit().putString("shared", profile).apply();
    }

    @SuppressLint("SetJavaScriptEnabled")
    private static void configure(Browser b) {
        WebView view = b.view;
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setSupportMultipleWindows(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setGeolocationEnabled(false);
        settings.setSaveFormData(false);
        settings.setSupportZoom(false);
        settings.setTextZoom(100);                   // (the phone's font size does not change X's layout)
        settings.setUseWideViewPort(false);          // (the page is as wide as the WebView, in CSS pixels)
        settings.setLoadWithOverviewMode(false);
        // (The user agent, and what the browser says of itself beyond it, are the phone's own: nothing is set.)
        // (Every WebView adds its app's name to each request, as the header X-Requested-With, and Android gives no
        // way to leave it out: «فحص المتصفح» says so in its report.)
        CookieManager cookies = cookiesOf(b);
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(view, true);
        view.setBackgroundColor(Color.WHITE);
        view.setOverScrollMode(View.OVER_SCROLL_NEVER);
        view.setVerticalScrollBarEnabled(false);
        view.setHorizontalScrollBarEnabled(false);
        view.setFocusable(manualTyping);
        view.setFocusableInTouchMode(manualTyping);
        view.setTag(b);
        view.setWebViewClient(new Client(b));
        view.setWebChromeClient(new Chrome());
        view.setDownloadListener((url, agent, disposition, mime, length) -> b.download = true);
    }

    static final class Client extends WebViewClient {
        final Browser browser;

        Client(Browser browser) {
            this.browser = browser;
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            // Only web pages open here: a link meant for another app (X's own, a store) is not followed.
            String scheme = String.valueOf(request.getUrl().getScheme()).toLowerCase(java.util.Locale.ROOT);
            return !(scheme.equals("https") || scheme.equals("http") || scheme.equals("about") || scheme.equals("data")
                || scheme.equals("blob"));
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            String url = String.valueOf(request.getUrl());
            if (url.contains("dm-core-") && url.contains(".js")) {
                byte[] body = patchedChatScript(url);
                if (body != null) {
                    HashMap<String, String> headers = new HashMap<>();
                    headers.put("Access-Control-Allow-Origin", "*");
                    headers.put("Cache-Control", "no-store");
                    return new WebResourceResponse("text/javascript", "utf-8", 200, "OK", headers, new ByteArrayInputStream(body));
                }
            }
            PyObject decide = browser.router;
            if (decide == null || browser.gone) {
                return null;
            }
            try {
                Map<String, String> headers = request.getRequestHeaders();
                String accept = "";
                boolean range = false;
                if (headers != null) {
                    for (Map.Entry<String, String> header : headers.entrySet()) {
                        String name = String.valueOf(header.getKey());
                        if (name.equalsIgnoreCase("Accept")) {
                            accept = String.valueOf(header.getValue());
                        } else if (name.equalsIgnoreCase("Range")) {
                            range = true;
                        }
                    }
                }
                PyObject stop = decide.call(String.valueOf(request.getUrl()), accept, range, request.isForMainFrame(),
                    String.valueOf(request.getMethod()));
                if (stop != null && stop.toBoolean()) {
                    return new WebResourceResponse("text/plain", "utf-8", 403, "Blocked", new HashMap<>(),
                        new ByteArrayInputStream(new byte[0]));
                }
            } catch (RuntimeException e) {
                // (the request goes through)
            }
            return null;
        }

        // What goes wrong with the page itself (not with the pictures and the like inside it) is noted in the log.
        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                AppLog.error("متصفح " + browser.id + ": لم تُفتح الصفحة " + address(request) + " — " + error.getDescription()
                    + " (" + error.getErrorCode() + ")");
            }
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request.isForMainFrame()) {
                AppLog.error("متصفح " + browser.id + ": الموقع ردّ بـ " + response.getStatusCode() + " على " + address(request));
            }
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            AppLog.error("متصفح " + browser.id + ": شهادة الموقع غير موثوقة: " + error.getUrl());
            handler.cancel();
        }

        private static String address(WebResourceRequest request) {
            String url = String.valueOf(request.getUrl());
            int query = url.indexOf('?');
            url = query < 0 ? url : url.substring(0, query);
            return url.length() > 140 ? url.substring(0, 140) + "…" : url;
        }

        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            // The page's process stopped (Android took its memory back, or it crashed): this browser is closed, and
            // the script opens another.
            AppLog.error("متصفح " + browser.id + ": توقفت عملية الصفحة (" + (detail.didCrash() ? "انهارت" : "أنهاها النظام، غالبًا لنقص الذاكرة") + ")");
            MAIN.post(() -> {
                Browser b = browsers.get(browser.id);
                if (b != null) {
                    browsers.remove(b.id);
                    destroy(b);
                    if (activity != null) {
                        activity.browsersChanged();
                    }
                }
            });
            return true;
        }
    }

    // X's chat code («dm-core-….js»), as the app serves it to its browsers: where the code hands over its libsodium
    // (…=e,….libsodium=…), it also hands it to the page's fast collecting (FAST_COLLECT_JS in the app's script),
    // which then reads the messages' text as X's page decrypts them. A copy for each address, made once.
    static final HashMap<String, byte[]> patchedChatScripts = new HashMap<>();
    static final Pattern SODIUM_HANDOVER =
        Pattern.compile("([A-Za-z_$][\\w$]*)=([A-Za-z_$][\\w$]*),([A-Za-z_$][\\w$]*)\\.libsodium=\\1(?![\\w$])");

    /** The chat code with its libsodium handed to the page too, or null (X's own then loads as it is). */
    static byte[] patchedChatScript(String url) {
        synchronized (patchedChatScripts) {
            if (patchedChatScripts.containsKey(url)) {
                return patchedChatScripts.get(url);
            }
        }
        byte[] patched = null;
        try {
            java.net.HttpURLConnection connection = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
            connection.setConnectTimeout(20_000);
            connection.setReadTimeout(40_000);
            connection.setRequestProperty("Accept", "*/*");
            byte[] raw;
            try (java.io.InputStream input = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] chunk = new byte[65536];
                for (int count = input.read(chunk); count >= 0; count = input.read(chunk)) {
                    output.write(chunk, 0, count);
                }
                raw = output.toByteArray();
            } finally {
                connection.disconnect();
            }
            // (each byte as one character, and back: the code is not changed anywhere else)
            String code = new String(raw, java.nio.charset.StandardCharsets.ISO_8859_1);
            Matcher found = SODIUM_HANDOVER.matcher(code);
            if (found.find()) {
                String added = found.group(0) + ",globalThis.__wrapSodium&&globalThis.__wrapSodium(" + found.group(1) + ",\"patched\")";
                patched = (code.substring(0, found.start()) + added + code.substring(found.end()))
                    .getBytes(java.nio.charset.StandardCharsets.ISO_8859_1);
                AppLog.add("ملف دردشة X عُدّل لقراءة الرسائل (" + found.group(0) + ")");
            } else {
                AppLog.error("لم يوجد في ملف دردشة X موضع تسليم libsodium: الجمع السريع يعتمد على الطريقة الأخرى");
            }
        } catch (Exception e) {
            AppLog.error("تعذّر تنزيل ملف دردشة X لتعديله: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        synchronized (patchedChatScripts) {
            patchedChatScripts.put(url, patched);
        }
        return patched;
    }

    /** Nothing a page shows may wait for an answer nobody is there to give. */
    static final class Chrome extends WebChromeClient {
        @Override
        public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
            result.cancel();
            return true;
        }

        @Override
        public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
            result.cancel();
            return true;
        }

        @Override
        public boolean onJsPrompt(WebView view, String url, String message, String defaultValue, JsPromptResult result) {
            result.cancel();
            return true;
        }

        @Override
        public boolean onJsBeforeUnload(WebView view, String url, String message, JsResult result) {
            result.confirm();
            return true;
        }

        @Override
        public void onPermissionRequest(PermissionRequest request) {
            request.deny();
        }

        @Override
        public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
            callback.invoke(origin, false, false);
        }

        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<android.net.Uri[]> callback, FileChooserParams params) {
            callback.onReceiveValue(null);
            return true;
        }
    }
}
