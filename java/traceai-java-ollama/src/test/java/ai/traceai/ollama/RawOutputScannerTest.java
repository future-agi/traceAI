package ai.traceai.ollama;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** The census scanner itself, over in-memory sources: each way of writing the raw output outside the helper is flagged. */
class RawOutputScannerTest {
    private static final String FLAG_TEST = """
            TraceConfig config = tracer.getConfig();
            if (config.isHideInputs() || config.isHideInputMessages()
                || config.isHideOutputs() || config.isHideOutputMessages()) {
                return;
            }
            """;
    private static String source(String helperBody, String callSite) {
        return "class T {\n"
            + "  void call() {\n    " + callSite + "\n  }\n"
            + "  private void setRawOutputUnlessHidden(Span span, Object r) {\n" + helperBody + "  }\n"
            + "}\n";
    }
    private static final String GOOD_HELPER = FLAG_TEST + "    tracer.setRawOutput(span, r);\n";

    @Test void aCallThroughTheHelperPasses() {
        var result = RawOutputScanner.scan("T.java", source(GOOD_HELPER, "setRawOutputUnlessHidden(span, r);"));
        assertThat(result.outside()).isEmpty();
        assertThat(result.inside()).isEqualTo(1);
    }
    @Test void aDirectCallIsFlagged() {
        assertThat(RawOutputScanner.scan("T.java", source(GOOD_HELPER, "tracer.setRawOutput(span, r);")).outside()).hasSize(1);
    }
    @Test void aSpaceBeforeTheParenIsFlagged() {
        assertThat(RawOutputScanner.scan("T.java", source(GOOD_HELPER, "tracer.setRawOutput (span, r);")).outside()).hasSize(1);
    }
    @Test void aMethodReferenceIsFlagged() {
        assertThat(RawOutputScanner.scan("T.java", source(GOOD_HELPER, "java.util.function.BiConsumer<Span, Object> w = tracer::setRawOutput;")).outside()).hasSize(1);
    }
    @Test void codeAfterABlockCommentOnTheSameLineIsFlagged() {
        assertThat(RawOutputScanner.scan("T.java", source(GOOD_HELPER, "/* note */ tracer.setRawOutput(span, r);")).outside()).hasSize(1);
    }
    @Test void aSecondMethodReusingTheHelperNameWithoutTheFlagTestIsFlagged() {
        String twoHelpers = "class T {\n"
            + "  private void setRawOutputUnlessHidden(Span span, Object r) {\n" + GOOD_HELPER + "  }\n"
            + "  private void setRawOutputUnlessHidden(Span span, Object r, int x) {\n    tracer.setRawOutput(span, r);\n  }\n"
            + "}\n";
        assertThat(RawOutputScanner.scan("T.java", twoHelpers).outside()).isNotEmpty();
    }
    @Test void aHelperWithoutTheHideFlagTestIsFlagged() {
        assertThat(RawOutputScanner.scan("T.java", source("    tracer.setRawOutput(span, r);\n", "setRawOutputUnlessHidden(span, r);")).outside()).isNotEmpty();
    }
    @Test void aMentionInACommentIsNotAWrite() {
        String commented = source(GOOD_HELPER, "setRawOutputUnlessHidden(span, r); // tracer.setRawOutput(span, r) is only inside the helper\n    /* tracer.setRawOutput(span, r); */");
        assertThat(RawOutputScanner.scan("T.java", commented).outside()).isEmpty();
    }
    @Test void aStringKeyWriteIsFlagged() {
        assertThat(RawOutputScanner.scan("T.java", source(GOOD_HELPER, "span.setAttribute(\"fi.raw_output\", \"x\");")).outside()).hasSize(1);
    }
}
