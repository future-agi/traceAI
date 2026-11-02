/**
 * Compatibility example for the legacy @google/generative-ai SDK only.
 * See ../README.md for supported operations and migration guidance.
 * Set FI_API_KEY, FI_SECRET_KEY, GOOGLE_API_KEY and GOOGLE_MODEL before running.
 */
import { register, ProjectType } from "@traceai/fi-core";
import { GoogleGenAIInstrumentation } from "@traceai/google-genai";
import * as google from "@google/generative-ai";

async function main() {
  const apiKey = process.env.GOOGLE_API_KEY;
  const modelName = process.env.GOOGLE_MODEL;
  if (!apiKey || !modelName || !process.env.FI_API_KEY || !process.env.FI_SECRET_KEY) {
    throw new Error("Set FI_API_KEY, FI_SECRET_KEY, GOOGLE_API_KEY and GOOGLE_MODEL.");
  }

  const provider = register({
    projectName: "gemini-legacy-app",
    projectType: ProjectType.OBSERVE,
  });
  const instrumentation = new GoogleGenAIInstrumentation();
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(google);

  try {
    const client = new google.GoogleGenerativeAI(apiKey);
    const model = client.getGenerativeModel({ model: modelName });
    const result = await model.generateContent("Explain quantum computing in one sentence.");
    console.log(result.response.text());
  } finally {
    await provider.shutdown();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
