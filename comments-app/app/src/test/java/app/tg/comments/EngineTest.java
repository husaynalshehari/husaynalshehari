package app.tg.comments;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/** The app and the comments server together: the server's link, the tweets read on the server without a browser,
 * what Claude is asked there (with the server's key), the reply as it is posted, and what the app says when
 * something is wrong. */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 35)
public class EngineTest {
    static final String SHE = "You write as a woman: when you speak of yourself, use feminine forms";
    static final String HE = "You write as a man: when you speak of yourself, use masculine forms";
    // The server's manners, in its order: a phone sends the number of its own.
    static final String[] MANNERS = {
        "short and direct, no emojis",
        "calm and down-to-earth",
        "sarcastic and witty",
        "blunt and critical, but not rude",
        "easygoing; now and then one emoji at the end",
        "curious; sometimes ends with a short question",
        "matter-of-fact; sticks to what the tweet says",
        "playful, with everyday slang",
    };
    static final String CURIOUS = MANNERS[5];
    static final String SARCASTIC = MANNERS[2];
    static final String POSTED = "والله الزحمة صارت جزء من يومنا وما لها حل قريب أبدا";

    ServerProcess server;
    Fakes fakes;
    Engine.ServerLink link;

    @Before
    public void start() throws Exception {
        server = ServerProcess.shared();
        fakes = ServerProcess.fakes();
        link = server.parsed();
    }

    String comment(String id) throws Exception {
        return Engine.comment(link, id, 0, 2, Collections.emptyList());
    }

    static String pin(char c) {
        return String.valueOf(c).repeat(64);
    }

    @Test
    public void linksInPastedText() {
        String pasted = "شوف هذي https://x.com/someone/status/1900000000000000001?s=20\n"
            + "twitter.com/i/web/status/1900000000000000002 و https://fxtwitter.com/a/status/1900000000000000003\n"
            + "1900000000000000004 مرة ثانية https://x.com/someone/status/1900000000000000001 رقم 12345 hello";
        assertEquals(Arrays.asList("1900000000000000001", "1900000000000000002", "1900000000000000003", "1900000000000000004"),
            Engine.tweetIds(pasted));
        assertTrue(Engine.tweetIds("مرحبا 12345 https://x.com/someone").isEmpty());
    }

    @Test
    public void serverLinks() {
        String token = "AbCdEf0123456789_-xyzABCDEFGHIJ12";
        Engine.ServerLink parsed = Engine.parseLink("https://1.2.3.4:8443/" + token + "#" + pin('a'));
        assertEquals("1.2.3.4", parsed.host);
        assertEquals(8443, parsed.port);
        assertEquals(token, parsed.token);
        assertEquals(pin('a'), parsed.pin);
        assertEquals("1.2.3.4:8443", parsed.shown());
        assertEquals("https://1.2.3.4:8443/" + token + "#" + pin('a'), parsed.text());
        // As pasted from a message: spaces, a line break inside it, direction marks; a fingerprint in capitals.
        Engine.ServerLink pasted = Engine.parseLink(" \u200Fhttps://1.2.3.4:8443/" + token + "/\n#" + pin('A') + "\u200E ");
        assertEquals(parsed.text(), pasted.text());
        assertEquals("[2001:db8::1]:8443", Engine.parseLink("https://[2001:db8::1]:8443/" + token + "#" + pin('b')).shown());
        assertEquals("server.example.com:443", Engine.parseLink("https://Server.Example.com/" + token + "#" + pin('c')).shown());
        for (String bad : new String[] {"", "hello", "http://1.2.3.4:8443/" + token + "#" + pin('a'),
                "https://1.2.3.4:8443/" + token, "https://1.2.3.4:8443/" + token + "#" + pin('g'),
                "https://1.2.3.4:8443/short#" + pin('a'), "https://1.2.3.4:8443/" + token + "?x=1#" + pin('a'),
                "https://tg:p@1.2.3.4:8443/" + token + "#" + pin('a'), "https://1.2.3.4:8443/a/" + token + "#" + pin('a'),
                "https://1.2.3.4:99999/" + token + "#" + pin('a'), "http://tg:Secret123abc@1.2.3.4:8899"}) {
            assertNull(bad, Engine.parseLink(bad));
        }
    }

