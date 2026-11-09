package ai.traceai.examples.arconia;

import ai.traceai.privacy.spring.ErrorDetailRedactionConfiguration;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;

/**
 * Compatibility entry point for applications that imported the original example configuration.
 * Prefer importing {@link ErrorDetailRedactionConfiguration} from traceai-java-core directly.
 * The maintained library now owns the filter; Arconia still owns telemetry and export.
 */
@Deprecated
@Configuration(proxyBeanMethods = false)
@Import(ErrorDetailRedactionConfiguration.class)
public class ErrorDetailRedaction {
}
