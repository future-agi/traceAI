package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class PrivacyCatalogTest {
    @Test void magicMediaHandlesEveryCatalogSignatureAroundDecodePrefixBoundary() throws Exception {
        Map<String, Object> media;
        try (var in = PrivacyCatalog.class.getResourceAsStream("privacy.json")) {
            media = WireCore.map(WireCore.get(WireCore.parse(new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8)), "media"));
        }
        int prefixBytes = ((Number)media.get("base64_prefix_decoded_bytes")).intValue(), checks = 0;
        var encoders = List.of(Base64.getEncoder(), Base64.getUrlEncoder(), Base64.getEncoder().withoutPadding(), Base64.getUrlEncoder().withoutPadding());
        List<String> failures = new ArrayList<>();
        for (Object rule : WireCore.list(media.get("magic"))) {
            List<Object> tests = WireCore.list(WireCore.get(rule, "tests")); int minimum = 0;
            for (Object test : tests) minimum = Math.max(minimum, ((Number)WireCore.get(test, "offset")).intValue() + HexFormat.of().parseHex((String)WireCore.get(test, "hex")).length);
            for (int size = minimum; size <= 2 * prefixBytes + 2; size++) {
                byte[] bytes = new byte[size]; Arrays.fill(bytes, (byte)0xfb);
                for (Object test : tests) {
                    byte[] signature = HexFormat.of().parseHex((String)WireCore.get(test, "hex"));
                    System.arraycopy(signature, 0, bytes, ((Number)WireCore.get(test, "offset")).intValue(), signature.length);
                }
                for (var encoder : encoders) {
                    String encoded = encoder.encodeToString(bytes);
                    for (String wrap : List.of("", "\n", "\r\n")) {
                        StringJoiner wrapped = new StringJoiner(wrap);
                        for (int i = 0; i < encoded.length(); i += 7) wrapped.add(encoded.substring(i, Math.min(encoded.length(), i + 7)));
                        String value = wrapped.toString();
                        String actual = PrivacyCatalog.INSTANCE.mediaString(value, "arbitrary", false, false, Map.of(), true);
                        if (!PrivacyCatalog.INSTANCE.omitted(value.length()).equals(actual)) failures.add(WireCore.get(rule, "id") + " bytes=" + size + " value=" + value.replace("\r", "\\r").replace("\n", "\\n"));
                        checks++;
                    }
                }
            }
        }
        System.out.println("Magic-prefix boundary cases: " + checks + "; failures: " + failures.size());
        assertTrue(failures.isEmpty(), () -> String.join("\n", failures.subList(0, Math.min(20, failures.size()))));
        for (int size = 1; size <= 2 * prefixBytes + 2; size++) {
            String value = Base64.getEncoder().encodeToString(new byte[size]);
            assertNull(PrivacyCatalog.INSTANCE.mediaString(value, "arbitrary", false, false, Map.of(), true), "unrecognized bytes=" + size);
        }
    }

    @Test void paddedMagicMediaIsScrubbedAcrossDetachedExportSurfacesWithoutMutation() {
        String media = MagicMediaApp.MEDIA, omitted = "<omitted: 44 base64 chars>";
        String json = "{\"nested\":[{\"content\":\"" + media + "\"}]}";
        String url = "https://example.invalid/path?public=keep&sample=" + URLEncoder.encode(media, StandardCharsets.UTF_8) + "#fragment";
        var originalAttrs = Attributes.builder().put("probe", media).put("probe.json", json).put("http.response.url", url)
            .put(AttributeKey.stringArrayKey("probe.array"), List.of(media, "PUBLIC"))
            .put("parts[0].value", media).build();
        var expectedAttrs = Attributes.builder().put("probe", omitted).put("probe.json", "{\"nested\":[{\"content\":\"" + omitted + "\"}]}")
            .put("http.response.url", "https://example.invalid/path?public=keep&sample=" + omitted + "#fragment")
            .put(AttributeKey.stringArrayKey("probe.array"), List.of(omitted, "PUBLIC")).put("parts[0].value", omitted).build();
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setResource(Resource.create(originalAttrs)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var link = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.getDefault());
            var span = provider.get("magic-media").spanBuilder(media).setAllAttributes(originalAttrs).addLink(link, originalAttrs).startSpan();
            span.setStatus(StatusCode.ERROR, media); span.addEvent(media, originalAttrs); span.end();
            var original = memory.getFinishedSpanItems().get(0);
            var serialized = new OtlpJsonExporter(AutoConfig.resolve(Map.of("config_file", "target/no-magic-config"), Map.of())).serialize(original);
            var scope = WireCore.map(WireCore.list(serialized.get("scopeSpans")).get(0));
            var data = WireCore.map(WireCore.list(scope.get("spans")).get(0));
            var expected = OtlpJsonExporter.attributes(expectedAttrs);
            assertEquals(expected, WireCore.get(serialized.get("resource"), "attributes"));
            assertEquals(expected, data.get("attributes"));
            assertEquals(expected, WireCore.get(WireCore.list(data.get("events")).get(0), "attributes"));
            assertEquals(expected, WireCore.get(WireCore.list(data.get("links")).get(0), "attributes"));
            assertEquals(omitted, data.get("name")); assertEquals(omitted, WireCore.get(data.get("status"), "message"));
            assertEquals(omitted, WireCore.get(WireCore.list(data.get("events")).get(0), "name"));
            assertFalse(WireCore.dump(serialized).contains(media));
            assertEquals(originalAttrs.asMap(), original.getAttributes().asMap()); assertEquals(originalAttrs.asMap(), original.getResource().getAttributes().asMap());
            assertEquals(originalAttrs.asMap(), original.getEvents().get(0).getAttributes().asMap()); assertEquals(originalAttrs.asMap(), original.getLinks().get(0).getAttributes().asMap());
            assertEquals(media, original.getName()); assertEquals(media, original.getStatus().getDescription());
        }
    }
}
