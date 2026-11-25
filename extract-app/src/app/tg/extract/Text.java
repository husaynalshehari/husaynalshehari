package app.tg.extract;

import java.text.Normalizer;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Text rules with nothing of Android in them, so they are also checked on a computer (see the tests). */
final class Text {
    private Text() {
    }

    /** The countries on the page, in their order: a code, and its name in Arabic. */
    static final String[] CODES = {"sa", "ae", "kw", "qa", "om", "bh"};
    static final String[] NAMES = {"السعودية", "الإمارات", "الكويت", "قطر", "عُمان", "البحرين"};
    /** The ones the original script looked for (Bahrain was not among them). */
    static final String[] DEFAULT_CODES = {"sa", "ae", "kw", "qa", "om"};

    // What X's «Account based in» can say for each country, in English or in Arabic (same order as CODES).
    private static final String[][] KEYS = {
        {"saudi arabia", "saudi", "ksa", "السعودية", "السعوديه"},
        {"united arab emirates", "uae", "emirates", "dubai", "abu dhabi", "sharjah",
            "الإمارات", "دبي", "أبوظبي", "أبو ظبي", "الشارقة"},
        {"kuwait", "الكويت", "كويت"},
        {"qatar", "قطر"},
        {"oman", "عُمان", "عمان"},
        {"bahrain", "البحرين", "بحرين"},
    };
    private static final Pattern[] FINDERS = new Pattern[KEYS.length];

    static {
        for (int i = 0; i < KEYS.length; i++) {
            StringBuilder any = new StringBuilder();
            for (String key : KEYS[i]) {
                if (any.length() > 0) {
                    any.append('|');
                }
                any.append(Pattern.quote(normalize(key)));
            }
            // Whole words only: "oman" is not in "romania".
            FINDERS[i] = Pattern.compile("(?<![\\p{L}\\p{N}])(?:" + any + ")(?![\\p{L}\\p{N}])");
        }
    }

    /** Lower case, Arabic without its marks and with one kind of alef, single spaces. */
    static String normalize(String text) {
        if (text == null) {
            return "";
        }
        String t = Normalizer.normalize(text, Normalizer.Form.NFKC).toLowerCase(Locale.ROOT);
        t = t.replaceAll("[\\u064B-\\u065F\\u0670\\u0640]", "");
        t = t.replaceAll("[\\u0622\\u0623\\u0625\\u0671]", "ا");
        return t.replaceAll("\\s+", " ").trim();
    }

    /** The code of the country, among the allowed ones, that the location names; null when none. */
    static String countryOf(String location, Set<String> allowed) {
        String t = normalize(location);
        if (t.isEmpty()) {
            return null;
        }
        for (int i = 0; i < CODES.length; i++) {
            if (allowed.contains(CODES[i]) && FINDERS[i].matcher(t).find()) {
                return CODES[i];
            }
        }
        return null;
    }

    static String nameOf(String code) {
        for (int i = 0; i < CODES.length; i++) {
            if (CODES[i].equals(code)) {
                return NAMES[i];
            }
        }
        return code == null ? "" : code;
    }

    private static final Pattern PROFILE_LINK =
        Pattern.compile("(?:x|twitter)\\.com/@?([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])", Pattern.CASE_INSENSITIVE);

    /** An X username from what the user typed: "name", "@name" or a link to the account. Null when it is none. */
    static String cleanHandle(String input) {
        if (input == null) {
            return null;
        }
        String s = input.trim();
        Matcher link = PROFILE_LINK.matcher(s);
        if (link.find()) {
            s = link.group(1);
        }
        if (s.startsWith("@")) {
            s = s.substring(1);
        }
        return s.matches("[A-Za-z0-9_]{1,15}") ? s : null;
    }

    /**
     * The usernames in a text the user wrote (one or more a line: names, @names or links), once each whatever the
     * case, in their order. bad[0] (if given) gets how many pieces were not usernames.
     */
    static List<String> handlesIn(String text, int[] bad) {
        List<String> names = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        int wrong = 0;
        for (String part : (text == null ? "" : text).split("[\\s,،]+")) {
            if (part.isEmpty()) {
                continue;
            }
            String name = cleanHandle(part);
            if (name == null) {
                wrong++;
            } else if (seen.add(name.toLowerCase(Locale.ROOT))) {
                names.add(name);
            }
        }
        if (bad != null && bad.length > 0) {
            bad[0] = wrong;
        }
        return names;
    }

