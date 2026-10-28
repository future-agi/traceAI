package ai.traceai.spring;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.metadata.ChatResponseMetadata;
import org.springframework.ai.chat.metadata.DefaultUsage;
import org.springframework.ai.chat.metadata.Usage;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;
import reactor.core.publisher.Flux;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;
import java.util.function.Supplier;

/**
 * Deterministic in-memory tracing fixture. Uses an explicit SdkTracerProvider; never the global registration.
 */
final class SpanTestSupport implements AutoCloseable {

    final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    final AtomicInteger spansStarted = new AtomicInteger();
    final AtomicInteger spansEnded = new AtomicInteger();
    final SdkTracerProvider provider;
    final FITracer tracer;

    SpanTestSupport() {
        this(TraceConfig.builder().build());
    }

    SpanTestSupport(TraceConfig config) {
        SpanProcessor counter = new SpanProcessor() {
            @Override
            public void onStart(Context parentContext, ReadWriteSpan span) {
                spansStarted.incrementAndGet();
            }

            @Override
            public boolean isStartRequired() {
                return true;
            }

            @Override
            public void onEnd(ReadableSpan span) {
                spansEnded.incrementAndGet();
            }

            @Override
            public boolean isEndRequired() {
                return true;
            }
        };
        this.provider = SdkTracerProvider.builder()
            .addSpanProcessor(counter)
            .addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .build();
        this.tracer = new FITracer(provider.get("spring-ai-unit-test"), config);
    }

    List<SpanData> finished() {
        return exporter.getFinishedSpanItems();
    }

    SpanData single() {
        List<SpanData> spans = finished();
        if (spans.size() != 1) {
            throw new AssertionError("expected exactly one exported span but found " + spans.size() + ": " + spans);
        }
        return spans.get(0);
    }

    @Override
    public void close() {
        provider.close();
    }

    // ------------------------------------------------------------------ real Spring AI objects

    static ChatResponse response(String text, Usage usage, String model) {
        ChatResponseMetadata.Builder metadata = ChatResponseMetadata.builder();
        if (usage != null) {
            metadata.usage(usage);
        }
        if (model != null) {
            metadata.model(model);
        }
        return new ChatResponse(List.of(new Generation(new AssistantMessage(text))), metadata.build());
    }

    static ChatResponse response(String text) {
        return new ChatResponse(List.of(new Generation(new AssistantMessage(text))));
    }

    static Usage usage(int prompt, int completion, int total) {
        return new DefaultUsage(prompt, completion, total);
    }

    /** Usage whose three counts are independently nullable (constructors cannot express a null total). */
    static Usage usage(Integer prompt, Integer completion, Integer total, Object nativeUsage) {
        return new Usage() {
            @Override
            public Integer getPromptTokens() {
                return prompt;
            }

            @Override
            public Integer getCompletionTokens() {
                return completion;
            }

            @Override
            public Integer getTotalTokens() {
                return total;
            }

            @Override
            public Object getNativeUsage() {
                return nativeUsage;
            }
        };
    }

    static ChatModel syncModel(Function<Prompt, ChatResponse> call) {
        return new ChatModel() {
            @Override
            public ChatResponse call(Prompt prompt) {
                return call.apply(prompt);
            }

            @Override
            public Flux<ChatResponse> stream(Prompt prompt) {
                throw new AssertionError("stream must not be used by this test");
            }

            @Override
            public ChatOptions getDefaultOptions() {
                return null;
            }
        };
    }

    static ChatModel streamModel(Function<Prompt, Flux<ChatResponse>> stream) {
        return new ChatModel() {
            @Override
            public ChatResponse call(Prompt prompt) {
                throw new AssertionError("call must not be used by this test");
            }

            @Override
            public Flux<ChatResponse> stream(Prompt prompt) {
                return stream.apply(prompt);
            }

            @Override
            public ChatOptions getDefaultOptions() {
                return null;
            }
        };
    }

    static ChatModel streamModel(Supplier<Flux<ChatResponse>> stream) {
        return streamModel(prompt -> stream.get());
    }
}
