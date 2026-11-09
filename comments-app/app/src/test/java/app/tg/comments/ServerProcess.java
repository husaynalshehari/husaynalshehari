package app.tg.comments;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/** The real comments server (../comments_server.py) on this machine, set up as on the groups server (--setup, with
 * the key given to it) and pointed at the stand-ins in Fakes: the app's requests go through it as they do from a
 * phone. */
final class ServerProcess {
    static final File SCRIPT = new File("../comments_server.py").getAbsoluteFile();
    private static Fakes sharedFakes;
    private static ServerProcess shared;

    final File dir;
    final int port;
    final String link;
    final Process process;
    final StringBuffer log = new StringBuffer();

    /** The stand-ins and one server for many tests (started once; each test begins with the stand-ins as new). */
    static synchronized ServerProcess shared() throws Exception {
        quick();
        if (shared == null) {
            sharedFakes = new Fakes();
            shared = new ServerProcess(sharedFakes, null, null);
        }
        sharedFakes.reset();
        return shared;
    }

    static synchronized Fakes fakes() {
        return sharedFakes;
    }

    /** The app waits less in tests. */
    static void quick() {
        Engine.retryWaitsMs = new long[] {1, 1};
        Engine.connectTimeoutMs = 5_000;
        Engine.pingTimeoutMs = 10_000;
        Engine.tweetTimeoutMs = 20_000;
        Engine.commentTimeoutMs = 30_000;
    }

    /** A server of its own: another model, or another address for Claude (null: as the shared one). */
    ServerProcess(Fakes fakes, String model, String claudeUrl) throws Exception {
        dir = Files.createTempDirectory("comments-server").toFile();
        port = freePort();
        List<String> setup = new ArrayList<>(Arrays.asList("python3", SCRIPT.getPath(), "--setup", "--no-service",
            "--port", String.valueOf(port)));
        if (model != null) {
            setup.add("--model");
            setup.add(model);
        }
        Process made = builder(fakes, claudeUrl, setup).start();
        String printed = new String(readAll(made.getInputStream()), StandardCharsets.UTF_8);
        if (!made.waitFor(60, TimeUnit.SECONDS) || made.exitValue() != 0) {
            throw new IllegalStateException("the server's set-up failed:\n" + printed);
        }
        Matcher found = Pattern.compile("https://\\S+").matcher(printed);
        if (!found.find()) {
            throw new IllegalStateException("no link in the set-up's output:\n" + printed);
        }
        link = found.group();
        process = builder(fakes, claudeUrl, Arrays.asList("python3", SCRIPT.getPath())).start();
        Thread reading = new Thread(() -> {
            byte[] buffer = new byte[4096];
            try (InputStream in = process.getInputStream()) {
                for (int n = in.read(buffer); n > 0; n = in.read(buffer)) {
                    log.append(new String(buffer, 0, n, StandardCharsets.UTF_8));
                }
            } catch (IOException e) {
                // (it was stopped)
            }
        });
        reading.setDaemon(true);
        reading.start();
        Runtime.getRuntime().addShutdownHook(new Thread(this::stop));
        Engine.ServerLink parsed = Engine.parseLink(link);
        long end = System.currentTimeMillis() + 30_000;
        while (true) {
            try {
                Engine.ping(parsed);
                return;
            } catch (Engine.CommentError e) {
                if (System.currentTimeMillis() > end || !process.isAlive()) {
                    throw new IllegalStateException("the server did not start: " + e.getMessage() + "\n" + log);
                }
                Thread.sleep(100);
            }
        }
    }

    Engine.ServerLink parsed() {
        return Engine.parseLink(link);
    }

    void stop() {
        process.destroy();
        try {
            process.waitFor(5, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        try (Stream<java.nio.file.Path> files = Files.walk(dir.toPath())) {
            files.sorted(Comparator.reverseOrder()).forEach(path -> path.toFile().delete());
        } catch (IOException e) {
            // (left in the temporary folder)
        }
    }

    private ProcessBuilder builder(Fakes fakes, String claudeUrl, List<String> command) {
        ProcessBuilder builder = new ProcessBuilder(command).redirectErrorStream(true);
        Map<String, String> env = builder.environment();
        for (String name : new String[] {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"}) {
            env.remove(name);                    // (the stand-ins are on this machine)
        }
        String here = "http://127.0.0.1:" + fakes.port;
        env.put("no_proxy", "*");
        env.put("COMMENTS_SERVER_DIR", dir.getPath());
        env.put("COMMENTS_CLAUDE_KEY", Fakes.CLAUDE_KEY);
        env.put("COMMENTS_PUBLIC_HOST", "127.0.0.1");
        env.put("COMMENTS_BIND", "127.0.0.1");
        env.put("COMMENTS_CLAUDE_URL", claudeUrl != null ? claudeUrl : here + "/v1/messages");
        env.put("COMMENTS_FX_URL", here + "/status/");
        env.put("COMMENTS_PHOTO_PREFIX", here + "/media/");
        env.put("COMMENTS_BUSY_WAITS", "0.01,0.01,0.01,0.01");
        env.put("PYTHONDONTWRITEBYTECODE", "1");
        env.put("PYTHONUNBUFFERED", "1");
        return builder;
    }

    /** A port nothing listens on (taken, then let go at once). */
    static int freePort() throws IOException {
        try (ServerSocket probe = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            return probe.getLocalPort();
        }
    }

    private static byte[] readAll(InputStream in) throws IOException {
        try (InputStream stream = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            for (int n = stream.read(buffer); n > 0; n = stream.read(buffer)) {
                out.write(buffer, 0, n);
            }
            return out.toByteArray();
        }
    }
}