    private static final Pattern COUNT = Pattern.compile(
        "([0-9][0-9,]*(?:\\.[0-9]+)?)\\s*(آلاف|ألف|الف|ملايين|مليون|مليار|k|m|b)?", Pattern.CASE_INSENSITIVE);

    /** A count as X shows it ("12.3K", "1,234", "١٢٫٣ ألف"...) as a number; -1 when there is none. */
    static long parseCount(String text) {
        if (text == null) {
            return -1;
        }
        StringBuilder plain = new StringBuilder();
        for (char c : text.toCharArray()) {
            if (c >= '٠' && c <= '٩') {
                plain.append((char) ('0' + (c - '٠')));
            } else if (c >= '۰' && c <= '۹') {
                plain.append((char) ('0' + (c - '۰')));
            } else if (c == '٫') {
                plain.append('.');
            } else if (c == '٬') {
                plain.append(',');
            } else {
                plain.append(c);
            }
        }
        Matcher m = COUNT.matcher(plain);
        if (!m.find()) {
            return -1;
        }
        double value;
        try {
            value = Double.parseDouble(m.group(1).replace(",", ""));
        } catch (NumberFormatException e) {
            return -1;
        }
        String unit = m.group(2) == null ? "" : m.group(2).toLowerCase(Locale.ROOT);
        if (unit.equals("k") || unit.equals("ألف") || unit.equals("الف") || unit.equals("آلاف")) {
            value *= 1_000;
        } else if (unit.equals("m") || unit.equals("مليون") || unit.equals("ملايين")) {
            value *= 1_000_000;
        } else if (unit.equals("b") || unit.equals("مليار")) {
            value *= 1_000_000_000;
        }
        return Math.round(value);
    }

    /** The words looked for in crypto accounts (one a line; "word*": any word that starts so). */
    static final String CRYPTO_WORDS = "crypto*\nbitcoin*\nbtc\neth\nethereum*\nblockchain*\nweb3*\nnft*\ndefi\naltcoin*\n"
        + "memecoin*\nsolana*\nbinance*\nairdrop*\ntrading\ntrader*\ntoken*\nhodl*\nsatoshi*\nكريبتو\nعملات رقمية\n"
        + "العملات الرقمية\nعملة رقمية\nبيتكوين\nبتكوين\nايثيريوم\nإيثريوم\nبلوكتشين\nتداول\nميم كوين";

    /** The words of the user's list: separated by new lines or commas, once each. */
    static List<String> wordsIn(String text) {
        List<String> words = new ArrayList<>();
        for (String part : (text == null ? "" : text).split("[\\n,،]+")) {
            String word = part.trim();
            if (!word.isEmpty() && !words.contains(word)) {
                words.add(word);
            }
        }
        return words;
    }

    /** What to look for: Arabic words anywhere (they take prefixes), others as whole words ("word*": as a start). */
    static List<Pattern> wordPatterns(List<String> words) {
        List<Pattern> patterns = new ArrayList<>();
        for (String raw : words) {
            String word = normalize(raw);
            boolean start = word.endsWith("*");
            if (start) {
                word = word.substring(0, word.length() - 1).trim();
            }
            if (word.isEmpty()) {
                continue;
            }
            if (word.matches(".*[\\u0600-\\u06FF].*")) {
                patterns.add(Pattern.compile(Pattern.quote(word)));
            } else {
                patterns.add(Pattern.compile("(?<![\\p{L}\\p{N}])" + Pattern.quote(word) + (start ? "" : "(?![\\p{L}\\p{N}])")));
            }
        }
        return patterns;
    }

    static boolean mentions(String text, List<Pattern> patterns) {
        String plain = normalize(text);
        for (Pattern pattern : patterns) {
            if (pattern.matcher(plain).find()) {
                return true;
            }
        }
        return false;
    }

    /** One CSV cell: quoted, on one line, and never read as a formula by a spreadsheet. */
    static String csvCell(String value) {
        String v = value == null ? "" : value.replace("\r", " ").replace("\n", " ").trim();
        if (!v.isEmpty() && "=+-@".indexOf(v.charAt(0)) >= 0) {
            v = "'" + v;
        }
        return "\"" + v.replace("\"", "\"\"") + "\"";
    }

    static String csvRow(String... cells) {
        StringBuilder row = new StringBuilder();
        for (int i = 0; i < cells.length; i++) {
            if (i > 0) {
                row.append(',');
            }
            row.append(csvCell(cells[i]));
        }
        return row.append("\r\n").toString();
    }
}
