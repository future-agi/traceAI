package ai.traceai.auto;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/** Which span kind each ADK span name maps to, across the names ADK versions have used. */
class GoogleAdkKindsTest {
    @Test void namesMapToKinds() {
        assertEquals("CHAIN", GoogleAdk.AdkSpan.kindOf("invocation"));
        assertEquals("AGENT", GoogleAdk.AdkSpan.kindOf("invoke_agent helper"));
        assertEquals("AGENT", GoogleAdk.AdkSpan.kindOf("agent_run [helper]"));
        assertEquals("LLM", GoogleAdk.AdkSpan.kindOf("call_llm"));
        assertEquals("TOOL", GoogleAdk.AdkSpan.kindOf("execute_tool weather"));
        assertEquals("TOOL", GoogleAdk.AdkSpan.kindOf("execute_tool [weather]"));
        assertEquals("TOOL", GoogleAdk.AdkSpan.kindOf("tool_call [weather]"));
    }

    @Test void otherSpansAreNotMirrored() {
        assertNull(GoogleAdk.AdkSpan.kindOf("send_data"));
        assertNull(GoogleAdk.AdkSpan.kindOf("GET /"));
        assertNull(GoogleAdk.AdkSpan.kindOf(null));
        assertNull(GoogleAdk.kindOf(new Object()));
        assertNull(GoogleAdk.kindOf(null));
    }
}
