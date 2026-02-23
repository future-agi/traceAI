/**
 * Example: Using HuggingFace instrumentation with TraceAI
 *
 * This example demonstrates how to set up and use the @traceai/huggingface
 * instrumentation to trace HuggingFace Inference API calls.
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { HuggingFaceInstrumentation } from "@traceai/huggingface";
import { HfInference } from "@huggingface/inference";

async function main() {
  // Step 1: Set up the tracer provider
  const provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  provider.register();

  // Step 2: Create and enable the instrumentation
  const instrumentation = new HuggingFaceInstrumentation({
    traceConfig: {
      // Optional: Configure tracing options
      // hideInputs: true,  // Hide input content in traces
      // hideOutputs: true, // Hide output content in traces
    },
  });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();

  // Step 3: Import and patch the HuggingFace module
  const hfModule = await import("@huggingface/inference");
  instrumentation.manuallyInstrument(hfModule);

  // Step 4: Create the HuggingFace client
  const client = new HfInference(process.env.HF_TOKEN);

  console.log("=== Text Generation Example ===\n");

  // Example 1: Text generation
  const textGenResponse = await client.textGeneration({
    model: "gpt2",
    inputs: "The quick brown fox",
    parameters: {
      max_new_tokens: 30,
      temperature: 0.7,
    },
  });

  console.log("Text Generation Response:", textGenResponse.generated_text);
  console.log("\n");

  console.log("=== Chat Completion Example ===\n");

  // Example 2: Chat completion
  const chatResponse = await client.chatCompletion({
    model: "meta-llama/Llama-2-7b-chat-hf",
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "What is 2 + 2?" },
    ],
    max_tokens: 50,
  });

  console.log("Chat Response:", chatResponse.choices[0]?.message?.content);
  console.log("\n");

  console.log("=== Streaming Chat Example ===\n");

  // Example 3: Streaming chat completion
  const stream = client.chatCompletionStream({
    model: "meta-llama/Llama-2-7b-chat-hf",
    messages: [
      { role: "user", content: "Count to 3." },
    ],
    max_tokens: 30,
  });

  process.stdout.write("Stream Response: ");
  for await (const chunk of stream) {
    const content = chunk.choices[0]?.delta?.content;
    if (content) {
      process.stdout.write(content);
    }
  }
  console.log("\n");

  console.log("=== Feature Extraction (Embeddings) Example ===\n");

  // Example 4: Feature extraction (embeddings)
  const embeddings = await client.featureExtraction({
    model: "sentence-transformers/all-MiniLM-L6-v2",
    inputs: "Hello, world!",
  });

  console.log("Embedding dimensions:", (embeddings as number[]).length);
  console.log("First 5 values:", (embeddings as number[]).slice(0, 5));
  console.log("\n");

  console.log("=== Summarization Example ===\n");

  // Example 5: Summarization
  const summaryText = `The tower is 324 metres (1,063 ft) tall, about the same height as an 81-storey building,
  and the tallest structure in Paris. Its base is square, measuring 125 metres (410 ft) on each side.
  During its construction, the Eiffel Tower surpassed the Washington Monument to become the tallest
  man-made structure in the world, a title it held for 41 years until the Chrysler Building in New York City
  was finished in 1930.`;

  const summaryResponse = await client.summarization({
    model: "facebook/bart-large-cnn",
    inputs: summaryText,
    parameters: {
      max_length: 100,
    },
  });

  console.log("Summary:", summaryResponse.summary_text);
  console.log("\n");

  console.log("=== Translation Example ===\n");

  // Example 6: Translation
  const translationResponse = await client.translation({
    model: "Helsinki-NLP/opus-mt-en-fr",
    inputs: "Hello, how are you?",
  });

  console.log("Translation (EN -> FR):", translationResponse.translation_text);
  console.log("\n");

  console.log("=== Question Answering Example ===\n");

  // Example 7: Question answering
  const qaResponse = await client.questionAnswering({
    model: "deepset/roberta-base-squad2",
    inputs: {
      question: "What is the capital of France?",
      context: "Paris is the capital and most populous city of France. It has been one of Europe's major centres of finance, diplomacy, commerce, fashion, science and arts.",
    },
  });

  console.log("Question: What is the capital of France?");
  console.log("Answer:", qaResponse.answer);
  console.log("Confidence Score:", qaResponse.score);
  console.log("\n");

  // Shutdown provider
  await provider.shutdown();
  console.log("Done! Check the console output above for trace spans.");
}

main().catch(console.error);
