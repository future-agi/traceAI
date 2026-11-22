package ai.traceai.auto;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Constructor;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/** The in-flight registry of ADK model calls holds identities only, and never suppresses a request that is not under one of its spans. */
class GoogleAdkInFlightTest {
    @SuppressWarnings("unchecked")
    private static Map<Object, Object> registry() throws Exception {
        var field = GoogleAdk.class.getDeclaredField("IN_FLIGHT_LLM");
        field.setAccessible(true);
        return (Map<Object, Object>) field.get(null);
    }

    private static void fill(int count) throws Exception {
        Class<?> flight = Class.forName("ai.traceai.auto.GoogleAdk$Flight");
        Constructor<?> constructor = flight.getDeclaredConstructor(long.class, Object[].class);
        constructor.setAccessible(true);
        for (int i = 0; i < count; i++) {
            Object identity = new Object();
            registry().put(identity, constructor.newInstance(System.nanoTime(), new Object[] {new Object()}));
        }
    }

    @AfterEach void clear() throws Exception { registry().clear(); }

    @Test void aSpanLessRequestIsNotSuppressedWhileAModelCallIsInFlight() throws Exception {
        fill(1);
        assertFalse(OtelBridge.snapshot(true).suppressed(), "another context's model call drops no span-less request");
    }

    @Test void whenTheRegistryIsFullNoSpanLessRequestIsSuppressedAndItHoldsNoContent() throws Exception {
        fill(256);
        assertEquals(256, registry().size());
        assertFalse(OtelBridge.snapshot(true).suppressed());
        assertFalse(OtelBridge.snapshot(false).suppressed());
        // An identity and a start time for each call, nothing else: every key and every ancestor is a bare Object.
        for (var entry : registry().entrySet()) {
            assertEquals(Object.class, entry.getKey().getClass());
            var flight = entry.getValue();
            for (var component : flight.getClass().getRecordComponents()) {
                Object value = component.getAccessor().invoke(flight);
                if (value instanceof Object[] ancestors) for (Object ancestor : ancestors) assertEquals(Object.class, ancestor.getClass());
                else assertTrue(value instanceof Long, "only a start time: " + component.getName());
            }
        }
    }
}
