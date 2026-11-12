package app.tg.groups;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.os.Build;
import android.webkit.WebView;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.PrintWriter;
import java.io.RandomAccessFile;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * The app's one log. Everything the script prints, what the app's own side notes, and every error (one that
 * nothing caught with its whole trace) go to one file in the app's folder, so that it is still there after a
 * crash or a restart. «السجل» in the menu shows its newest part and saves it as a file to send. A line that
 * tells of an error starts with ✗.
 */
public final class AppLog {
    static final int MAX_BYTES = 3_000_000;          // past this the file is cut back to its newer half
    static final int SHOWN_CHARS = 24_000;         // on the screen, and what «نسخ» takes
    static final int SAVED_CHARS = 900_000;        // in the file saved to the phone's Downloads
    private static final Object LOCK = new Object();
    private static final SimpleDateFormat STAMP = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.US);
    private static final List<String> early = new ArrayList<>();
    private static File file;
    private static long size;
    static volatile String device = "";

    private AppLog() {
    }

    static void init(Context context) {
        synchronized (LOCK) {
            File wanted = new File(context.getFilesDir(), "app-log.txt");
            if (wanted.equals(file)) {
                return;
            }
            file = wanted;
            size = file.length();
            for (String lines : early) {
                append(lines);
            }
            early.clear();
        }
        device = describe(context);
    }

    /** What happened, a line or several. The script's side calls this too, for all that it prints. */
    public static void add(String text) {
        write(text, false);
    }

    /** An error: its first line starts with ✗. */
    public static void error(String text) {
        write(text, true);
    }

    static void error(String what, Throwable problem) {
        StringWriter trace = new StringWriter();
        problem.printStackTrace(new PrintWriter(trace));
        write(what + ": " + trace.toString().trim(), true);
    }

    private static void write(String text, boolean error) {
        if (text == null || text.isEmpty()) {
            return;
        }
        synchronized (LOCK) {
            String stamp = STAMP.format(new Date());
            StringBuilder out = new StringBuilder();
            boolean first = true;
            for (String line : text.split("\n")) {
                if (line.trim().isEmpty()) {
                    continue;
                }
                out.append(stamp).append(' ');
                if (error) {
                    out.append(first ? "✗ " : "  ");
                }
                out.append(line).append('\n');
                first = false;
            }
            if (out.length() == 0) {
                return;
            }
            if (file == null) {
                if (early.size() < 500) {
                    early.add(out.toString());
                }
                return;
            }
            append(out.toString());
        }
    }

    private static void append(String lines) {
        byte[] bytes = lines.getBytes(StandardCharsets.UTF_8);
        try (FileOutputStream stream = new FileOutputStream(file, true)) {
            stream.write(bytes);
            size += bytes.length;
        } catch (IOException e) {
            return;
        }
        if (size > MAX_BYTES) {
            trim();
        }
    }

    private static void trim() {
        try {
            byte[] kept = lastBytes(MAX_BYTES / 2);
            File temporary = new File(file.getPath() + ".tmp");
            try (FileOutputStream stream = new FileOutputStream(temporary)) {
                stream.write(kept);
            }
            if (temporary.renameTo(file)) {
                size = file.length();
            }
        } catch (IOException e) {
            // (the log stays as long as it is)
        }
    }

    /** The file's last bytes, from the start of a line. */
    private static byte[] lastBytes(int most) throws IOException {
        try (RandomAccessFile reader = new RandomAccessFile(file, "r")) {
            long length = reader.length();
            long from = Math.max(0, length - most);
            reader.seek(from);
            byte[] data = new byte[(int) (length - from)];
            reader.readFully(data);
            if (from == 0) {
                return data;
            }
            int start = 0;
            while (start < data.length && data[start] != '\n') {
                start++;
            }
            return Arrays.copyOfRange(data, Math.min(data.length, start + 1), data.length);
        }
    }

    /** The newest part of the log, at most this many characters, from the start of a line. */
    public static String tail(int maxChars) {
        synchronized (LOCK) {
            if (file == null || !file.isFile()) {
                return "";
            }
            try {
                String text = new String(lastBytes(Math.min(MAX_BYTES, maxChars * 3)), StandardCharsets.UTF_8);
                if (text.length() > maxChars) {
                    int cut = text.indexOf('\n', text.length() - maxChars);
                    text = cut < 0 ? text.substring(text.length() - maxChars) : text.substring(cut + 1);
                }
                return text;
            } catch (IOException e) {
                return "";
            }
        }
    }

    /** This app and this phone, in one line. */
    static String describe(Context context) {
        String webview = "؟";
        try {
            PackageInfo provider = WebView.getCurrentWebViewPackage();
            if (provider != null) {
                webview = provider.versionName;
            }
        } catch (Throwable e) {
            // (no WebView to ask)
        }
        String version = "؟";
        try {
            version = context.getPackageManager().getPackageInfo(context.getPackageName(), 0).versionName;
        } catch (Exception e) {
            // (stays unknown)
        }
        return "قروبات " + version + " · " + Build.MANUFACTURER + " " + Build.MODEL + " · أندرويد " + Build.VERSION.RELEASE
            + " · WebView " + webview;
    }

    /** What «السجل» shows: the app and the phone, the script's state, then the newest part of the log. */
    static String report(int maxChars) {
        StringBuilder out = new StringBuilder();
        out.append(device).append('\n');
        out.append("السكربت: ").append(Engine.phase);
        if (Engine.port > 0) {
            out.append(" (المنفذ ").append(Engine.port).append(')');
        }
        out.append("\n\n");
        String body = tail(maxChars);
        out.append(body.isEmpty() ? "لا شيء في السجل بعد.\n" : body);
        return out.toString();
    }
}
