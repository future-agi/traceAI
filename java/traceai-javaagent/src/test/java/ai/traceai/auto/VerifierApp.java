package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import java.util.*;

/** Runs against the shaded agent while using only application-owned, unshaded APIs. */
public final class VerifierApp {
    static void run(String[] args) throws Exception {
        if (args[2].equals("verify_parent")) {
            var parent = SpanContext.createFromRemoteParent("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.builder().put("vendor", "state").build());
            try (var scope = Span.wrap(parent).makeCurrent()) { PlainApp.call(args); }
            return;
        }
        if (args[2].equals("verify_cohere")) { cohereConventions(args); return; }
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            io.opentelemetry.sdk.OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
            var tracer = new ai.traceai.FITracer(provider.get("existing-wrapper"));
            var span = tracer.startSpan("existing-wrapper", ai.traceai.FISpanKind.LLM);
            var input = new LinkedHashMap<String, Object>(); input.put("password", "WRAPPER_PRIVATE_PASSWORD"); input.put("prompt", "x".repeat(40000));
            tracer.setRawInput(span, input); tracer.setRawOutput(span, input);
            // Attribute conventions emitted by the existing Semantic Kernel and Cohere wrappers.
            span.setAttribute(ai.traceai.SemanticConventions.EMBEDDING_INPUT_TEXT + ".0", "PRIVATE_WRAPPER_EMBEDDING_INPUT");
            span.setAttribute(ai.traceai.SemanticConventions.GEN_AI_RERANKER_QUERY, "PRIVATE_WRAPPER_RERANK_QUERY");
            span.setAttribute(ai.traceai.SemanticConventions.GEN_AI_RERANKER_INPUT_DOCUMENTS, 3L);
            String partial = "{\"password\":\"WRAPPER_EVENT_PASSWORD\",\"prompt\":\"";
            span.setStatus(StatusCode.ERROR, partial);
            span.addEvent("exception", io.opentelemetry.api.common.Attributes.of(AttributeKey.stringKey("exception.message"), partial));
            try (var scope = span.makeCurrent()) { PlainApp.call(args); } finally { span.end(); }
            var own = memory.getFinishedSpanItems().get(0);
            String raw = own.getAttributes().get(AttributeKey.stringKey("fi.raw_input"));
            if (raw == null || raw.length() != 32001 || !raw.contains("WRAPPER_PRIVATE_PASSWORD")) throw new AssertionError("Application raw input changed");
            if (!partial.equals(own.getStatus().getDescription()) || !partial.equals(own.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.message")))) throw new AssertionError("Application diagnostics changed");
            if (!"PRIVATE_WRAPPER_EMBEDDING_INPUT".equals(own.getAttributes().get(AttributeKey.stringKey(ai.traceai.SemanticConventions.EMBEDDING_INPUT_TEXT + ".0")))
                || !"PRIVATE_WRAPPER_RERANK_QUERY".equals(own.getAttributes().get(AttributeKey.stringKey(ai.traceai.SemanticConventions.GEN_AI_RERANKER_QUERY)))) throw new AssertionError("Application wrapper attributes changed");
            System.out.println("WRAPPER_RAW_UNCHANGED=true");
            Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("flush").invoke(null);
        }
    }
    static void cohereConventions(String[] args) throws Exception {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            // Reproduce TracedCohereClient.chat's direct attributes, which bypass FITracer masking.
            var tracer = new ai.traceai.FITracer(provider.get("existing-cohere-wrapper"), ai.traceai.TraceConfig.builder().hideInputs(true).hideOutputs(true).build());
            var span = tracer.startSpan("Cohere Chat", ai.traceai.FISpanKind.LLM);
            tracer.setInputValue(span, "PRIVATE_NORMAL_COHERE_INPUT"); tracer.setOutputValue(span, "PRIVATE_NORMAL_COHERE_OUTPUT");
            String preamble = "PRIVATE_COHERE_SYSTEM_PROMPT", arguments = "{\"location\":\"PRIVATE_COHERE_TOOL_ARGUMENT\"}";
            String tool = ai.traceai.SemanticConventions.MESSAGE_TOOL_CALLS + ".0.";
            span.setAttribute("cohere.preamble", preamble);
            span.setAttribute(tool + ai.traceai.SemanticConventions.TOOL_CALL_FUNCTION_NAME, "PRIVATE_COHERE_TOOL_NAME");
            span.setAttribute(tool + ai.traceai.SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS, arguments);
            try (var scope = span.makeCurrent()) { PlainApp.call(args); } finally { span.end(); }
            var own = memory.getFinishedSpanItems().get(0).getAttributes();
            if (!preamble.equals(own.get(AttributeKey.stringKey("cohere.preamble"))) || !arguments.equals(own.get(AttributeKey.stringKey(tool + ai.traceai.SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS)))) throw new AssertionError("Application Cohere attributes changed");
            if (own.get(AttributeKey.stringKey("input.value")) != null || own.get(AttributeKey.stringKey("output.value")) != null) throw new AssertionError("Application FITracer hide flags were not enabled");
            System.out.println("COHERE_APPLICATION_UNCHANGED=true");
            Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("flush").invoke(null);
        }
    }
}
