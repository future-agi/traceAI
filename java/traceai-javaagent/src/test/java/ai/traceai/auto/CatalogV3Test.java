package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

/** Privacy catalog v3 and the A7 TIMING family, beyond the shared fixture bundle. */
class CatalogV3Test {
    // Obviously fake credential shapes, assembled from parts so no literal looks like a real key.
    static final String PROVIDER_KEY = "sk-" + "test-PLACEHOLDER-PLACEHOLDER";
    static final String ACCESS_KEY = "AKIA" + "PLACEHOLDER00000";
    static final String JWT = "eyJ" + "PLACEHOLDER.PLACEHOLDER.PLACEHOLDER";
    static final String PEM = "-----BEGIN " + "PRIVATE KEY-----\nPLACEHOLDER\n-----END " + "PRIVATE KEY-----";
    static final String CONTENT = "gen_ai.output.messages.0.message.content";

    static WirePrivacy privacy() { return new WirePrivacy(new KnownSecrets().snapshot(), false); }
    static Object clean(WirePrivacy privacy, String key, Object value) { return privacy.clean(key, value, WirePrivacy.Scope.EMPTY, true); }

    @Test void credentialValueShapesAreRedactedWithoutACredentialName() {
        var privacy = privacy();
        for (String shape : List.of(PROVIDER_KEY, ACCESS_KEY, JWT, "Bearer PLACEHOLDER-VALUE", "basic PLACEHOLDER-VALUE", PEM)) {
            Object once = clean(privacy, CONTENT, "before " + shape + " after");
            assertEquals("before <redacted> after", once, shape);
            assertEquals(once, clean(privacy, CONTENT, once), "idempotent for " + shape);
        }
        // The same check covers nested JSON, provider error text and attribute names.
        assertEquals(Map.of("detail", "key <redacted> is invalid"), clean(privacy, "", Map.of("detail", "key " + PROVIDER_KEY + " is invalid")));
        assertEquals("Incorrect API key provided: <redacted>.", privacy.text("Incorrect API key provided: " + PROVIDER_KEY + "."));
        assertEquals("metadata.<redacted>", privacy.text("metadata." + ACCESS_KEY));
        // Ordinary prose and short look-alikes are unchanged.
        for (String text : List.of("sk-short", "the AKIA prefix alone", "eyJ.not.a.token", "Bearer short"))
            assertEquals(text, clean(privacy, CONTENT, text), text);
    }

    @Test void tokenTimingNamesKeepNumericValuesButCredentialMarkersStillWin() {
        var privacy = privacy();
        for (String name : List.of("gen_ai.response.time_to_first_token", "gen_ai.server.time_to_first_token", "gen_ai.server.time_per_output_token",
                "time_per_token", "inter_token_latency", "token_latency", "tokens_per_second", "output_tokens_per_second", "ms_per_token", "seconds_per_token")) {
            assertEquals(.125, clean(privacy, name, .125), name);
            assertEquals(Map.of(name, 7L), clean(privacy, "", Map.of(name, 7L)), name);
            assertEquals("https://example.invalid/v1?" + name + "=0.125&model=keep", privacy.text("https://example.invalid/v1?" + name + "=0.125&model=keep"), name);
        }
        for (String name : List.of("secret.time_to_first_token", "time_to_first_token.api_key", "provider_secret_time_to_first_token",
                "provider_auth_tokens_per_second", "api_token_latency_key", "session_token", "access_token", "token"))
            assertEquals("<redacted>", clean(privacy, name, .125), name);
        // v3 no longer exempts access-key identifiers or logprob token text by name.
        assertEquals("<redacted>", clean(privacy, "aws_access_key_id", "AKIA-IDENTIFIER"));
        assertEquals("<redacted>", clean(privacy, "choices.0.logprobs.content.0.token", "Hello"));
    }

