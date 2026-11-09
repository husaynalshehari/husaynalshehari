package app.tg.groups;

import android.content.Context;
import android.content.SharedPreferences;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;

/**
 * The groups script inside the app. Python is started once for the app's process, and the script (gg.py, with
 * content.py beside it) runs in a thread of its own from the app's private folder, where it keeps its data as it
 * does on a server. Its page is served on this phone only (127.0.0.1), at an address made up for this install, so
 * that no other app on the phone can open it.
 */
final class Engine {
    static final int PREFERRED_PORT = 5177;
    static final long SCRIPT_STACK_BYTES = 16L * 1024 * 1024;
    static volatile String phase = "idle";          // idle, starting, running, failed
    static volatile String error = "";
    static volatile int port;
    static volatile String path = "";
    static volatile File folder;

    private Engine() {
    }

    static synchronized void start(Context context) {
        if (!phase.equals("idle")) {
            return;
        }
        phase = "starting";
        Context app = context.getApplicationContext();
        // (A deep stack: Python reads the whole script in this thread.)
        new Thread(null, () -> run(app), "gg-script", SCRIPT_STACK_BYTES).start();
    }

    private static void run(Context app) {
        try {
            File home = new File(app.getFilesDir(), "gg");
            unpack(app, home);
            folder = home;
            SharedPreferences prefs = app.getSharedPreferences("engine", Context.MODE_PRIVATE);
            String saved = prefs.getString("path", "");
            if (!saved.matches("[a-z0-9]{16}")) {
                String letters = "abcdefghijklmnopqrstuvwxyz0123456789";
                SecureRandom random = new SecureRandom();
                StringBuilder made = new StringBuilder();
                for (int i = 0; i < 16; i++) {
                    made.append(letters.charAt(random.nextInt(letters.length())));
                }
                saved = made.toString();
                prefs.edit().putString("path", saved).apply();
            }
            path = saved;
            port = freePort();
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(app));
            }
            phase = "running";
            Python.getInstance().getModule("ggapp").callAttr("main", home.getAbsolutePath(), port, path);
            // The script is meant to run until the app ends: it stopped.
            error = call("state");
            phase = "failed";
        } catch (Throwable e) {
            error = e.getClass().getSimpleName() + ": " + e.getMessage();
            phase = "failed";
        }
    }

    /** The script's files as they came with this version of the app, put in the app's folder when the app is new or
     * was updated. Files taken since by «تحديث السكربت» stay until an update of the app brings other ones. */
    static void unpack(Context app, File home) throws Exception {
        if (!home.isDirectory() && !home.mkdirs()) {
            throw new IOException("cannot make " + home);
        }
        byte[] script = read(app.getAssets().open("gg/gg.py"));
        byte[] content = read(app.getAssets().open("gg/content.py"));
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        digest.update(script);
        digest.update(content);
        StringBuilder mark = new StringBuilder();
        for (byte b : digest.digest()) {
            mark.append(Character.forDigit((b >> 4) & 0xF, 16)).append(Character.forDigit(b & 0xF, 16));
        }
        File marker = new File(home, ".bundled");
        String had = marker.isFile() ? new String(read(new FileInputStream(marker)), StandardCharsets.UTF_8).trim() : "";
        File scriptFile = new File(home, "gg.py");
        File contentFile = new File(home, "content.py");
        if (!mark.toString().equals(had) || !scriptFile.isFile() || !contentFile.isFile()) {
            write(scriptFile, script);
            write(contentFile, content);
            write(marker, mark.toString().getBytes(StandardCharsets.UTF_8));
        }
    }

    private static byte[] read(InputStream in) throws IOException {
        try (InputStream stream = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[65536];
            for (int n = stream.read(buffer); n > 0; n = stream.read(buffer)) {
                out.write(buffer, 0, n);
            }
            return out.toByteArray();
        }
    }

    private static void write(File file, byte[] data) throws IOException {
        File temporary = new File(file.getPath() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(temporary)) {
            out.write(data);
        }
        if (!temporary.renameTo(file)) {
            throw new IOException("cannot write " + file);
        }
    }

    /** The same port every time when it is free (the page keeps what it remembers in the browser), else any. */
    private static int freePort() throws IOException {
        InetAddress local = InetAddress.getByName("127.0.0.1");
        try (ServerSocket probe = new ServerSocket(PREFERRED_PORT, 1, local)) {
            return probe.getLocalPort();
        } catch (IOException taken) {
            try (ServerSocket probe = new ServerSocket(0, 1, local)) {
                return probe.getLocalPort();
            }
        }
    }

    static String url() {
        return "http://127.0.0.1:" + port + "/" + path;
    }

    /** Whether the script's page answers yet. */
    static boolean answers() {
        if (port <= 0 || !phase.equals("running")) {
            return false;
        }
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http://127.0.0.1:" + port + "/healthz").openConnection();
            connection.setConnectTimeout(1500);
            connection.setReadTimeout(3000);
            connection.setUseCaches(false);
            return connection.getResponseCode() == 200;
        } catch (IOException | RuntimeException e) {
            return false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    /** A function of ggapp.py, by name: its answer as text. */
    static String call(String function, Object... arguments) {
        try {
            PyObject answer = Python.getInstance().getModule("ggapp").callAttr(function, arguments);
            return answer == null ? "" : answer.toString();
        } catch (Throwable e) {
            return e.getClass().getSimpleName() + ": " + e.getMessage();
        }
    }
}
