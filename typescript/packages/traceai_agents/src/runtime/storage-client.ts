import { fork, type ChildProcess } from "node:child_process";
import { AgentError } from "../security/files.js";
interface Pending {
  resolve: (value: any) => void;
  reject: (reason: Error) => void;
  timer: NodeJS.Timeout;
}
export class StorageClient {
  private readonly child: ChildProcess;
  private readonly pending = new Map<number, Pending>();
  private sequence = 0;
  private bytes = 0;
  private queued = 0;
  private closed = false;
  private tail: Promise<unknown> = Promise.resolve();
  constructor(
    entry: string,
    private readonly onFailure?: () => void,
  ) {
    this.child = fork(entry, [], {
      env: { PATH: "/usr/bin:/bin", HOME: "/" },
      stdio: ["ignore", "ignore", "ignore", "ipc"],
      serialization: "advanced",
      execArgv: ["--max-old-space-size=128"],
    });
    this.child.on("message", (v: any) => {
      const pending = this.pending.get(v?.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(v.id);
      if (v.ok) pending.resolve(v.result);
      else
        pending.reject(
          new AgentError(
            /^[A-Z_]{1,64}$/.test(v.code) ? v.code : "STORAGE_UNAVAILABLE",
            Number.isInteger(v.exitCode) && v.exitCode >= 2 && v.exitCode <= 7
              ? v.exitCode
              : 7,
          ),
        );
    });
    this.child.on("error", () => this.fail());
    this.child.on("exit", () => this.fail());
  }
  private fail(): void {
    if (this.closed) return;
    this.closed = true;
    this.onFailure?.();
    for (const p of this.pending.values()) {
      clearTimeout(p.timer);
      p.reject(new AgentError("STORAGE_UNAVAILABLE"));
    }
    this.pending.clear();
  }
  call<T = any>(op: string, args: unknown = {}): Promise<T> {
    const size = Buffer.byteLength(JSON.stringify(args));
    if (
      this.closed ||
      this.queued >= 128 ||
      size > 1_048_576 ||
      this.bytes + size > 1_048_576 ||
      this.sequence > Number.MAX_SAFE_INTEGER - 1
    )
      return Promise.reject(new AgentError("STORAGE_UNAVAILABLE"));
    this.bytes += size;
    this.queued++;
    const run = () =>
      new Promise<T>((resolve, reject) => {
        if (this.closed) return reject(new AgentError("STORAGE_UNAVAILABLE"));
        const id = ++this.sequence;
        const timer = setTimeout(
          () => {
            this.child.kill("SIGKILL");
            this.fail();
          },
          op === "init" ? 5000 : 200,
        );
        this.pending.set(id, { resolve, reject, timer });
        this.child.send({ id, op, args }, (error) => {
          if (error) this.fail();
        });
      });
    const result = this.tail.then(run).finally(() => {
      this.bytes -= size;
      this.queued--;
    });
    this.tail = result.catch(() => undefined);
    return result;
  }
  async close(): Promise<void> {
    try {
      await this.call("close");
    } finally {
      if (this.child.connected) this.child.disconnect();
      this.closed = true;
    }
  }
}
