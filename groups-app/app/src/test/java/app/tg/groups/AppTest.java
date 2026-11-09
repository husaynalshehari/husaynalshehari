package app.tg.groups;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.robolectric.Shadows.shadowOf;

import android.content.res.Configuration;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebView;

import androidx.webkit.UserAgentMetadata;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;

import java.io.File;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

/** The app's page and its browsers, as far as they can be tried without a phone: the page is built and does not fall
 * over, a browser asked for from another thread (as the script asks) is made at the page size asked for and shown
 * scaled, the tabs, the small window, and the script's files put in the app's folder. */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35, qualifiers = "ar-w411dp-h891dp-xxhdpi")
public class AppTest {
    static final String UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
    ActivityController<MainActivity> controller;
    MainActivity page;
    ExecutorService script;

    @Before
    public void start() {
        script = Executors.newSingleThreadExecutor();
        controller = Robolectric.buildActivity(MainActivity.class).setup().visible();
        page = controller.get();
    }

    @After
    public void stop() {
        script.shutdownNow();
        controller.pause().stop().destroy();
    }

    /** A call from the script's side: made on another thread, while this one runs the page's own thread. */
    <T> T fromScript(Callable<T> call) throws Exception {
        Future<T> answer = script.submit(call);
        long end = System.currentTimeMillis() + 20_000;
        while (!answer.isDone() && System.currentTimeMillis() < end) {
            shadowOf(Looper.getMainLooper()).idle();
            Thread.sleep(5);
        }
        return answer.get();
    }

    void layout() {
        View root = page.root;
        int width = View.MeasureSpec.makeMeasureSpec(1233, View.MeasureSpec.EXACTLY);
        int height = View.MeasureSpec.makeMeasureSpec(2673, View.MeasureSpec.EXACTLY);
        root.measure(width, height);
        root.layout(0, 0, 1233, 2673);
        shadowOf(Looper.getMainLooper()).idle();
    }

    @Test
    public void thePageIsBuilt() {
        assertEquals(View.VISIBLE, page.panel.getVisibility());
        assertEquals(View.VISIBLE, page.status.getVisibility());            // (until the script's page has loaded)
        // (Python cannot start on this machine as it does on a phone: that shows as a message, not as a crash.)
        String shown = page.status.getText().toString();
        assertTrue(shown, shown.equals("يشغّل السكربت…") || shown.startsWith("لم يبدأ السكربت. افتح القائمة ⋮ ثم «السجل»"));
        assertEquals("المتصفح", page.tabBrowser.getText().toString());
        assertEquals(View.GONE, page.report.getVisibility());
        assertEquals(0, BrowserHost.count());
        assertTrue(BrowserHost.visible);
        // The browser tab, with nothing open yet.
        page.tabBrowser.performClick();
        assertEquals(View.INVISIBLE, page.panel.getVisibility());
        assertEquals(View.VISIBLE, page.emptyNote.getVisibility());
        assertEquals(View.GONE, page.status.getVisibility());
        page.onBackPressed();                                               // back: to the panel
        assertEquals(View.VISIBLE, page.panel.getVisibility());
        assertFalse(page.onBrowserTab);
    }

