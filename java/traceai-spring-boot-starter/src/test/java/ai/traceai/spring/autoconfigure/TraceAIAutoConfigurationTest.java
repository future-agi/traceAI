package ai.traceai.spring.autoconfigure;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.support.AbstractBeanDefinition;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

import static org.assertj.core.api.Assertions.assertThat;

class TraceAIAutoConfigurationTest {
    private final ApplicationContextRunner runner = new ApplicationContextRunner()
        .withConfiguration(AutoConfigurations.of(TraceAIAutoConfiguration.class));

    @AfterEach
    void reset() {
        TraceAI.shutdown();
        GlobalOpenTelemetry.resetForTest();
    }

    @Test
    void borrowsApplicationOpenTelemetryWithoutDuplicatePipeline() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            OpenTelemetry sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
            runner.withBean(OpenTelemetry.class, () -> sdk,
                    definition -> ((AbstractBeanDefinition) definition).setDestroyMethodName(""))
                .run(context -> {
                    assertThat(context).hasNotFailed().hasSingleBean(FITracer.class);
                    FITracer tracer = context.getBean(FITracer.class);
                    assertThat(tracer).isSameAs(TraceAI.getTracer());
                    tracer.startSpan("borrowed", FISpanKind.CHAIN).end();
                    assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                    assertThat(exporter.getFinishedSpanItems().get(0).getName()).isEqualTo("borrowed");
                });
            sdk.getTracer("application").spanBuilder("after-context").startSpan().end();
            assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        }
    }

    @Test
    void existingGlobalAndApplicationBeanDoNotCollide() {
        try (SdkTracerProvider provider = SdkTracerProvider.builder().build()) {
            OpenTelemetry sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
            runner.withBean(OpenTelemetry.class, () -> sdk,
                    definition -> ((AbstractBeanDefinition) definition).setDestroyMethodName(""))
                .run(context -> assertThat(context).hasNotFailed().hasSingleBean(FITracer.class));
        }
    }

    @Test
    void providerOnlyBeanIsBorrowed() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            runner.withBean(SdkTracerProvider.class, () -> provider,
                    definition -> ((AbstractBeanDefinition) definition).setDestroyMethodName(""))
                .run(context -> {
                    assertThat(context).hasNotFailed();
                    context.getBean(FITracer.class).startSpan("provider-only", FISpanKind.CHAIN).end();
                    assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                });
            provider.get("application").spanBuilder("still-running").startSpan().end();
            assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        }
        assertThat(TraceAI.isInitialized()).isFalse();
    }

    @Test
    void disabledStarterDoesNotInitializeAnything() {
        runner.withPropertyValues("traceai.enabled=false").run(context -> {
            assertThat(context).hasNotFailed().doesNotHaveBean(FITracer.class);
            assertThat(TraceAI.isInitialized()).isFalse();
        });
    }

    @Test
    void customTracerIsNotReplaced() {
        FITracer custom = new FITracer(OpenTelemetry.noop().getTracer("custom"));
        runner.withBean(FITracer.class, () -> custom).run(context -> {
            assertThat(context).hasNotFailed().hasSingleBean(FITracer.class);
            assertThat(context.getBean(FITracer.class)).isSameAs(custom);
            assertThat(TraceAI.isInitialized()).isFalse();
        });
    }

    @Test
    void alreadyInitializedTracerRemainsApplicationOwned() {
        TraceAI.init(ai.traceai.TraceConfig.builder().build(), OpenTelemetry.noop());
        FITracer applicationTracer = TraceAI.getTracer();
        runner.run(context -> assertThat(context.getBean(FITracer.class)).isSameAs(applicationTracer));
        assertThat(TraceAI.isInitialized()).isTrue();
        assertThat(TraceAI.getTracer()).isSameAs(applicationTracer);
    }

    @Test
    void standaloneContextsCanRestartWithoutTouchingGlobal() {
        GlobalOpenTelemetry.set(OpenTelemetry.noop());
        OpenTelemetry global = GlobalOpenTelemetry.get();
        for (int i = 0; i < 2; i++) {
            runner.run(context -> {
                assertThat(context).hasNotFailed().hasSingleBean(FITracer.class);
                assertThat(context.getBean(FITracer.class)).isSameAs(TraceAI.getTracer());
            });
            assertThat(TraceAI.isInitialized()).isFalse();
            assertThat(GlobalOpenTelemetry.get()).isSameAs(global);
        }
    }

    @Test
    void ambiguousApplicationSdksFailInsteadOfSilentlyCreatingAnotherProvider() {
        runner.withBean("one", OpenTelemetry.class, OpenTelemetry::noop)
            .withBean("two", OpenTelemetry.class, OpenTelemetry::noop)
            .run(context -> {
                assertThat(context).hasFailed();
                assertThat(TraceAI.isInitialized()).isFalse();
            });
    }

    @Test
    void primaryApplicationSdkResolvesAmbiguity() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            OpenTelemetry sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
            runner.withBean("unused", OpenTelemetry.class, OpenTelemetry::noop)
                .withBean("primary", OpenTelemetry.class, () -> sdk, definition -> {
                    definition.setPrimary(true);
                    ((AbstractBeanDefinition) definition).setDestroyMethodName("");
                }).run(context -> {
                    assertThat(context).hasNotFailed();
                    context.getBean(FITracer.class).startSpan("primary", FISpanKind.CHAIN).end();
                    assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                });
        }
    }

    @Test
    void consoleExporterKeepsTheStarterOwnedProvider() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            OpenTelemetry sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
            runner.withPropertyValues("traceai.enable-console-exporter=true")
                .withBean(OpenTelemetry.class, () -> sdk,
                    definition -> ((AbstractBeanDefinition) definition).setDestroyMethodName(""))
                .run(context -> context.getBean(FITracer.class).startSpan("console", FISpanKind.CHAIN).end());
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
        }
    }

    @Test
    void doesNotPullInSpringAi() {
        assertThat(getClass().getClassLoader().getResource("ai/traceai/spring/TracedChatModel.class"))
            .isNull();
        assertThat(getClass().getClassLoader().getResource("org/springframework/ai/chat/model/ChatModel.class"))
            .isNull();
    }
}