    @Test
    public void theServerReadsTheTweetAndWritesItsComment() throws Exception {
        assertEquals("claude-haiku-5-5", Engine.ping(link));
        Engine.Tweet tweet = Engine.readTweet(link, "1900000000000000001");
        assertEquals("someone", tweet.author);
        assertEquals("https://x.com/someone/status/1900000000000000001", tweet.url);
        assertEquals("الزحمة اليوم في طريق الملك فهد ما لها حل", tweet.text);
        assertEquals(1, tweet.photos);                              // (only the tweet's own photos)
        Engine.Tweet quoted = Engine.readTweet(link, "1900000000000000002");
        assertEquals("الأسعار ارتفعت هذا الشهر", quoted.quote);
        assertEquals(1, quoted.photos);                             // (a video's picture)

        assertEquals(POSTED, comment("1900000000000000001"));       // no diacritics, no commas
        assertEquals(1, fakes.asked.size());
        Fakes.Asked asked = fakes.asked.get(0);
        // Claude is asked by the server, with the server's key: the phone has none.
        assertEquals(Fakes.CLAUDE_KEY, asked.headers.get("x-api-key"));
        assertEquals("2026-10-21", asked.headers.get("anthropic-version"));
        assertEquals("claude-haiku-5-5", asked.body.getString("model"));
        assertEquals(2048, asked.body.getInt("max_tokens"));
        assertEquals("medium", asked.body.getJSONObject("output_config").getString("effort"));
        assertEquals(1, asked.images());
        assertEquals(Collections.singletonList("/media/a.jpg?name=medium"), fakes.photoPaths);     // in a medium size
        String text = asked.text();
        assertTrue(text.startsWith("Look at the tweet's photos too. Write one public reply to this tweet"));
        assertTrue(text.contains("Not personal: the reply is about the tweet's topic, not about you."));
        assertTrue(text.contains("صار معي، جربت، شفت بنفسي، أخوي، عندنا في البيت، أتذكر لما، لو كنت مكانه"));
        assertTrue(text.contains("everyday spoken Saudi Arabic (عامية سعودية بيضاء)"));
        assertTrue(text.contains("At most 100 characters, spaces included"));
        assertTrue(text.contains("no diacritics (no تشكيل") && text.contains("no commas (، or ,)"));
        assertTrue(text.contains(HE));
        assertTrue(text.contains("Your manner (always the same): " + SARCASTIC + "."));
        assertTrue(text.contains("The photos are the tweet's own: never mention them"));
        assertTrue(text.endsWith("\n\nTweet text:\nالزحمة اليوم في طريق الملك فهد ما لها حل"));

        Engine.comment(link, "1900000000000000002", 1, 5, Arrays.asList("أول رد كتبته قبل شوي عن الأسعار", "ثاني رد عن الدوري"));
        String second = fakes.asked.get(1).text();
        assertTrue(second.contains(SHE));
        assertTrue(second.contains("Your manner (always the same): " + CURIOUS + "."));
        assertTrue(second.contains("other words):\n- أول رد كتبته قبل شوي عن الأسعار\n- ثاني رد عن الدوري"));
        assertTrue(second.contains("\n(It quotes: الأسعار ارتفعت هذا الشهر)"));
    }

    @Test
    public void tweetsThatCannotBeRead() throws Exception {
        for (String gone : new String[] {"1900000000000000404", "1900000000000000003"}) {
            try {
                Engine.readTweet(link, gone);
                fail("read a tweet that is not there");
            } catch (Engine.CommentError e) {
                assertEquals("التغريدة غير موجودة أو حسابها خاص.", e.getMessage());
            }
        }
        try {
            Engine.readTweet(link, "1900000000000000429");
            fail("read while FxTwitter is busy");
        } catch (Engine.CommentError e) {
            assertTrue(e.getMessage().startsWith("خدمة قراءة التغريدات مشغولة"));
        }
        assertTrue(fakes.asked.isEmpty());
    }

    @Test
    public void theReplyAsItIsPosted() throws Exception {
        fakes.reply = "الرد: «ما أتوقع، إنها بتخلص قريب صراحةً 😂»";
        assertEquals("ما أتوقع إنها بتخلص قريب صراحة 😂", comment("1900000000000000002"));
        fakes.reply = "السعر 1,500 ريال، وهذا كثير على اللي يبيه";
        assertEquals("السعر 1500 ريال وهذا كثير على اللي يبيه", comment("1900000000000000002"));
    }

    @Test
    public void aLongReplyIsAskedAgainThenShortened() throws Exception {
        String longer = "الزحمة في طريق الملك فهد صارت شي يومي وما أحد يحسب حسابه لأن المشاريع ما خلصت والناس كلها طالعة في نفس الوقت للدوامات";
        fakes.reply = longer;
        String reply = comment("1900000000000000001");
        assertEquals(2, fakes.asked.size());
        assertTrue(reply.length() <= 100);
        assertTrue(longer.startsWith(reply));
        assertFalse(Arrays.asList("في", "و", "لأن").contains(reply.substring(reply.lastIndexOf(' ') + 1)));
    }