    @Test
    public void aBrowserAsTheScriptAsksForIt() throws Exception {
        int id = fromScript(() -> BrowserHost.open("0a1b2c3d4e5f6a7b8c9d0e1f", UA, 600, 1800));
        assertTrue("opened: " + BrowserHost.lastError(), id > 0);
        assertEquals(1, BrowserHost.count());
        assertEquals("المتصفح ●", page.tabBrowser.getText().toString());
        assertTrue((page.getWindow().getAttributes().flags & WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) != 0);
        BrowserHost.Browser browser = BrowserHost.browsers.get(id);
        WebView view = browser.view;
        assertEquals("gg-0a1b2c3d4e5f6a7b8c9d0e1f", browser.profile);
        assertEquals(UA, view.getSettings().getUserAgentString());
        assertTrue(view.getSettings().getJavaScriptEnabled());
        assertEquals(100, view.getSettings().getTextZoom());
        assertTrue(shadowOf(view).getLastLoadDataWithBaseURL().data.contains("<title>gg-" + id + "</title>"));

        // The WebView is exactly the page's size (600×1800 CSS pixels at this screen's 3 pixels each), and is shown
        // scaled so that its whole width fits the screen; the tab is as long as the scaled page.
        layout();
        assertEquals(1800, view.getMeasuredWidth());
        assertEquals(5400, view.getMeasuredHeight());
        assertEquals(1233f / 1800f, view.getScaleX(), 0.001f);
        assertEquals(view.getScaleX(), view.getScaleY(), 0.0001f);
        assertEquals(1233, page.stage.getMeasuredWidth());
        assertEquals(Math.round(5400 * 1233f / 1800f), page.stage.getMeasuredHeight());

        // The script makes the page taller while it collects from a group, then a phone's width.
        fromScript(() -> { BrowserHost.resize(id, 600, 25000); return true; });
        layout();
        assertEquals(1800, view.getMeasuredWidth());
        assertEquals(75000, view.getMeasuredHeight());
        fromScript(() -> { BrowserHost.resize(id, 384, 854); return true; });
        layout();
        assertEquals(1152, view.getMeasuredWidth());
        assertEquals(1f, view.getScaleX(), 0.0001f);                         // (narrower than the screen: not enlarged)

        // What else the script's side calls.
        fromScript(() -> { BrowserHost.setCookie(id, "https://x.com/", "auth_token=abc; Domain=.x.com; Path=/; Secure; HttpOnly; SameSite=Lax"); return true; });
        assertTrue(String.valueOf(android.webkit.CookieManager.getInstance().getCookie("https://x.com/")).contains("auth_token=abc"));
        fromScript(() -> { BrowserHost.setRouter(id, null); return true; });
        assertFalse(fromScript(() -> BrowserHost.downloadStarted(id)));
        assertNotNull(fromScript(() -> BrowserHost.capture(id, 0, 0, 120, 60, true, 70)));
        assertTrue(BrowserHost.awaitVisible(10));
        fromScript(() -> { BrowserHost.show(id); return true; });

        // A second browser, for another account at the same time.
        int other = fromScript(() -> BrowserHost.open("ffffffffffffffffffffffff", UA, 600, 1800));
        assertTrue(other > id);
        assertEquals(2, page.stage.getChildCount());

        // The small window over other apps shows the browsers alone; back to full size as it was.
        page.onPictureInPictureModeChanged(true, new Configuration());
        assertEquals(View.GONE, page.bar.getVisibility());
        assertEquals(View.INVISIBLE, page.panel.getVisibility());
        page.onPictureInPictureModeChanged(false, new Configuration());
        assertEquals(View.VISIBLE, page.bar.getVisibility());
        assertEquals(View.VISIBLE, page.panel.getVisibility());

        // Closed: gone from the tab; with the last one, the screen may sleep again.
        fromScript(() -> { BrowserHost.close(id); return true; });
        assertEquals(1, BrowserHost.count());
        assertEquals(1, page.stage.getChildCount());
        fromScript(() -> { BrowserHost.close(other); return true; });
        assertEquals(0, BrowserHost.count());
        assertEquals("المتصفح", page.tabBrowser.getText().toString());
        assertEquals(0, page.getWindow().getAttributes().flags & WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        assertTrue(fromScript(() -> BrowserHost.open("x", UA, 600, 1800)) > other);    // (numbers are not used twice)
    }

    @Test
    public void whileTheAppIsNotOnTheScreenATaskWaits() throws Exception {
        controller.pause().stop();
        assertFalse(BrowserHost.visible);
        long before = System.currentTimeMillis();
        assertFalse(BrowserHost.awaitVisible(300));
        assertTrue(System.currentTimeMillis() - before >= 250);
        Future<Boolean> waiting = script.submit(() -> BrowserHost.awaitVisible(20_000));
        Thread.sleep(100);
        assertFalse(waiting.isDone());
        controller.start().resume();
        assertTrue(waiting.get());
    }

    @Test
    public void theReportPage() {
        page.openReport("فحص المتصفح", "");
        assertEquals(View.VISIBLE, page.report.getVisibility());
        new Lines(page).add("✓ فتح المتصفح والاتصال به");
        new Lines(page).add("✗ شيء: خطأ");
        shadowOf(Looper.getMainLooper()).idle();
        assertEquals("✓ فتح المتصفح والاتصال به\n✗ شيء: خطأ\n", page.reportText.getText().toString());
        page.onBackPressed();
        assertEquals(View.GONE, page.report.getVisibility());
    }

    @Test
    public void whatTheBrowserSaysOfItself() {
        UserAgentMetadata metadata = BrowserHost.metadata(UA);
        assertEquals("Windows", metadata.getPlatform());
        assertEquals("131.0.0.0", metadata.getFullVersion());
        assertFalse(metadata.isMobile());
        assertEquals(3, metadata.getBrandVersionList().size());
        assertEquals("Google Chrome", metadata.getBrandVersionList().get(1).getBrand());
        assertEquals("131", metadata.getBrandVersionList().get(1).getMajorVersion());
    }

    @Test
    public void theScriptsFilesGoToTheAppsFolder() throws Exception {
        File folder = new File(page.getFilesDir(), "gg-here");
        Engine.unpack(page, folder);
        File script = new File(folder, "gg.py");
        File marker = new File(folder, ".bundled");
        assertTrue(script.length() > 300_000);
        assertTrue(new File(folder, "content.py").length() > 50_000);
        String mark = new String(java.nio.file.Files.readAllBytes(marker.toPath())).trim();
        assertEquals(64, mark.length());
        // A script taken since by «تحديث السكربت» stays when the app starts again…
        java.nio.file.Files.write(script.toPath(), "# newer, from the update\n".getBytes());
        Engine.unpack(page, folder);
        assertEquals("# newer, from the update\n", new String(java.nio.file.Files.readAllBytes(script.toPath())));
        // …until an update of the app brings another script with it.
        java.nio.file.Files.write(marker.toPath(), "0".repeat(64).getBytes());
        Engine.unpack(page, folder);
        assertTrue(script.length() > 300_000);
        assertEquals(mark, new String(java.nio.file.Files.readAllBytes(marker.toPath())).trim());
    }

    @Test
    public void theScriptsPageIsAtAnAddressOfItsOwn() throws Exception {
        long end = System.currentTimeMillis() + 30_000;
        while (!Engine.phase.equals("failed") && !Engine.phase.equals("running") && System.currentTimeMillis() < end) {
            Thread.sleep(50);
        }
        assertTrue(Engine.port > 0);
        assertTrue(Engine.path, Engine.path.matches("[a-z0-9]{16}"));
        assertEquals("http://127.0.0.1:" + Engine.port + "/" + Engine.path, Engine.url());
        assertFalse(Engine.answers());                 // (nothing serves it on this machine)
    }
}
