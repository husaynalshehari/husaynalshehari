package app.tg.extract;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** Checks Text's rules on a computer (no Android needed): run by build.sh before the app is packed. */
public final class TextTest {
    private static int failures;

    public static void main(String[] args) {
        Set<String> gulf = new HashSet<>(Arrays.asList(Text.DEFAULT_CODES));
        Set<String> all = new HashSet<>(Arrays.asList(Text.CODES));
        Set<String> saudiOnly = new HashSet<>(Arrays.asList("sa"));

        // X's «Account based in», in English and in Arabic.
        same(Text.countryOf("Saudi Arabia", gulf), "sa");
        same(Text.countryOf("United Arab Emirates", gulf), "ae");
        same(Text.countryOf("Kuwait", gulf), "kw");
        same(Text.countryOf("Qatar", gulf), "qa");
        same(Text.countryOf("Oman", gulf), "om");
        same(Text.countryOf("المملكة العربية السعودية", gulf), "sa");
        same(Text.countryOf("الإمارات العربية المتحدة", gulf), "ae");
        same(Text.countryOf("الامارات", gulf), "ae");
        same(Text.countryOf("دولة الكويت", gulf), "kw");
        same(Text.countryOf("قطر", gulf), "qa");
        same(Text.countryOf("عُمان", gulf), "om");
        same(Text.countryOf("سلطنة عمان", gulf), "om");
        same(Text.countryOf("Dubai, UAE", gulf), "ae");
        same(Text.countryOf("  saudi   ARABIA ", gulf), "sa");
        // Not these.
        same(Text.countryOf("Romania", gulf), null);              // ("oman" inside a word)
        same(Text.countryOf("Germany", gulf), null);
        same(Text.countryOf("United States", gulf), null);
        same(Text.countryOf("", gulf), null);
        same(Text.countryOf(null, gulf), null);
        same(Text.countryOf("Bahrain", gulf), null);               // (not chosen by default)
        same(Text.countryOf("Bahrain", all), "bh");
        same(Text.countryOf("مملكة البحرين", all), "bh");
        same(Text.countryOf("Qatar", saudiOnly), null);           // (only the chosen countries)

        // What the user types as the start account.
        same(Text.cleanHandle("vextrand"), "vextrand");
        same(Text.cleanHandle("@vextrand "), "vextrand");
        same(Text.cleanHandle("https://x.com/vextrand"), "vextrand");
        same(Text.cleanHandle("https://x.com/vextrand/status/2122283252757000"), "vextrand");
        same(Text.cleanHandle("twitter.com/Some_Name?s=21"), "Some_Name");
        same(Text.cleanHandle("bad name"), null);
        same(Text.cleanHandle("averyveryverylongname"), null);
        same(Text.cleanHandle(""), null);

        // The search list as the user edits it.
        int[] bad = new int[1];
        same(String.join("|", Text.handlesIn("@a1\n b_2 \nhttps://x.com/C3\nnot-valid!\n@A1\n\n", bad)), "a1|b_2|C3");
        same(String.valueOf(bad[0]), "1");
        same(String.join("|", Text.handlesIn("one, two، three", null)), "one|two|three");
        same(String.valueOf(Text.handlesIn("", bad).size()), "0");

        // CSV cells.
        same(Text.csvCell("a \"quoted\" name"), "\"a \"\"quoted\"\" name\"");
        same(Text.csvCell("two\nlines"), "\"two lines\"");
        same(Text.csvCell("=HYPERLINK(1)"), "\"'=HYPERLINK(1)\"");
        same(Text.csvCell(null), "\"\"");
        same(Text.csvRow("a", "b"), "\"a\",\"b\"\r\n");

        if (failures > 0) {
            System.out.println(failures + " check(s) failed");
            System.exit(1);
        }
        System.out.println("TextTest: all checks passed");
    }

    private static void same(String got, String wanted) {
        if (got == null ? wanted != null : !got.equals(wanted)) {
            failures++;
            System.out.println("FAIL: got [" + got + "], wanted [" + wanted + "]");
        }
    }
}
