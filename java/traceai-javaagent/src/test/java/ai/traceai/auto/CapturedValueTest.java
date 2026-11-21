package ai.traceai.auto;

import java.lang.reflect.Method;
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

    static final class SecretDate extends Date { SecretDate() { super(1_700_000_000_000L); } @Override public String toString() { return "date-with-SYNTH-SECRET"; } }
    @Test void anApplicationDateIsEpochMillisAndAnApplicationPathIsAPlaceholder() {
        assertEquals("\"1700000000000\"", text(new SecretDate()));
        java.nio.file.Path app = new java.nio.file.Path() {
            public java.nio.file.FileSystem getFileSystem() { return null; } public boolean isAbsolute() { return false; } public java.nio.file.Path getRoot() { return null; }
            public java.nio.file.Path getFileName() { return null; } public java.nio.file.Path getParent() { return null; } public int getNameCount() { return 0; }
            public java.nio.file.Path getName(int index) { return null; } public java.nio.file.Path subpath(int a, int b) { return null; }
            public boolean startsWith(java.nio.file.Path other) { return false; } public boolean endsWith(java.nio.file.Path other) { return false; }
            public java.nio.file.Path normalize() { return this; } public java.nio.file.Path resolve(java.nio.file.Path other) { return this; }
            public java.nio.file.Path relativize(java.nio.file.Path other) { return this; } public java.net.URI toUri() { return null; }
            public java.nio.file.Path toAbsolutePath() { return this; } public java.nio.file.Path toRealPath(java.nio.file.LinkOption... options) { return this; }
            public java.nio.file.WatchKey register(java.nio.file.WatchService w, java.nio.file.WatchEvent.Kind<?>[] k, java.nio.file.WatchEvent.Modifier... m) { return null; }
            public int compareTo(java.nio.file.Path other) { return 0; }
            @Override public String toString() { return "path-with-SYNTH-SECRET"; }
        };
        String captured = text(app);
        assertFalse(captured.contains("SYNTH"), captured);
        assertTrue(captured.startsWith("\"<"), captured);
        assertEquals("\"/tmp/x\"", text(java.nio.file.Path.of("/tmp/x")));
    }

    @Test void inputNamesCookiesFlagsAndUserInfoAreRedactedInPlainText() {
        for (String[] c : new String[][] {
            {"Cookie: a=SYNTHONE; b=SYNTHTWO; c=SYNTHTHREE", "SYNTH"}, {"Set-Cookie: sid=SYNTHSID; Path=/", "SYNTH"},
            {"proxy-authorization: Digest username=\"SYNTHU\", response=\"SYNTHR\"", "SYNTH"}, {"run --passphrase SYNTHFLAG now", "SYNTH"},
            {"x user[pwd]=SYNTHBR&a=1", "SYNTH"}, {"jdbc:postgresql://u:SYNTHUI@h:5432/db", "SYNTH"}, {"{\"pass\": \"SYNTHJ\", \"n\": 1}", "SYNTH"}, {"run --password  SYNTHTWOSP now", "SYNTH"}, {"run --password\tSYNTHTAB now", "SYNTH"}, {"run --password \"SYNTH multi word\" now", "SYNTH"},
            {"{'password': 'SYNTHSQ', 'user': 'bob'}", "SYNTH"}, {"postgres://admin:SYNTHPA/SS@db.host/app", "SYNTH"}, {"{\"pin\":{\"a\":\"SYNTHNEST\"},\"n\":1}", "SYNTH"}}) {
            String captured = text(c[0]);
            assertFalse(captured.contains(c[1]), c[0] + " -> " + captured);
        }
        assertEquals("\"{\\\"pass\\\":\\\"<redacted>\\\",\\\"n\\\":1}\"", text("{\"pass\":\"x\",\"n\":1}"));
        assertEquals("\"note=keep, id=7\"", text("note=keep, id=7"));
    }

    // ---- bounded scans ---------------------------------------------------------------------------------------------------

    /** A user-information search never looks back past the start of the token it belongs to: the work is linear in the text. */
    @Test void userInformationAndJsonNameScansAreLinear() {
        Map<String, String> inputs = new LinkedHashMap<>();
        inputs.put("scheme separators with no at sign", "://".repeat(50_000));
        inputs.put("scheme separators and a late at sign", "://".repeat(50_000) + "@");
        inputs.put("scheme separators in a word", "x://".repeat(40_000));
        inputs.put("json names", "x\": ".repeat(40_000));
        inputs.put("single-quoted json names", "x': ".repeat(40_000));
        inputs.put("a long name before every colon", ("\"" + "n".repeat(120) + "\": ").repeat(1_500));
        inputs.forEach((label, value) -> {
            String redacted = assertTimeoutPreemptively(java.time.Duration.ofMillis(1_000), () -> CapturedValue.redactCredentialPairs(value), label + " is not linear");
            assertTrue(redacted.length() <= value.length() + 64, label);
            String captured = assertTimeoutPreemptively(java.time.Duration.ofMillis(1_000), () -> text(value), label + " (as captured) is not bounded");
            assertTrue(captured.length() <= 140_000, label + ": " + captured.length()); // the budget, doubled for the escapes of a JSON string
        });
    }

    // ---- authorization schemes with no name in front ------------------------------------------------------------------------

    @Test void negotiateDigestAndNtlmValuesAreReplacedWithNoNameInFrontOfThem() {
        String token = java.util.Base64.getEncoder().encodeToString("SYNTHNEGOTIATE0123456789".getBytes());
        Map<String, String[]> inputs = new LinkedHashMap<>();
        inputs.put("Negotiate " + token + ",", new String[] {token, token.substring(0, 12)});
        inputs.put("Negotiate " + token, new String[] {token.substring(0, 12)});
        inputs.put("see NTLM " + token + " end", new String[] {token.substring(0, 12)});
        inputs.put("Digest username=\"SYNTHDU\", realm=\"r\", nonce=\"SYNTHDN\", response=\"SYNTHDR\"", new String[] {"SYNTHDU", "SYNTHDN", "SYNTHDR"});
        inputs.put("x Digest username=SYNTHDU2, nonce=SYNTHDN2, response=SYNTHDR2 y", new String[] {"SYNTHDU2", "SYNTHDN2", "SYNTHDR2"});
        inputs.forEach((value, markers) -> {
            String captured = text(value);
            for (String marker : markers) assertFalse(captured.contains(marker), value + " -> " + captured);
            assertTrue(captured.contains("<redacted>"), captured);
        });
        assertEquals("\"the digest of the report\"", text("the digest of the report"));
    }

    // ---- map keys ------------------------------------------------------------------------------------------------------------

    static final AtomicInteger KEY_CALLS = new AtomicInteger();
    static final class CountedHolder {
        @Override public String toString() { KEY_CALLS.incrementAndGet(); return "SYNTHKEYTEXT"; }
        @Override public int hashCode() { KEY_CALLS.incrementAndGet(); return 7; }
        @Override public boolean equals(Object other) { KEY_CALLS.incrementAndGet(); return other == this; }
    }
    static final class CountedNumeric extends Number {
        @Override public String toString() { KEY_CALLS.incrementAndGet(); throw new IllegalStateException("toString"); }
        @Override public int hashCode() { KEY_CALLS.incrementAndGet(); return 9; }
        @Override public boolean equals(Object other) { KEY_CALLS.incrementAndGet(); return other == this; }
        @Override public int intValue() { return 1; } @Override public long longValue() { return 1; }
        @Override public float floatValue() { return 1; } @Override public double doubleValue() { return 1; }
    }
    enum Mode { FAST }

    @Test void anApplicationKeyIsRedactedAndNoMethodOfItRuns() {
        Map<Object, Object> map = new LinkedHashMap<>();
        map.put(new CountedHolder(), "kept-value-1");
        map.put(new CountedNumeric(), "kept-value-2");
        KEY_CALLS.set(0);
        String captured = text(map);
        assertEquals(0, KEY_CALLS.get(), "application code ran for a key: " + captured);
        assertFalse(captured.contains("SYNTHKEYTEXT"), captured);
        assertTrue(captured.contains("CountedHolder") && captured.contains("CountedNumeric"), captured);
        assertTrue(captured.contains("kept-value-1"), captured);
    }

    @Test void onlyKnownJdkValueTypesAreShownAsKeys() {
        Map<Object, Object> map = new LinkedHashMap<>();
        map.put(1, "a"); map.put(2L, "b"); map.put((short)3, "c"); map.put((byte)4, "d"); map.put(1.5d, "e"); map.put(2.5f, "f");
        map.put(true, "g"); map.put('c', "h"); map.put("text", "i"); map.put(Mode.FAST, "j");
        map.put(java.util.UUID.fromString("123e4567-e89b-12d3-a456-426614174000"), "k");
        String captured = text(map);
        for (String key : List.of("\"1\"", "\"2\"", "\"3\"", "\"4\"", "\"1.5\"", "\"2.5\"", "\"true\"", "\"c\"", "\"text\"", "\"FAST\"", "\"123e4567-e89b-12d3-a456-426614174000\""))
            assertTrue(captured.contains(key + ":"), key + " in " + captured);
        Map<Object, Object> others = new LinkedHashMap<>();
        others.put(new java.math.BigInteger("12345678901234567890"), "x");
        others.put(new java.util.concurrent.atomic.AtomicInteger(5), "y");
        others.put(java.nio.file.Path.of("/tmp/SYNTHPATHKEY"), "z");
        others.put(java.time.Instant.parse("2026-01-01T00:00:00Z"), "w");
        String shown = text(others);
        for (String hidden : List.of("12345678901234567890", "SYNTHPATHKEY", "2026-01-01")) assertFalse(shown.contains(hidden), shown);
        assertTrue(shown.contains("<java.math.BigInteger>"), shown);
    }

    @Test void aStringKeyGoesThroughTheStringPipeline() {
        String secret = "SYNTH-KEY-SECRET-0123456789";
        KnownSecrets.PROCESS.register(secret);
        Map<Object, Object> map = new LinkedHashMap<>();
        map.put("lookup " + secret + " done", "v");
        map.put("password=SYNTHKEYPW id=1", "w");
        String captured = text(map);
        assertFalse(captured.contains(secret), captured);
        assertFalse(captured.contains("SYNTHKEYPW"), captured);
        assertTrue(captured.contains("lookup <redacted> done"), captured);
    }

    // ---- the cut and a shape that straddles it --------------------------------------------------------------------------------

    /** Secrets shrink the read window, so the cut ends up well inside the window: a JWT that begins before the cut and ends past the window leaves no prefix. */
    @Test void aJwtThatStraddlesTheCutAfterShrinkingSecretsLeavesNoByte() {
        String secret = "T".repeat(4096);
        KnownSecrets.PROCESS.register(secret);
        String filler = "f ".repeat(430);
        String jwt = "eyJ" + "a".repeat(1500) + "." + "b".repeat(1500) + "." + "c".repeat(100);
        String value = secret + " " + secret + " " + filler + jwt;
        Object captured = CapturedValue.of(value, 1000);
        String shown = String.valueOf(captured);
        assertFalse(shown.contains("eyJ"), shown);
        assertFalse(shown.contains("aaaa"), shown);
        assertTrue(shown.endsWith(CapturedValue.CUT), shown);
    }

    // ---- parameters compiled without -parameters ---------------------------------------------------------------------------------

    private static Method compiled(boolean withParameters, String source, String name, Class<?>... types) throws Exception {
        Path dir = Files.createTempDirectory("traceai-params");
        try {
            Path file = dir.resolve("Compiled" + (withParameters ? "With" : "Without") + ".java");
            Files.writeString(file, source.replace("CLASSNAME", "Compiled" + (withParameters ? "With" : "Without")));
            javax.tools.JavaCompiler compiler = javax.tools.ToolProvider.getSystemJavaCompiler();
            assertNotNull(compiler, "a JDK is needed to compile the fixture");
            List<String> options = new ArrayList<>(List.of("-d", dir.toString()));
            if (withParameters) options.add("-parameters");
            options.add(file.toString());
            assertEquals(0, compiler.run(null, null, null, options.toArray(new String[0])));
            try (java.net.URLClassLoader loader = new java.net.URLClassLoader(new java.net.URL[] {dir.toUri().toURL()}, CapturedValueTest.class.getClassLoader())) {
                Class<?> type = loader.loadClass("Compiled" + (withParameters ? "With" : "Without"));
                // Loaded eagerly: the class is read while its directory still exists.
                return type.getMethod(name, types);
            }
        } finally {
            try (Stream<Path> files = Files.walk(dir)) { files.sorted(Comparator.reverseOrder()).forEach(path -> path.toFile().delete()); }
        }
    }
    private static final String LOGIN = "public class CLASSNAME { public String login(String password, int attempts, char[] pin, byte[] key, StringBuilder note, CharSequence phrase, Object other) { return \"ok\"; } public String describe(String note, int count) { return \"ok\"; } }";

    @SuppressWarnings("unchecked")
    static Map<String, Object> inputValues(Method method, Object... arguments) throws Exception {
        Method read = AnnotationTracing.class.getDeclaredMethod("inputValues", Method.class, Object[].class);
        read.setAccessible(true);
        return (Map<String, Object>)read.invoke(null, method, arguments);
    }
    static void resetNameWarning() throws Exception {
        Method reset = AnnotationTracing.class.getDeclaredMethod("resetNameWarning");
        reset.setAccessible(true);
        reset.invoke(null);
    }

    @Test void textArgumentsAreRedactedWhenTheClassWasCompiledWithoutParameterNames() throws Exception {
        Method login = compiled(false, LOGIN, "login", String.class, int.class, char[].class, byte[].class, StringBuilder.class, CharSequence.class, Object.class);
        assertFalse(login.getParameters()[0].isNamePresent(), "the fixture must be compiled without -parameters");
        resetNameWarning();
        Map<String, Object> values = inputValues(login, "SYNTHPASSWORD", 3, "SYNTHPIN".toCharArray(), "SYNTHKEY".getBytes(), new StringBuilder("SYNTHNOTE"), (CharSequence)"SYNTHPHRASE", "SYNTHOTHER");
        String shown = AnnotationTracing.render(values, 4096);
        for (String secret : List.of("SYNTHPASSWORD", "SYNTHPIN", "SYNTHKEY", "SYNTHNOTE", "SYNTHPHRASE")) assertFalse(shown.contains(secret), shown);
        assertTrue(shown.contains("\"arg1\":3"), "a number keeps its value: " + shown);
    }

    @Test void theNameWarningIsOnePerProcessAndNamesTheFixAndNoValue() throws Exception {
        Method describe = compiled(false, LOGIN, "describe", String.class, int.class);
        resetNameWarning();
        java.io.PrintStream saved = System.err;
        java.io.ByteArrayOutputStream captured = new java.io.ByteArrayOutputStream();
        try {
            System.setErr(new java.io.PrintStream(captured, true));
            for (int i = 0; i < 5; i++) inputValues(describe, "SYNTHWARNVALUE" + i, i);
        } finally { System.setErr(saved); }
        String log = captured.toString();
        long lines = log.lines().filter(line -> line.contains("-parameters")).count();
        assertEquals(1, lines, log);
        assertFalse(log.contains("SYNTHWARNVALUE"), log);
    }

    @Test void parameterNamesInTheClassFileAreJudgedByNameAndNoWarningIsPrinted() throws Exception {
        Method login = compiled(true, LOGIN, "login", String.class, int.class, char[].class, byte[].class, StringBuilder.class, CharSequence.class, Object.class);
        assertTrue(login.getParameters()[0].isNamePresent());
        resetNameWarning();
        java.io.PrintStream saved = System.err;
        java.io.ByteArrayOutputStream captured = new java.io.ByteArrayOutputStream();
        Map<String, Object> values;
        try {
            System.setErr(new java.io.PrintStream(captured, true));
            values = inputValues(login, "SYNTHPASSWORD", 3, "x".toCharArray(), "y".getBytes(), new StringBuilder("keep-note"), (CharSequence)"keep-phrase", "keep-other");
        } finally { System.setErr(saved); }
        String shown = AnnotationTracing.render(values, 4096);
        assertFalse(shown.contains("SYNTHPASSWORD"), shown);
        assertTrue(shown.contains("keep-other"), shown);
        assertEquals("", captured.toString());
    }
}
