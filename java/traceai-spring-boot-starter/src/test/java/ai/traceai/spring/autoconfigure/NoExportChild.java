package ai.traceai.spring.autoconfigure;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.AppenderBase;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

/**
 * Runs the starter in a JVM of its own, so the process environment (FI_BASE_URL, FI_API_KEY, FI_SECRET_KEY) is the real one.
 * args are property values. Prints STARTED=true|false, one WARN line per warning, and DONE.
 */
public final class NoExportChild {
    public static void main(String[] args) {
        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        AppenderBase<ILoggingEvent> appender = new AppenderBase<>() {
            @Override protected void append(ILoggingEvent event) { if (event.getLevel().isGreaterOrEqual(Level.WARN)) System.out.println("WARN: " + event.getFormattedMessage()); }
        };
        appender.setContext(root.getLoggerContext()); appender.start(); root.addAppender(appender);
        new ApplicationContextRunner().withConfiguration(AutoConfigurations.of(TraceAIAutoConfiguration.class)).withPropertyValues(args).run(context -> {
            boolean failed = context.getStartupFailure() != null;
            System.out.println("STARTED=" + !failed);
            if (failed) System.out.println("FAILURE=" + context.getStartupFailure());
            else context.getBean(FITracer.class).startSpan("probe", FISpanKind.CHAIN).end();
            TraceAI.shutdown();
        });
        System.out.println("DONE");
    }
    private NoExportChild() { }
}
