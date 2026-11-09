package app.tg.comments;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Authenticator;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.PasswordAuthentication;
import java.net.Proxy;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Random;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * One comment for each tweet, without a browser: the tweet is read through FxTwitter's free API (its text, the
 * post it quotes and its photos), Claude is asked with the same rules as the groups script's comments (about
 * the tweet's topic, not about the writer; everyday Saudi Arabic; no compliments and no lessons), and the reply
 * is made ready to post: no diacritics, no commas, at most 100 characters.
 */
public final class Engine {
    // The addresses (tests point them at local stand-ins) and how long a busy Claude is waited for.
    static String fxStatusUrl = "https://api.fxtwitter.com/status/";
    static String claudeUrl = "https://api.anthropic.com/v1/messages";
    static String claudeVersion = "2026-10-21";
    static long[] busyWaitsMs = {5000, 12000, 20000, 30000};
    static String photoPrefix = "https://pbs.twimg.com/";

    public static final String DEFAULT_MODEL = "claude-haiku-5-5";
    public static final int MAX_CHARS = 100;
    public static final int MAX_TWEETS = 20;
    static final int TWEET_TEXT = 1200;
    static final int MAX_PHOTOS = 2;
    static final int MAX_PHOTO_BYTES = 3_500_000;
    static final String EFFORT = "medium";
    private static final Set<String> effortRefused = Collections.synchronizedSet(new HashSet<>());

    public static final String[] VOICES = {"male", "female", "neutral"};
    static final String[] VOICE_RULES = {
        "You write as a man: when you speak of yourself, use masculine forms (in Arabic: مبسوط، متأكد).",
        "You write as a woman: when you speak of yourself, use feminine forms (in Arabic: مبسوطة، متأكدة).",
        "The reply must suit a man or a woman: never use words that show the writer's gender "
            + "(in Arabic, no forms like مبسوط or مبسوطة, متأكد or متأكدة).",
    };
    // How a reply takes the tweet: one is drawn for each reply, so they do not all read the same way.
    static final String[] ANGLES = {
        "a frank remark on its point, the way people talk among friends",
        "you disagree with one point in it, and say why",
        "a detail in it that most people will miss",
        "a light, dry joke about the situation",
        "you doubt one part of it, and say what does not add up",
        "a comparison with something from everyday life",
        "what it will lead to, or what comes next",
        "a short question about one point in it",
    };
    // How this phone writes: one manner, drawn once and kept, as a real person keeps their own.
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
    static final int[][] LENGTHS = {{6, 10}, {8, 13}, {10, 16}};

    private static final Pattern TWEET_LINK = Pattern.compile(
        "(?:x|twitter)\\.com/[A-Za-z0-9_]{1,15}(?:/web)?/status(?:es)?/(\\d{6,25})|(?<![\\d/])(\\d{15,25})(?!\\d)");
    private static final Pattern KEY = Pattern.compile("sk-ant-[A-Za-z0-9_\\-]{20,250}");
    private static final Pattern MODEL = Pattern.compile("[a-z0-9][a-z0-9.\\-]{1,60}");
    private static final Pattern DIACRITICS = Pattern.compile("[\\u064B-\\u0652\\u0656-\\u065F\\u0670]");
    // Words a reply cannot end on, when one that is too long is shortened.
    static final Set<String> HANGING_WORDS = new HashSet<>(Arrays.asList(
        "و", "او", "أو", "بس", "لكن", "لان", "لأن", "عشان", "اللي", "الي", "في", "من", "على", "عن", "مع", "الى",
        "إلى", "يعني", "ثم", "حتى", "اذا", "إذا", "لو", "ما", "لا", "كان", "and", "or", "but", "the", "to", "of",
        "in", "a", "an", "with", "for", "that", "ve", "ama", "ile", "için", "bir", "de", "da"));

    private Engine() {
    }

    /** What stops one tweet's comment; its message is shown on the tweet's card. */
    public static final class CommentError extends Exception {
        public CommentError(String message) {
            super(message);
        }
    }

