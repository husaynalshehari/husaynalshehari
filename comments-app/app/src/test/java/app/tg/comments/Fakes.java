package app.tg.comments;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Stand-ins on this machine for FxTwitter, the tweets' photos and Claude, which the comments server is pointed at
 * (see ServerProcess): no real service is asked. */
final class Fakes {
    static final String CLAUDE_KEY = "sk-ant-api03-" + "Rq9w".repeat(20) + "Zx_w";
    static final byte[] JPEG = {(byte) 0xFF, (byte) 0xD8, (byte) 0xFF, (byte) 0xE0, 1, 2, 3, 4, 5, 6, (byte) 0xFF, (byte) 0xD9};

    /** One request Claude got: its headers and body. */
    static final class Asked {
        final Map<String, String> headers;
        final JSONObject body;

        Asked(Map<String, String> headers, JSONObject body) {
            this.headers = headers;
            this.body = body;
        }

        String text() {
            JSONArray content = body.optJSONArray("messages").optJSONObject(0).optJSONArray("content");
            return content.optJSONObject(content.length() - 1).optString("text");
        }

        int images() {
            JSONArray content = body.optJSONArray("messages").optJSONObject(0).optJSONArray("content");
            int n = 0;
            for (int i = 0; i < content.length(); i++) {
                if ("image".equals(content.optJSONObject(i).optString("type"))) {
                    n++;
                }
            }
            return n;
        }
    }

    final ServerSocket server;
    final int port;
    final List<Asked> asked = Collections.synchronizedList(new ArrayList<>());
    final List<String> photoPaths = Collections.synchronizedList(new ArrayList<>());
    final Deque<Object[]> answers = new ArrayDeque<>();      // {status, body} given before the default answer
    static final String REPLY = "والله الزحمةُ صارت جزء من يومنا، وما لها حل قريب أبداً";
    volatile String reply = REPLY;

    Fakes() throws IOException {
        server = new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"));
        port = server.getLocalPort();
        Thread accepting = new Thread(() -> {
            while (!server.isClosed()) {
                try {
                    Socket socket = server.accept();
                    new Thread(() -> serve(socket)).start();
                } catch (IOException e) {
                    return;
                }
            }
        });
        accepting.setDaemon(true);
        accepting.start();
    }

    /** As new, for the next test (the same stand-ins serve one server for many tests). */
    synchronized void reset() {
        asked.clear();
        photoPaths.clear();
        answers.clear();
        reply = REPLY;
    }

    void stop() {
        try {
            server.close();
        } catch (IOException e) {
            // (already closed)
        }
    }

    /** One request (HTTP/1.1, answered and closed): its line, headers and body, then the answer. */
    static final class Exchange {
        String method;
        String target;
        final Map<String, String> headers = new HashMap<>();
        byte[] body = new byte[0];
        int status = 200;
        String type = "application/json";
        byte[] answer = new byte[0];

        String path() {
            int q = target.indexOf('?');
            return q < 0 ? target : target.substring(0, q);
        }
    }

    static String line(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int b;
        while ((b = in.read()) >= 0 && b != '\n') {
            if (b != '\r') {
                out.write(b);
            }
        }
        return out.toString(StandardCharsets.UTF_8.name());
    }

    private void serve(Socket socket) {
        try (Socket s = socket) {
            InputStream in = s.getInputStream();
            Exchange exchange = new Exchange();
            String[] first = line(in).split(" ");
            exchange.method = first[0];
            exchange.target = first.length > 1 ? first[1] : "/";
            for (String header = line(in); !header.isEmpty(); header = line(in)) {
                int colon = header.indexOf(':');
                if (colon > 0) {
                    exchange.headers.put(header.substring(0, colon).trim().toLowerCase(), header.substring(colon + 1).trim());
                }
            }
            int length = Integer.parseInt(exchange.headers.getOrDefault("content-length", "0"));
            exchange.body = new byte[length];
            for (int read = 0; read < length; ) {
                int n = in.read(exchange.body, read, length - read);
                if (n < 0) {
                    break;
                }
                read += n;
            }
            String path = exchange.path();
            if (path.startsWith("/status/")) {
                fxTwitter(exchange);
            } else if (path.startsWith("/media/")) {
                photo(exchange);
            } else if (path.equals("/v1/messages")) {
                claude(exchange);
            } else {
                exchange.status = 404;
            }
            OutputStream out = s.getOutputStream();
            out.write(("HTTP/1.1 " + exchange.status + " X\r\nContent-Type: " + exchange.type + "\r\nContent-Length: "
                + exchange.answer.length + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.UTF_8));
            out.write(exchange.answer);
            out.flush();
        } catch (IOException e) {
            // (the client went away)
        }
    }

