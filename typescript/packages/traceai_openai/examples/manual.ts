import { trace, context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { register } from "@traceai/fi-core";
import { ProjectType } from "@traceai/fi-core";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { OpenAIInstrumentation } from "@traceai/openai";

// Enable OpenTelemetry internal diagnostics for debugging
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

// Activate a context manager for consistent context propagation
context.setGlobalContextManager(new AsyncLocalStorageContextManager());

// Get a tracer instance FIRST
const tracerProvider = register({
    projectName: "combined-tracing-example",
    projectType: ProjectType.OBSERVE,
    sessionName: "combined-tracing-example-session-" + Date.now(),
});

console.log("Tracer provider initialized:", tracerProvider);

const tracer = tracerProvider.getTracer("combined-tracing-example");

// Register instrumentation BEFORE importing OpenAI
console.log("Registering OpenAI Instrumentation...");
const openaiInstrumentation = new OpenAIInstrumentation();
registerInstrumentations({
    tracerProvider: tracerProvider,
    instrumentations: [openaiInstrumentation],
});

// Import OpenAI AFTER instrumentation is registered
import OpenAI from "openai";

// Initialize OpenAI client AFTER instrumentation
const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});

console.log("OpenAI client initialized. Instrumentation patched:", (openaiInstrumentation as any).isPatched?.());

// Example 1: Enhancing auto-instrumented spans with manual additions
async function enhancedTextGeneration(prompt: string) {
    // Create a manual span that will contain the auto-instrumented OpenAI span
    return tracer.startActiveSpan("enhanced-text-generation", async (manualSpan) => {
        manualSpan.setAttribute("fi.span.kind", "chain");
        manualSpan.setAttribute("custom.prompt.length", prompt.length);
        manualSpan.setAttribute("custom.task.type", "text-generation");
        
        // Add a manual event before the auto-instrumented call
        manualSpan.addEvent("Starting OpenAI text generation", {
            "prompt": prompt.substring(0, 100) + "..." // Truncated for logging
        });
        
        console.log("About to make OpenAI call. Active span:", trace.getActiveSpan()?.spanContext());
        
        try {
            // This call will be auto-instrumented by OpenAIInstrumentation
            const completion = await openai.chat.completions.create({
                messages: [{ role: "user", content: prompt }],
                model: "gpt-4o-mini", // Using a valid model name
            });
            
            console.log("OpenAI call completed. Current active span:", trace.getActiveSpan()?.spanContext());
            
            // After the auto-instrumented call, get the active span and enhance it
            const currentSpan = trace.getActiveSpan();
            if (currentSpan) {
                console.log("Found active span after OpenAI call, adding custom attributes");
                // Add custom attributes to the auto-created span
                currentSpan.setAttribute("custom.response.length", completion.choices[0]?.message?.content?.length || 0);
                currentSpan.setAttribute("custom.tokens.estimated", Math.ceil((completion.choices[0]?.message?.content?.length || 0) / 4));
                
                // Add a custom event to the auto-created span
                currentSpan.addEvent("Response received and processed", {
                    "response_quality": "high",
                    "processing_time": Date.now()
                });
            } else {
                console.log("No active span found after OpenAI call - instrumentation may not be working");
            }
            
            manualSpan.addEvent("OpenAI text generation completed successfully");
            manualSpan.setStatus({ code: 1 }); // OK status
            
            return completion;
            
        } catch (error) {
            console.error("Error in OpenAI call:", error);
            // Handle errors in both manual and auto spans
            const currentSpan = trace.getActiveSpan();
            if (currentSpan) {
                currentSpan.recordException(error as Error);
                currentSpan.setStatus({ 
                    code: 2, // ERROR status
                    message: (error as Error).message 
                });
            }
            
            manualSpan.recordException(error as Error);
            manualSpan.setStatus({ 
                code: 2, // ERROR status
                message: `Text generation failed: ${(error as Error).message}` 
            });
            
            throw error;
        } finally {
            manualSpan.end();
        }
    });
}

// Example 2: Creating manual child spans of auto-instrumented spans
async function processWithAnalysis(prompt: string) {
    return tracer.startActiveSpan("ai-processing-pipeline", async (pipelineSpan) => {
        pipelineSpan.setAttribute("fi.span.kind", "chain");
        
        console.log("Starting auto-instrumented OpenAI call in processing pipeline");
        
        // Auto-instrumented OpenAI call
        const response = await openai.chat.completions.create({
            messages: [{ role: "user", content: prompt }],
            model: "gpt-4o-mini",
        });
        
        // Get the currently active span (could be the auto-created OpenAI span)
        const currentSpan = trace.getActiveSpan();
        if (currentSpan) {
            console.log("Found active span, adding analysis flag");
            currentSpan.setAttribute("custom.analysis.enabled", true);
        } else {
            console.log("No active span found in processing pipeline");
        }
        
        // Create manual analysis spans as children
        const content = response.choices[0]?.message?.content || "";
        
        await tracer.startActiveSpan("content-analysis", async (analysisSpan) => {
            analysisSpan.setAttribute("fi.span.kind", "tool");
            analysisSpan.setAttribute("analysis.content.length", content.length);
            analysisSpan.setAttribute("analysis.word.count", content.split(' ').length);
            
            // Simulate analysis work
            await new Promise(resolve => setTimeout(resolve, 100));
            
            analysisSpan.addEvent("Content analysis completed", {
                "sentiment": "positive",
                "complexity": "medium"
            });
            
            analysisSpan.end();
        });
        
        await tracer.startActiveSpan("content-validation", async (validationSpan) => {
            validationSpan.setAttribute("fi.span.kind", "tool");
            
            // Simulate validation
            const isValid = content.length > 10;
            validationSpan.setAttribute("validation.result", isValid);
            validationSpan.setAttribute("validation.criteria", "min_length_check");
            
            if (!isValid) {
                validationSpan.setStatus({ 
                    code: 2, 
                    message: "Content validation failed" 
                });
            }
            
            validationSpan.end();
        });
        
        pipelineSpan.end();
        return response;
    });
}

