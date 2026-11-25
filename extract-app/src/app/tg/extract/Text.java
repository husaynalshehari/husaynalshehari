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
