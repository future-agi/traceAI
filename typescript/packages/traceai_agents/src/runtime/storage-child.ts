import { Spool } from "../storage/store.js";
import { AgentError } from "../security/files.js";
let store: Spool | undefined;
process.on("message", (message: any) => {
  let result: unknown;
  try {
    if (
      !message ||
      !Number.isSafeInteger(message.id) ||
      Buffer.byteLength(JSON.stringify(message)) > 1_048_576
    )
      throw new AgentError("INVALID_RPC");
    if (message.op === "init") {
      if (store) throw new AgentError("ALREADY_INITIALIZED");
      store = new Spool(message.args.root, message.args.version, true);
      result = { ready: true };
    } else {
      if (!store) throw new AgentError("STORAGE_UNAVAILABLE");
      const a = message.args;
      switch (message.op) {
        case "route":
          result = store.route(a);
          break;
        case "validateRoute":
          result = store.validateRoute(a);
          break;
        case "getRoute":
          result = store.getRoute(a);
          break;
        case "admit":
          result = store.admit(
            a.route,
            a.fact,
            a.cursor,
            a.observed === true,
            a.expectedEpoch,
          );
          break;
        case "next":
          result = store.next(a.route, a.includeDisabled);
          break;
        case "complete":
          result = store.complete(a.id, a.epoch, a.result);
          break;
        case "releaseAuth":
          result = store.releaseAuth(a);
          break;
        case "disable":
          result = store.disable(a);
          break;
        case "purge":
          result = store.purge(a);
          break;
        case "expire":
          result = store.expire();
          break;
        case "status":
          result = store.status();
          break;
        case "close":
          store.close();
          store = undefined;
          result = { closed: true };
          break;
        default:
          throw new AgentError("INVALID_RPC");
      }
    }
    process.send?.({ id: message.id, ok: true, result });
  } catch (e) {
    process.send?.({
      id: message?.id,
      ok: false,
      code: e instanceof AgentError ? e.code : "STORAGE_UNAVAILABLE",
      exitCode: e instanceof AgentError ? e.exitCode : 7,
    });
  }
});
process.on("disconnect", () => {
  try {
    store?.close();
  } finally {
    process.exit(0);
  }
});
