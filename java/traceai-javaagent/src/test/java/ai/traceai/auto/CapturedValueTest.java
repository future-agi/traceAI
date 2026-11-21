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
        // An application object's own toString() is never exported as text: it is its type placeholder.
        assertEquals("\"<ai.traceai.auto.CapturedValueTest$Described>\"", text(new Described()));
    }
    @Test void aThrowingToStringNeverEscapes() {
        assertDoesNotThrow(() -> text(new Throwing()));
        assertDoesNotThrow(() -> text(List.of(new Throwing())));
    }

    /** A Collections wrapper is judged by nothing it wraps: it is a type placeholder and is never iterated, locked or read. */
    @Test void collectionsWrappersArePlaceholdersAndNeverIterated() {
        AtomicInteger iterators = new AtomicInteger();
        Collection<String> lazy = new AbstractCollection<>() {
            @Override public Iterator<String> iterator() { iterators.incrementAndGet(); return List.of("row").iterator(); }
            @Override public int size() { iterators.incrementAndGet(); return 1; }
        };
        List<Object> wrappers = List.of(Collections.unmodifiableCollection(lazy), Collections.synchronizedCollection(lazy), Collections.checkedCollection(lazy, String.class));
        for (Object wrapper : wrappers) {
            String captured = text(wrapper);
            assertTrue(captured.startsWith("\"<java.util.Collections$"), captured);
        }
        assertEquals(0, iterators.get(), "no iterator() or size() call");
        assertEquals("\"<java.util.Collections$UnmodifiableRandomAccessList>\"", text(Collections.unmodifiableList(new ArrayList<>(List.of("a")))));
        assertEquals("\"<java.util.Collections$SynchronizedMap>\"", text(Collections.synchronizedMap(new HashMap<>())));
    }

    static final class Counting { static final AtomicInteger CALLS = new AtomicInteger(); @Override public String toString() { CALLS.incrementAndGet(); return "Counting[" + "x".repeat(5000) + "]"; } }
    record Account(String name, Counting token) { }
    static final class Accessors { static final AtomicInteger CALLS = new AtomicInteger(); }
    record Metered(String value) { public String value() { Accessors.CALLS.incrementAndGet(); return value; } }

    /** A declared toString() and record accessors run at most once per capture, however many times the text is cut again. */
    /** Control characters take six characters each as JSON, so the text is cut again and again: render() must retry at least twice. */
    static final class Escapy { static final AtomicInteger CALLS = new AtomicInteger(); @Override public String toString() { CALLS.incrementAndGet(); return "\u0001".repeat(5000); } }
    record EscapyRecord(String value) { public String value() { Accessors.CALLS.incrementAndGet(); return "\u0002".repeat(5000); } }
    @Test void toStringAndAccessorsRunOnceAcrossTheRenderRetries() {
        Escapy.CALLS.set(0); Accessors.CALLS.set(0);
        String json = AnnotationTracing.render(new Escapy(), 100);
        assertEquals("\"<ai.traceai.auto.CapturedValueTest$Escapy>\"", json);
        assertEquals(0, Escapy.CALLS.get(), "toString() is never run");
        String record = AnnotationTracing.render(new EscapyRecord("x"), 100);
        assertDoesNotThrow(() -> com.google.gson.JsonParser.parseString(record));
        assertEquals(1, Accessors.CALLS.get(), "the accessor once");
    }

    static final class Lombok { @Override public String toString() { return "User(name=alice, password=hunter2-synthetic, apiToken: tok-synthetic-9, nested=Inner(secret=inner-synthetic-3, id=7))"; } }

    /** Text from a declared toString() gets the credential-name redaction that maps and records get. */
    @Test void aCredentialNamedPairInAPlainStringIsRedactedByName() {
        String captured = text(new Lombok().toString());
        for (String leaked : List.of("hunter2-synthetic", "tok-synthetic-9", "inner-synthetic-3")) assertFalse(captured.contains(leaked), captured);
        assertTrue(captured.contains("name=alice") && captured.contains("id=7"), "ordinary pairs stay: " + captured);
        assertTrue(captured.contains("password=<redacted>"), captured);
    }

    @Test void aToStringThatFailsOrYieldsNullIsAPlaceholder() {
        assertDoesNotThrow(() -> text(new Throwing()));
        assertTrue(text(new Throwing()).startsWith("\"<"), text(new Throwing()));
        Object nothing = new Object() { @Override public String toString() { return null; } };
        assertDoesNotThrow(() -> text(nothing));
    }

    /** Numbers are charged against the text limit while the tree is walked. */
    @Test void numbersAreChargedAgainstTheTextLimit() {
        String captured = new com.google.gson.GsonBuilder().disableHtmlEscaping().create().toJson(CapturedValue.of(Collections.nCopies(100, 1234567890123L), 64));
        assertTrue(captured.length() < 400, "length " + captured.length() + ": " + captured);
        assertTrue(captured.contains(CapturedValue.CUT), captured);
    }

    /** render(): always valid JSON, never longer than the limit, whatever the value. */
    @Test void renderIsAlwaysValidJsonWithinTheLimit() {
        for (int limit : new int[] {16, 64, 300, 5000}) {
            for (Object value : new Object[] {"x".repeat(100_000), Map.of("k", "v".repeat(10_000)), new ArrayList<>(Collections.nCopies(500, "item")), null, 42, new Point(1, 2)}) {
                String json = AnnotationTracing.render(value, limit);
                assertTrue(json.length() <= limit || json.length() <= CapturedValue.CUT.length() + 2, "limit " + limit + " length " + json.length());
                assertDoesNotThrow(() -> com.google.gson.JsonParser.parseString(json), json);
            }
        }
    }

    /** A secret that straddles the cut of a string is replaced before the cut, so no part of it remains. */
    @Test void aKnownSecretStraddlingTheTextCutIsReplacedBeforeTheCut() {
        String secret = "STRADDLE-SECRET-VALUE-0042";
        KnownSecrets.PROCESS.register(secret);
        String value = "a".repeat(40) + secret + "tail".repeat(100);
        for (int budget : new int[] {16, 41, 44, 50, 60, 66}) {
            String captured = new com.google.gson.GsonBuilder().create().toJson(CapturedValue.of(value, budget));
            assertFalse(captured.contains("STRADDLE"), "budget " + budget + ": " + captured);
            assertFalse(captured.contains("-SECRET"), "budget " + budget + ": " + captured);
        }
    }

    /** One annotation transformer per JVM: the first claim wins, whichever copy of the engine asks. */
    @Test void theAnnotationTransformerIsClaimedOncePerJvm() {
        System.getProperties().remove(TraceAIAgent.TRANSFORMER_CLAIM);
        assertTrue(TraceAIAgent.claimAnnotationTransformer());
        assertFalse(TraceAIAgent.claimAnnotationTransformer());
        assertFalse(TraceAIAgent.claimAnnotationTransformer());
        System.getProperties().remove(TraceAIAgent.TRANSFORMER_CLAIM);
        assertTrue(TraceAIAgent.claimAnnotationTransformer());
        System.getProperties().remove(TraceAIAgent.TRANSFORMER_CLAIM);
    }

    /** The value of a credential-named pair is redacted whole: through its matching close, its pair boundary, its PEM END line. */
    static final class Fixed { final String text; Fixed(String text) { this.text = text; } @Override public String toString() { return text; } }
    @Test void aCredentialValueIsRedactedWholeWhateverItsShape() {
        Map<String, String[]> cases = new LinkedHashMap<>();
        cases.put("Account(user=bob, password=[h, u, n, t, e, r], id=7)", new String[] {"h, u", "n, t", "e, r", "]"});
        cases.put("Cfg(secret=a,b,c, id=7)", new String[] {",b", "b,c"});
        cases.put("Cfg(apiKey=Inner(value=x, other=y), id=7)", new String[] {"value=x", "other=y"});
        cases.put("K(privateKey=-----BEGIN PRIVATE KEY-----\nMIIsynthetic\nline2\n-----END PRIVATE KEY-----, id=7)", new String[] {"MIIsynthetic", "line2", "BEGIN", "END PRIVATE"});
        cases.put("Q(token=\"a,b\", id=7)", new String[] {"a,b", "\\\"a"});
        cases.put("Esc(secret=\"a\\\"b,c\", id=7)", new String[] {"b,c"});
        cases.put("Brace(credentials={k=v, k2=v2}, id=7)", new String[] {"k=v", "k2"});
        cases.put("Unclosed(password=[h, u, n, id=7", new String[] {"h, u", "id=7"});
        cases.forEach((text, leaked) -> {
            String captured = text(text);
            for (String fragment : leaked) assertFalse(captured.contains(fragment), "'" + fragment + "' left in " + captured);
            assertTrue(captured.contains("<redacted>"), captured);
            if (!text.startsWith("Unclosed")) assertTrue(captured.contains("id=7"), "ordinary pairs stay: " + captured);
        });
    }

    /** A registered secret that appears twice around the cut leaves no fragment, at every cut position. */
    @Test void aKnownSecretTwiceAroundTheCutLeavesNoFragment() {
        String secret = "TWICE-SYNTH-SECRET-VALUE-0042";
        KnownSecrets.PROCESS.register(secret);
        String value = "a".repeat(30) + secret + "-b-" + secret + "c".repeat(200);
        for (int budget = 16; budget < 30 + 2 * secret.length() + 10; budget++) {
            String captured = new com.google.gson.GsonBuilder().disableHtmlEscaping().create().toJson(CapturedValue.of(value, budget));
            assertFalse(captured.contains("TWICE"), "budget " + budget + ": " + captured);
            assertFalse(captured.contains("-SYNTH"), "budget " + budget + ": " + captured);
            assertFalse(captured.contains("SYNTH-S"), "budget " + budget + ": " + captured);
        }
    }

    /** Shaped credentials (a JWT, an sk- key, a Bearer token) cut short would no longer match: they are replaced before the cut. */
    @Test void shapedCredentialsStraddlingTheCutLeaveNoPrefix() {
        String jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJTWU5USEVUSUMifQ.SYNTHSIGNATUREAAAAAAAAAAAAAA";
        String sk = "sk-SYNTHKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
        String bearer = "Bearer SYNTHBEARERTOKENAAAAAAAAAAAAAAAA";
        for (String credential : List.of(jwt, sk, bearer)) {
            String value = "a".repeat(40) + " " + credential + " " + "c".repeat(300);
            for (int budget = 16; budget < 41 + credential.length() + 6; budget++) {
                String captured = new com.google.gson.GsonBuilder().disableHtmlEscaping().create().toJson(CapturedValue.of(value, budget));
                assertFalse(captured.contains("eyJ"), "budget " + budget + ": " + captured);
                assertFalse(captured.contains("sk-S"), "budget " + budget + ": " + captured);
                assertFalse(captured.contains("SYNTH"), "budget " + budget + ": " + captured);
            }
        }
    }

    @Test void aUriShowsNeitherUserInfoNorQueryValues() {
        String captured = text(java.net.URI.create("https://user:pw-synthetic@example.com:8443/path/x?token=abc-synthetic&x=1#frag-synthetic"));
        for (String leaked : List.of("user", "pw-synthetic", "abc-synthetic", "frag-synthetic", "x=1")) assertFalse(captured.contains(leaked), leaked + " in " + captured);
        assertTrue(captured.contains("example.com") && captured.contains("/path/x") && captured.contains("token"), captured);
    }

    /** A huge string is read once, in time that does not grow with its length, and what is exported is bounded. */
    @Test void hugeStringsAreReadOnceInBoundedTime() {
        String secret = "H".repeat(4096);
        KnownSecrets.PROCESS.register(secret);
        Map<String, String> inputs = new LinkedHashMap<>();
        inputs.put("pairs", ("password=x(" + "filler ".repeat(3)).repeat(5_000_000 / 32));
        inputs.put("dashed name run", "a-".repeat(2_500_000));
        inputs.put("shrinking secrets", (secret + " ").repeat(1200));
        inputs.put("separators", ", ".repeat(2_500_000));
        inputs.forEach((label, value) -> {
            String captured = assertTimeoutPreemptively(java.time.Duration.ofSeconds(1), () -> text(value), label + " took longer than a second");
            assertTrue(captured.length() <= 70_000, label + ": " + captured.length());
        });
    }

    @Test void aSingleEqualsPaddingNeverStartsAPairInsideACredentialValue() {
        String basic = java.util.Base64.getEncoder().encodeToString("user:SYNTHB64PASS".getBytes());
        for (String text : List.of("authorization=Basic " + basic + ", id=5", "Authorization: Basic " + basic + ")", "authorization=Negotiate " + basic + ",", "authorization=Basic " + basic + "; id=5", "authorization=Basic " + basic + "\", id=5")) {
            String captured = text(text);
            assertFalse(captured.contains(basic.substring(0, 12)), captured);
        }
    }
}
