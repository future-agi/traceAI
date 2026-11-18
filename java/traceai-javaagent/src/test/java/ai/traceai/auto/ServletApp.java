package ai.traceai.auto;

import java.io.IOException;
import java.lang.reflect.Proxy;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;

/**
 * A servlet application for IncomingRequestsE2ETest, run in a child JVM with the agent attached.
 * Arguments: container ({@code tomcat} or {@code javax}), the mock provider's chat endpoint, then one
 * incoming request per argument, {@code path|traceparent|tracestate}, with {@code -} for an absent
 * header. Paths: {@code /call} makes one AI call; {@code /throw} makes it and then throws; {@code /async}
 * makes it and then completes the request from another thread; {@code /outside} (javax only) makes
 * the AI call on the same thread outside any request.
 */
public final class ServletApp {
    static final HttpClient CLIENT = HttpClient.newHttpClient();
    static String endpoint;

    public static void main(String[] args) throws Exception {
        endpoint = args[1];
        List<String[]> requests = new ArrayList<>();
        for (int i = 2; i < args.length; i++) requests.add(args[i].split("\\|", -1));
        if (args[0].equals("tomcat")) tomcat(requests); else javax(requests);
        System.out.println("DONE");
    }

    /** One chat call through the JDK HttpClient. The test orders exported spans by start time. */
    static void callProvider(String label) throws IOException, InterruptedException {
        HttpResponse<String> response = CLIENT.send(HttpRequest.newBuilder(URI.create(endpoint)).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build(), HttpResponse.BodyHandlers.ofString());
        System.out.println("CALL " + label + " THREAD=" + Thread.currentThread().getName() + " STATUS=" + response.statusCode());
    }

    static final class JakartaServlet extends jakarta.servlet.http.HttpServlet {
        @Override protected void doGet(jakarta.servlet.http.HttpServletRequest request, jakarta.servlet.http.HttpServletResponse response) throws IOException {
            try { callProvider(request.getParameter("label")); } catch (InterruptedException error) { throw new IOException(error); }
            switch (request.getRequestURI()) {
                case "/throw" -> throw new IllegalStateException("handler fails after its AI call");
                case "/async" -> {
                    jakarta.servlet.AsyncContext async = request.startAsync();
                    Thread completion = new Thread(() -> {
                        try { Thread.sleep(50); async.getResponse().getWriter().write("async"); } catch (Exception ignored) { }
                        async.complete();
                    }, "async-completion");
                    completion.start();
                }
                default -> response.getWriter().write("ok");
            }
        }
    }

    /** An embedded Tomcat with a single request thread, so every request runs on the same pooled thread. */
    static void tomcat(List<String[]> requests) throws Exception {
        Path base = Files.createTempDirectory(Path.of("target"), "tomcat-");
        org.apache.catalina.startup.Tomcat tomcat = new org.apache.catalina.startup.Tomcat();
        tomcat.setBaseDir(base.toAbsolutePath().toString());
        org.apache.catalina.connector.Connector connector = new org.apache.catalina.connector.Connector("HTTP/1.1");
        connector.setPort(0);
        connector.setProperty("address", "127.0.0.1");
        connector.setProperty("maxThreads", "1");
        connector.setProperty("minSpareThreads", "1");
        tomcat.getService().addConnector(connector);
        tomcat.setConnector(connector);
        org.apache.catalina.Context context = tomcat.addContext("", base.toAbsolutePath().toString());
        org.apache.catalina.Wrapper wrapper = org.apache.catalina.startup.Tomcat.addServlet(context, "app", new JakartaServlet());
        wrapper.setAsyncSupported(true);
        context.addServletMappingDecoded("/*", "app");
        tomcat.start();
        try {
            int port = connector.getLocalPort();
            for (int i = 0; i < requests.size(); i++) {
                String[] request = requests.get(i);
                HttpURLConnection connection = (HttpURLConnection)URI.create("http://127.0.0.1:" + port + request[0] + "?label=" + i).toURL().openConnection();
                if (!request[1].equals("-")) connection.setRequestProperty("traceparent", request[1]);
                if (!request[2].equals("-")) connection.setRequestProperty("tracestate", request[2]);
                int status = connection.getResponseCode();
                try (var in = status >= 400 ? connection.getErrorStream() : connection.getInputStream()) { if (in != null) in.readAllBytes(); }
                System.out.println("RESPONSE " + i + " STATUS=" + status);
            }
        } finally { tomcat.stop(); tomcat.destroy(); }
    }

    /** javax.servlet's HttpServlet#service called directly, with JDK-proxy request and response objects. */
    static void javax(List<String[]> requests) throws Exception {
        javax.servlet.http.HttpServlet servlet = new javax.servlet.http.HttpServlet() {
            @Override protected void doGet(javax.servlet.http.HttpServletRequest request, javax.servlet.http.HttpServletResponse response) throws IOException {
                try { callProvider(request.getParameter("label")); } catch (InterruptedException error) { throw new IOException(error); }
                if (request.getRequestURI().equals("/throw")) throw new IllegalStateException("handler fails after its AI call");
            }
        };
        for (int i = 0; i < requests.size(); i++) {
            String[] request = requests.get(i); String label = String.valueOf(i);
            if (request[0].equals("/outside")) { callProvider(label); continue; }
            Map<String, String> headers = new HashMap<>();
            if (!request[1].equals("-")) headers.put("traceparent", request[1]);
            if (!request[2].equals("-")) headers.put("tracestate", request[2]);
            Object incoming = proxy(javax.servlet.http.HttpServletRequest.class, (method, arguments) -> switch (method) {
                case "getMethod" -> "GET";
                case "getProtocol" -> "HTTP/1.1";
                case "getRequestURI" -> request[0];
                case "getParameter" -> "label".equals(arguments[0]) ? label : null;
                case "getHeader" -> headers.get(String.valueOf(arguments[0]).toLowerCase(Locale.ROOT));
                case "getHeaders" -> Collections.enumeration(headers.containsKey(String.valueOf(arguments[0]).toLowerCase(Locale.ROOT))
                    ? List.of(headers.get(String.valueOf(arguments[0]).toLowerCase(Locale.ROOT))) : List.of());
                case "getHeaderNames" -> Collections.enumeration(headers.keySet());
                case "getDateHeader" -> -1L;
                default -> null;
            });
            Object outgoing = proxy(javax.servlet.http.HttpServletResponse.class, (method, arguments) -> null);
            try { servlet.service((javax.servlet.ServletRequest)incoming, (javax.servlet.ServletResponse)outgoing); System.out.println("RESPONSE " + i + " OK"); }
            catch (IllegalStateException error) { System.out.println("RESPONSE " + i + " THREW"); }
        }
    }
    interface Answer { Object answer(String method, Object[] arguments); }
    static Object proxy(Class<?> api, Answer answer) {
        return Proxy.newProxyInstance(api.getClassLoader(), new Class<?>[] {api}, (self, method, arguments) -> {
            switch (method.getName()) {
                case "hashCode": return System.identityHashCode(self);
                case "equals": return self == arguments[0];
                case "toString": return api.getSimpleName() + "@mock";
                default:
            }
            Object value = answer.answer(method.getName(), arguments);
            if (value != null || !method.getReturnType().isPrimitive()) return value;
            Class<?> type = method.getReturnType();
            return type == boolean.class ? false : type == long.class ? 0L : type == int.class ? 0 : type == void.class ? null : 0;
        });
    }
    private ServletApp() {}
}
