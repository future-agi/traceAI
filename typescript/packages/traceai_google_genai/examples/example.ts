/**
 * @traceai/google-genai Real-World Example
 *
 * This example demonstrates how to use the Google Generative AI (Gemini)
 * instrumentation with TraceAI for observability.
 *
 * Prerequisites:
 * 1. Set environment variables (see .env.example)
 * 2. Install dependencies: pnpm install
 * 3. Run: npx ts-node example.ts
 */

import { register, ProjectType } from "@traceai/fi-core";
import { GoogleGenAIInstrumentation } from "@traceai/google-genai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import "dotenv/config";

// Enable OpenTelemetry diagnostics for debugging
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

// Validate environment variables
const fiApiKey = process.env.FI_API_KEY;
const fiSecretKey = process.env.FI_SECRET_KEY;
const googleApiKey = process.env.GOOGLE_API_KEY;

if (!fiApiKey || !fiSecretKey) {
  console.error("FI_API_KEY and FI_SECRET_KEY environment variables must be set.");
  process.exit(1);
}

if (!googleApiKey) {
  console.error("GOOGLE_API_KEY environment variable must be set.");
  process.exit(1);
}

async function main() {
  console.log("Starting Google Generative AI Instrumentation Example...\n");

  // 1. Register TraceAI Core TracerProvider
  const tracerProvider = register({
    projectName: "google-genai-instrumentation-example",
    projectType: ProjectType.OBSERVE,
    sessionName: "google-genai-example-session-" + Date.now(),
  });
  console.log("TraceAI Core TracerProvider registered.");

  // 2. Register Google GenAI Instrumentation BEFORE importing the SDK
  registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [
      new GoogleGenAIInstrumentation({
        traceConfig: {
          hideInputs: false,
          hideOutputs: false,
        },
      }),
    ],
  });
  console.log("Google GenAI Instrumentation registered.\n");

  // 3. Dynamically import Google GenAI SDK AFTER instrumentation is registered
  const { GoogleGenerativeAI } = await import("@google/generative-ai");
  const genAI = new GoogleGenerativeAI(googleApiKey);

  try {
    // === Test Case 1: Basic Content Generation ===
    console.log("--- Test Case 1: Basic Content Generation ---");
    const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });

    const basicResult = await model.generateContent("What is the capital of Germany? Answer in one sentence.");
    const basicResponse = await basicResult.response;
    console.log("Response:", basicResponse.text());
    console.log();

    // === Test Case 2: Content with System Instruction ===
    console.log("--- Test Case 2: Content with System Instruction ---");
    const systemModel = genAI.getGenerativeModel({
      model: "gemini-1.5-flash",
      systemInstruction: "You are a helpful assistant that speaks like Shakespeare.",
    });

    const systemResult = await systemModel.generateContent("Tell me about the moon.");
    const systemResponse = await systemResult.response;
    console.log("Response:", systemResponse.text());
    console.log();

    // === Test Case 3: Multi-turn Chat ===
    console.log("--- Test Case 3: Multi-turn Chat ---");
    const chatModel = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
    const chat = chatModel.startChat({
      history: [
        {
          role: "user",
          parts: [{ text: "My name is Bob." }],
        },
        {
          role: "model",
          parts: [{ text: "Hello Bob! It's nice to meet you. How can I help you today?" }],
        },
      ],
    });

    const chatResult = await chat.sendMessage("What's my name?");
    const chatResponse = await chatResult.response;
    console.log("Response:", chatResponse.text());
    console.log();

    // === Test Case 4: Streaming Generation ===
    console.log("--- Test Case 4: Streaming Generation ---");
    const streamModel = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });

    const streamResult = await streamModel.generateContentStream("Write a short poem about the stars.");

    process.stdout.write("Streaming response: ");
    for await (const chunk of streamResult.stream) {
      const chunkText = chunk.text();
      process.stdout.write(chunkText);
    }
    console.log("\n");

    // === Test Case 5: Generation with Configuration ===
    console.log("--- Test Case 5: Generation with Configuration ---");
    const configModel = genAI.getGenerativeModel({
      model: "gemini-1.5-flash",
      generationConfig: {
        temperature: 0.9,
        topP: 0.95,
        topK: 40,
        maxOutputTokens: 200,
      },
    });

    const configResult = await configModel.generateContent("Write a creative story opening in 2 sentences.");
    const configResponse = await configResult.response;
    console.log("Response:", configResponse.text());
    console.log();

    // === Test Case 6: JSON Mode ===
    console.log("--- Test Case 6: JSON Mode ---");
    const jsonModel = genAI.getGenerativeModel({
      model: "gemini-1.5-flash",
      generationConfig: {
        responseMimeType: "application/json",
      },
    });

    const jsonResult = await jsonModel.generateContent(
      "List 3 planets with their approximate distance from the sun in AU. Return as JSON array with 'name' and 'distance_au' keys."
    );
    const jsonResponse = await jsonResult.response;
    console.log("JSON Response:", jsonResponse.text());
    console.log();

    // === Test Case 7: Function Calling ===
    console.log("--- Test Case 7: Function Calling ---");
    const functionModel = genAI.getGenerativeModel({
      model: "gemini-1.5-flash",
      tools: [
        {
          functionDeclarations: [
            {
              name: "get_weather",
              description: "Get the current weather in a given location",
              parameters: {
                type: "object",
                properties: {
                  location: {
                    type: "string",
                    description: "The city name, e.g. Tokyo, New York",
                  },
                  unit: {
                    type: "string",
                    enum: ["celsius", "fahrenheit"],
                    description: "The unit of temperature",
                  },
                },
                required: ["location"],
              },
            },
          ],
        },
      ],
    });

    const functionResult = await functionModel.generateContent("What's the weather in London?");
    const functionResponse = await functionResult.response;

    const functionCalls = functionResponse.functionCalls();
    if (functionCalls && functionCalls.length > 0) {
      console.log("Function calls:", JSON.stringify(functionCalls, null, 2));
    } else {
      console.log("Response:", functionResponse.text());
    }
    console.log();

    // === Test Case 8: Embeddings ===
    console.log("--- Test Case 8: Embeddings ---");
    const embeddingModel = genAI.getGenerativeModel({ model: "text-embedding-004" });

    const embeddingResult = await embeddingModel.embedContent("Hello, world!");
    console.log("Embedding dimensions:", embeddingResult.embedding.values.length);
    console.log("First 5 values:", embeddingResult.embedding.values.slice(0, 5));
    console.log();

    // === Test Case 9: Batch Embeddings ===
    console.log("--- Test Case 9: Batch Embeddings ---");
    const batchEmbeddingResult = await embeddingModel.batchEmbedContents({
      requests: [
        { content: { role: "user", parts: [{ text: "What is the meaning of life?" }] } },
        { content: { role: "user", parts: [{ text: "How do computers work?" }] } },
        { content: { role: "user", parts: [{ text: "Why is the sky blue?" }] } },
      ],
    });
    console.log("Batch embeddings count:", batchEmbeddingResult.embeddings.length);
    console.log("Each embedding dimensions:", batchEmbeddingResult.embeddings[0].values.length);
    console.log();

    console.log("--- All Test Cases Completed Successfully ---\n");

  } catch (error) {
    console.error("Error during Google GenAI API calls:", error);
  } finally {
    // 4. Shutdown the provider to ensure all spans are flushed
    console.log("Shutting down tracer provider...");
    await new Promise(resolve => setTimeout(resolve, 2000));
    await tracerProvider.shutdown();
    console.log("Tracer provider shut down successfully.");
  }
}

main().catch((error) => {
  console.error("Unhandled error:", error);
  process.exit(1);
});
