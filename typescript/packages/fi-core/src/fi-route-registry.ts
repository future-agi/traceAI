/**
 * Which options objects were declared by the SDK's own wiring to be the Future AGI route. A WeakSet
 * keyed on object identity: no property a caller can put on its own options (typed or untyped, or by
 * spreading them into another object) makes an options object a member.
 */
const declared = new WeakSet<object>();
export function declareFiRoute<T extends object>(options: T): T {
  declared.add(options);
  return options;
}
export function isFiRouteDeclared(options: unknown): boolean {
  return typeof options === "object" && options !== null && declared.has(options);
}