    synchronized void answer(int status, String body) {
        answers.add(new Object[] {status, body});
    }

    static String claudeText(String text) throws Exception {
        return new JSONObject().put("content", new JSONArray()
                .put(new JSONObject().put("type", "thinking").put("thinking", "…"))
                .put(new JSONObject().put("type", "text").put("text", text)))
            .put("stop_reason", "end_turn").toString();
    }

    static String claudeError(String type, String message) throws Exception {
        return new JSONObject().put("type", "error")
            .put("error", new JSONObject().put("type", type).put("message", message)).toString();
    }

    private void fxTwitter(Exchange exchange) {
        String id = exchange.path().substring("/status/".length());
        String media = "http://127.0.0.1:" + port + "/media/";
        try {
            JSONObject tweet;
            switch (id) {
                case "1900000000000000001":
                    tweet = new JSONObject().put("url", "https://x.com/someone/status/1900000000000000001")
                        .put("text", "الزحمة اليوم في طريق الملك فهد ما لها حل")
                        .put("author", new JSONObject().put("screen_name", "someone"))
                        .put("media", new JSONObject().put("all", new JSONArray()
                            .put(new JSONObject().put("type", "photo").put("url", media + "a.jpg?name=orig"))
                            .put(new JSONObject().put("type", "photo").put("url", "https://elsewhere.example/b.jpg"))));
                    break;
                case "1900000000000000002":
                    tweet = new JSONObject().put("url", "https://x.com/other/status/1900000000000000002")
                        .put("text", "شوفوا هذا")
                        .put("quote", new JSONObject().put("text", "الأسعار ارتفعت هذا الشهر"))
                        .put("author", new JSONObject().put("screen_name", "other"))
                        .put("media", new JSONObject().put("all", new JSONArray()
                            .put(new JSONObject().put("type", "video").put("thumbnail_url", media + "v.jpg"))));
                    break;
                case "1900000000000000003":
                    send(exchange, 200, new JSONObject().put("code", 404).put("message", "NOT_FOUND").toString(), "application/json");
                    return;
                case "1900000000000000429":
                    send(exchange, 429, "{}", "application/json");
                    return;
                default:
                    send(exchange, 404, "{}", "application/json");
                    return;
            }
            send(exchange, 200, new JSONObject().put("code", 200).put("tweet", tweet).toString(), "application/json");
        } catch (Exception e) {
            send(exchange, 500, "{}", "application/json");
        }
    }

    private void photo(Exchange exchange) {
        photoPaths.add(exchange.target);
        exchange.type = "image/jpeg";
        exchange.answer = JPEG;
    }

    private void claude(Exchange exchange) {
        try {
            asked.add(new Asked(exchange.headers, new JSONObject(new String(exchange.body, StandardCharsets.UTF_8))));
            Object[] next;
            synchronized (this) {
                next = answers.poll();
            }
            if (next != null) {
                send(exchange, (Integer) next[0], (String) next[1], "application/json");
            } else {
                send(exchange, 200, claudeText(reply), "application/json");
            }
        } catch (Exception e) {
            send(exchange, 500, "{}", "application/json");
        }
    }

    private static void send(Exchange exchange, int status, String body, String type) {
        exchange.status = status;
        exchange.type = type;
        exchange.answer = body.getBytes(StandardCharsets.UTF_8);
    }
}
