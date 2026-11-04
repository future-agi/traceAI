package ai.traceai.spring.compatibility;

import ai.traceai.FITracer;
import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.TracedChatModel;
import ai.traceai.spring.TracedEmbeddingModel;
import com.google.gson.JsonParser;
import io.micrometer.observation.ObservationRegistry;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.ai.embedding.EmbeddingOptions;
import org.springframework.ai.embedding.EmbeddingRequest;

import static org.junit.jupiter.api.Assertions.*;

/** Consumer regressions against the installed JAR and real, loopback-only OpenAI models. */
@Timeout(30)
class WrapperModelContractTest {
    @Test
    void publicChatOptionsPreserveTheConfiguredModel() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            var delegate = Models.chat(f.url(), ObservationRegistry.NOOP);
            var wrapper = new TracedChatModel(delegate, new FITracer(f.sdk.get("wrapper-contract")), "openai");
            assertEquals("synthetic-model", Models.chatOptions(delegate).getModel());
            assertEquals("synthetic-model", Models.chatOptions(wrapper).getModel());
            assertEquals("synthetic-model", wrapper.getDefaultOptions().getModel());
            assertTrue(f.requests.isEmpty(), "Accessing options must not issue a model request");
            assertTrue(f.spans().isEmpty(), "Accessing options must not open a span");
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"convenience", "null-options", "empty-options", "explicit-model"})
    void embeddingResponseModelNeverReplacesTheRequestModel(String path) throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.reply(NativeSpringAITest.EMBEDDING.replace("synthetic-embedding", "synthetic-response-embedding"));
            var wrapper = new TracedEmbeddingModel(Models.embedding(f.url(), ObservationRegistry.NOOP),
                new FITracer(f.sdk.get("wrapper-contract")), "openai");
            boolean explicitModel = path.equals("explicit-model");
            var texts = List.of("synthetic input");
            var response = path.equals("convenience") ? wrapper.embedForResponse(texts)
                : wrapper.call(new EmbeddingRequest(texts, path.equals("null-options") ? null
                    : EmbeddingOptions.builder().model(explicitModel ? "synthetic-request-override" : null).build()));

            assertEquals("synthetic-response-embedding", response.getMetadata().getModel());
            assertEquals(1, f.requests.size());
            String sentModel = JsonParser.parseString(f.requests.element()).getAsJsonObject().get("model").getAsString();
            assertEquals(explicitModel ? "synthetic-request-override" : "synthetic-embedding", sentModel);
            var span = f.onlySpan();
            assertEquals("synthetic-response-embedding", NativeSpringAITest.attr(span, "gen_ai.response.model"));
            // The common EmbeddingModel API does not expose defaults on the 1.1.x floor.
            // An unknown request model must stay absent instead of being guessed from the response.
            assertEquals(explicitModel ? sentModel : null, NativeSpringAITest.attr(span, "gen_ai.request.model"));
        }
    }
}
