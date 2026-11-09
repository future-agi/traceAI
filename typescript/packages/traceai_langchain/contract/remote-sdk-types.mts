// Compile in each isolated packed consumer, against real SDK floor and latest types.
import { Client } from "@langchain/langgraph-sdk";
import { wrapLangGraphClient, langGraphTraceRequestHook } from "@traceai/langchain";

const client = wrapLangGraphClient(new Client());
const result: Promise<unknown> = client.runs.wait(null, "assistant", { input: { question: "synthetic" } });
const stream: AsyncIterable<unknown> = client.runs.stream("thread", "assistant", { input: { question: "synthetic" } });
void result;
void stream;
void langGraphTraceRequestHook;
