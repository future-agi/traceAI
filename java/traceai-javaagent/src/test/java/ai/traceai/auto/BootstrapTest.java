package ai.traceai.auto;

import java.io.*;
import java.nio.file.*;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class BootstrapTest {
    @Test void everyManifestEntrypointHasJava8Bytecode() throws Exception {
        var document = javax.xml.parsers.DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(Path.of("pom.xml").toFile());
        for (String entry : List.of("Premain-Class", "Agent-Class", "Main-Class")) {
            String name = document.getElementsByTagName(entry).item(0).getTextContent();
            try (var in = new DataInputStream(Objects.requireNonNull(getClass().getClassLoader().getResourceAsStream(name.replace('.', '/') + ".class")))) {
                assertEquals(0xcafebabe, in.readInt()); in.readUnsignedShort();
                assertEquals(52, in.readUnsignedShort(), entry + " must load before the Java 17 gate");
            }
        }
    }
    @Test void unsupportedRuntimeGateDoesNotInvokeImplementation() throws Exception {
        Class<?> bootstrap = Class.forName("ai.traceai.auto.AgentBootstrap");
        var gate = bootstrap.getDeclaredMethod("runIfSupported", String.class, Runnable.class); gate.setAccessible(true);
        var invoked = new java.util.concurrent.atomic.AtomicInteger();
        var err = System.err; var bytes = new ByteArrayOutputStream();
        try {
            System.setErr(new PrintStream(bytes));
            for (String version : Arrays.asList("1.8", "8", "11", "16", "garbage", null)) {
                gate.invoke(null, version, (Runnable)invoked::incrementAndGet);
                assertEquals(0, invoked.get(), "unsupported runtime must not link the real agent");
            }
            for (String version : List.of("17", "21", "27")) gate.invoke(null, version, (Runnable)invoked::incrementAndGet);
            assertEquals(3, invoked.get());
            assertEquals(6, bytes.toString().lines().count());
        } finally { System.setErr(err); }
    }
}
