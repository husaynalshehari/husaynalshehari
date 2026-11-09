package app.tg.comments;

import static org.junit.Assert.assertTrue;
import static org.robolectric.Shadows.shadowOf;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.os.Looper;
import android.view.View;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;
import org.robolectric.annotation.GraphicsMode;

import java.io.File;
import java.io.FileOutputStream;

/** Pictures of the page as Android draws it (a phone 411 wide, Arabic), kept in build/screens to be looked at. */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35, qualifiers = "ar-w411dp-h891dp-xxhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
public class ScreensTest {
    Fakes fakes;

    @Before
    public void start() throws Exception {
        fakes = new Fakes();
    }

    @After
    public void stop() {
        fakes.stop();
    }

    static void picture(View view, String name) throws Exception {
        int width = view.getWidth() > 0 ? view.getWidth() : view.getResources().getDisplayMetrics().widthPixels;
        view.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED));
        View content = ((android.widget.ScrollView) view).getChildAt(0);
        int height = Math.max(view.getHeight(), content.getMeasuredHeight());
        view.layout(0, 0, width, height);
        content.layout(0, 0, width, content.getMeasuredHeight());
        Bitmap bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
        view.draw(new Canvas(bitmap));
        File folder = new File("build/screens");
        folder.mkdirs();
        try (FileOutputStream out = new FileOutputStream(new File(folder, name + ".png"))) {
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out);
        }
    }

    @Test
    public void thePage() throws Exception {
        MainActivity page = Robolectric.buildActivity(MainActivity.class).setup().visible().get();
        View root = page.findViewById(android.R.id.content);
        View scroll = ((android.view.ViewGroup) root).getChildAt(0);
        picture(scroll, "1-first-open");

        page.keyField.setText(Fakes.CLAUDE_KEY);
        page.voiceGroup.check(page.voiceIds[0]);
        page.saveSettings();
        page.tweetsField.setText("https://x.com/someone/status/1900000000000000001\n"
            + "https://x.com/other/status/1900000000000000002\nhttps://x.com/a/status/1900000000000000404");
        fakes.reply = "والله الزحمة صارت جزء من يومنا وما لها حل قريب أبدا";
        page.sendButton.performClick();
        shadowOf(Looper.getMainLooper()).idle();
        picture(scroll, "2-working");
        long end = System.currentTimeMillis() + 20_000;
        do {
            Thread.sleep(20);
            shadowOf(Looper.getMainLooper()).idle();
        } while (page.running > 0 && System.currentTimeMillis() < end);
        page.cards.get(0).copyButton.performClick();
        shadowOf(Looper.getMainLooper()).idle();
        picture(scroll, "3-comments");
        assertTrue(new File("build/screens/3-comments.png").length() > 1000);
    }
}
