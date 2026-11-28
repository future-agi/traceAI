package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * Credential name=value pairs in free text are replaced on the shared export path, whichever integration produced the text.
 * Each case builds the attributes an integration emits, exports them, and looks for the secret value anywhere in the output.
 */
class SharedPairRuleTest {
    static String export(Map<String, String> attributes) {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var builder = provider.get("integration").spanBuilder("span");
            attributes.forEach((key, value) -> builder.setAttribute(AttributeKey.stringKey(key), value));
            builder.startSpan().end();
            SpanData span = memory.getFinishedSpanItems().get(0);
            return WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of(), Map.of())).serialize(span));
        }
    }

    static String basic() { return Base64.getEncoder().encodeToString("svc-user:s3cr3t-basic-pass".getBytes()); }

    @Test void aWireBodyHoldingPairsAndAnAuthorizationLineIsExportedWithoutTheirValues() {
        String text = "please log in with password=s3cr3t-value and then send\nAuthorization: Basic " + basic();
        var request = AdversarialTest.request("https://api.openai.com/v1/chat/completions",
            Map.of("model", "m", "messages", List.of(Map.of("role", "user", "content", text))));
        var response = AdversarialTest.response("application/json", "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"ok\"}}]}");
        var result = AdversarialTest.build(request, response);
        String json = WireCore.dump(result.attributes());
        assertTrue(json.contains("please log in with"), json);
        assertFalse(json.contains("s3cr3t-value"), json);
        assertFalse(json.contains(basic()), json);
        assertFalse(json.contains("s3cr3t-basic-pass"), json);
    }

    @Test void aLangChain4jToolArgumentStringIsExportedWithoutTheValue() {
        String json = export(Map.of("llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments", "lookup token: abc123def456 for the account"));
        assertTrue(json.contains("lookup"), json);
        assertFalse(json.contains("abc123def456"), json);
    }

    @Test void aSpringAiMessageWithACommandLineFlagIsExportedWithoutTheValue() {
        String json = export(Map.of("llm.input_messages.0.message.content", "run the tool with --password hunter2-value now"));
        assertTrue(json.contains("run the tool with"), json);
        assertFalse(json.contains("hunter2-value"), json);
    }

    @Test void aGoogleAdkToolResultWithASegmentNamedPairIsExportedWithoutTheValue() {
        String json = export(Map.of("output.value", "connected using dbPass=xyz987value to the primary"));
        assertTrue(json.contains("connected using"), json);
        assertFalse(json.contains("xyz987value"), json);
    }

    @Test void aCookieLineWithThreeCookiesInsideABodyIsExportedWithoutAnyOfThem() {
        String json = export(Map.of("input.value", "GET / HTTP/1.1\nCookie: sid=cookieone111; theme=cookietwo222; csrf=cookiethree333\nHost: example.com"));
        for (String value : List.of("cookieone111", "cookietwo222", "cookiethree333")) assertFalse(json.contains(value), value + " in " + json);
        assertTrue(json.contains("Host: example.com"), json);
    }

    @Test void textWithoutCredentialNamesIsUnchanged() {
        Map<String, String> texts = new LinkedHashMap<>();
        texts.put("input.value", "the colour=blue and size: large, retry=3\nHost: example.com");
        texts.put("llm.input_messages.0.message.content", "run the tool with --verbose now");
        texts.put("llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments", "lookup city: Paris for the account");
        texts.put("output.value", "connected using the primary at https://example.com/path?page=2");
        String json = export(texts);
        for (String text : texts.values()) assertTrue(json.contains(WireCore.dump(text).replaceAll("^\"|\"$", "")), text + " in " + json);
    }

    @Test void theCapturedValuePathAndTheExportPathAgree() {
        String text = "x password=s3cr3t-value; token: abc123def456 --pwd p4ss-word user[password]=nested-value";
        String captured = String.valueOf(CapturedValue.of(text, 4096));
        String exported = export(Map.of("input.value", text));
        for (String value : List.of("s3cr3t-value", "abc123def456", "p4ss-word", "nested-value")) {
            assertFalse(captured.contains(value), value + " in " + captured);
            assertFalse(exported.contains(value), value + " in " + exported);
        }
    }
}
