package ai.traceai.examples.quarkus;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.quarkus.arc.Arc;
import io.quarkus.test.common.QuarkusTestResource;
import io.quarkus.test.junit.QuarkusTest;
import java.nio.file.Path;
import java.util.jar.JarFile;
import org.junit.jupiter.api.Test;

/** Exercise discovery from the installed consumer JAR, not reactor classes or copied application helpers. */
@QuarkusTest
@QuarkusTestResource(StandIns.class)
class PackagedAdapterTest {

    @Test
    void discoversAllContributorsFromTheIndexedDependency() throws Exception {
        for (String name : new String[] {"FutureAgiTracingDefaults", "FutureAgiTracingDefaults$DropHostName", "ToolErrorStatus"}) {
            Class<?> type = Class.forName("ai.traceai.quarkus.langchain4j." + name);
            assertTrue(Arc.container().instance(type).isAvailable(), name + " must survive CDI bean removal");
            Path source = Path.of(type.getProtectionDomain().getCodeSource().getLocation().toURI());
            assertTrue(source.getFileName().toString().startsWith("traceai-java-quarkus-langchain4j-"), source.toString());
            assertTrue(source.toString().endsWith(".jar"), "tests must consume the packaged artifact");
            try (JarFile jar = new JarFile(source.toFile())) {
                assertNotNull(jar.getJarEntry("META-INF/jandex.idx"), "CDI discovery must not require customer indexing");
            }
        }
    }

    @Test
    void applicationContainsNoCopiedContributors() {
        assertThrows(ClassNotFoundException.class,
                () -> Class.forName("ai.traceai.examples.quarkus.FutureAgiTracingDefaults"));
        assertThrows(ClassNotFoundException.class,
                () -> Class.forName("ai.traceai.examples.quarkus.ToolErrorStatus"));
    }
}
