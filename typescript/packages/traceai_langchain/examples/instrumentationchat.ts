import { LangChainInstrumentation } from "../src/index";
import { ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import {
  NodeTracerProvider,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-node";
import { Resource } from "@opentelemetry/resources";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
// import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";
import { ATTR_PROJECT_NAME } from "@traceai/fi-semantic-conventions";
import { ATTR_SERVICE_NAME } from "@traceai/fi-semantic-conventions/src/resource/ResourceAttributes";
// For troubleshooting, set the log level to DiagLogLevel.DEBUG
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

const provider = new NodeTracerProvider({
  resource: new Resource({
    [ATTR_SERVICE_NAME]: "langchain-service",
    [ATTR_PROJECT_NAME]: "langchain-project",
  }),
});

provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.addSpanProcessor(
  new SimpleSpanProcessor(
    new OTLPTraceExporter({
      url: "http://localhost:8000/tracer/observation-span/create_otel_span/",
    }),
  ),
);

const lcInstrumentation = new LangChainInstrumentation();
// LangChain must be manually instrumented as it doesn't have a traditional module structure
lcInstrumentation.manuallyInstrument(CallbackManagerModule);

// provider.register();

// eslint-disable-next-line no-console
console.log("👀 TraceAI initialized");
