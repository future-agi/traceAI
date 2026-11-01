import { AgentError, readProtected } from "../security/files.js";
export async function boundedStdin(
  stream: NodeJS.ReadableStream = process.stdin,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    const cleanup = () => {
      stream.removeListener("data", data);
      stream.removeListener("end", end);
      stream.removeListener("error", error);
    };
    const error = () => {
      cleanup();
      reject(new AgentError("INVALID_AUTH", 2));
    };
    const data = (chunk: Buffer | string) => {
      const b = Buffer.from(chunk);
      size += b.length;
      if (size > 16_384) {
        stream.pause();
        error();
      } else chunks.push(b);
    };
    const end = () => {
      cleanup();
      resolve(Buffer.concat(chunks));
    };
    stream.on("data", data);
    stream.once("end", end);
    stream.once("error", error);
  });
}
async function masked(label: string): Promise<string> {
  if (!process.stdin.isTTY || !process.stderr.isTTY)
    throw new AgentError("TTY_REQUIRED", 2);
  const wasRaw = process.stdin.isRaw;
  return new Promise((resolve, reject) => {
    let text = "";
    const cleanup = () => {
      process.stdin.removeListener("data", data);
      process.stdin.setRawMode(wasRaw);
      process.stdin.pause();
      process.stderr.write("\n");
    };
    const data = (chunk: Buffer) => {
      for (const char of chunk.toString("utf8")) {
        if (char === "\u0003" || char === "\u0004") {
          cleanup();
          reject(new AgentError("AUTH_CANCELLED", 2));
          return;
        }
        if (char === "\r" || char === "\n") {
          cleanup();
          resolve(text);
          return;
        }
        if (char === "\u007f" || char === "\b") {
          text = text.slice(0, -1);
          continue;
        }
        text += char;
        if (Buffer.byteLength(text) > 4096) {
          cleanup();
          reject(new AgentError("INVALID_AUTH", 2));
          return;
        }
      }
    };
    process.stdin.setRawMode(true);
    process.stdin.on("data", data);
    process.stderr.write(label + " (input hidden)\n");
    process.stdin.resume();
  });
}
export async function authInput(
  options: Record<string, string | boolean>,
): Promise<Buffer> {
  if (
    ["stdin", "tty", "credential-file"].filter((k) => options[k] !== undefined)
      .length !== 1
  )
    throw new AgentError("ONE_AUTH_INPUT_REQUIRED", 2);
  if (options.stdin) {
    if (process.stdin.isTTY) throw new AgentError("USE_MASKED_TTY", 2);
    return boundedStdin();
  }
  if (options.tty)
    return Buffer.from(
      JSON.stringify({
        api_key: await masked("API key"),
        secret_key: await masked("Secret key"),
      }),
    );
  return readProtected(String(options["credential-file"]), 16_384);
}
