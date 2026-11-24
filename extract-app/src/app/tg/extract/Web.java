package app.tg.extract;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.MutableContextWrapper;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONException;
import org.json.JSONTokener;

import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/**
 * The app's one browser: X is signed in there and the extraction works there. It is shown in the app's page; while a
 * task works and the app is off the screen, it moves to an invisible window over the other apps (with the user's
 * permission), where Android keeps it at full speed. All of it happens on the main thread; the task's thread reaches
 * it through onMain and js.
 */
final class Web {
    static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final int MATCH = ViewGroup.LayoutParams.MATCH_PARENT;
    private static final Pattern SIGNED_IN = Pattern.compile("(?:^|;\\s*)auth_token=[^;\\s]+");

    static WebView view;
    /** One more each time the browser is made anew (its page died, or the task asked for a fresh one). */
    static volatile int generation;
    private static MutableContextWrapper base;
    private static FrameLayout overlay;
    private static ViewGroup stage;

    private Web() {
    }

    @SuppressLint("SetJavaScriptEnabled")
    private static WebView ensure(Context context) {
        if (view != null) {
            return view;
        }
        base = new MutableContextWrapper(context);
        WebView w = new WebView(base);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setSupportMultipleWindows(false);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setGeolocationEnabled(false);
        s.setTextZoom(100);
        s.setBlockNetworkImage(Engine.running);           // (pictures are not needed while a task reads)
        // The user agent stays the phone's own: nothing is hidden or overwritten.
        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(w, true);
        w.setBackgroundColor(Color.WHITE);
        w.setWebViewClient(new Client());
        w.setWebChromeClient(new Chrome());
        view = w;
        return w;
    }

