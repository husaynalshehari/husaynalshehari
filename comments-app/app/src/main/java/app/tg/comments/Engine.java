package app.tg.comments;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URL;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLException;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * The app's side of the comments server (comments_server.py, on the groups server). The server keeps the Claude
 * key, reads each tweet without a browser and asks Claude for its comment with the groups script's rules; the app
 * keeps only the server's link (its address, a secret, and its certificate's fingerprint), and talks to no other
 * server. The connection is encrypted, and taken only from the server whose certificate the link names.
 */
public final class Engine {
    public static final int MAX_TWEETS = 20;
    /** How many manners the server writes in (how a person writes): this phone's is drawn once and kept. */
    public static final int MANNERS = 8;
    // How long the server is waited for (tests shorten them), and before a connection that failed is tried again.
    static int connectTimeoutMs = 15_000;
    static int pingTimeoutMs = 20_000;
    static int tweetTimeoutMs = 60_000;
    static int commentTimeoutMs = 300_000;
    static long[] retryWaitsMs = {1500, 4000};

    private static final Pattern TWEET_LINK = Pattern.compile(
        "(?:x|twitter)\\.com/[A-Za-z0-9_]{1,15}(?:/web)?/status(?:es)?/(\\d{6,25})|(?<![\\d/])(\\d{15,25})(?!\\d)");
    private static final Pattern X_LINK = Pattern.compile("https://(?:www\\.|mobile\\.)?(?:x|twitter)\\.com/[A-Za-z0-9_/]{1,80}");
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9_\\-]{16,64}");
    private static final Pattern PIN = Pattern.compile("[0-9a-f]{64}");
    // What a link may carry when pasted from a message: spaces, line breaks, direction marks, zero-width spaces.
    private static final Pattern PASTED_MARKS = Pattern.compile("[\\s\\u00A0\\u200B-\\u200F\\u202A-\\u202E\\u2066-\\u2069\\uFEFF]+");

    static final String OWNER_LINK = " (يطبعه صاحب السيرفر: python3 comments_server.py --link)";
    static final String WRONG_CERTIFICATE = "شهادة السيرفر لا تطابق الرابط. خذ الرابط الحالي" + OWNER_LINK + ".";
    static final String WRONG_SECRET = "رابط السيرفر قديم أو ناقص. خذ الرابط الحالي" + OWNER_LINK + ".";
    static final String NO_SAFE_CONNECTION = "لم يتم الاتصال الآمن بالسيرفر: على هذا العنوان والمنفذ برنامج آخر، "
        + "أو انقطع الاتصال. راجع الرابط وأعد المحاولة.";
    static final String NOT_REACHED = "تعذّر الوصول إلى السيرفر. تأكد من اتصال الهاتف بالإنترنت ومن رابط السيرفر.";
    static final String LOST = "انقطع الاتصال بالسيرفر قبل أن يرد. أعد المحاولة.";
    static final String SLOW = "طال انتظار السيرفر ولم يرد. أعد المحاولة.";
    static final String UNCLEAR = "رد غير مفهوم من السيرفر.";

    private Engine() {
    }

    /** What stops one tweet's comment; its message is shown on the tweet's card. */
    public static final class CommentError extends Exception {
        public CommentError(String message) {
            super(message);
        }
    }

    /** A connection that did not reach the server and may be tried again; its message is shown when it is not. */
    private static final class NotReached extends Exception {
        NotReached(String message) {
            super(message);
        }
    }

    public static final class Tweet {
        public final String id;
        public final String url;
        public final String author;
        public final String text;
        public final String quote;
        public final int photos;

        Tweet(String id, String url, String author, String text, String quote, int photos) {
            this.id = id;
            this.url = url;
            this.author = author;
            this.text = text;
            this.quote = quote;
            this.photos = photos;
        }
    }

    /** The server's link, as comments_server.py prints it: https://address:port/secret#fingerprint. */
    public static final class ServerLink {
        final String host;
        final int port;
        final String token;
        final String pin;

        ServerLink(String host, int port, String token, String pin) {
            this.host = host;
            this.port = port;
            this.token = token;
            this.pin = pin;
        }

        /** Where it is, without its secret. */
        public String shown() {
            return (host.indexOf(':') >= 0 ? "[" + host + "]" : host) + ":" + port;
        }

        /** The link as it is kept. */
        public String text() {
            return "https://" + shown() + "/" + token + "#" + pin;
        }

        String address(String action) {
            return "https://" + shown() + "/" + token + "/" + action;
        }
    }

    /** A server's link as pasted (with whatever a message adds around it), or null when it is not one. */
    public static ServerLink parseLink(String pasted) {
        String link = PASTED_MARKS.matcher(pasted == null ? "" : pasted).replaceAll("");
        if (link.isEmpty()) {
            return null;
        }
        try {
            URI uri = new URI(link);
            String host = uri.getHost();
            String path = uri.getRawPath() == null ? "" : uri.getRawPath();
            String token = path.startsWith("/") ? path.substring(1) : path;
            token = token.endsWith("/") ? token.substring(0, token.length() - 1) : token;
            String pin = uri.getRawFragment() == null ? "" : uri.getRawFragment().toLowerCase(Locale.ROOT);
            int port = uri.getPort() == -1 ? 443 : uri.getPort();
            if (!"https".equalsIgnoreCase(uri.getScheme()) || host == null || uri.getRawUserInfo() != null
                    || uri.getRawQuery() != null || port < 1 || port > 65535 || !TOKEN.matcher(token).matches()
                    || !PIN.matcher(pin).matches()) {
                return null;
            }
            if (host.startsWith("[") && host.endsWith("]")) {
                host = host.substring(1, host.length() - 1);
            }
            return new ServerLink(host.toLowerCase(Locale.ROOT), port, token, pin);
        } catch (URISyntaxException e) {
            return null;
        }
    }

    /** The tweets in pasted text, each once and in order: x.com / twitter.com links or bare tweet numbers. */
    public static List<String> tweetIds(String text) {
        List<String> found = new ArrayList<>();
        Matcher m = TWEET_LINK.matcher(text == null ? "" : text);
        while (m.find()) {
            String id = m.group(1) != null ? m.group(1) : m.group(2);
            if (!found.contains(id)) {
                found.add(id);
            }
        }
        return found;
    }

    // ---------------------------------------------------------------- what the server is asked

    /** Whether the server answers with this link: the model it writes with. */
    public static String ping(ServerLink link) throws CommentError {
        return call(link, "ping", null, pingTimeoutMs).optString("model", "");
    }

    /** One tweet, as the server read it: its link, author, text, the post it quotes, and how many photos it has. */
    public static Tweet readTweet(ServerLink link, String id) throws CommentError {
        JSONObject tweet = call(link, "tweet", ask(id), tweetTimeoutMs).optJSONObject("tweet");
        if (tweet == null) {
            throw new CommentError(UNCLEAR);
        }
        String url = tweet.optString("url", "");
        return new Tweet(id, X_LINK.matcher(url).matches() ? url : "https://x.com/i/status/" + id,
            tweet.optString("author", ""), tweet.optString("text", "").trim(), tweet.optString("quote", "").trim(),
            Math.max(0, tweet.optInt("photos", 0)));
    }

    /** The tweet's comment, as it is posted: the server asks Claude with this phone's voice and manner, and its
     * last replies (this one must not look like them). */
    public static String comment(ServerLink link, String id, int voice, int manner, List<String> before) throws CommentError {
        JSONObject body;
        try {
            body = ask(id).put("voice", voice).put("manner", manner).put("before", new JSONArray(before));
        } catch (JSONException e) {
            throw new CommentError(UNCLEAR);
        }
        String reply = call(link, "comment", body, commentTimeoutMs).optString("comment", "").trim();
        if (reply.isEmpty()) {
            throw new CommentError("لم يصل تعليق من السيرفر. أعد المحاولة.");
        }
        return reply;
    }

    private static JSONObject ask(String id) throws CommentError {
        try {
            return new JSONObject().put("id", id);
        } catch (JSONException e) {
            throw new CommentError(UNCLEAR);
        }
    }

    /** One request to the server; a connection that did not reach it is tried again, a little later. */
    private static JSONObject call(ServerLink link, String action, JSONObject body, int readTimeout) throws CommentError {
        for (int attempt = 0; ; attempt++) {
            try {
                return exchange(link, action, body, readTimeout);
            } catch (NotReached e) {
                if (attempt >= retryWaitsMs.length) {
                    throw new CommentError(e.getMessage());
                }
                sleep(retryWaitsMs[attempt]);
            }
        }
    }

    private static JSONObject exchange(ServerLink link, String action, JSONObject body, int readTimeout)
            throws CommentError, NotReached {
        Pinned pinned = new Pinned(link.pin);
        HttpsURLConnection c;
        byte[] sent = body == null ? null : body.toString().getBytes(StandardCharsets.UTF_8);
        try {
            SSLContext tls = SSLContext.getInstance("TLS");
            tls.init(null, new TrustManager[] {pinned}, null);
            c = (HttpsURLConnection) new URL(link.address(action)).openConnection();
            c.setSSLSocketFactory(tls.getSocketFactory());
            c.setHostnameVerifier((host, session) -> true);    // (the certificate is known by its fingerprint instead)
            c.setConnectTimeout(connectTimeoutMs);
            c.setReadTimeout(readTimeout);
            c.setUseCaches(false);
            c.setRequestProperty("Accept", "application/json");
            if (sent != null) {
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setFixedLengthStreamingMode(sent.length);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            }
        } catch (IOException | GeneralSecurityException | RuntimeException e) {
            throw new CommentError(NOT_REACHED);
        }
        int status;
        String raw;
        try {
            try {
                c.connect();
            } catch (IOException e) {
                throw notReached(link, pinned, e);
            }
            try {
                if (sent != null) {
                    try (OutputStream out = c.getOutputStream()) {
                        out.write(sent);
                    }
                }
                status = c.getResponseCode();
                InputStream stream = status < 400 ? c.getInputStream() : c.getErrorStream();
                raw = stream == null ? "" : new String(readAll(stream, 1 << 20), StandardCharsets.UTF_8);
            } catch (IOException e) {
                throw new CommentError(pinned.wrong ? WRONG_CERTIFICATE : e instanceof SocketTimeoutException ? SLOW : LOST);
            }
        } finally {
            c.disconnect();
        }
        if (status == 404) {
            throw new CommentError(WRONG_SECRET);
        }
        JSONObject answer;
        try {
            answer = new JSONObject(raw);
        } catch (JSONException e) {
            throw new CommentError(status == 200 ? UNCLEAR : "ردّ السيرفر برمز " + status + ". أعد المحاولة.");
        }
        if (!answer.optBoolean("ok", false)) {
            String error = answer.optString("error", "").trim();
            throw new CommentError(error.isEmpty() ? "ردّ السيرفر برمز " + status + ". أعد المحاولة." : error);
        }
        return answer;
    }

    /** Why a connection did not reach the server, in words its owner can act on: thrown when trying again would
     * not help, returned (to be tried again) when it may. */
    private static NotReached notReached(ServerLink link, Pinned pinned, IOException e) throws CommentError {
        StringBuilder said = new StringBuilder();
        for (Throwable t = e; t != null && said.length() < 2000; t = t.getCause() == t ? null : t.getCause()) {
            said.append(' ').append(t.getMessage());
        }
        String m = said.toString().toLowerCase(Locale.ROOT);
        String port = String.valueOf(link.port);
        if (pinned.wrong) {
            throw new CommentError(WRONG_CERTIFICATE);
        }
        if (seen(e, SocketTimeoutException.class)) {
            throw new CommentError("لا رد من السيرفر على المنفذ " + port + ". افتح هذا المنفذ (TCP) في جدار حماية "
                + "الاستضافة من لوحتها، وتأكد من عنوان السيرفر.");
        }
        if (seen(e, NoRouteToHostException.class) || m.contains("ehostunreach") || m.contains("no route to host")) {
            throw new CommentError("لا طريق إلى السيرفر على المنفذ " + port + ": المنفذ مغلق في جدار حمايته، "
                + "أو الهاتف بلا إنترنت.");
        }
        if (seen(e, UnknownHostException.class) || m.contains("enetunreach") || m.contains("network is unreachable")) {
            throw new CommentError(NOT_REACHED);
        }
        if (seen(e, ConnectException.class) && m.contains("refused")) {
            return new NotReached("السيرفر رفض الاتصال على المنفذ " + port + ": برنامج التعليقات متوقف، أو المنفذ "
                + "مغلق في جدار حمايته (على السيرفر: systemctl restart comments-server).");
        }
        return new NotReached(seen(e, SSLException.class) ? NO_SAFE_CONNECTION : NOT_REACHED);
    }

    /** Whether the failure is of this kind, or came from one (a phone's connection may wrap what happened). */
    private static boolean seen(Throwable e, Class<?> kind) {
        for (Throwable t = e; t != null; t = t.getCause() == t ? null : t.getCause()) {
            if (kind.isInstance(t)) {
                return true;
            }
        }
        return false;
    }

    /** Takes the server's certificate only when it is the one the link names (by its SHA-256 fingerprint). */
    private static final class Pinned implements X509TrustManager {
        final String pin;
        volatile boolean wrong;

        Pinned(String pin) {
            this.pin = pin;
        }

        @Override
        public void checkServerTrusted(X509Certificate[] chain, String authType) throws CertificateException {
            if (chain == null || chain.length == 0 || !pin.equals(sha256(chain[0].getEncoded()))) {
                wrong = true;
                throw new CertificateException("not the certificate this link names");
            }
        }

        @Override
        public void checkClientTrusted(X509Certificate[] chain, String authType) throws CertificateException {
            throw new CertificateException("only the server is checked");
        }

        @Override
        public X509Certificate[] getAcceptedIssuers() {
            return new X509Certificate[0];
        }
    }

    static String sha256(byte[] data) {
        try {
            StringBuilder hex = new StringBuilder();
            for (byte b : MessageDigest.getInstance("SHA-256").digest(data)) {
                hex.append(Character.forDigit((b >> 4) & 0xF, 16)).append(Character.forDigit(b & 0xF, 16));
            }
            return hex.toString();
        } catch (GeneralSecurityException e) {
            return "";
        }
    }

    // ---------------------------------------------------------------- small helpers

    private static byte[] readAll(InputStream in, int limit) throws IOException {
        try (InputStream stream = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[16384];
            int n;
            while ((n = stream.read(buffer)) > 0) {
                out.write(buffer, 0, n);
                if (out.size() > limit) {
                    break;
                }
            }
            return out.toByteArray();
        }
    }

    private static void sleep(long ms) throws CommentError {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new CommentError("أُوقف.");
        }
    }
}
