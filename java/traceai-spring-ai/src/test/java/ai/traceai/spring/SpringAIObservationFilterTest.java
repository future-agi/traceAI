package ai.traceai.spring;

import io.micrometer.observation.Observation;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import org.springframework.ai.chat.prompt.Prompt;
import static org.assertj.core.api.Assertions.assertThat;

class SpringAIObservationFilterTest {
    @Test void unrelatedObservationsAreUnchanged() {
        Observation.Context context = new Observation.Context();
        assertThat(new SpringAIObservationFilter(true, true).map(context)).isSameAs(context);
        assertThat(context.getAllKeyValues()).isEmpty();
    }

    @Test void optedInContentIsBoundedWithoutChangingApplicationObjects() {
        String large = "s".repeat(50_000);
        var context = ChatModelObservationContext.builder().prompt(new Prompt(large)).provider("openai").build();
        var response = SpanTestSupport.response(large);
        context.setResponse(response);
        var filter = new SpringAIObservationFilter(true, true);
        assertThat(filter.map(context)).isSameAs(context);
        assertThat(context.getHighCardinalityKeyValue("input.value").getValue()).hasSizeBetween(28_000, 32_000);
        assertThat(context.getHighCardinalityKeyValue("output.value").getValue()).hasSizeBetween(28_000, 32_000);
        assertThat(context.getResponse()).isSameAs(response);
        assertThat(response.getResult().getOutput().getText()).hasSize(50_000);
        filter.map(context);
        assertThat(context.getHighCardinalityKeyValues().stream().filter(k -> k.getKey().equals("fi.span.kind")).count()).isEqualTo(1);
    }

    @Test void reusableFilterRetainsNoOtherOperationsContent() {
        var filter = new SpringAIObservationFilter(true, true);
        var first = ChatModelObservationContext.builder().prompt(new Prompt("first-input")).provider("openai").build();
        first.setResponse(SpanTestSupport.response("first-output"));
        filter.map(first);
        var second = ChatModelObservationContext.builder().prompt(new Prompt("second-input")).provider("openai").build();
        filter.map(second);
        assertThat(second.getHighCardinalityKeyValue("input.value").getValue()).isEqualTo("second-input");
        assertThat(second.getHighCardinalityKeyValue("output.value")).isNull();
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(ints = {1, 9, 20, 31})
    void aKnownSecretStraddlingTheContentCapLeavesNoPrefix(int inside) {
        String secret = "PFX-SPRING-FILTER-STRADDLE-SECRET";
        ai.traceai.privacy.KnownSecrets.process().register(secret);
        String text = "a".repeat(32_000 - inside) + secret + "tail".repeat(50);
        var context = ChatModelObservationContext.builder().prompt(new Prompt(text)).provider("openai").build();
        context.setResponse(SpanTestSupport.response(text));
        new SpringAIObservationFilter(true, true).map(context);
        for (String key : new String[] {"input.value", "output.value"}) {
            assertThat(context.getHighCardinalityKeyValue(key).getValue()).as(key).doesNotContain("PFX")
                .endsWith(ai.traceai.privacy.KnownSecrets.TRUNCATION_MARKER);
        }
        for (String key : new String[] {"gen_ai.input.messages", "gen_ai.output.messages"}) {
            assertThat(context.getHighCardinalityKeyValue(key).getValue()).as(key).doesNotContain("PFX");
        }
    }
}