    public static final class Tweet {
        public final String id;
        public final String url;
        public final String author;
        public final String text;
        public final String quote;
        public final List<String> photos;

        Tweet(String id, String url, String author, String text, String quote, List<String> photos) {
            this.id = id;
            this.url = url;
            this.author = author;
            this.text = text;
            this.quote = quote;
            this.photos = photos;
        }
    }

    public static final class Photo {
        final String mime;
        final byte[] data;

        Photo(String mime, byte[] data) {
            this.mime = mime;
            this.data = data;
        }
    }

    /** The proxy Claude is reached through, when a home VPN is what Claude refuses: the groups server, from the link
     * in the settings (http://user:password@host:port). Only Claude's requests go through it; the tweets and their
     * photos are read directly. The connection stays encrypted from the phone to Claude (the proxy only passes it on). */
    public static final class ProxyLink {
        final String host;
        final int port;
        final String user;
        final String password;

        ProxyLink(String host, int port, String user, String password) {
            this.host = host;
            this.port = port;
            this.user = user;
            this.password = password;
        }

        /** Where it is, without its password. */
        public String shown() {
            return host + ":" + port;
        }
    }

    static volatile ProxyLink proxy;

    /** A proxy's link as pasted (http://user:password@host:port, or without http://), or null when it is not one. */
    public static ProxyLink parseProxy(String text) {
        String link = text == null ? "" : text.trim();
        if (link.isEmpty()) {
            return null;
        }
        if (!link.contains("://")) {
            link = "http://" + link;
        }
        try {
            URI uri = new URI(link);
            String path = uri.getRawPath();
            if (!"http".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getPort() < 1 || uri.getPort() > 65535
                    || (path != null && !path.isEmpty() && !path.equals("/")) || uri.getRawQuery() != null) {
                return null;
            }
            String info = uri.getUserInfo() == null ? "" : uri.getUserInfo();
            int colon = info.indexOf(':');
            return new ProxyLink(uri.getHost(), uri.getPort(), colon < 0 ? info : info.substring(0, colon),
                colon < 0 ? "" : info.substring(colon + 1));
        } catch (URISyntaxException e) {
            return null;
        }
    }

    /** Claude is reached through this proxy from now on (null: directly). Its username and password are given when
     * the proxy asks for them (and to nobody else). */
    public static void useProxy(ProxyLink link) {
        proxy = link;
        Authenticator.setDefault(link == null || link.user.isEmpty() ? null : new Authenticator() {
            @Override
            protected PasswordAuthentication getPasswordAuthentication() {
                ProxyLink now = proxy;
                if (now == null || getRequestorType() != RequestorType.PROXY) {
                    return null;
                }
                return new PasswordAuthentication(now.user, now.password.toCharArray());
            }
        });
    }

    /** Why the proxy itself turned the request away (its password, or an address it does not let through), or
     * null when it did not. */
    static String proxyRefusal(ProxyLink through, String message) {
        if (through == null) {
            return null;
        }
        // (A status, as a word of its own: a port like 44071 in the message is not one.)
        String m = String.valueOf(message).toLowerCase(Locale.ROOT);
        if (m.contains("authenticate") || Pattern.compile("\\b40[17]\\b").matcher(m).find()) {
            return "البروكسي رفض اسم المستخدم أو كلمة المرور. راجع رابطه في «الإعدادات».";
        }
        if (Pattern.compile("\\b403\\b").matcher(m).find()) {
            return "البروكسي لا يسمح بالوصول إلى Claude. أعد تشغيل أمر إعداده على سيرفرك.";
        }
        return null;
    }

    public static boolean validKey(String key) {
        return key != null && KEY.matcher(key).matches();
    }