    @Test
    public void aCutOffReplyIsAskedAgain() throws Exception {
        fakes.answer(200, Fakes.claudeText("الزحمة اليوم و"));
        assertEquals(POSTED, comment("1900000000000000001"));
        assertEquals(2, fakes.asked.size());
    }

    @Test
    public void aModelWithNoEffortSettingIsAskedWithoutIt() throws Exception {
        ServerProcess old = new ServerProcess(fakes, "claude-old-model", null);
        try {
            Engine.ServerLink oldLink = old.parsed();
            assertEquals("claude-old-model", Engine.ping(oldLink));
            fakes.answer(400, Fakes.claudeError("invalid_request_error", "output_config.effort: not supported by this model"));
            assertFalse(Engine.comment(oldLink, "1900000000000000002", 0, 0, Collections.emptyList()).isEmpty());
            assertEquals(2, fakes.asked.size());
            assertTrue(fakes.asked.get(0).body.has("output_config"));
            assertFalse(fakes.asked.get(1).body.has("output_config"));
            Engine.comment(oldLink, "1900000000000000002", 0, 0, Collections.emptyList());
            assertFalse(fakes.asked.get(2).body.has("output_config"));     // (not sent it again)
        } finally {
            old.stop();
        }
    }

    @Test
    public void aPhotoClaudeCannotReadIsLeftOut() throws Exception {
        fakes.answer(400, Fakes.claudeError("invalid_request_error", "Could not process image"));
        assertEquals(POSTED, comment("1900000000000000001"));
        assertEquals(1, fakes.asked.get(0).images());
        assertEquals(0, fakes.asked.get(1).images());
    }

    @Test
    public void aBusyClaudeIsAskedAgain() throws Exception {
        fakes.answer(429, Fakes.claudeError("rate_limit_error", "Too many requests"));
        fakes.answer(529, Fakes.claudeError("overloaded_error", "Overloaded"));
        assertEquals(POSTED, comment("1900000000000000001"));
        assertEquals(3, fakes.asked.size());
    }

    @Test
    public void whatStopsAComment() throws Exception {
        Object[][] cases = {
            {401, Fakes.claudeError("authentication_error", "invalid x-api-key"),
                "مفتاح Claude في السيرفر غير صالح. غيّره على السيرفر: python3 comments_server.py --key"},
            {403, Fakes.claudeError("forbidden", "Request not allowed"), "Claude لا يقبل الطلبات من هذا السيرفر."},
            {403, Fakes.claudeError("permission_error", "Your API key does not have permission to use the specified resource."),
                "مفتاح Claude في السيرفر بلا صلاحية لهذا الطلب: Your API key does not have permission to use the specified resource."},
            {400, Fakes.claudeError("invalid_request_error", "Your credit balance is too low to access the Anthropic API."),
                "بلغ حساب Claude حد الصرف أو نفد رصيده."},
            {404, Fakes.claudeError("not_found_error", "model: claude-nope"),
                "النموذج المضبوط في السيرفر غير موجود. غيّره: python3 comments_server.py --model …"},
            {200, "{\"content\":[],\"stop_reason\":\"refusal\"}", "امتنع Claude عن التعليق على هذه التغريدة."},
        };
        for (Object[] c : cases) {
            fakes.answer((Integer) c[0], (String) c[1]);
            try {
                comment("1900000000000000001");
                fail("no error for " + c[1]);
            } catch (Engine.CommentError e) {
                assertEquals(c[2], e.getMessage());
            }
        }
        fakes.answer(400, Fakes.claudeError("invalid_request_error", "bad key " + Fakes.CLAUDE_KEY + " here"));
        try {
            comment("1900000000000000001");
            fail("no error");
        } catch (Engine.CommentError e) {
            assertEquals("رفض Claude الطلب: bad key … here", e.getMessage());    // (the key is never shown)
        }
    }

    @Test
    public void whenTheServerCannotReachClaude() throws Exception {
        ServerProcess cutOff = new ServerProcess(fakes, null, "http://127.0.0.1:" + ServerProcess.freePort() + "/v1/messages");
        try {
            try {
                Engine.comment(cutOff.parsed(), "1900000000000000001", 0, 0, Collections.emptyList());
                fail("no error");
            } catch (Engine.CommentError e) {
                assertEquals("السيرفر لم يصل إلى Claude. أعد المحاولة بعد قليل.", e.getMessage());
            }
        } finally {
            cutOff.stop();
        }
    }

