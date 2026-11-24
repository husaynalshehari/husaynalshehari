package app.tg.extract;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONException;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;

/**
 * What the extraction keeps on the phone, and nowhere else: the waiting list, the accounts already checked, the
 * accounts already extracted (never twice), and the results file.
 */
final class Store {
    static final int QUEUE_MAX = 5000;
    private static final String[] HEADER = {
        "اليوزر", "الاسم", "النبذة", "الموقع", "الدولة", "رابط الحساب", "صورة الحساب", "وُجد عبر", "وقت الاستخراج"};
    private static Store instance;

    private final File dir;
    private final ArrayList<String> queue = new ArrayList<>();
    private final HashSet<String> checked = new HashSet<>();
    private final HashSet<String> extracted = new HashSet<>();
    private int results;

    static synchronized Store get(Context context) {
        if (instance == null) {
            instance = new Store(context.getApplicationContext());
        }
        return instance;
    }

    private Store(Context app) {
        dir = app.getFilesDir();
        List<String> saved = readJsonList(file("queue.json"));
        if (saved == null) {
            saved = readJsonList(file("queue.json.bak"));     // (the list itself was lost or damaged)
        }
        if (saved != null) {
            queue.addAll(saved);
        }
        checked.addAll(readLines(file("checked.txt")));
        for (String name : readLines(file("extracted.txt"))) {
            extracted.add(name.toLowerCase(Locale.ROOT));
        }
        results = resultUsernames().size();
    }

    private File file(String name) {
        return new File(dir, name);
    }

    // ---------------------------------------------------------------- the waiting list

    /** A task starts from this account: it goes first in the list (the list of an earlier task stays after it). */
    synchronized void prepare(String seed) {
        for (int i = queue.size() - 1; i >= 0; i--) {
            if (queue.get(i).equalsIgnoreCase(seed)) {
                queue.remove(i);
            }
        }
        queue.add(0, seed);
        saveQueue();
    }

    synchronized String poll() {
        if (queue.isEmpty()) {
            return null;
        }
        String next = queue.remove(0);
        saveQueue();
        return next;
    }

    synchronized void pushFront(String name) {
        queue.add(0, name);
        saveQueue();
    }

    /** Added at the end of the list unless it is already there or the list is full. */
    synchronized boolean addToQueue(String name) {
        if (queue.size() >= QUEUE_MAX) {
            return false;
        }
        for (String waiting : queue) {
            if (waiting.equalsIgnoreCase(name)) {
                return false;
            }
        }
        queue.add(name);
        saveQueue();
        return true;
    }

    synchronized void saveQueue() {
        File list = file("queue.json");
        if (list.isFile() && list.length() > 2) {
            copy(list, file("queue.json.bak"));
        }
        writeAtomic(list, new JSONArray(queue).toString().getBytes(StandardCharsets.UTF_8));
    }

    synchronized int queueSize() {
        return queue.size();
    }

    // ---------------------------------------------------------------- checked and extracted accounts

    synchronized boolean isChecked(String name) {
        return checked.contains(name);
    }

    synchronized void addChecked(String name) {
        if (checked.add(name)) {
            append(file("checked.txt"), name + "\n");
        }
    }

    synchronized int checkedCount() {
        return checked.size();
    }

    synchronized boolean isExtracted(String name) {
        return extracted.contains(name.toLowerCase(Locale.ROOT));
    }

    synchronized int extractedCount() {
        return extracted.size();
    }

    // ---------------------------------------------------------------- results

