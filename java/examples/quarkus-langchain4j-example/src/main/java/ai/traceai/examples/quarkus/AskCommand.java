package ai.traceai.examples.quarkus;

import io.quarkus.runtime.QuarkusApplication;
import io.quarkus.runtime.annotations.QuarkusMain;
import jakarta.enterprise.context.control.ActivateRequestContext;
import jakarta.inject.Inject;

/**
 * Command-mode entry point: asks one question, prints the answer and exits. Quarkus shuts the OpenTelemetry SDK down
 * on exit, which flushes any span still queued.
 *
 * <pre>java -jar target/quarkus-app/quarkus-run.jar "What is the weather in Paris?"</pre>
 */
@QuarkusMain
public class AskCommand implements QuarkusApplication {

    @Inject
    Assistant assistant;

    @Override
    @ActivateRequestContext
    public int run(String... args) {
        String question = args.length > 0 ? String.join(" ", args) : "What is the weather in Paris right now?";
        try {
            System.out.println(assistant.chat(question));
            return 0;
        } catch (RuntimeException e) {
            // Only the exception type: the message can contain the prompt or the provider's error body.
            System.err.println("The AI service call failed: " + e.getClass().getName());
            return 1;
        }
    }
}
