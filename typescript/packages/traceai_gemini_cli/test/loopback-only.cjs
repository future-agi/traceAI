// Test-only egress guard, inherited by the real Gemini binary and hook subprocesses.
const dns = require("node:dns");
const net = require("node:net");
const { syncBuiltinESMExports } = require("node:module");
const fs = require("node:fs");
const local = (host) =>
  ["127.0.0.1", "localhost", "::1", "[::1]"].includes(String(host));
function block(host) {
  if (process.env.EGRESS_EVIDENCE)
    fs.appendFileSync(process.env.EGRESS_EVIDENCE, "BLOCKED_NON_LOOPBACK\n");
  const e = new Error("TEST_NON_LOOPBACK_BLOCKED");
  e.code = "ENETUNREACH";
  return e;
}
const lookup = dns.lookup;
dns.lookup = function (host, ...args) {
  if (!local(host)) {
    const cb = args[args.length - 1];
    if (typeof cb === "function")
      return process.nextTick(() => cb(block(host)));
    throw block(host);
  }
  return lookup.call(this, host, ...args);
};
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  let first = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host =
    typeof first === "object"
      ? first.host
      : typeof args[1] === "string"
        ? args[1]
        : "localhost";
  if (host && !local(host)) throw block(host);
  return connect.apply(this, args);
};
syncBuiltinESMExports();
