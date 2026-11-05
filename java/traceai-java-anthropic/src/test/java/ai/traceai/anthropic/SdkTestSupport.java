package ai.traceai.anthropic;

import com.anthropic.client.okhttp.AnthropicOkHttpClient;

/** Only bridges SDK package moves; tests still exercise the real SDK and HTTP transport. */
final class SdkTestSupport {
    private SdkTestSupport() {}

    static Object paramsBuilder(String input) throws Exception {
        Object builder = modelClass("MessageCreateParams").getMethod("builder").invoke(null);
        call(builder, "model", String.class, "test-model");
        call(builder, "maxTokens", long.class, 64L);
        call(builder, "addUserMessage", String.class, input);
        return builder;
    }

    static Class<?> modelClass(String name) throws ClassNotFoundException {
        try {
            return Class.forName("com.anthropic.models.messages." + name);
        } catch (ClassNotFoundException legacyPackage) {
            return Class.forName("com.anthropic.models." + name);
        }
    }

    static Object call(Object target, String method) throws Exception {
        return target.getClass().getMethod(method).invoke(target);
    }

    static Object call(Object target, String method, Class<?> type, Object value) throws Exception {
        return target.getClass().getMethod(method, type).invoke(target, value);
    }

    static void setTemperature(Object builder, double value) throws Exception {
        try {
            call(builder, "temperature", double.class, value);
        } catch (NoSuchMethodException removedInSdk3) {
            org.junit.jupiter.api.Assumptions.abort("SDK 3 removed the temperature request parameter");
        }
    }

    static void disableNativeTracing(AnthropicOkHttpClient.Builder builder) throws Exception {
        try {
            call(builder, "openTelemetry", boolean.class, false);
        } catch (NoSuchMethodException legacySdk) {
            // SDK 0.1.0 has no native tracing. Modern clients must have exactly one owner.
        }
    }
}
