import http from "http";
import { AddressInfo } from "net";

export const FAKE_REPLY = "Hello from fake";
export const FAKE_USAGE = { prompt_tokens: 11, completion_tokens: 7, total_tokens: 18 };
export const FAKE_TOOL_ARGUMENTS = { city: "Paris" };
export const BROKEN_STREAM_PATH = "/broken";

interface ChatRequest {
  model: string;
  stream?: boolean;
  tools?: { function: { name: string } }[];
  input?: string | string[];
}

export interface FakeOpenAIServer {
  baseURL: string;
  brokenStreamBaseURL: string;
  close: () => Promise<void>;
}

function embedding(text: string): number[] {
  const vector = new Array(8).fill(0);
  for (let i = 0; i < text.length; i++) {
    vector[i % 8] += text.charCodeAt(i) / 1000;
  }
  return vector;
}

function sse(res: http.ServerResponse, payload: object) {
  res.write(`data: ${JSON.stringify(payload)}\n\n`);
}

function handleChat(req: http.IncomingMessage, body: ChatRequest, res: http.ServerResponse) {
  const toolName = body.tools?.[0]?.function.name;
  const base = { id: "chatcmpl-1", created: 1, model: body.model };

  if (!body.stream) {
    const message = toolName
      ? {
          role: "assistant",
          content: null,
          tool_calls: [
            {
              id: "call_1",
              type: "function",
              function: { name: toolName, arguments: JSON.stringify(FAKE_TOOL_ARGUMENTS) },
            },
          ],
        }
      : { role: "assistant", content: FAKE_REPLY };
    res.setHeader("content-type", "application/json");
    res.end(
      JSON.stringify({
        ...base,
        object: "chat.completion",
        choices: [{ index: 0, message, finish_reason: toolName ? "tool_calls" : "stop" }],
        usage: FAKE_USAGE,
      }),
    );
    return;
  }

  res.setHeader("content-type", "text/event-stream");
  const chunk = { ...base, object: "chat.completion.chunk" };
  const words = FAKE_REPLY.split(/(?= )/);
  for (const word of words) {
    sse(res, {
      ...chunk,
      choices: [{ index: 0, delta: { role: "assistant", content: word }, finish_reason: null }],
    });
  }
  if (req.url?.startsWith(BROKEN_STREAM_PATH)) {
    res.destroy();
    return;
  }
  sse(res, { ...chunk, choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: FAKE_USAGE });
  res.end("data: [DONE]\n\n");
}

export function startFakeOpenAIServer(): Promise<FakeOpenAIServer> {
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => (raw += chunk));
    req.on("end", () => {
      const body: ChatRequest = raw ? JSON.parse(raw) : { model: "" };
      if (req.url?.endsWith("/embeddings")) {
        const inputs = Array.isArray(body.input) ? body.input : [body.input ?? ""];
        res.setHeader("content-type", "application/json");
        res.end(
          JSON.stringify({
            object: "list",
            model: body.model,
            data: inputs.map((text, index) => ({ object: "embedding", index, embedding: embedding(text) })),
            usage: { prompt_tokens: 5, total_tokens: 5 },
          }),
        );
        return;
      }
      if (req.url?.endsWith("/chat/completions")) {
        handleChat(req, body, res);
        return;
      }
      res.statusCode = 404;
      res.end("{}");
    });
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        baseURL: `http://127.0.0.1:${port}/v1`,
        brokenStreamBaseURL: `http://127.0.0.1:${port}${BROKEN_STREAM_PATH}/v1`,
        close: () =>
          new Promise((done) => {
            server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });
}
