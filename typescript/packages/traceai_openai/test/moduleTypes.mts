import OpenAI from "openai";
import { OpenAIInstrumentation } from "@traceai/openai";

// ESM must resolve the ESM OpenAI declaration, not its nominally distinct CJS type.
new OpenAIInstrumentation().manuallyInstrument(OpenAI);