    private static final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest request) {
            String scheme = request.getUrl().getScheme();
            return !"https".equalsIgnoreCase(scheme) && !"http".equalsIgnoreCase(scheme);   // (no app links)
        }

        @Override
        public boolean onRenderProcessGone(WebView v, RenderProcessGoneDetail detail) {
            if (v == view) {
                Engine.log("⚠️ توقفت صفحة المتصفح (" + (detail.didCrash() ? "تعطّلت" : "أوقفها النظام لقلة الذاكرة")
                    + ") - فُتح متصفح جديد");
                recreate();
            }
            return true;
        }
    }

    /** Pages' dialogs are answered at once (nobody may be looking), and leaving a page is never held up. */
    private static final class Chrome extends WebChromeClient {
        @Override
        public boolean onJsAlert(WebView v, String url, String message, JsResult result) {
            result.confirm();
            return true;
        }

        @Override
        public boolean onJsConfirm(WebView v, String url, String message, JsResult result) {
            result.cancel();
            return true;
        }

        @Override
        public boolean onJsPrompt(WebView v, String url, String message, String value, JsPromptResult result) {
            result.cancel();
            return true;
        }

        @Override
        public boolean onJsBeforeUnload(WebView v, String url, String message, JsResult result) {
            result.confirm();
            return true;
        }
    }

    // ---------------------------------------------------------------- where it is shown

    /** The app is on the screen: the browser is in its page. */
    static void showIn(ViewGroup into, Activity activity) {
        WebView w = ensure(activity);
        base.setBaseContext(activity);
        leaveOverlay();
        stage = into;
        if (w.getParent() != into) {
            detach(w);
            into.addView(w, 0, new FrameLayout.LayoutParams(MATCH, MATCH));
        }
        w.onResume();
        w.resumeTimers();
    }

    static boolean canOverlay(Context context) {
        return Settings.canDrawOverlays(context);
    }

    /** The app leaves the screen while a task works: the browser goes on in the invisible window. */
    static void moveToOverlay(Context context) {
        if (view == null || overlay != null || !canOverlay(context)) {
            return;
        }
        Context app = context.getApplicationContext();
        FrameLayout host = new FrameLayout(app);
        try {
            detach(view);
            base.setBaseContext(app);
            host.addView(view, new FrameLayout.LayoutParams(MATCH, MATCH));
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(MATCH, MATCH,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                    | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON,
                PixelFormat.TRANSLUCENT);
            lp.gravity = Gravity.TOP | Gravity.START;
            lp.alpha = 0f;                                      // (nothing of it shows)
            lp.setTitle("extract-background");
            ((WindowManager) app.getSystemService(Context.WINDOW_SERVICE)).addView(host, lp);
            overlay = host;
        } catch (Throwable t) {
            Engine.log("⚠️ تعذّر فتح نافذة الخلفية، فقد يبطؤ العمل حتى تعود إلى التطبيق");
            detach(view);
            if (stage != null) {
                stage.addView(view, 0, new FrameLayout.LayoutParams(MATCH, MATCH));
                base.setBaseContext(stage.getContext());
            }
        }
        view.onResume();
        view.resumeTimers();
    }

    private static void leaveOverlay() {
        FrameLayout host = overlay;
        if (host == null) {
            return;
        }
        overlay = null;
        try {
            host.removeAllViews();
        } catch (Throwable ignore) {
            // (already empty)
        }
        try {
            ((WindowManager) host.getContext().getSystemService(Context.WINDOW_SERVICE)).removeViewImmediate(host);
        } catch (Throwable ignore) {
            // (already gone)
        }
    }

    /** The app's page is closed for good: the browser leaves it (and keeps working in the background if a task runs). */
    static void release(Activity activity) {
        if (stage != null && stage.getContext() == activity) {
            if (view != null && view.getParent() == stage) {
                stage.removeView(view);
            }
            stage = null;
        }
        if (base != null && base.getBaseContext() == activity) {
            base.setBaseContext(activity.getApplicationContext());
        }
        if (Engine.running) {
            moveToOverlay(activity.getApplicationContext());
        }
    }

    private static void detach(WebView w) {
        if (w.getParent() instanceof ViewGroup) {
            ((ViewGroup) w.getParent()).removeView(w);
        }
    }

    static void taskStarted() {
        if (view != null) {
            view.getSettings().setBlockNetworkImage(true);
        }
    }

    /** The task ended: the invisible window goes, and the browser is back in the app's page if it is open. */
    static void taskEnded() {
        if (overlay != null) {
            leaveOverlay();
            if (view != null && view.getParent() == null && stage != null) {
                stage.addView(view, 0, new FrameLayout.LayoutParams(MATCH, MATCH));
                base.setBaseContext(stage.getContext());
            }
        }
        if (view != null) {
            view.getSettings().setBlockNetworkImage(false);
        }
    }

    /** A fresh browser in the old one's place (the original script's "new browser"). Main thread. */
    static void recreate() {
        WebView old = view;
        if (old == null) {
            return;
        }
        Context context = base.getBaseContext();
        ViewGroup parent = old.getParent() instanceof ViewGroup ? (ViewGroup) old.getParent() : null;
        int index = parent != null ? parent.indexOfChild(old) : 0;
        if (parent != null) {
            parent.removeView(old);
        }
        try {
            old.stopLoading();
            old.destroy();
        } catch (Throwable ignore) {
            // (its page was already gone)
        }
        view = null;
        WebView fresh = ensure(context);
        if (parent != null) {
            parent.addView(fresh, index, new FrameLayout.LayoutParams(MATCH, MATCH));
        }
        generation++;
    }

    // ---------------------------------------------------------------- the X session

    static boolean loggedIn() {
        try {
            String cookies = CookieManager.getInstance().getCookie("https://x.com");
            return cookies != null && SIGNED_IN.matcher(cookies).find();
        } catch (Throwable t) {
            return false;
        }
    }

    /** Signs in with an auth_token the user pasted (the session of another account goes first). Main thread. */
    static void setToken(String token, Runnable then) {
        CookieManager cookies = CookieManager.getInstance();
        cookies.removeAllCookies(done -> {
            cookies.setCookie("https://x.com",
                "auth_token=" + token + "; Domain=.x.com; Path=/; Max-Age=31536000; Secure; HttpOnly");
            cookies.flush();
            then.run();
        });
    }

    /** Signs out: X's cookies and the pages' saved data go. Main thread. */
    static void clearSession(Runnable then) {
        CookieManager cookies = CookieManager.getInstance();
        cookies.removeAllCookies(done -> {
            cookies.flush();
            WebStorage.getInstance().deleteAllData();
            if (view != null) {
                view.clearCache(true);
                view.loadUrl("about:blank");
            }
            if (then != null) {
                then.run();
            }
        });
    }

    static void flushCookies() {
        CookieManager.getInstance().flush();
    }

    static String currentUrl() {
        return view != null ? view.getUrl() : null;
    }

    /** The path of an X address ("/" for X's front page); null for anything that is not X. */
    static String pathOf(String url) {
        if (url == null) {
            return null;
        }
        try {
            Uri u = Uri.parse(url);
            String host = u.getHost();
            if (host == null || !(host.equals("x.com") || host.endsWith(".x.com")
                || host.equals("twitter.com") || host.endsWith(".twitter.com"))) {
                return null;
            }
            String path = u.getPath();
            return path == null || path.isEmpty() ? "/" : path;
        } catch (Throwable t) {
            return null;
        }
    }

    /** X asks to sign in (or shows its front page, which it does to visitors who are not signed in). */
    static boolean isSignInPath(String path) {
        return path != null && (path.equals("/") || path.equals("/login") || path.startsWith("/i/flow/login")
            || path.startsWith("/i/flow/signup") || path.startsWith("/logout"));
    }

    /** X locked the account until it is checked (a puzzle, a code...). */
    static boolean isLockedPath(String path) {
        return path != null && path.startsWith("/account/access");
    }

    static boolean canGoBack() {
        return view != null && view.canGoBack();
    }

    static void goBack() {
        if (view != null) {
            view.goBack();
        }
    }

    // ---------------------------------------------------------------- reaching it from the task's thread

    static void load(String url) {
        onMain(() -> {
            if (view != null) {
                view.loadUrl(url);
            }
            return true;
        }, 5_000, false);
    }

    /** On the main thread, from the app's page: the script's JSON value is given to the callback (null if none). */
    static void evaluate(String script, ValueCallback<String> then) {
        if (view == null) {
            then.onReceiveValue(null);
            return;
        }
        view.evaluateJavascript(script, then);
    }

    /** From the task's thread: runs the script in the page and gives back its JSON value (null if it did not answer). */
    static String js(String script, long timeoutMs) {
        final String[] out = {null};
        final CountDownLatch answered = new CountDownLatch(1);
        MAIN.post(() -> {
            WebView w = view;
            if (w == null) {
                answered.countDown();
                return;
            }
            try {
                w.evaluateJavascript(script, value -> {
                    out[0] = value;
                    answered.countDown();
                });
            } catch (Throwable t) {
                answered.countDown();
            }
        });
        try {
            answered.await(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return out[0];
    }

    /** A JSON string value as Java text (null for anything else). */
    static String jsonString(String json) {
        if (json == null) {
            return null;
        }
        try {
            Object value = new JSONTokener(json).nextValue();
            return value instanceof String ? (String) value : null;
        } catch (JSONException e) {
            return null;
        }
    }

    /** Runs the work on the main thread and waits for its result (the fallback if it took longer than allowed). */
    @SuppressWarnings("unchecked")
    static <T> T onMain(Callable<T> work, long timeoutMs, T fallback) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            try {
                return work.call();
            } catch (Exception e) {
                return fallback;
            }
        }
        final Object[] box = {fallback};
        final CountDownLatch finished = new CountDownLatch(1);
        MAIN.post(() -> {
            try {
                box[0] = work.call();
            } catch (Throwable ignore) {
                // (the fallback stays)
            } finally {
                finished.countDown();
            }
        });
        try {
            if (!finished.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                return fallback;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return fallback;
        }
        return (T) box[0];
    }
}
