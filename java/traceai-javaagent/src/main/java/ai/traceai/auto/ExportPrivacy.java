package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import java.util.*;

/** The sole authoritative privacy boundary, used immediately before OTLP serialization. */
final class ExportPrivacy {
    static WirePrivacy privacy(AutoConfig config) { return new WirePrivacy(KnownSecrets.PROCESS.snapshot(), config.flag("FI_HIDE_INPUT_IMAGES")); }
    @SuppressWarnings({"rawtypes", "unchecked"})
    static Attributes attributes(Attributes source, WirePrivacy privacy, AutoConfig config, boolean event, String operation) {
        AttributesBuilder result = Attributes.builder();
        WireCore.Match match = new WireCore.Match(Map.of("operation", operation), "", "", 0, Map.of(), Map.of());
        Map<String, Object> siblings = new LinkedHashMap<>();
        source.forEach((key, value) -> siblings.put(key.getKey(), value));
        WirePrivacy.Scope scope = privacy.scope(siblings, "", WirePrivacy.Scope.EMPTY);
        source.forEach((key, value) -> {
            if (TraceAIAuto.hidden(key.getKey(), match, config)) return;
            Object clean = privacy.clean(key.getKey(), value, scope, true);
            // Only inspect materialized sanitizer output, never an application's List implementation.
            if (event && TraceAIAuto.hidesContent(config) && (clean instanceof String || clean instanceof List<?> list && list.stream().anyMatch(String.class::isInstance))) return;
            String name = privacy.text(key.getKey());
            AttributeKey typed = clean instanceof String ? AttributeKey.stringKey(name) : switch (key.getType()) {
                case STRING -> AttributeKey.stringKey(name); case BOOLEAN -> AttributeKey.booleanKey(name);
                case LONG -> AttributeKey.longKey(name); case DOUBLE -> AttributeKey.doubleKey(name);
                case STRING_ARRAY -> AttributeKey.stringArrayKey(name); case BOOLEAN_ARRAY -> AttributeKey.booleanArrayKey(name);
                case LONG_ARRAY -> AttributeKey.longArrayKey(name); case DOUBLE_ARRAY -> AttributeKey.doubleArrayKey(name);
            };
            result.put(typed, clean);
        });
        return result.build();
    }
    private ExportPrivacy() {}
}
