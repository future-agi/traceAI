package ai.traceai.auto;

import java.net.URI;
import java.net.http.*;
import java.util.Base64;

/** Packaged as a standalone executable app.jar: only JDK dependencies, no tracing code. */
public final class JdkOnlyApp {
    public static void main(String[] args) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(args[0])).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString("{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}")).build();
        byte[] body = HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.ofByteArray()).body();
        System.out.println("BODY=" + Base64.getEncoder().encodeToString(body));
    }
}
