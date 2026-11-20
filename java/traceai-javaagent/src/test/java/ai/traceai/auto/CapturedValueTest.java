package ai.traceai.auto;

import java.nio.file.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

/** What an annotated method's arguments and return value become in a span: never iterated unless materialised, bounded, acyclic. */
class CapturedValueTest {
    static String text(Object value) { return new com.google.gson.GsonBuilder().disableHtmlEscaping().create().toJson(AnnotationTracing.json(value, 0)); }

    record Node(String name, List<Object> children) { }
    record Point(int x, int y) { }
    static final class Bean { private final String password = "bean-private-secret"; public String getName() { return "bean"; } }
    static final class Described { private final String hidden = "field-secret"; @Override public String toString() { return "Described[ok]"; } }
    static final class Throwing { @Override public String toString() { throw new IllegalStateException("toString failed"); } }

    @Test void aSingleUseIterableIsNotIterated() {
        AtomicInteger iterators = new AtomicInteger();
        Iterable<String> once = () -> { iterators.incrementAndGet(); return List.of("a", "b").iterator(); };
        String captured = text(once);
        assertEquals(0, iterators.get(), "capturing an argument must not call iterator(): " + captured);
        assertFalse(captured.contains("\"a\""), captured);
    }
    @Test void aDirectoryStreamIsStillReadableAfterCapture() throws Exception {
        Path dir = Files.createTempDirectory(Path.of("target"), "capture-");
        Files.writeString(dir.resolve("one.txt"), "x"); Files.writeString(dir.resolve("two.txt"), "y");
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir)) {
            String captured = text(stream);
            assertTrue(captured.contains("DirectoryStream") || captured.contains("<"), captured);
            int count = 0; for (Path ignored : stream) count++;
            assertEquals(2, count, "the method still reads every entry");
        }
    }
    @Test void aLazyMapIteratorStreamAndEnumerationAreNotTouched() {
        AtomicInteger touched = new AtomicInteger();
        Map<String, String> lazy = new AbstractMap<>() { @Override public Set<Entry<String, String>> entrySet() { touched.incrementAndGet(); return Set.of(); } };
        Iterator<String> iterator = new Iterator<>() { public boolean hasNext() { touched.incrementAndGet(); return false; } public String next() { return null; } };
        Enumeration<String> enumeration = new Enumeration<>() { public boolean hasMoreElements() { touched.incrementAndGet(); return false; } public String nextElement() { return null; } };
        Stream<String> stream = Stream.of("a", "b");
        for (Object value : new Object[] {lazy, iterator, enumeration, stream}) text(value);
        assertEquals(0, touched.get());
        assertEquals(2, stream.count(), "the stream is not consumed");
    }
    @Test void materialisedJdkCollectionsAreCaptured() {
        assertEquals("[\"a\",\"b\"]", text(new ArrayList<>(List.of("a", "b"))));
        assertEquals("[\"a\",\"b\"]", text(List.of("a", "b")));
        assertEquals("{\"k\":1}", text(new LinkedHashMap<>(Map.of("k", 1))));
        assertEquals("{\"k\":1}", text(Map.of("k", 1)));
        assertEquals("[\"a\"]", text(Collections.unmodifiableList(new ArrayList<>(List.of("a")))));
        assertEquals("[1,2]", text(new int[] {1, 2}));
        assertEquals("[\"x\"]", text(new String[] {"x"}));
    }
    @Test void charAndByteArraysAreOnePlaceholderEach() {
        assertEquals("\"<char[8]>\"", text("password".toCharArray()));
        assertEquals("\"<byte[3]>\"", text(new byte[] {1, 2, 3}));
    }
    @Test void aCyclicStructureIsBoundedAndDoesNotThrow() {
        List<Object> self = new ArrayList<>(); self.add(self); self.add("tail");
        assertEquals("[\"<cycle>\",\"tail\"]", text(self));
        List<Object> kids = new ArrayList<>(); Node node = new Node("n", kids); kids.add(node);
        String captured = text(node);
        assertTrue(captured.contains("<cycle>") && captured.length() < 200, captured);
    }
    @Test void aTenMegabyteGraphIsBoundedInTimeAndOutput() {
        String big = "lorem ipsum ".repeat(90_000);
        List<Object> graph = new ArrayList<>();
        for (int i = 0; i < 10; i++) graph.add(big);
        List<Object> wide = new ArrayList<>();
        for (int i = 0; i < 100; i++) { List<Object> row = new ArrayList<>(); for (int j = 0; j < 100; j++) row.add(Collections.nCopies(100, "item")); wide.add(row); }
        graph.add(wide);
        long start = System.nanoTime();
        String captured = text(graph);
        long millis = (System.nanoTime() - start) / 1_000_000;
        assertTrue(captured.length() <= 70_000, "output length " + captured.length());
        assertTrue(millis < 2000, "took " + millis + " ms");
    }
    @Test void aLongStringIsCutWhileItIsRead() {
        assertTrue(text("y".repeat(10_000_000)).length() <= 70_000);
    }
    @Test void recordsShowTheirComponentsAndBeansTheirPrivateFieldsNever() {
        assertEquals("{\"x\":1,\"y\":2}", text(new Point(1, 2)));
        String bean = text(new Bean());
        assertFalse(bean.contains("bean-private-secret"), bean);
        assertEquals("\"Described[ok]\"", text(new Described()));
        assertFalse(text(new Described()).contains("field-secret"));
    }
    @Test void aThrowingToStringNeverEscapes() {
        assertDoesNotThrow(() -> text(new Throwing()));
        assertDoesNotThrow(() -> text(List.of(new Throwing())));
    }
}
