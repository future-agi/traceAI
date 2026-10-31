import { createConnection } from "node:net";
import { minimize } from "./privacy.js";
import {
  LIMITS,
  STRUCTURE_POLICY,
  type CanonicalFact,
  type Policy,
} from "./schema.js";
export interface Ack {
  protocol_version: 1;
  durable: boolean;
  reason_code: string;
  receipt_id?: string;
}
export function frame(body: unknown): Buffer {
  const bytes = Buffer.from(JSON.stringify(body));
  if (bytes.length > LIMITS.ingress) throw new Error("OVERSIZE_FRAME");
  const size = Buffer.alloc(4);
  size.writeUInt32BE(bytes.length);
  return Buffer.concat([size, bytes]);
}
/** One bounded local attempt; no fallback file, credential lookup, HTTP, or stdout. */
export async function observe(
  socketPath: string,
  routeRef: string,
  fact: CanonicalFact,
  policy: Policy = STRUCTURE_POLICY,
  budgetMs = 25,
): Promise<Ack> {
  const dropped: Ack = {
    protocol_version: 1,
    durable: false,
    reason_code: "LOCAL_UNAVAILABLE",
  };
  let bytes: Buffer;
  try {
    bytes = frame({
      protocol_version: 1,
      operation: "observe",
      route_ref: routeRef,
      fact: minimize(fact, policy),
    });
  } catch {
    return { ...dropped, reason_code: "INVALID_FACT" };
  }
  return new Promise((resolve) => {
    let done = false,
      received = Buffer.alloc(0);
    const socket = createConnection({ path: socketPath });
    const finish = (ack: Ack): void => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(ack);
    };
    const timer = setTimeout(
      () => finish({ ...dropped, reason_code: "LOCAL_TIMEOUT" }),
      Math.max(1, Math.min(25, budgetMs)),
    );
    socket.on("error", () => finish(dropped));
    socket.on("end", () => finish(dropped));
    socket.on("connect", () => socket.write(bytes));
    socket.on("data", (chunk) => {
      if (received.length + chunk.length > 4096) return finish(dropped);
      received = Buffer.concat([received, chunk]);
      if (received.length < 4) return;
      const length = received.readUInt32BE(0);
      if (length > 4092) return finish(dropped);
      if (received.length < length + 4) return;
      try {
        const ack = JSON.parse(received.subarray(4, 4 + length).toString());
        if (
          ack.protocol_version !== 1 ||
          typeof ack.durable !== "boolean" ||
          !/^[A-Z_]{1,64}$/.test(ack.reason_code)
        )
          return finish(dropped);
        finish({
          protocol_version: 1,
          durable: ack.durable,
          reason_code: ack.reason_code,
          ...(typeof ack.receipt_id === "string" &&
          /^[0-9]{1,20}$/.test(ack.receipt_id)
            ? { receipt_id: ack.receipt_id }
            : {}),
        });
      } catch {
        finish(dropped);
      }
    });
  });
}
