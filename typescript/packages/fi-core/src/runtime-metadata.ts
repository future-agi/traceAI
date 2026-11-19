// Priority reserves a slot, not an exemption from export privacy. Only values
// matching the SDK's bounded metadata shapes may receive admission evidence.
const uuid = (v: unknown) => typeof v === "string" && v.length === 36 && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(v);
const slug = (v: unknown) => typeof v === "string" && v.length <= 64 && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(v);
const label = (v: unknown) => typeof v === "string" && /^[a-z0-9][a-z0-9-]{0,39}$/.test(v);
const count = (v: unknown) => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const version = (v: unknown) => count(v) && v !== 0;
const digest = (v: unknown) => typeof v === "string" && v.length === 71 && /^sha256:[0-9a-f]{64}$/.test(v);
const flag = (v: unknown) => typeof v === "boolean";
const list = (check: (v: unknown) => boolean) => (v: unknown) => Array.isArray(v) && v.length <= 32 && v.every(check);
const ref = (v: unknown) => {
  if (typeof v !== "string" || v.length > 82) return false;
  const [name, n, ...extra] = v.split("@v");
  return !extra.length && slug(name) && !!n && /^[1-9][0-9]*$/.test(n) && Number.isSafeInteger(Number(n));
};
const schema = new Map<string, (v: unknown) => boolean>([
  ...["fi.skill", "fi.context"].flatMap(prefix => Object.entries({
    id: uuid, version_id: uuid, name: slug, digest, version, requested_version: version, requested_label: label,
  }).map(([key, check]): [string, (v: unknown) => boolean] => [`${prefix}.${key}`, check])),
  ["fi.skill.files", count], ["fi.skill.bytes", count], ["fi.skill.via_context.version_id", uuid],
  ["fi.skill.load.via", v => ["sdk.read_file", "sdk.read_all", "context.read", "deepagents.read", "explicit", "lock"].some(s => v === s)],
  ["fi.skill.load.evidence", v => v === "delivered" || v === "declared"], ["fi.skill.load.cached", flag], ["fi.skill.load.root_file", flag],
  ["fi.skills.version_ids", list(uuid)], ["fi.skills.ids", list(uuid)], ["fi.skills.refs", list(ref)],
  ["fi.skills.count", count], ["fi.skills.truncated", flag], ["fi.context.binding_id", uuid], ["fi.context.session_id", uuid],
  ["fi.context.linked_skill_version_ids", list(uuid)], ["gen_ai.agent.name", slug], ["gen_ai.agent.id", uuid],
]);
export const isRuntimeKey = (key: string) => schema.has(key);
/** Input is the already-sanitized FI admission map, never a host-supplied object. */
export const validRuntimeValue = (key: string, value: unknown) => schema.get(key)?.(value) === true;

// Module-private to fi-core's package (no public subpath). The ESM build refers
// to the CJS instance so an ESM exporter can check an FI-authored CJS value,
// and vice versa. Never publish evidence on span attributes or process globals.
export const receipts = new WeakMap<object, string>();
export const admittedRuntime = new WeakMap<object, Map<string, unknown>>();

// Constructor identity only. Membership grants no receipt or span provenance.
export const runtimeProcessors = new WeakSet<object>();
