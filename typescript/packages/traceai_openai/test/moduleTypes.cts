import OpenAI from "openai";
import { OpenAIInstrumentation } from "@traceai/openai";

// Keep the existing CommonJS entry point and declaration working.
new OpenAIInstrumentation().manuallyInstrument(OpenAI);
