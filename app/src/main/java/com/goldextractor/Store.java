package com.goldextractor;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * Local storage on the phone (same files as the Python script):
 * queue.json, checked_users.json, extracted_users.json, plus results.json with the saved accounts.
 */
public class Store {

    private static final String QUEUE = "queue.json";
    private static final String CHECKED = "checked_users.json";
    private static final String EXTRACTED = "extracted_users.json";
    private static final String RESULTS = "results.json";

    private final File dir;
    private final Set<String> checked = new HashSet<>();
    private final Set<String> extracted = new HashSet<>();
    private JSONArray results = new JSONArray();

    public Store(File dir) {
        this.dir = dir;
        for (String s : readStringArray(new File(dir, CHECKED))) checked.add(key(s));
        for (String s : readStringArray(new File(dir, EXTRACTED))) extracted.add(key(s));
        JSONArray r = readArray(new File(dir, RESULTS));
        if (r != null) results = r;
    }

    private static String key(String username) {
        return username.trim().toLowerCase(Locale.ROOT);
    }

    // ---------------------------------------------------------------- IO helpers

    public static String readStream(InputStream in) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) != -1) bos.write(buf, 0, n);
        String s = new String(bos.toByteArray(), StandardCharsets.UTF_8);
        if (s.startsWith("﻿")) s = s.substring(1);
        return s;
    }

    private static String readFile(File f) throws IOException {
        try (FileInputStream in = new FileInputStream(f)) {
            return readStream(in);
        }
    }

    private static JSONArray readArray(File f) {
        if (!f.exists()) return null;
        try {
            return new JSONArray(readFile(f));
        } catch (Exception e) {
            return null;
        }
    }

    private static List<String> readStringArray(File f) {
        List<String> out = new ArrayList<>();
        JSONArray a = readArray(f);
        if (a == null) return out;
        for (int i = 0; i < a.length(); i++) {
            String s = a.optString(i, "").trim();
            if (!s.isEmpty()) out.add(s);
        }
        return out;
    }

    /** Write to a temp file, then rename over the target, so a crash never leaves a half-written file. */
    private void writeAtomic(String name, String content) {
        File target = new File(dir, name);
        File tmp = new File(dir, name + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(content.getBytes(StandardCharsets.UTF_8));
            out.getFD().sync();
        } catch (IOException e) {
            return;
        }
        if (!tmp.renameTo(target)) {
            target.delete();
            tmp.renameTo(target);
        }
    }

    private static void copyFile(File from, File to) {
        try (FileInputStream in = new FileInputStream(from); FileOutputStream out = new FileOutputStream(to)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
        } catch (IOException ignored) {
        }
    }

    // ---------------------------------------------------------------- queue

    public synchronized List<String> loadQueue() {
        for (String name : new String[]{QUEUE, QUEUE + ".bak"}) {
            List<String> q = readStringArray(new File(dir, name));
            if (!q.isEmpty()) return q;
        }
        return new ArrayList<>();
    }

    public synchronized void saveQueue(List<String> queue) {
        File f = new File(dir, QUEUE);
        // never overwrite a queue that has data with an empty list
        if (queue.isEmpty() && f.exists() && !readStringArray(f).isEmpty()) return;
        if (f.exists()) copyFile(f, new File(dir, QUEUE + ".bak"));
        writeAtomic(QUEUE, new JSONArray(queue).toString());
    }

    public synchronized void clearQueue() {
        new File(dir, QUEUE).delete();
        new File(dir, QUEUE + ".bak").delete();
    }

    // ---------------------------------------------------------------- checked users

    public synchronized boolean isChecked(String username) {
        return checked.contains(key(username));
    }

    public synchronized void addChecked(String username) {
        if (checked.add(key(username))) {
            writeAtomic(CHECKED, new JSONArray(checked).toString());
        }
    }

    public synchronized int checkedCount() {
        return checked.size();
    }

    public synchronized void clearChecked() {
        checked.clear();
        new File(dir, CHECKED).delete();
    }

    // ---------------------------------------------------------------- extracted (no duplicates, ever)

    public synchronized boolean isExtracted(String username) {
        return extracted.contains(key(username));
    }

    /** Saves the account; returns false if it was already extracted before. */
    public synchronized boolean addExtracted(JSONObject record) {
        String k = key(record.optString("username", ""));
        if (k.isEmpty() || !extracted.add(k)) return false;
        results.put(record);
        writeAtomic(EXTRACTED, new JSONArray(extracted).toString());
        writeAtomic(RESULTS, results.toString());
        return true;
    }

    public synchronized int extractedCount() {
        return extracted.size();
    }

    public synchronized int resultsCount() {
        return results.length();
    }

    /** CSV with a UTF-8 BOM so Excel shows Arabic correctly. */
    public synchronized String resultsCsv() {
        StringBuilder sb = new StringBuilder("﻿");
        sb.append("username,name,bio,location,profile_image,link,extracted_at\n");
        for (int i = 0; i < results.length(); i++) {
            JSONObject r = results.optJSONObject(i);
            if (r == null) continue;
            String u = r.optString("username", "");
            sb.append(csv(u)).append(',')
                    .append(csv(r.optString("name", ""))).append(',')
                    .append(csv(r.optString("bio", ""))).append(',')
                    .append(csv(r.optString("location", ""))).append(',')
                    .append(csv(r.optString("profile_image", ""))).append(',')
                    .append(csv("https://x.com/" + u)).append(',')
                    .append(csv(r.optString("extracted_at", ""))).append('\n');
        }
        return sb.toString();
    }

    private static String csv(String s) {
        if (s == null) s = "";
        s = s.replace("\r", " ").replace("\n", " ");
        return "\"" + s.replace("\"", "\"\"") + "\"";
    }

    // ---------------------------------------------------------------- import

    /** Reads usernames from a JSON array (strings or objects with "username") or plain text. */
    public static List<String> parseUsernames(String text) {
        List<String> out = new ArrayList<>();
        String t = text == null ? "" : text.trim();
        try {
            JSONArray a = new JSONArray(t);
            for (int i = 0; i < a.length(); i++) {
                Object o = a.opt(i);
                String s = null;
                if (o instanceof String) {
                    s = (String) o;
                } else if (o instanceof JSONObject) {
                    JSONObject j = (JSONObject) o;
                    s = j.optString("username", j.optString("twitter_username", ""));
                }
                addCleanName(out, s);
            }
        } catch (JSONException e) {
            for (String line : t.split("[\\r\\n,;]+")) addCleanName(out, line);
        }
        return out;
    }

    private static void addCleanName(List<String> out, String s) {
        if (s == null) return;
        s = s.trim();
        if (s.startsWith("@")) s = s.substring(1);
        if (s.matches("[A-Za-z0-9_]{1,30}")) out.add(s);
    }

    /** Merges usernames into "queue", "checked" or "extracted". Returns how many were new. */
    public synchronized int importUsernames(String target, List<String> names) {
        int added = 0;
        if ("queue".equals(target)) {
            List<String> q = loadQueue();
            Set<String> seen = new HashSet<>();
            for (String s : q) seen.add(key(s));
            for (String n : names) {
                if (seen.add(key(n))) {
                    q.add(n);
                    added++;
                }
            }
            if (added > 0) saveQueue(q);
        } else if ("checked".equals(target)) {
            for (String n : names) if (checked.add(key(n))) added++;
            if (added > 0) writeAtomic(CHECKED, new JSONArray(checked).toString());
        } else if ("extracted".equals(target)) {
            for (String n : names) if (extracted.add(key(n))) added++;
            if (added > 0) writeAtomic(EXTRACTED, new JSONArray(extracted).toString());
        }
        return added;
    }
}
