package ai.traceai.auto;

import java.nio.file.*;
import java.util.*;
import java.util.stream.Stream;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

class RoundSixUsageTest {
    @TestFactory Stream<DynamicTest> everySharedUsageProjection() throws Exception {
        List<Path> paths;
        try (var walk = Files.walk(WireFixturesTest.SPEC.resolve("fixtures"))) { paths = walk.filter(p -> p.toString().endsWith(".json")).sorted().toList(); }
        return paths.stream().filter(path -> {
            try {
                var expect = WireCore.map(WireCore.get(WireCore.parse(Files.readString(path)), "expect"));
                return WireCore.map(expect.get("attributes")).keySet().stream().anyMatch(k -> k.startsWith("gen_ai.usage."))
                    || WireCore.list(expect.get("absent")).stream().anyMatch(k -> String.valueOf(k).startsWith("gen_ai.usage."));
            } catch (Exception e) { throw new RuntimeException(e); }
        }).map(path -> DynamicTest.dynamicTest(path.getFileName().toString(), () -> {
            var fixture = WireCore.map(WireCore.parse(Files.readString(path))); var expected = WireCore.map(fixture.get("expect"));
            var request = WireFixturesTest.request(WireCore.map(fixture.get("request")));
            var result = WireCore.build(WireCore.match(request.method, request.url), request, WireFixturesTest.response(WireCore.map(fixture.get("response"))));
            assertNotNull(result, path.toString());
            WireCore.map(expected.get("attributes")).forEach((key, value) -> {
                if (key.startsWith("gen_ai.usage.")) assertEquals(WireCore.JSON.toJsonTree(value), WireCore.JSON.toJsonTree(result.attributes().get(key)), key);
            });
            for (Object key : WireCore.list(expected.get("absent"))) if (String.valueOf(key).startsWith("gen_ai.usage.")) assertFalse(result.attributes().containsKey(key), "unexpected " + key);
        }));
    }
}
