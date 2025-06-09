import { register, ProjectType } from "@traceai/fi-core";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

import * as dotenv from 'dotenv';
dotenv.config();


// Enable OpenTelemetry internal diagnostics
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

// 1. Register FI Core TracerProvider (sets up exporter)
const tracerProvider = register({
  projectName: "test-4",
  projectType: ProjectType.OBSERVE,
  sessionName: "test-session-" + Date.now(),
});

console.log("Tracer provider initialized",tracerProvider);
// 2. Register OpenAI Instrumentation *BEFORE* importing/using OpenAI client
console.log("Registering OpenAI Instrumentation...");
registerInstrumentations({
  tracerProvider: tracerProvider as any,
  instrumentations: [new OpenAIInstrumentation()], 
});

import OpenAI from 'openai';

async function main() {
    const openai = new OpenAI({
        apiKey: process.env.OPENAI_API_KEY,
    });
    console.log("OpenAI client initialized",process.env.OPENAI_API_KEY);
    const response = await openai.chat.completions.create({
        model: "gpt-4.1-mini",
        messages: [{ role: "user", content: "Hello, world!" }],
    });
    console.log(response);
}
main();