    synchronized void addResult(String username, String name, String bio, String location, String country,
                                String image, String foundVia) {
        File csv = file("results.csv");
        StringBuilder out = new StringBuilder();
        if (!csv.isFile() || csv.length() == 0) {
            out.append('﻿').append(Text.csvRow(HEADER));       // (the mark lets Excel read the Arabic)
        }
        String time = new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).format(new Date());
        out.append(Text.csvRow(username, name, bio, location, country, "https://x.com/" + username, image, foundVia, time));
        append(csv, out.toString());
        results++;
        String key = username.toLowerCase(Locale.ROOT);
        if (extracted.add(key)) {
            append(file("extracted.txt"), key + "\n");
        }
    }

    synchronized int resultCount() {
        return results;
    }

    /** The usernames in the results file, in their order. */
    synchronized List<String> resultUsernames() {
        List<String> names = new ArrayList<>();
        for (String line : readLines(file("results.csv"))) {
            String row = line.startsWith("﻿") ? line.substring(1) : line;
            int end = row.startsWith("\"") ? row.indexOf('"', 1) : -1;
            if (end > 1) {
                String name = row.substring(1, end);
                if (name.matches("[A-Za-z0-9_]{1,15}")) {
                    names.add(name);
                }
            }
        }
        return names;
    }

    synchronized void copyResults(OutputStream out) throws IOException {
        try (InputStream in = new FileInputStream(file("results.csv"))) {
            byte[] buffer = new byte[16 * 1024];
            for (int n; (n = in.read(buffer)) > 0; ) {
                out.write(buffer, 0, n);
            }
        }
    }

    /** A copy of the results to share, under a name with the date (the shared folder keeps only this one). */
    synchronized File shareCopy() throws IOException {
        File folder = ResultsProvider.folder(dir);
        File[] old = folder.listFiles();
        if (old != null) {
            for (File f : old) {
                f.delete();
            }
        }
        File copy = new File(folder, "extract-" + new SimpleDateFormat("yyyyMMdd-HHmm", Locale.US).format(new Date()) + ".csv");
        try (OutputStream out = new FileOutputStream(copy)) {
            copyResults(out);
        }
        return copy;
    }

    /** The results go (and these accounts may be extracted again later). */
    synchronized void clearResults() {
        file("results.csv").delete();
        file("extracted.txt").delete();
        extracted.clear();
        results = 0;
    }

    /** The waiting list and the checked accounts go: the next task searches from the start. */
    synchronized void clearSearch() {
        queue.clear();
        checked.clear();
        file("queue.json").delete();
        file("queue.json.bak").delete();
        file("checked.txt").delete();
    }

    // ---------------------------------------------------------------- files

    private static List<String> readJsonList(File f) {
        if (!f.isFile()) {
            return null;
        }
        try {
            JSONArray a = new JSONArray(new String(readAll(f), StandardCharsets.UTF_8));
            List<String> out = new ArrayList<>();
            for (int i = 0; i < a.length(); i++) {
                String s = a.optString(i, "").trim();
                if (!s.isEmpty()) {
                    out.add(s);
                }
            }
            return out;
        } catch (IOException | JSONException e) {
            return null;
        }
    }

    private static List<String> readLines(File f) {
        List<String> out = new ArrayList<>();
        if (!f.isFile()) {
            return out;
        }
        try (BufferedReader in = new BufferedReader(new InputStreamReader(new FileInputStream(f), StandardCharsets.UTF_8))) {
            for (String line; (line = in.readLine()) != null; ) {
                if (!line.trim().isEmpty()) {
                    out.add(line.trim());
                }
            }
        } catch (IOException ignore) {
            // (what was read stays)
        }
        return out;
    }

    private static byte[] readAll(File f) throws IOException {
        try (InputStream in = new FileInputStream(f)) {
            byte[] data = new byte[(int) f.length()];
            int at = 0;
            for (int n; at < data.length && (n = in.read(data, at, data.length - at)) > 0; ) {
                at += n;
            }
            return data;
        }
    }

    private static void append(File f, String text) {
        try (FileOutputStream out = new FileOutputStream(f, true)) {
            out.write(text.getBytes(StandardCharsets.UTF_8));
            out.getFD().sync();
        } catch (IOException e) {
            Engine.log("⚠️ تعذّر الحفظ في " + f.getName() + ": " + e.getMessage());
        }
    }

    /** Written beside, then put in place: a cut in the middle never leaves half a file. */
    private static void writeAtomic(File f, byte[] data) {
        File tmp = new File(f.getPath() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(data);
            out.getFD().sync();
        } catch (IOException e) {
            Engine.log("⚠️ تعذّر حفظ " + f.getName() + ": " + e.getMessage());
            return;
        }
        if (!tmp.renameTo(f)) {
            Engine.log("⚠️ تعذّر حفظ " + f.getName());
        }
    }

    private static void copy(File from, File to) {
        try {
            writeAtomic(to, readAll(from));
        } catch (IOException ignore) {
            // (only the backup copy)
        }
    }
}
