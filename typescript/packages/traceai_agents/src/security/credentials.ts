import { randomBytes } from "node:crypto";
import { join } from "node:path";
import { unlinkSync } from "node:fs";
import { token } from "@traceai/agent-core";
import {
  AgentError,
  atomicProtected,
  protectedDirectory,
  readProtected,
} from "./files.js";
export interface BindingKeys {
  identity_key: string;
  content_key: string;
}
export interface Credentials extends BindingKeys {
  api_key: string;
  secret_key: string;
}
export type StoredSecrets = BindingKeys | Credentials;
export function hasAuth(value: StoredSecrets): value is Credentials {
  return "api_key" in value && "secret_key" in value;
}
export interface SecretStore {
  get(ref: string): Promise<StoredSecrets | undefined>;
  set(ref: string, value: StoredSecrets): Promise<void>;
  remove(ref: string): Promise<void>;
}
/** Revoke upstream authentication, not the destination's durable identity. */
export async function removeAuth(
  store: SecretStore,
  ref: string,
): Promise<void> {
  const previous = await store.get(ref);
  if (previous)
    await store.set(ref, {
      identity_key: previous.identity_key,
      content_key: previous.content_key,
    });
}
export function parseAuth(bytes: Buffer): {
  api_key: string;
  secret_key: string;
} {
  if (bytes.length > 16_384) throw new AgentError("INVALID_AUTH", 2);
  try {
    const v = JSON.parse(bytes.toString("utf8"));
    if (
      !v ||
      Object.keys(v).sort().join(",") !== "api_key,secret_key" ||
      ![v.api_key, v.secret_key].every(
        (x) => typeof x === "string" && /^[\x21-\x7e]{1,4096}$/.test(x),
      )
    )
      throw new Error();
    return { api_key: v.api_key, secret_key: v.secret_key };
  } catch {
    throw new AgentError("INVALID_AUTH", 2);
  }
}
export function newCredentials(
  auth: ReturnType<typeof parseAuth>,
  previous?: BindingKeys,
): Credentials {
  return {
    ...auth,
    identity_key: previous?.identity_key ?? randomBytes(32).toString("hex"),
    content_key: previous?.content_key ?? randomBytes(32).toString("hex"),
  };
}
function decode(text: string): StoredSecrets {
  try {
    const v = JSON.parse(text);
    const auth =
      "api_key" in v || "secret_key" in v
        ? parseAuth(
            Buffer.from(
              JSON.stringify({ api_key: v.api_key, secret_key: v.secret_key }),
            ),
          )
        : {};
    if (
      ![v.identity_key, v.content_key].every(
        (k) => typeof k === "string" && /^[a-f0-9]{64}$/.test(k),
      )
    )
      throw new Error();
    return {
      ...auth,
      identity_key: v.identity_key,
      content_key: v.content_key,
    };
  } catch {
    throw new AgentError("HELD_CREDENTIALS", 3);
  }
}
/** Explicit fallback only. Never selected automatically if the OS store fails. */
export class FileSecretStore implements SecretStore {
  constructor(private readonly root: string) {
    protectedDirectory(root, true);
  }
  private path(ref: string): string {
    if (!token(ref)) throw new AgentError("INVALID_REFERENCE");
    return join(this.root, ref + ".json");
  }
  async get(ref: string): Promise<StoredSecrets | undefined> {
    try {
      return decode(readProtected(this.path(ref)).toString());
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") return undefined;
      throw e;
    }
  }
  async set(ref: string, value: StoredSecrets): Promise<void> {
    atomicProtected(this.path(ref), JSON.stringify(value));
  }
  async remove(ref: string): Promise<void> {
    try {
      readProtected(this.path(ref));
      unlinkSync(this.path(ref));
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
    }
  }
}
/** Loaded only during explicit installed-product auth/runtime actions, never host callbacks. */
export class OSSecretStore implements SecretStore {
  private async entry(ref: string) {
    if (!token(ref) || !["darwin", "linux"].includes(process.platform))
      throw new AgentError("UNQUALIFIED_PLATFORM", 5);
    const { Entry } = await import("@napi-rs/keyring");
    return new Entry("com.futureagi.agents.v1", ref, {
      linux: { store: "secret-service" },
    });
  }
  async get(ref: string): Promise<StoredSecrets | undefined> {
    try {
      const value = (await this.entry(ref)).getPassword();
      return value == null ? undefined : decode(value);
    } catch {
      throw new AgentError("HELD_CREDENTIALS", 3);
    }
  }
  async set(ref: string, value: StoredSecrets): Promise<void> {
    try {
      (await this.entry(ref)).setPassword(JSON.stringify(value));
    } catch {
      throw new AgentError("HELD_CREDENTIALS", 3);
    }
  }
  async remove(ref: string): Promise<void> {
    try {
      (await this.entry(ref)).deletePassword();
    } catch {
      throw new AgentError("HELD_CREDENTIALS", 3);
    }
  }
}