    /** A server of this machine's that answers what it is sent with something that is not TLS, or nothing at all. */
    static ServerSocket otherServer(boolean silent, List<Socket> held) throws Exception {
        ServerSocket other = new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"));
        Thread accepting = new Thread(() -> {
            while (!other.isClosed()) {
                try {
                    Socket socket = other.accept();
                    if (silent) {
                        held.add(socket);
                        continue;
                    }
                    try (Socket s = socket) {
                        // All the app sent is read first (a socket closed with bytes unread would reset instead).
                        InputStream in = s.getInputStream();
                        s.setSoTimeout(300);
                        try {
                            while (in.read(new byte[4096]) > 0) {
                                continue;
                            }
                        } catch (java.net.SocketTimeoutException e) {
                            // (it waits for an answer)
                        }
                        OutputStream out = s.getOutputStream();
                        out.write("HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.UTF_8));
                        out.flush();
                        s.shutdownOutput();
                        Thread.sleep(300);
                    }
                } catch (Exception e) {
                    return;
                }
            }
        });
        accepting.setDaemon(true);
        accepting.start();
        return other;
    }

    @Test
    public void aWrongLinkOrAServerThatIsDown() throws Exception {
        String token = link.token;
        // Another certificate than the link's: nothing is sent to it.
        try {
            Engine.ping(Engine.parseLink("https://127.0.0.1:" + server.port + "/" + token + "#" + pin('0')));
            fail("took a certificate the link does not name");
        } catch (Engine.CommentError e) {
            assertEquals(Engine.WRONG_CERTIFICATE, e.getMessage());
        }
        // Another secret (an old link).
        try {
            Engine.ping(Engine.parseLink("https://127.0.0.1:" + server.port + "/" + "Zz".repeat(16) + "#" + link.pin));
            fail("answered another secret");
        } catch (Engine.CommentError e) {
            assertEquals(Engine.WRONG_SECRET, e.getMessage());
        }
        try {
            Engine.readTweet(Engine.parseLink("https://127.0.0.1:" + server.port + "/" + "Zz".repeat(16) + "#" + link.pin),
                "1900000000000000001");
            fail("answered another secret");
        } catch (Engine.CommentError e) {
            assertEquals(Engine.WRONG_SECRET, e.getMessage());
        }
        // A server that is down: nothing listens on its port.
        int closed = ServerProcess.freePort();
        try {
            Engine.ping(Engine.parseLink("https://127.0.0.1:" + closed + "/" + token + "#" + link.pin));
            fail("reached a server that is down");
        } catch (Engine.CommentError e) {
            assertEquals("السيرفر رفض الاتصال على المنفذ " + closed + ": برنامج التعليقات متوقف، أو المنفذ مغلق في جدار "
                + "حمايته (على السيرفر: systemctl restart comments-server).", e.getMessage());
        }
        // Another program on that port, which does not speak TLS.
        List<Socket> held = Collections.synchronizedList(new ArrayList<>());
        try (ServerSocket other = otherServer(false, held)) {
            try {
                Engine.ping(Engine.parseLink("https://127.0.0.1:" + other.getLocalPort() + "/" + token + "#" + link.pin));
                fail("took a server that is not one");
            } catch (Engine.CommentError e) {
                assertEquals(Engine.NO_SAFE_CONNECTION, e.getMessage());
            }
        }
        // No answer at all (a port the hosting's firewall drops, here a server that says nothing).
        try (ServerSocket silent = otherServer(true, held)) {
            Engine.pingTimeoutMs = 700;
            try {
                Engine.ping(Engine.parseLink("https://127.0.0.1:" + silent.getLocalPort() + "/" + token + "#" + link.pin));
                fail("no answer was taken for one");
            } catch (Engine.CommentError e) {
                assertEquals("لا رد من السيرفر على المنفذ " + silent.getLocalPort() + ". افتح هذا المنفذ (TCP) في جدار "
                    + "حماية الاستضافة من لوحتها، وتأكد من عنوان السيرفر.", e.getMessage());
            }
        } finally {
            ServerProcess.quick();
            for (Socket socket : held) {
                socket.close();
            }
        }
        assertEquals("claude-haiku-5-5", Engine.ping(link));        // (the right link still answers)
    }

    /** The tests run on the computer's Java, whose regex takes more than Android's (ICU): what Android refuses
     * (it throws IllegalArgumentException on a phone) is kept out of the app's code. */
    @Test
    public void noRegexFlagAndroidRefuses() throws Exception {
        for (String name : new String[] {"Engine.java", "MainActivity.java"}) {
            String source = new String(java.nio.file.Files.readAllBytes(
                java.nio.file.Paths.get("src/main/java/app/tg/comments/" + name)), StandardCharsets.UTF_8);
            for (String refused : new String[] {"UNICODE_CHARACTER_CLASS", "CANON_EQ", "(?U)"}) {
                assertFalse(name + " uses " + refused, source.contains(refused));
            }
        }
    }
}
