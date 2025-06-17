import {
    NodeTracerProvider,
    SimpleSpanProcessor,
  } from "@opentelemetry/sdk-trace-node";
import { resourceFromAttributes } from "@opentelemetry/resources";

import { trace, context } from "@opentelemetry/api";
import { GRPCSpanExporter } from "@traceai/fi-core";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
const otlpProcessor = new SimpleSpanProcessor(
    new GRPCSpanExporter({
        endpoint: "localhost:50051",
        headers: {
            "X-Api-Key": "0ce7f643df7e490c8e1018cb3a231591",
            "X-Secret-Key": "a22030bba7d04054b49e1ae97a548e8b",
        },
    })
  );

const provider = new NodeTracerProvider({
    resource: resourceFromAttributes({
        "project_name": "test_project_2_grpc",
        "project_type": "observe",

    }),
    spanProcessors: [otlpProcessor],
});

provider.register();
console.log("Provider registered");

console.log("Registering OpenAI Instrumentation...");
registerInstrumentations({
    tracerProvider: provider,
});

// Import OpenAI AFTER instrumentation is registered
import OpenAI from "openai";

// Initialize OpenAI client AFTER instrumentation
const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});


const tracer = provider.getTracer("combined-tracing-example-te-sarthak");


// Test function to validate instrumentation is working
async function testInstrumentation() {
    const span = tracer.startSpan("test_manual_span");
    const response = await openai.chat.completions.create({
        messages: [{ role: "user", content: "Say hello" }],
        model: "gpt-4o-mini",
    });
    span.end();

}

// Main execution combining all approaches
async function main() {
    console.log("Starting combined auto + manual tracing example...");
    console.log("Environment check - OpenAI API Key:", process.env.OPENAI_API_KEY ? "Set" : "NOT SET");
    
    try {
        // Test instrumentation first
        await testInstrumentation();
        
        console.log("\nAll operations completed successfully!");
        
    } catch (error) {
        console.error("Error in main execution:", error);
        
        // Even in error handling, we can work with active spans
        // const activeSpan = trace.getActiveSpan();
        // if (activeSpan) {
        //     activeSpan.recordException(error as Error);
        //     activeSpan.setStatus({ 
        //         code: 2, 
        //         message: `Main execution failed: ${(error as Error).message}` 
        //     });
        // }
    }
}

// Run the example
main().catch(console.error);