package ai.traceai.apptest;

import java.io.File;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * Runs {@link AnnotatedApp} in a class loader of its own whose parent is the platform loader, the way a fat jar or a web
 * application keeps the agent jar away from the system class path. args[0] is the scenario, args[1] the class path of that loader.
 */
public final class IsolatedLauncher {
    public static void main(String[] args) throws Exception {
        List<URL> urls = new ArrayList<>();
        for (String entry : args[1].split(java.util.regex.Pattern.quote(File.pathSeparator))) urls.add(Path.of(entry).toUri().toURL());
        try (URLClassLoader loader = new URLClassLoader(urls.toArray(new URL[0]), ClassLoader.getPlatformClassLoader())) {
            Thread.currentThread().setContextClassLoader(loader);
            Class<?> app = loader.loadClass("ai.traceai.apptest.AnnotatedApp");
            try { ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto"); System.out.println("AGENT_ON_SYSTEM_CLASS_PATH"); } catch (ClassNotFoundException expected) { }
            app.getMethod("main", String[].class).invoke(null, (Object) new String[] {args[0]});
        }
    }
    private IsolatedLauncher() { }
}
