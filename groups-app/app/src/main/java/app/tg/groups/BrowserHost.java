package app.tg.groups;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.util.SparseArray;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.webkit.ProfileStore;
import androidx.webkit.UserAgentMetadata;
import androidx.webkit.WebSettingsCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import com.chaquo.python.PyObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.util.Arrays;
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
        int cssWidth = 600;
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

    /** A new browser for this profile, with the page this size in CSS pixels: its number (0 or less when it could
     * not be made, and lastError says why). Its first page is titled gg-&lt;number&gt;, which is how the script
     * finds it among the WebViews DevTools lists. */
    public static int open(String profile, String userAgent, int width, int height) {
        lastError = "";
        return onMain(() -> {
            MainActivity host = activity;
            if (host == null) {
                lastError = "التطبيق ليس مفتوحًا على الشاشة. افتحه ثم أعد المحاولة.";
                return -1;
            }
            Browser b = new Browser();
            b.id = ++lastId;
            b.cssWidth = Math.max(200, width);
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
            configure(b, String.valueOf(userAgent == null ? "" : userAgent));
            browsers.put(b.id, b);
            host.stage.add(b);
            view.loadDataWithBaseURL(null, "<html><head><title>gg-" + b.id + "</title></head><body></body></html>",
                "text/html", "utf-8", null);
            host.browsersChanged();
            return b.id;
        }, 30_000, -1);
    }

    public static void close(int id) {
        onMain(() -> {
            Browser b = browsers.get(id);
            if (b != null) {
                browsers.remove(id);
                destroy(b);
                if (activity != null) {
                    activity.browsersChanged();
                }
            }
            return true;
        }, 15_000, false);
    }

    /** The page's size in CSS pixels: the WebView itself is made that size (the tab shows it scaled to fit). */
    public static void resize(int id, int width, int height) {
        onMain(() -> {
            Browser b = browsers.get(id);
            if (b == null) {
                return false;
            }
            b.cssWidth = Math.max(200, width);
            b.cssHeight = Math.max(200, height);
            if (activity != null) {
                activity.stage.sized(b);
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
            if (b != null && activity != null) {
                activity.stage.front(b);
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
        activity = null;
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

    static void setManualTyping(boolean allowed) {
        manualTyping = allowed;
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
            if (activity != null) {
                activity.stage.remove(view);
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
    private static void configure(Browser b, String userAgent) {
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
        if (!userAgent.isEmpty()) {
            settings.setUserAgentString(userAgent);
        }
        // (Every WebView adds its app's name to each request, as the header X-Requested-With, and Android gives no
        // way to leave it out: «فحص المتصفح» says so in its report.)
        if (!userAgent.isEmpty() && supported(WebViewFeature.USER_AGENT_METADATA)) {
            try {
                WebSettingsCompat.setUserAgentMetadata(settings, metadata(userAgent));
            } catch (RuntimeException e) {
                // (an older WebView)
            }
        }
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

    /** What a browser with this user agent says where a site asks for more than the user agent (the same as the
     * script's side sets through DevTools). */
    static UserAgentMetadata metadata(String userAgent) {
        String major = "131";
        String full = "131.0.0.0";
        Matcher chrome = Pattern.compile("Chrome/(\\d+)((?:\\.\\d+)*)").matcher(userAgent);
        if (chrome.find()) {
            major = chrome.group(1);
            full = chrome.group(1) + chrome.group(2);
        }
        boolean android = userAgent.contains("Android");
        boolean windows = userAgent.contains("Windows NT");
        boolean mac = userAgent.contains("Mac OS X");
        UserAgentMetadata.BrandVersion chromium = new UserAgentMetadata.BrandVersion.Builder()
            .setBrand("Chromium").setMajorVersion(major).setFullVersion(full).build();
        UserAgentMetadata.BrandVersion google = new UserAgentMetadata.BrandVersion.Builder()
            .setBrand("Google Chrome").setMajorVersion(major).setFullVersion(full).build();
        UserAgentMetadata.BrandVersion other = new UserAgentMetadata.BrandVersion.Builder()
            .setBrand("Not_A Brand").setMajorVersion("24").setFullVersion("24.0.0.0").build();
        return new UserAgentMetadata.Builder()
            .setBrandVersionList(Arrays.asList(chromium, google, other))
            .setFullVersion(full)
            .setPlatform(android ? "Android" : windows ? "Windows" : mac ? "macOS" : "Linux")
            .setPlatformVersion(windows ? "10.0.0" : "")
            .setArchitecture(android ? "arm" : "x86")
            .setModel("")
            .setMobile(userAgent.contains("Mobile"))
            .setBitness(64)
            .setWow64(false)
            .build();
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

        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            // The page's process stopped (Android took its memory back, or it crashed): this browser is closed, and
            // the script opens another.
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
