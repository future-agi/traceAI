import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

public class Bench {
    static String env(String key) { return System.getenv(key); }
    public static void main(String[] args) throws Exception {
        System.out.println("{\"kind\":\"ready\"}");
        System.out.flush();
        if ("1".equals(env("BENCH_STARTUP_ONLY"))) return;
        var client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
        int n = Integer.parseInt(env("BENCH_N")), warmup = Integer.parseInt(env("BENCH_WARMUP"));
        for (boolean stream : new boolean[]{false, true}) {
            for (int i = 0; i < n + warmup; i++) {
                String id = env("BENCH_PAIR") + "-" + (stream ? 1 : 0) + "-" + i;
                String request = "{\"model\":\"gpt-4o-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"" + id + "\"}],\"stream\":" + stream + ",\"stream_options\":{\"include_usage\":true}}";
                var req = HttpRequest.newBuilder(URI.create(env("BENCH_PROVIDER") + "/v1/chat/completions"))
                    .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(request)).build();
                long start = System.nanoTime(), first = 0;
                var response = client.send(req, HttpResponse.BodyHandlers.ofInputStream());
                if (response.statusCode() != 200) throw new IOException("HTTP " + response.statusCode());
                var bytes = new ByteArrayOutputStream();
                try (var input = response.body()) {
                    byte[] buffer = new byte[4096]; int count;
                    while ((count = input.read(buffer)) != -1) {
                        if (count > 0 && first == 0) first = System.nanoTime();
                        bytes.write(buffer, 0, count);
                    }
                }
                long end = System.nanoTime();
                System.out.println("{\"kind\":\"call\",\"id\":\"" + id + "\",\"stream\":" + stream + ",\"warmup\":" + (i < warmup) + ",\"latency_us\":" + (end-start)/1000.0 + ",\"ttfc_us\":" + (first-start)/1000.0 + ",\"body_b64\":\"" + Base64.getEncoder().encodeToString(bytes.toByteArray()) + "\"}");
            }
        }
        client.send(HttpRequest.newBuilder(URI.create(env("BENCH_PROVIDER") + "/health")).GET().build(), HttpResponse.BodyHandlers.discarding());
        client.send(HttpRequest.newBuilder(URI.create(env("BENCH_PROVIDER") + "/v1/chat/completions")).header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString("{\"not_ai\":true}")).build(), HttpResponse.BodyHandlers.discarding());
        System.out.println("{\"kind\":\"done\"}");
        Thread.sleep((long)(Double.parseDouble(env("BENCH_SETTLE_SECONDS")) * 1000));
    }
}
