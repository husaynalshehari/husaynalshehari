package com.goldextractor;

import android.app.Activity;
import android.webkit.CookieManager;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Wraps the app's WebView and exposes blocking calls (load / eval / waitFor),
 * similar to Playwright's sync API used by the Python script.
 * The browser profile (cookies, login, local storage) is persistent: it lives in the app's
 * private storage and survives restarts and recreate(), like the script's persistent profile.
 * The blocking methods must be called from a background thread, never the UI thread.
 */
public class Browser {

    /** Thrown when the WebView renderer crashed or the browser was destroyed. */
    public static class CrashedException extends Exception {
        public CrashedException() {
            super("browser crashed");
        }
    }

    private final Activity activity;
    private final FrameLayout container;
    private volatile WebView webView;
    private volatile boolean crashed = false;
    private volatile boolean destroyed = false;
    private volatile boolean blockImages;
    private volatile CountDownLatch pageLatch;

    /** Must be called on the UI thread. */
    public Browser(Activity activity, FrameLayout container, boolean blockImages) {
        this.activity = activity;
        this.container = container;
        this.blockImages = blockImages;
        createOnUi();
    }

    // ---------------------------------------------------------------- setup

    private void createOnUi() {
        if (destroyed) return;
        WebView wv = new WebView(activity);
        WebSettings s = wv.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setBlockNetworkImage(blockImages);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        String ua = s.getUserAgentString();
        if (ua != null) {
            // look like normal mobile Chrome instead of an embedded WebView
            s.setUserAgentString(ua.replace("; wv", ""));
        }

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(wv, true);

        // keep the renderer alive even when the screen is not showing it
        wv.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false);

        wv.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                // after a manual login X lands on /home: save the session to disk right away
                if (url != null && url.contains("x.com/home")) CookieManager.getInstance().flush();
                CountDownLatch l = pageLatch;
                if (l != null) l.countDown();
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String scheme = request.getUrl().getScheme();
                // stay inside the WebView for web links, ignore app links like twitter://
                return scheme == null || !(scheme.equals("http") || scheme.equals("https"));
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                if (view == webView) {
                    crashed = true;
                    webView = null;
                    CountDownLatch l = pageLatch;
                    if (l != null) l.countDown();
                }
                try {
                    container.removeView(view);
                    view.destroy();
                } catch (Exception ignored) {
                }
                return true; // handled: do not crash the app
            }
        });

        container.addView(wv, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        webView = wv;
        crashed = false;
    }

    // ---------------------------------------------------------------- thread helpers

    private void ui(Runnable r) {
        activity.runOnUiThread(() -> {
            if (!destroyed) r.run();
        });
    }

    private void uiAndWait(Runnable r, long timeoutMs) throws InterruptedException {
        CountDownLatch done = new CountDownLatch(1);
        activity.runOnUiThread(() -> {
            try {
                if (!destroyed) r.run();
            } finally {
                done.countDown();
            }
        });
        done.await(timeoutMs, TimeUnit.MILLISECONDS);
    }

    private void checkAlive() throws CrashedException {
        if (destroyed || crashed || webView == null) throw new CrashedException();
    }

    // ---------------------------------------------------------------- blocking API (background thread)

    /** Loads a URL and waits for onPageFinished (or the timeout). */
    public boolean load(String url, long timeoutMs) throws InterruptedException, CrashedException {
        checkAlive();
        CountDownLatch latch = new CountDownLatch(1);
        pageLatch = latch;
        ui(() -> {
            WebView wv = webView;
            if (wv != null) wv.loadUrl(url);
        });
        boolean finished = latch.await(timeoutMs, TimeUnit.MILLISECONDS);
        checkAlive();
        return finished;
    }

    /**
     * Runs JavaScript and returns the decoded JSON result:
     * String, Boolean, Number, JSONArray, JSONObject, or null.
     */
    public Object eval(String js, long timeoutMs) throws InterruptedException, CrashedException {
        checkAlive();
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<String> out = new AtomicReference<>();
        ui(() -> {
            WebView wv = webView;
            if (wv == null) {
                latch.countDown();
                return;
            }
            wv.evaluateJavascript(js, value -> {
                out.set(value);
                latch.countDown();
            });
        });
        boolean ok = latch.await(timeoutMs, TimeUnit.MILLISECONDS);
        checkAlive();
        if (!ok) return null;
        String v = out.get();
        if (v == null || v.isEmpty() || v.equals("null")) return null;
        try {
            return new JSONTokener(v).nextValue();
        } catch (JSONException e) {
            return null;
        }
    }

    /** Polls until the CSS selector exists in the page, or the timeout passes. */
    public boolean waitFor(String selector, long timeoutMs) throws InterruptedException, CrashedException {
        String js = "(function(){return !!document.querySelector(" + JSONObject.quote(selector) + ");})();";
        long end = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < end) {
            if (Boolean.TRUE.equals(eval(js, 5000))) return true;
            Thread.sleep(500);
        }
        return false;
    }

    public String getUrl() throws InterruptedException {
        AtomicReference<String> out = new AtomicReference<>("");
        uiAndWait(() -> {
            WebView wv = webView;
            if (wv != null && wv.getUrl() != null) out.set(wv.getUrl());
        }, 5000);
        return out.get();
    }

    /** Destroys the WebView and creates a fresh one (the "new browser" of the script). Cookies are kept. */
    public void recreate() throws InterruptedException {
        uiAndWait(() -> {
            WebView old = webView;
            webView = null;
            if (old != null) {
                try {
                    container.removeView(old);
                    old.stopLoading();
                    old.destroy();
                } catch (Exception ignored) {
                }
            }
            createOnUi();
        }, 15000);
    }

    // ---------------------------------------------------------------- UI-thread API

    /** Writes the login session (cookies) to disk now; the rest of the browser profile is saved automatically. */
    public void saveSession() {
        CookieManager.getInstance().flush();
    }

    public void loadAsync(String url) {
        ui(() -> {
            WebView wv = webView;
            if (wv != null) wv.loadUrl(url);
        });
    }

    public void setBlockImages(boolean block) {
        blockImages = block;
        ui(() -> {
            WebView wv = webView;
            if (wv != null) wv.getSettings().setBlockNetworkImage(block);
        });
    }

    /** UI thread: logs out of X by removing every cookie. */
    public void clearCookies() {
        CookieManager cm = CookieManager.getInstance();
        cm.removeAllCookies(null);
        cm.flush();
    }

    /** UI thread: returns true if the WebView went back a page. */
    public boolean goBack() {
        WebView wv = webView;
        if (wv != null && wv.canGoBack()) {
            wv.goBack();
            return true;
        }
        return false;
    }

    /** UI thread. */
    public void destroy() {
        destroyed = true;
        WebView wv = webView;
        webView = null;
        CountDownLatch l = pageLatch;
        if (l != null) l.countDown();
        if (wv != null) {
            try {
                container.removeView(wv);
                wv.stopLoading();
                wv.destroy();
            } catch (Exception ignored) {
            }
        }
        CookieManager.getInstance().flush();
    }
}
