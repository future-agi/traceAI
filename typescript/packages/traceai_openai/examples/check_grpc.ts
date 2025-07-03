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
        endpoint: "https://grpc.futureagi.com",
        headers: {
            "X-Api-Key": process.env.FI_API_KEY || "",
            "X-Secret-Key": process.env.FI_SECRET_KEY || "",
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
    // Start parent span
    const parentSpan = tracer.startSpan("parent-span", {
        attributes: { "fi.span.kind": "chain" }
    });

    // Optionally, set input attribute for parent
    parentSpan.setAttribute("input", "Parent span input");

    // Use context.with to make parentSpan the current span
    await context.with(trace.setSpan(context.active(), parentSpan), async () => {
        for (let i = 0; i < 3; i++) {
            // Start child span as child of parent
            const childSpan = tracer.startSpan(
                `child-span-${i}`,
                {
                    attributes: { "fi.span.kind": "chain" },
                }
            );
            childSpan.setAttribute("input", `Child span ${i} input`);

            await context.with(trace.setSpan(context.active(), childSpan), async () => {
                for (let j = 0; j < 2; j++) {
                    // Start grandchild span as child of childSpan
                    const grandchildSpan = tracer.startSpan(
                        `grandchild-span-${i}-${j}`,
                        {
                            attributes: { "fi.span.kind": "chain" }
                        }
                    );
                    grandchildSpan.setAttribute("input", `Grandchild span ${i}-${j} input`);

                    await context.with(trace.setSpan(context.active(), grandchildSpan), async () => {
                        // Optionally, do some work here
                        // For demonstration, call OpenAI only on the first grandchild
                        if (i === 0 && j === 0) {
                            const response = await openai.chat.completions.create({
                                messages: [{ role: "user", content: "Say hello" }],
                                model: "gpt-4o-mini",
                            });
                            // Optionally, record response in span
                            grandchildSpan.setAttribute("openai_response_id", response.id || "");
                        }
                    });
                    grandchildSpan.end();
                }
            });
            childSpan.end();
        }
    });

    parentSpan.end();
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