    public static boolean validModel(String model) {
        return model != null && MODEL.matcher(model).matches();
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

    // ---------------------------------------------------------------- the tweet, without a browser

    /** One tweet through FxTwitter: its link, author, text, the post it quotes, and its photos' addresses
     * (a video's picture for a video). */
    public static Tweet readTweet(String id) throws CommentError {
        String link = "https://x.com/i/status/" + id;
        JSONObject data;
        try {
            HttpURLConnection c = open(fxStatusUrl + id, 20_000);
            c.setRequestProperty("User-Agent", "tg-comments/1.0");
            c.setRequestProperty("Accept", "application/json");
            int code = c.getResponseCode();
            if (code == 401 || code == 403 || code == 404 || code == 410) {
                throw new CommentError("التغريدة غير موجودة أو حسابها خاص.");
            }
            if (code == 429 || code == 503) {
                throw new CommentError("خدمة قراءة التغريدات مشغولة الآن. أعد المحاولة بعد دقائق.");
            }
            if (code != 200) {
                throw new CommentError("تعذّرت قراءة التغريدة (رمز " + code + ").");
            }
            data = new JSONObject(new String(readAll(c.getInputStream(), 4 << 20), StandardCharsets.UTF_8));
        } catch (IOException | JSONException e) {
            throw new CommentError("تعذّرت قراءة التغريدة. تأكد من الاتصال بالإنترنت.");
        }
        JSONObject tweet = data.optJSONObject("tweet");
        if (tweet == null) {
            throw new CommentError("التغريدة غير موجودة أو حسابها خاص.");
        }
        List<String> photos = new ArrayList<>();
        JSONObject media = tweet.optJSONObject("media");
        JSONArray all = media == null ? null : media.optJSONArray("all");
        for (int i = 0; all != null && i < all.length(); i++) {
            JSONObject item = all.optJSONObject(i);
            if (item == null) {
                continue;
            }
            String address = "photo".equals(item.optString("type")) ? item.optString("url") : item.optString("thumbnail_url");
            if (address.startsWith(photoPrefix) && !photos.contains(address)) {
                photos.add(address);
            }
        }
        JSONObject author = tweet.optJSONObject("author");
        JSONObject quote = tweet.optJSONObject("quote");
        String url = tweet.optString("url", "");
        return new Tweet(id, url.isEmpty() ? link : url, author == null ? "" : author.optString("screen_name", ""),
            tweet.optString("text", "").trim(), quote == null ? "" : quote.optString("text", "").trim(),
            new ArrayList<>(photos.subList(0, Math.min(4, photos.size()))));
    }

    /** The tweet's first photos, in a medium size (null for one that cannot be had). */
    public static List<Photo> downloadPhotos(Tweet tweet) {
        List<Photo> photos = new ArrayList<>();
        for (String address : tweet.photos) {
            if (photos.size() >= MAX_PHOTOS) {
                break;
            }
            String url = address.contains("name=") ? address.replaceAll("name=[A-Za-z0-9]+", "name=medium") : address;
            try {
                HttpURLConnection c = open(url, 30_000);
                c.setRequestProperty("User-Agent", "tg-comments/1.0");
                if (c.getResponseCode() != 200) {
                    continue;
                }
                String mime = String.valueOf(c.getContentType()).split(";")[0].trim().toLowerCase(Locale.ROOT);
                byte[] body = readAll(c.getInputStream(), MAX_PHOTO_BYTES + 1);
                if (body.length > 0 && body.length <= MAX_PHOTO_BYTES
                        && Arrays.asList("image/jpeg", "image/png", "image/webp", "image/gif").contains(mime)) {
                    photos.add(new Photo(mime, body));
                }
            } catch (IOException e) {
                // (the comment is still asked for, from the tweet's text)
            }
        }
        return photos;
    }

    // ---------------------------------------------------------------- what Claude is asked

    /** The ask for one reply, with the angle and the length drawn for it, this phone's manner and voice, and
     * the replies it must not look like. */
    static String prompt(int voice, int manner, List<String> before, boolean photos, Random rng) {
        int[] length = LENGTHS[rng.nextInt(LENGTHS.length)];
        StringBuilder p = new StringBuilder();
        if (photos) {
            p.append("Look at the tweet's photos too. ");
        }
        p.append("Write one public reply to this tweet, as an ordinary person scrolling X who comments on what it says. ")
            .append("Read the whole tweet first and answer what it really says (its point, and its ending when it tells a story); ")
            .append("do not guess beyond it. ")
            .append("Not personal: the reply is about the tweet's topic, not about you. No stories or experiences of yours, ")
            .append("nothing that happened to you, your family or your friends, and nothing about your own life (nothing like: ")
            .append("صار معي، جربت، شفت بنفسي، أخوي، عندنا في البيت، أتذكر لما، لو كنت مكانه). ")
            .append("Speak about its topic, not to please its author: do not compliment, thank or flatter the author, do not ")
            .append("praise the tweet (nothing like: well said, you're right, great post, صدقت، كفو، أبدعت، كلامك ذهب), and do ")
            .append("not just agree. Honest, never rude: no insults, and do not mock the author. ")
            .append("This time: ").append(ANGLES[rng.nextInt(ANGLES.length)]).append(". ")
            .append("Your manner (always the same): ").append(MANNERS[Math.floorMod(manner, MANNERS.length)]).append(". ")
            .append("If the tweet is a prayer (دعاء), about religion, a death, an illness or other sad news, leave the angle and ")
            .append("the manner: reply sincerely and respectfully, with no joke and no disagreement. ")
            .append("At most ").append(MAX_CHARS).append(" characters, spaces included (about ").append(length[0]).append(" to ")
            .append(length[1]).append(" words). Finish the sentence. Never stop mid-word or mid-phrase. ")
            .append("Write in the tweet's own language. An Arabic tweet gets everyday spoken Saudi Arabic (عامية سعودية بيضاء), ")
            .append("the way people really type on X, never formal Arabic (فصحى): no words like الأولى، ينبغي، لا تثبت، ما زال، ")
            .append("إذ، لذا، حيث، كما أن. A tweet in another language (English, Turkish…) gets its reply in that language. ")
            .append("Plain letters only: no diacritics (no تشكيل: no fatha, damma, kasra, tanween, shadda or sukun), and no ")
            .append("commas (، or ,). ").append(VOICE_RULES[Math.floorMod(voice, VOICE_RULES.length)]).append(" ")
            .append("Sound like a real person, specific and casual, not a teacher, a preacher, a brand or an assistant: no ")
            .append("advice or lessons to the author or to people (nothing like: الأولى أن، يجب، الأفضل أن، تذكّر، نصيحتي), no ")
            .append("general wisdom or sayings, no neat conclusion; just make your point about the topic itself. ")
            .append(photos
                ? "The photos are the tweet's own: never mention them, the picture, the post or the tweet "
                    + "(no الصورة، المنشور، التغريدة); react to what is in them. "
                : "Never mention the post or the tweet (no المنشور، التغريدة); react to what it says. ")
            .append("Do not open with filler (like صراحة، فعلاً، والله) or by repeating the tweet. ")
            .append("No hashtags, no links, no quotes, no labels, no introduction. Return only the finished reply.");
        if (!before.isEmpty()) {
            p.append("\n\nYour own last replies to other tweets. This one must not look like them (another opening, ")
                .append("another shape, other words):");
            for (String text : before) {
                p.append("\n- ").append(text);
            }
        }
        return p.toString();
    }

    /** The tweet's words as Claude reads them (with the post it quotes). */
    static String tweetWords(Tweet tweet) {
        String words = tweet.text.isEmpty() ? "(no text: only its photos)" : tweet.text;
        if (!tweet.quote.isEmpty()) {
            words += "\n(It quotes: " + tweet.quote + ")";
        }
        return words.length() > TWEET_TEXT ? words.substring(0, TWEET_TEXT) : words;
    }

    /** One answer from Claude (its text blocks; it may think first), at medium effort. A model that takes no
     * effort setting is asked again without it, a busy Claude a few times, and photos it cannot read are left
     * out. */
    static String askClaude(String key, String model, List<Photo> photos, String text) throws CommentError {
        List<Photo> sent = new ArrayList<>(photos);
        ProxyLink through = proxy;
        boolean effort = !effortRefused.contains(model);
        int busy = 0;
        int lost = 0;
        while (true) {
            String detail = "";
            String errorCode = "";
            int status;
            try {
                JSONArray content = new JSONArray();
                for (Photo photo : sent) {
                    content.put(new JSONObject().put("type", "image").put("source", new JSONObject()
                        .put("type", "base64").put("media_type", photo.mime)
                        .put("data", Base64.getEncoder().encodeToString(photo.data))));
                }
                content.put(new JSONObject().put("type", "text").put("text", text));
                // (Its thinking counts toward max_tokens: room for it, and for the short reply after it.)
                JSONObject body = new JSONObject().put("model", model).put("max_tokens", 2048)
                    .put("messages", new JSONArray().put(new JSONObject().put("role", "user").put("content", content)));
                if (effort) {
                    body.put("output_config", new JSONObject().put("effort", EFFORT));
                }
                HttpURLConnection c = open(claudeUrl, 90_000, through);
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                c.setRequestProperty("x-api-key", key);
                c.setRequestProperty("anthropic-version", claudeVersion);
                try (OutputStream out = c.getOutputStream()) {
                    out.write(body.toString().getBytes(StandardCharsets.UTF_8));
                }
                status = c.getResponseCode();
                InputStream stream = status < 400 ? c.getInputStream() : c.getErrorStream();
                String raw = stream == null ? "" : new String(readAll(stream, 2 << 20), StandardCharsets.UTF_8);
                if (status == 200) {
                    JSONObject answer = new JSONObject(raw);
                    if ("refusal".equals(answer.optString("stop_reason"))) {
                        throw new CommentError("امتنع Claude عن التعليق على هذه التغريدة.");
                    }
                    StringBuilder words = new StringBuilder();
                    JSONArray blocks = answer.optJSONArray("content");
                    for (int i = 0; blocks != null && i < blocks.length(); i++) {
                        JSONObject block = blocks.optJSONObject(i);
                        if (block != null && "text".equals(block.optString("type"))) {
                            words.append(words.length() > 0 ? "\n" : "").append(block.optString("text"));
                        }
                    }
                    return words.toString();
                }
                try {
                    JSONObject error = new JSONObject(raw).optJSONObject("error");
                    if (error != null) {
                        detail = error.optString("message", "");
                        JSONObject details = error.optJSONObject("details");
                        errorCode = details == null ? "" : details.optString("error_code", "");
                    }
                } catch (JSONException e) {
                    detail = "";
                }
            } catch (IOException | JSONException e) {
                String refused = e instanceof IOException ? proxyRefusal(through, e.getMessage()) : null;
                if (refused != null) {
                    throw new CommentError(refused);
                }
                if (lost < 2) {
                    sleep(busyWaitsMs[lost++]);
                    continue;
                }
                throw new CommentError(through == null ? "تعذّر الاتصال بـ Claude. تأكد من الاتصال بالإنترنت."
                    : "تعذّر الاتصال بـ Claude عبر البروكسي. تأكد من رابطه في «الإعدادات» ومن أن سيرفرك يعمل.");
            }
            detail = detail.replace(key, "…").replaceAll("\\s+", " ").trim();
            if (detail.length() > 160) {
                detail = detail.substring(0, 160);
            }
            String lower = detail.toLowerCase(Locale.ROOT);
            if (effort && status == 400 && (lower.contains("effort") || lower.contains("output_config"))) {
                effort = false;              // this model takes no effort setting: asked again without it,
                effortRefused.add(model);    // and not sent it again
                continue;
            }
            if (!sent.isEmpty() && status == 400 && lower.contains("image")) {
                sent.clear();                // a photo it cannot read: asked again from the text alone
                continue;
            }
            if (status == 407) {
                throw new CommentError(proxyRefusal(through, "407"));
            }
            if (status == 401) {
                throw new CommentError("مفتاح Claude غير صالح. راجعه في «الإعدادات».");
            }
            if (status == 403) {
                if (lower.contains("not allowed") || lower.contains("forbidden") || lower.contains("region")
                        || lower.contains("country")) {
                    // Claude does not take requests from where this one comes: a home VPN, most often.
                    throw new CommentError(through == null
                        ? "Claude لا يقبل الطلب من هذه الشبكة. إذا كان عندك VPN منزلي فضع رابط «البروكسي» في «الإعدادات»."
                        : "Claude لا يقبل الطلب حتى عبر البروكسي.");
                }
                throw new CommentError("مفتاح Claude بلا صلاحية لهذا الطلب" + (detail.isEmpty() ? "." : ": " + detail));
            }
            if ("enforced_spend_limit_reached".equals(errorCode) || lower.contains("usage limits")
                    || lower.contains("credit balance") || lower.contains("billing")) {
                throw new CommentError("بلغ حساب Claude حد الصرف أو نفد رصيده.");
            }
            if (status == 404 && lower.contains("model")) {
                throw new CommentError("النموذج غير موجود. راجع اسمه في «الإعدادات».");
            }
            if (status == 429 || status == 500 || status == 502 || status == 503 || status == 504 || status == 529) {
                if (busy < busyWaitsMs.length) {
                    sleep(busyWaitsMs[busy++]);
                    continue;
                }
                throw new CommentError("Claude مشغول الآن. أعد المحاولة بعد قليل.");
            }
            throw new CommentError(detail.isEmpty() ? "تعذّر الاتصال بـ Claude (رمز " + status + ")." : "رفض Claude الطلب: " + detail);
        }
    }

    /** One reply as it is posted: whole (a reply cut off is asked for again), no diacritics, no commas, at most
     * MAX_CHARS characters (one that comes back longer is asked for once more, then shortened at a word). */
    public static String comment(String key, String model, int voice, int manner, Tweet tweet, List<Photo> photos,
                                 List<String> before, Random rng) throws CommentError {
        String first = polish(completeReply(key, model, voice, manner, tweet, photos, before, rng));
        if (length(first) <= MAX_CHARS) {
            return first;
        }
        String second;
        try {
            second = polish(completeReply(key, model, voice, manner, tweet, photos, before, rng));   // too long: once more
        } catch (CommentError e) {
            second = "";
        }
        if (!second.isEmpty() && length(second) <= MAX_CHARS) {
            return second;
        }
        return fit(second.isEmpty() || length(first) <= length(second) ? first : second, MAX_CHARS);
    }

    private static String completeReply(String key, String model, int voice, int manner, Tweet tweet, List<Photo> photos,
                                        List<String> before, Random rng) throws CommentError {
        String best = "";
        for (int attempt = 0; attempt < 2; attempt++) {
            String ask = prompt(voice, manner, before, !photos.isEmpty(), rng) + "\n\nTweet text:\n" + tweetWords(tweet);
            String text = clean(askClaude(key, model, photos, ask));
            if (isComplete(text)) {
                return text;
            }
            if (text.length() > best.length()) {
                best = text;
            }
        }
        if (isComplete(best)) {
            return best;
        }
        throw new CommentError("لم يكتب Claude تعليقًا كاملًا. أعد المحاولة.");
    }

    // ---------------------------------------------------------------- the reply's text (as in the groups script)

    static String clean(String raw) {
        String text = (raw == null ? "" : raw).replaceAll("\\s+", " ").trim().replace("**", "");
        text = Pattern.compile("^(?:reply|comment|التعليق|الرد|الدعاء)\\s*[:：\\-]\\s*", Pattern.CASE_INSENSITIVE)
            .matcher(text).replaceFirst("");
        // (Arabic letters named by their range: Android's regex refuses the flag for Unicode word letters.)
        text = Pattern.compile("^[\\w\\u0600-\\u06FF]{2,24}\\s*[:：]\\s*").matcher(text).replaceFirst("");
        text = stripChars(text, "\"'«»“” ");
        if (text.length() > 280) {
            String cut = text.substring(0, 280);
            int end = Math.max(Math.max(cut.lastIndexOf('؟'), cut.lastIndexOf('?')),
                Math.max(Math.max(cut.lastIndexOf('!'), cut.lastIndexOf('.')), cut.lastIndexOf('…')));
            text = end >= 40 ? cut.substring(0, end + 1) : cut.substring(0, Math.max(0, cut.lastIndexOf(' ')));
        }
        return text.trim();
    }

    static boolean isComplete(String text) {
        // An emoji or two at the end are fine: the words before them are what is checked.
        String t = (text == null ? "" : text).replaceAll("[\\s\\u200d\\ufe0f\\u2600-\\u27bf\\x{1F000}-\\x{1FAFF}]+$", "");
        String[] words = t.trim().isEmpty() ? new String[0] : t.trim().split("\\s+");
        if (words.length < 6) {
            return false;
        }
        if (Pattern.compile("[،,؛;:：\\-–—/\\\\*]$").matcher(t.trim()).find()) {
            return false;
        }
        String last = words[words.length - 1].replaceAll("[.!?؟…\"'«»“”]+$", "");
        return last.length() > 2;
    }

    /** The reply as it is posted: no Arabic diacritics and no commas (a number's thousands keep their digits). */
    static String polish(String text) {
        String t = DIACRITICS.matcher(text == null ? "" : text).replaceAll("");
        t = t.replaceAll("(?<=\\d),(?=\\d{3})", "");
        t = t.replaceAll("\\s*[،,؛;]\\s*", " ");
        return t.replaceAll("\\s+", " ").trim();
    }

    /** A reply cut to at most limit characters, at a word, and not on a word that leaves it hanging. */
    static String fit(String text, int limit) {
        List<String> words = new ArrayList<>(Arrays.asList(text.trim().isEmpty() ? new String[0] : text.trim().split("\\s+")));
        while (!words.isEmpty() && length(String.join(" ", words)) > limit) {
            words.remove(words.size() - 1);
        }
        while (!words.isEmpty() && HANGING_WORDS.contains(words.get(words.size() - 1).toLowerCase(Locale.ROOT))) {
            words.remove(words.size() - 1);
        }
        return String.join(" ", words);
    }

    static int length(String text) {
        return text.codePointCount(0, text.length());
    }

    /** What the settings show of the key: only its first and last letters. */
    public static String shown(String key) {
        return key == null || key.length() < 12 ? "" : key.substring(0, 7) + "…" + key.substring(key.length() - 4);
    }

    // ---------------------------------------------------------------- small helpers

    private static String stripChars(String text, String chars) {
        int start = 0;
        int end = text.length();
        while (start < end && chars.indexOf(text.charAt(start)) >= 0) {
            start++;
        }
        while (end > start && chars.indexOf(text.charAt(end - 1)) >= 0) {
            end--;
        }
        return text.substring(start, end);
    }

    private static HttpURLConnection open(String address, int readTimeout) throws IOException {
        return open(address, readTimeout, null);
    }

    private static HttpURLConnection open(String address, int readTimeout, ProxyLink through) throws IOException {
        URL url = new URL(address);
        HttpURLConnection c = (HttpURLConnection) (through == null ? url.openConnection()
            : url.openConnection(new Proxy(Proxy.Type.HTTP, new InetSocketAddress(through.host, through.port))));
        c.setConnectTimeout(20_000);
        c.setReadTimeout(readTimeout);
        c.setUseCaches(false);
        return c;
    }

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
