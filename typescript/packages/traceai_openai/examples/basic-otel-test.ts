import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

// Enable OpenTelemetry internal diagnostics
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

async function main() {
  // FI Telemetry Credentials
  const fiApiKey = process.env.FI_API_KEY;
  const fiSecretKey = process.env.FI_SECRET_KEY;
  if (!fiApiKey || !fiSecretKey) {
    console.error(
      "FI_API_KEY and FI_SECRET_KEY environment variables must be set for telemetry.",
    );
    process.exit(1);
  }

  // OpenAI API Key
  const openaiApiKey = process.env.OPENAI_API_KEY;
  if (!openaiApiKey) {
    console.error("OPENAI_API_KEY environment variable must be set.");
    process.exit(1);
  }

  const fiBaseUrl = process.env.FI_BASE_URL;
  console.log(`Initializing FI tracer...`);
  if (fiBaseUrl) {
    console.log(`Using custom FI endpoint: ${fiBaseUrl}`);
  }

  // 1. Register FI Core TracerProvider (sets up exporter)
  const tracerProvider = register({
    projectName: "ts-observability-suite-v1",
    projectType: ProjectType.EXPERIMENT,
    projectVersionName: "1.0.1",
    verbose: true,
    ...(fiBaseUrl && { endpoint: fiBaseUrl }),
    batch: false, // Send spans immediately for testing
  });

  // 2. Register OpenAI Instrumentation *BEFORE* importing/using OpenAI client
  console.log("Registering OpenAI Instrumentation...");
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [new OpenAIInstrumentation()], // Using default config for OpenAIInstrumentation
  });

  // 3. NOW Import and Initialize OpenAI Client
  const OpenAI = (await import("openai")).default;
  const openai = new OpenAI({
    apiKey: openaiApiKey,
  });

  // 4. Make an OpenAI API call
  console.log("Making OpenAI chat completion call...");
  try {
    const chatCompletion = await openai.chat.completions.create({
      messages: [{ role: "user", content: "Say this is a test!" }],
      model: "gpt-4o",
    });
    console.log("OpenAI API call successful:");
    console.log(JSON.stringify(chatCompletion.choices[0], null, 2));
  } catch (error) {
    console.error("Error making OpenAI API call:", error);
  }

  // 5. Shutdown the provider to ensure spans are flushed
  console.log("Shutting down tracer provider...");
  try {
    await tracerProvider.shutdown();
    console.log("Tracer provider shut down successfully.");
  } catch (error) {
    console.error("Error shutting down tracer provider:", error);
  }
}

main().catch((error) => {
  console.error("Unhandled error in main function:", error);
  process.exit(1);
}); 