// Example 3: Context propagation between auto and manual spans
async function chainedOperations() {
    return tracer.startActiveSpan("chained-ai-operations", async (rootSpan) => {
        rootSpan.setAttribute("fi.span.kind", "chain");
        
        console.log("Starting first auto-instrumented operation");
        
        // First operation - auto-instrumented
        const firstResponse = await openai.chat.completions.create({
            messages: [{ role: "user", content: "Generate a creative story title" }],
            model: "gpt-4o-mini",
        });
        
        // Extract the title and use it in manual processing
        const title = firstResponse.choices[0]?.message?.content || "";
        
        // Manual span that uses context from the auto-instrumented span
        await tracer.startActiveSpan("title-processing", async (processingSpan) => {
            processingSpan.setAttribute("fi.span.kind", "tool");
            processingSpan.setAttribute("processing.input", title);
            
            // Get active span context for correlation
            const activeSpan = trace.getActiveSpan();
            if (activeSpan) {
                const spanContext = activeSpan.spanContext();
                processingSpan.setAttribute("correlation.parent.trace_id", spanContext.traceId);
                processingSpan.setAttribute("correlation.parent.span_id", spanContext.spanId);
            }
            
            // Process the title
            const processedTitle = title.toUpperCase().trim();
            processingSpan.setAttribute("processing.output", processedTitle);
            
            processingSpan.end();
            
            console.log("Starting second auto-instrumented operation with processed title");
            
            // Second auto-instrumented operation using processed result
            const storyResponse = await openai.chat.completions.create({
                messages: [{ 
                    role: "user", 
                    content: `Write a short story with the title: "${processedTitle}"` 
                }],
                model: "gpt-4o-mini",
            });
            
            // Enhance the auto-created span for the second call
            const currentSpan = trace.getActiveSpan();
            if (currentSpan) {
                console.log("Enhancing second auto-created span with story metadata");
                currentSpan.setAttribute("custom.story.title", processedTitle);
                currentSpan.setAttribute("custom.story.word_count", 
                    storyResponse.choices[0]?.message?.content?.split(' ').length || 0);
            } else {
                console.log("No active span found for second OpenAI call");
            }
            
            return storyResponse;
        });
        
        rootSpan.end();
    });
}

// Test function to validate instrumentation is working
async function testInstrumentation() {
    console.log("\n=== Testing OpenAI Instrumentation ===");
    
    // Simple test to see if spans are created
    const testSpan = tracer.startSpan("test-span");
    console.log("Test span created:", testSpan.spanContext());
    
    try {
        console.log("Making simple OpenAI call to test auto instrumentation...");
        const response = await openai.chat.completions.create({
            messages: [{ role: "user", content: "Say hello" }],
            model: "gpt-4o-mini",
        });
        
        console.log("Response received:", {
            model: response.model,
            content_length: response.choices[0]?.message?.content?.length
        });
        
        // Check if any spans were created during this call
        const activeSpan = trace.getActiveSpan();
        console.log("Active span after simple call:", activeSpan?.spanContext());
        
    } catch (error) {
        console.error("Error in simple test:", error);
    } finally {
        testSpan.end();
    }
}

// Main execution combining all approaches
async function main() {
    console.log("Starting combined auto + manual tracing example...");
    console.log("Environment check - OpenAI API Key:", process.env.OPENAI_API_KEY ? "Set" : "NOT SET");
    
    try {
        // Test instrumentation first
        await testInstrumentation();
        
        // Example 1: Enhanced auto-instrumentation
        console.log("\n1. Enhanced text generation...");
        await enhancedTextGeneration("Write a poem about technology and nature");
        
        // Example 2: Manual analysis of auto-instrumented calls
        console.log("\n2. Processing with analysis...");
        await processWithAnalysis("Explain quantum computing in simple terms");
        
        // Example 3: Chained operations
        console.log("\n3. Chained operations...");
        await chainedOperations();
        
        console.log("\nAll operations completed successfully!");
        
    } catch (error) {
        console.error("Error in main execution:", error);
        
        // Even in error handling, we can work with active spans
        const activeSpan = trace.getActiveSpan();
        if (activeSpan) {
            activeSpan.recordException(error as Error);
            activeSpan.setStatus({ 
                code: 2, 
                message: `Main execution failed: ${(error as Error).message}` 
            });
        }
    } finally {
        // Give time for spans to be exported
        setTimeout(async () => {
            try {
                await tracerProvider.shutdown();
                console.log("Tracer provider shut down successfully.");
            } catch (error) {
                console.error("Error shutting down tracer provider:", error);
            }
        }, 2000);
    }
}

// Run the example
main().catch(console.error);
