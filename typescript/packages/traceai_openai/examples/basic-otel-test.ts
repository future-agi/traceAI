import { register, ProjectType, EvalSpanKind, EvalName, EvalTag, EvalTagType, ModelChoices } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { diag } from "@opentelemetry/api";

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
  console.log("fiBaseUrl", fiBaseUrl);
  // console.log(`Initializing FI tracer...`);
  if (fiBaseUrl) {
    // console.log(`Using custom FI endpoint: ${fiBaseUrl}`);
  }

  // 1. Register FI Core TracerProvider (sets up exporter)
  const tracerProvider = register({
    projectName: "ts-observability-suite-v5",
    projectType: ProjectType.EXPERIMENT,
    projectVersionName: "sarthak_f2",
    // sessionName: "basic-otel-test-session-" + Date.now(), // OBSERVE only
    evalTags: [
      new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.LLM,
        eval_name: EvalName.CHUNK_ATTRIBUTION,
        config: {},
        custom_eval_name: "Chunk_Attribution_5",
        mapping: {
          "context": "raw.input",
          "output": "raw.output"
        },
        model: ModelChoices.TURING_SMALL
      }),
      new EvalTag(
        {
          type: EvalTagType.OBSERVATION_SPAN,
          value: EvalSpanKind.LLM,
          eval_name: "toxic_nature",
          custom_eval_name: "toxic_nature_custom_eval_config_5",
          mapping: {
            "output": "raw.output"
          }
        }
      )
      // new EvalTag(
      //   {
      //     type: EvalTagType.OBSERVATION_SPAN,
      //     value: EvalSpanKind.LLM,
      //     eval_name: "custom-eval-1",
      //     custom_eval_name: "custom-eval-1-config_eval_2",
      //     mapping: {
      //       "output": "raw.output",
      //       "input": "raw.input"
      //     }
      //   }
      // ),
      // new EvalTag(
      //   {
      //     type: EvalTagType.OBSERVATION_SPAN,
      //     value: EvalSpanKind.LLM,
      //     eval_name: "detereministic_custom_eval_template",
      //     custom_eval_name: "detereministic_custom_eval_template_2",
      //     mapping: {
      //       "output": "raw.output",
      //       "query": "raw.input",
      //       "input": "raw.input"
      //     }
      //   }
      // )
      // ,
      // new EvalTag({
      //   type: EvalTagType.OBSERVATION_SPAN,
      //   value: EvalSpanKind.LLM,
      //   eval_name: EvalName.SUMMARY_QUALITY,
      //   config: {},
      //   custom_eval_name: "Summary_Quality_1",
      //   mapping: {
      //     "context": "raw.input",
      //     "output": "raw.output"
      //   }
      // })
    ]
  });

  // 2. Register OpenAI Instrumentation *BEFORE* importing/using OpenAI client
  // console.log("Registering OpenAI Instrumentation...");
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [new OpenAIInstrumentation()],
  });

  // 3. NOW Import and Initialize OpenAI Client
  const { OpenAI } = await import("openai");
  const openai = new OpenAI({
    apiKey: openaiApiKey,
  });

  // 4. Make an OpenAI API call
  // console.log("\n--- Making OpenAI chat completion call ---");
  try {
    const chatCompletion = await openai.chat.completions.create({
      messages: [{ role: "user", content: "Say this is a test for chat completions!" }],
      model: "gpt-4o",
    });
    // console.log("OpenAI Chat Completions API call successful:");
    // console.log(JSON.stringify(chatCompletion.choices[0], null, 2));
  } catch (error) {
    console.error("Error making OpenAI Chat Completions API call:", error);
  }

  // 4.1 Make an OpenAI Completions API call
  // console.log("\n--- Making OpenAI completions.create call ---");
  try {
    const completion = await openai.completions.create({
      prompt: "This is a test for legacy completions.",
      model: "gpt-3.5-turbo-instruct", // Correct model for legacy completions
    });
    // console.log("OpenAI Completions API call successful:");
    // console.log(JSON.stringify(completion.choices[0], null, 2));
  } catch (error) {
    console.error("Error making OpenAI Completions API call:", error);
  }

  // 4.2 Make an OpenAI Embeddings API call
  // console.log("\n--- Making OpenAI embeddings.create call ---");
  try {
    const embedding = await openai.embeddings.create({
      input: "This is a test for embeddings.",
      model: "text-embedding-3-small", // An embedding model
    });
    // console.log("OpenAI Embeddings API call successful (showing first embedding):");
    // // console.log(JSON.stringify(embedding.data[0], null, 2)); // Embeddings can be large
    // console.log(`Embedding for "This is a test for embeddings.": ${embedding.data[0].embedding.slice(0,5)}... [truncated]`);
    // console.log(`Total embeddings received: ${embedding.data.length}`);
  } catch (error) {
    console.error("Error making OpenAI Embeddings API call:", error);
  }

  // 4.3 Make an OpenAI Responses API call (conditionally, if available, as per api.md)
  // console.log("\n--- Attempting OpenAI responses.create call (as per api.md) ---");
  if (openai.responses && typeof openai.responses.create === 'function') {
    // console.log("Found openai.responses.create - attempting call.");
    try {
      const response = await openai.responses.create({
        input: [
          {
            type: "message",
            role: "user", 
            content: "Generate a response for the responses API (testing client.responses.create)."
          }
        ],
        model: "gpt-4o", // Assuming a compatible model; this might need adjustment
      });
      // console.log("OpenAI Responses API call (via client.responses.create) successful:");
      // console.log(JSON.stringify(response, null, 2));
    } catch (error) {
      console.error("Error making OpenAI Responses API call (via client.responses.create):", error);
    }
  } else {
    // console.log("openai.responses.create not found (as per api.md structure), skipping this call.");
    // Optionally, you could also try the other path here if you want to test both:
    // // console.log("Attempting (openai as any).OpenAI?.Responses?.prototype?.create as a fallback...");
    // if ((openai as any).OpenAI?.Responses?.prototype?.create && typeof (openai as any).OpenAI.Responses.prototype.create === 'function') { ... }
  }

  // 4.4 Make a streaming OpenAI Chat Completion call
  // console.log("\n--- Making OpenAI chat completion call (STREAMING) ---");
  try {
    const stream = await openai.chat.completions.create({
      messages: [{ role: "user", content: "Tell me a short story, in chunks." }],
      model: "gpt-4o",
      stream: true,
    });
    // console.log("OpenAI Chat Completions API call (STREAMING) initiated.");
    let fullStory = "";
    for await (const chunk of stream) {
      const content = chunk.choices[0]?.delta?.content || "";
      fullStory += content;
      // process.stdout.write(content); // Optionally print stream in real-time
    }
    // console.log("\nFull streamed story: ", fullStory);
    // console.log("OpenAI Chat Completions API call (STREAMING) successful and consumed.");
  } catch (error) {
    console.error("Error making OpenAI Chat Completions API call (STREAMING):", error);
  }

  // 5. Shutdown the provider to ensure spans are flushed
  // console.log("\n--- Shutting down tracer provider ---");
  try {
    await tracerProvider.shutdown();
    // console.log("Tracer provider shut down successfully.");
  } catch (error) {
    console.error("Error shutting down tracer provider:", error);
  }
}

main().catch((error) => {
  console.error("Unhandled error in main function:", error);
  process.exit(1);
}); 
