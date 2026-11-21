package ai.traceai.bedrock;

import ai.traceai.privacy.KnownSecrets;
import ai.traceai.SemanticConventions;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import io.opentelemetry.api.trace.Span;
import software.amazon.awssdk.services.bedrockruntime.model.ContentBlockDeltaEvent;
import software.amazon.awssdk.services.bedrockruntime.model.ContentBlockStartEvent;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamMetadataEvent;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamOutput;
import software.amazon.awssdk.services.bedrockruntime.model.MessageStartEvent;
import software.amazon.awssdk.services.bedrockruntime.model.MessageStopEvent;

import java.util.Map;
import java.util.TreeMap;

/** Bounded telemetry aggregation only; the application still receives every original event. */
final class BedrockStreamOutput {
    private static final int MAX_CHARACTERS = 32000;
    private static final int MAX_BLOCKS = 128;
    private boolean cutByLastBound;
    private final BedrockTracing tracing;
    private final Span span;
    private final Map<Integer, Block> blocks = new TreeMap<>();
    private final long startedNanos = System.nanoTime();
    private Double firstContentSeconds;
    private JsonObject usage;
    private String finishReason;
    private String role = "assistant";
    private int characters;
    private boolean truncated;

    BedrockStreamOutput(BedrockTracing tracing, Span span) {
        this.tracing = tracing;
        this.span = span;
    }

    void resetAttempt() {
        blocks.clear();
        usage = null;
        finishReason = null;
        role = "assistant";
        characters = 0;
        truncated = false;
        // First content delivered to the caller is invocation-level, even if AWS later retries.
    }

    void accept(ConverseStreamOutput event) {
        if (firstContentSeconds == null && hasContent(event)) {
            firstContentSeconds = (System.nanoTime() - startedNanos) / 1e9;
        }
        if (event instanceof ConverseStreamMetadataEvent) {
            var value = ((ConverseStreamMetadataEvent) event).usage();
            if (value != null) usage = BedrockTracing.sdkJson(value, 0).getAsJsonObject();
        } else if (event instanceof MessageStopEvent) {
            finishReason = ((MessageStopEvent) event).stopReasonAsString();
        }
        if (tracing.hideStreamOutput()) return;
        if (event instanceof MessageStartEvent) {
            String value = ((MessageStartEvent) event).roleAsString();
            if (value != null) role = whole(value);
        } else if (event instanceof ContentBlockStartEvent) {
            var start = (ContentBlockStartEvent) event;
            Block block = block(start.contentBlockIndex());
            if (block != null && start.start() != null && start.start().toolUse() != null) {
                block.toolId = whole(start.start().toolUse().toolUseId());
                block.toolName = whole(start.start().toolUse().name());
            }
        } else if (event instanceof ContentBlockDeltaEvent) {
            var delta = (ContentBlockDeltaEvent) event;
            Block block = block(delta.contentBlockIndex());
            if (block != null && delta.delta() != null) {
                if (delta.delta().text() != null) {
                    block.text.append(bounded(delta.delta().text()));
                    block.textCut |= cutByLastBound;
                }
                if (delta.delta().toolUse() != null) {
                    block.arguments.append(bounded(delta.delta().toolUse().input()));
                    block.argumentsCut |= cutByLastBound;
                }
            }
        }
    }

    private static boolean hasContent(ConverseStreamOutput event) {
        if (event instanceof ContentBlockStartEvent) {
            var start = ((ContentBlockStartEvent) event).start();
            return start != null && start.toolUse() != null
                && (nonEmpty(start.toolUse().toolUseId()) || nonEmpty(start.toolUse().name()));
        }
        if (event instanceof ContentBlockDeltaEvent) {
            var delta = ((ContentBlockDeltaEvent) event).delta();
            return delta != null && (nonEmpty(delta.text())
                || delta.toolUse() != null && nonEmpty(delta.toolUse().input()));
        }
        return false;
    }

    private static boolean nonEmpty(String value) {
        return value != null && !value.isEmpty();
    }

    void capture() {
        // Defer attempt-specific attributes so retries cannot leave stale usage/cache/stop fields.
        if (usage != null) tracing.captureUsage(span, usage);
        if (finishReason != null) span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, finishReason);
        if (firstContentSeconds != null) span.setAttribute("gen_ai.client.time_to_first_token", firstContentSeconds);
        if (tracing.hideStreamOutput()) return;
        JsonArray content = new JsonArray();
        for (Block block : blocks.values()) {
            JsonObject value = new JsonObject();
            if (block.toolId != null || block.toolName != null || block.arguments.length() > 0) {
                JsonObject tool = new JsonObject();
                tool.addProperty("toolUseId", block.toolId);
                tool.addProperty("name", block.toolName);
                try {
                    tool.add("input", BedrockTracing.boundedCopy(JsonParser.parseString(finished(block.arguments, block.argumentsCut)), 0));
                } catch (RuntimeException malformedOrPartial) {
                    tool.addProperty("input", finished(block.arguments, block.argumentsCut));
                }
                value.add("toolUse", tool);
            } else {
                value.addProperty("text", finished(block.text, block.textCut));
            }
            content.add(value);
        }
        if (truncated) span.setAttribute("gen_ai.bedrock.output_truncated", true);
        tracing.captureStreamOutput(span, role, content, truncated);
        blocks.clear();
    }

    private Block block(Integer index) {
        if (index == null) return null;
        Block existing = blocks.get(index);
        if (existing != null) return existing;
        if (blocks.size() >= MAX_BLOCKS) {
            truncated = true;
            return null;
        }
        Block value = new Block();
        blocks.put(index, value);
        return value;
    }

    private String bounded(String value) {
        cutByLastBound = false;
        if (value == null) return null;
        int length = Math.min(value.length(), MAX_CHARACTERS - characters);
        characters += length;
        if (length < value.length()) {
            truncated = true;
            cutByLastBound = true;
        }
        return value.substring(0, length);
    }

    /** A single value (role, tool id, tool name): when the cap cuts it, no part of a known secret may remain. */
    private String whole(String value) {
        String kept = bounded(value);
        return cutByLastBound ? KnownSecrets.sealCut(kept) : kept;
    }

    /** Text accumulated across chunks that the cap cut: no part of a known secret that continued past the cut. */
    private static String finished(StringBuilder text, boolean cut) {
        return cut ? KnownSecrets.sealCut(text.toString()) : text.toString();
    }

    private static final class Block {
        final StringBuilder text = new StringBuilder();
        final StringBuilder arguments = new StringBuilder();
        boolean textCut;
        boolean argumentsCut;
        String toolId;
        String toolName;
    }
}