    @Test void audioUsageCountersAreMetricsNotAudioPayload() {
        var privacy = privacy();
        assertEquals(5L, clean(privacy, "gen_ai.usage.input_tokens.audio", 5L));
        assertEquals(7L, clean(privacy, "gen_ai.usage.output_tokens.audio", 7L));
        assertEquals(0L, clean(privacy, "gen_ai.usage.cache_read_tokens.audio", 0L));
        assertEquals(9L, clean(privacy, "gen_ai.audio.bytes", 9L));
        assertEquals(Map.of("gen_ai.usage.input_tokens.audio", 5L), clean(privacy, "", Map.of("gen_ai.usage.input_tokens.audio", 5L)));
        // Explicit media hints, other audio fields and invalid typed counts keep the media policy.
        var hinted = privacy.scope(Map.<String, Object>of("gen_ai.usage.mime_type", "audio/wav"), "", WirePrivacy.Scope.EMPTY);
        assertEquals("<omitted: media>", privacy.clean("gen_ai.usage.input_tokens.audio", 5L, hinted, true));
        assertEquals("<omitted: media>", clean(privacy, "input_audio", 5L));
        assertEquals("<omitted: media>", clean(privacy, "gen_ai.audio.bytes", -1L));
        assertEquals("<omitted: media>", clean(privacy, "gen_ai.audio.bytes", 1.5));
        assertEquals("<omitted: media>", clean(privacy, "gen_ai.usage.input_tokens.audio.data", 5L));
        assertEquals("<omitted: media>", privacy.clean("gen_ai.usage.input_tokens.audio", List.of(5L), WirePrivacy.Scope.EMPTY, true));
        // Forwarded application spans keep the typed counters and timing values.
        var forwarded = ExportPrivacy.attributes(Attributes.of(AttributeKey.longKey("gen_ai.usage.input_tokens.audio"), 5L,
            AttributeKey.doubleKey("gen_ai.server.time_to_first_token"), .125), privacy, AutoConfig.resolve(Map.of(), Map.of()), false, "chat");
        assertEquals(5L, forwarded.get(AttributeKey.longKey("gen_ai.usage.input_tokens.audio")));
        assertEquals(.125, forwarded.get(AttributeKey.doubleKey("gen_ai.server.time_to_first_token")));
    }

    @Test void wireChatSpansCarryNoLogprobTokens() {
        // The provider-root logprob exception applies only where a parsed provider response is
        // exported. Java wire spans export message text, never logprob blocks; this pins that.
        String body = "{\"id\":\"r\",\"model\":\"m\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"visible answer\"},"
            + "\"logprobs\":{\"content\":[{\"token\":\"LOGPROB_ONLY_TOKEN\",\"logprob\":-0.2,\"top_logprobs\":[{\"token\":\"LOGPROB_ALTERNATIVE\",\"logprob\":-0.3}]}]},"
            + "\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}";
        var request = AdversarialTest.request("https://api.openai.com/v1/chat/completions",
            Map.of("model", "m", "messages", List.of(Map.of("role", "user", "content", "hi")), "logprobs", true, "top_logprobs", 1));
        var result = AdversarialTest.build(request, AdversarialTest.response("application/json", body));
        String exported = WireCore.dump(result.attributes());
        assertTrue(exported.contains("visible answer"), exported);
        assertFalse(exported.contains("LOGPROB_ONLY_TOKEN"), exported);
        assertFalse(exported.contains("LOGPROB_ALTERNATIVE"), exported);
    }

    @Test void timingFamilyIsProtectedAfterUsageAndBeforeProvenanceAndContent() {
        Map<String, Object> source = new LinkedHashMap<>();
        source.put("gen_ai.span.kind", "LLM"); source.put("gen_ai.usage.total_tokens", 5L);
        source.put("gen_ai.response.latency", 1.5); source.put("gen_ai.server.request.duration", 2.0);
        source.put("gen_ai.server.time_per_output_token", .01); source.put("gen_ai.server.time_to_first_token", .125);
        source.put("traceai.capture.layer", "wire");
        source.put("input.value", "question"); source.put("input.mime_type", "text/plain");
        source.put("output.value", "answer"); source.put("output.mime_type", "text/plain");
        for (String key : List.of("gen_ai.response.latency", "gen_ai.response.time_to_first_chunk", "gen_ai.response.time_to_first_text", "gen_ai.response.time_to_first_token",
                "gen_ai.server.time_to_first_token", "gen_ai.server.time_per_output_token", "gen_ai.server.request.duration"))
            assertTrue(AttributeProjection.family(key) < AttributeProjection.UNPROTECTED, key);
        // Every protected key, including all present TIMING keys, is reserved before content.
        var selected = AttributeProjection.select(source, 8, "chat");
        assertEquals(Set.of(AttributeProjection.DROPPED, "gen_ai.span.kind", "gen_ai.usage.total_tokens", "gen_ai.response.latency", "gen_ai.server.request.duration",
            "gen_ai.server.time_per_output_token", "gen_ai.server.time_to_first_token", "traceai.capture.layer"), selected.keySet());
        assertEquals(4L, selected.get(AttributeProjection.DROPPED));
        assertEquals(.125, selected.get("gen_ai.server.time_to_first_token"));
        // Overflow keeps degradation, identity, outcome, model, usage, TIMING, then provenance, lexical inside a family.
        assertEquals(Set.of(AttributeProjection.DROPPED, "gen_ai.span.kind", "gen_ai.usage.total_tokens", "gen_ai.response.latency"),
            AttributeProjection.select(source, 4, "chat").keySet());
        Map<String, Object> reverse = new LinkedHashMap<>();
        List<String> keys = new ArrayList<>(source.keySet()); Collections.reverse(keys); keys.forEach(k -> reverse.put(k, source.get(k)));
        for (int limit = 0; limit <= source.size() + 1; limit++) assertEquals(AttributeProjection.select(source, limit, "chat"), AttributeProjection.select(reverse, limit, "chat"));
    }
}
