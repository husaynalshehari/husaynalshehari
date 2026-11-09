package app.tg.comments;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.fail;

import org.junit.After;
import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;

import java.util.Collections;
import java.util.Random;

/** Only when REAL_PROXY is given (the link of a real tinyproxy set up by proxy-setup.sh): Claude's real address
 * through it, with a made-up key. Claude answering «invalid x-api-key» (401, which costs nothing) shows the
 * encrypted tunnel through the proxy, with its password, works; a wrong password is told apart. */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35)
public class RealProxyTest {
    @After
    public void stop() {
        Engine.useProxy(null);
    }

    @Test
    public void claudeThroughARealProxy() throws Exception {
        String link = System.getenv("REAL_PROXY");
        Assume.assumeTrue("REAL_PROXY not given", link != null && !link.isEmpty());
        Engine.claudeUrl = "https://api.anthropic.com/v1/messages";
        Engine.busyWaitsMs = new long[] {1, 1, 1, 1};
        Engine.Tweet tweet = new Engine.Tweet("1", "https://x.com/i/status/1", "a", "نص تجربة", "", Collections.emptyList());
        String madeUp = "sk-ant-api03-" + "made_up_key_for_a_check" + "x".repeat(40);
        Engine.ProxyLink right = Engine.parseProxy(link);
        // A wrong password first, to another name of the same machine: Java on a computer keeps a proxy's password
        // once it worked, and would send the right one again in its place (a phone asks each time).
        Engine.useProxy(Engine.parseProxy("http://" + right.user + ":wrong" + right.password + "@localhost:" + right.port));
        try {
            Engine.comment(madeUp, Engine.DEFAULT_MODEL, 0, 0, tweet, Collections.emptyList(), Collections.emptyList(), new Random(1));
            fail("a wrong proxy password went through");
        } catch (Engine.CommentError e) {
            assertEquals("البروكسي رفض اسم المستخدم أو كلمة المرور. راجع رابطه في «الإعدادات».", e.getMessage());
        }
        Engine.useProxy(right);
        try {
            Engine.comment(madeUp, Engine.DEFAULT_MODEL, 0, 0, tweet, Collections.emptyList(), Collections.emptyList(), new Random(1));
            fail("a made-up key was taken");
        } catch (Engine.CommentError e) {
            assertEquals("مفتاح Claude غير صالح. راجعه في «الإعدادات».", e.getMessage());     // (Claude itself answered)
        }
    }
}
