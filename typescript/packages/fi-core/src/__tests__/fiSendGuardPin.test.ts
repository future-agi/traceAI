import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import * as ts from "typescript";

/**
 * Static pins for the send-level guard (fi-send-guard.ts), read with the TypeScript compiler API rather than kept as a hand-written
 * list of call sites. In the modules that carry Future AGI keys:
 *   1. every transport primitive (fetch, http / https / undici requests, dispatcher and channel construction, grpc clients) appears
 *      only inside the guarded functions of fi-send-guard.ts;
 *   2. the one judging function (guardSend) is called from exactly the three guarded entry points;
 *   3. each exporter constructor and each fetch site goes through its guarded entry point.
 * A new `fetch(`, `http.request`, undici `request`/`fetch`/`Pool`/`Client`/`Agent` or grpc channel/client construction outside the guard
 * fails here until it is routed through the guard.
 */
const PACKAGES = join(__dirname, "..", "..", "..");
const KEY_CARRYING = [
  { pkg: "fi-core", dir: join(PACKAGES, "fi-core", "src"), files: ["otel.ts", "fi-request-policy.ts", "otlp-header-isolation.ts", "fi-route.ts", "fi-route-registry.ts", "fi-send-guard.ts", "fi-endpoint.ts", "secrets.ts"] },
  { pkg: "traceai_auto", dir: join(PACKAGES, "traceai_auto", "src"), files: ["index.ts", "config.ts"] },
];
interface Parsed { id: string; sf: ts.SourceFile }
const PARSED: Parsed[] = KEY_CARRYING.flatMap(({ pkg, dir, files }) => files.map(name => ({
  id: `${pkg}/${name}`, sf: ts.createSourceFile(join(dir, name), readFileSync(join(dir, name), "utf8"), ts.ScriptTarget.Latest, true),
})));

function owner(node: ts.Node): string {
  let found = "<top level>";
  for (let p: ts.Node | undefined = node; p; p = p.parent) {
    if (ts.isFunctionDeclaration(p) && p.name) found = p.name.text;
    else if (ts.isMethodDeclaration(p) || ts.isConstructorDeclaration(p) || ts.isGetAccessorDeclaration(p) || ts.isSetAccessorDeclaration(p)) {
      const klass = p.parent as ts.ClassLikeDeclaration;
      found = `${klass.name?.text ?? "<anonymous class>"}.${ts.isConstructorDeclaration(p) ? "constructor" : p.name.getText()}`;
    } else if ((ts.isArrowFunction(p) || ts.isFunctionExpression(p)) && p.parent && ts.isVariableDeclaration(p.parent) && ts.isSourceFile(p.parent.parent.parent.parent)) found = p.parent.name.getText();
  }
  return found;
}
function walk(sf: ts.SourceFile, visit: (node: ts.Node) => void): void { const go = (node: ts.Node): void => { visit(node); ts.forEachChild(node, go); }; go(sf); }
const sites = (collect: (node: ts.Node) => boolean): string[] => {
  const found = new Set<string>();
  for (const { id, sf } of PARSED) walk(sf, node => { if (collect(node)) found.add(`${id}::${owner(node)}`); });
  return [...found].sort();
};

const TRANSPORT_MODULES = /^(?:node:)?(?:http|https|http2|net|tls|dgram|undici)$/;
const DISPATCHER_CLASSES = /^(?:Pool|Client|BalancedPool|RoundRobinPool|H2CClient|Agent|ProxyAgent|EnvHttpProxyAgent|Dispatcher)$/;
const NETWORK_MEMBERS = /^(?:request|get|fetch|connect|createConnection|stream|pipeline|upgrade|dispatch)$/;
const GRPC_CONSTRUCTORS = /^(?:makeGenericClientConstructor|makeClientConstructor)$/;

/** A call or construction that can put a request on the wire. */
function isTransportPrimitive(node: ts.Node): boolean {
  if (ts.isCallExpression(node)) {
    const callee = node.expression;
    if (ts.isIdentifier(callee)) return callee.text === "fetch" || callee.text === "request" || callee.text === "connect" || GRPC_CONSTRUCTORS.test(callee.text);
    if (ts.isPropertyAccessExpression(callee)) {
      if (callee.name.text === "fetch" || GRPC_CONSTRUCTORS.test(callee.name.text)) return true;
      const receiver = callee.expression.getText();
      return /^(?:http|https|http2|net|tls|undici|globalThis)$/.test(receiver) && NETWORK_MEMBERS.test(callee.name.text);
    }
  }
  if (ts.isNewExpression(node)) {
    const name = ts.isIdentifier(node.expression) ? node.expression.text : ts.isPropertyAccessExpression(node.expression) ? node.expression.name.text : "";
    return DISPATCHER_CLASSES.test(name) || name === "Client" || /^(?:Socket|Server)$/.test(name);
  }
  if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
    const spec = ts.isImportDeclaration(node) ? node.moduleSpecifier : node.moduleSpecifier;
    return spec !== undefined && ts.isStringLiteral(spec) && TRANSPORT_MODULES.test(spec.text);
  }
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "require") return ts.isStringLiteral(node.arguments[0]) && TRANSPORT_MODULES.test((node.arguments[0] as ts.StringLiteral).text);
  return false;
}

describe("every transport primitive in the key-carrying modules is inside the guard", () => {
  test("the exact set of sites", () => {
    expect(sites(isTransportPrimitive)).toEqual([
      "fi-core/fi-send-guard.ts::<top level>", // the imports of the transports the guard uses
      "fi-core/fi-send-guard.ts::guardOtlpGrpcExporter", // the channel it builds with proxies off
      "fi-core/fi-send-guard.ts::guardedFetch", // the one fetch
    ]);
  });
  test("a scan with a stray fetch would be caught: the scanner sees fetch, request, dispatcher construction and imports", () => {
    const sample = ts.createSourceFile("sample.ts", `import http from "node:http"; import { Pool } from "undici"; function a(){ fetch("x"); http.request("y"); new Pool("z"); undici.fetch("w"); globalThis.fetch("v"); grpc.makeGenericClientConstructor({}, "n"); }`, ts.ScriptTarget.Latest, true);
    let count = 0;
    walk(sample, node => { if (isTransportPrimitive(node)) count++; });
    expect(count).toBeGreaterThanOrEqual(8);
  });
});

describe("the one judging function is called from the three guarded entry points only", () => {
  test("guardSend", () => {
    expect(sites(node => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "guardSend")).toEqual([
      "fi-core/fi-send-guard.ts::guardOtlpGrpcExporter", "fi-core/fi-send-guard.ts::guardOtlpHttpExporter", "fi-core/fi-send-guard.ts::guardedFetch"]);
  });
  test("every fetch site in otel.ts goes through guardedFetch", () => {
    expect(sites(node => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "guardedFetch")).toEqual([
      "fi-core/otel.ts::HTTPSpanExporter.export", "fi-core/otel.ts::checkCustomEvalConfigExists", "fi-core/otel.ts::checkCustomEvalTemplateExists"]);
  });
  test("every OTLP exporter constructor goes through its guarded entry point", () => {
    expect(sites(node => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "guardOtlpHttpExporter")).toEqual(["fi-core/otel.ts::OTLPHTTPSpanExporter.constructor"]);
    expect(sites(node => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "guardOtlpGrpcExporter")).toEqual(["fi-core/otel.ts::GRPCSpanExporter.constructor"]);
  });
  test("no code outside the guard writes an outgoing key header: the writers are the decider's reader and the auto route factory call only", () => {
    const KEY_HEADER = /^x-(?:api|secret)-key$/i;
    const writers = sites(node => {
      if (ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node)) { const name = node.name; return (ts.isStringLiteralLike(name) || ts.isIdentifier(name)) && KEY_HEADER.test(name.text); }
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && ["set", "add", "append"].includes(node.expression.name.text)) return node.arguments.some(arg => ts.isStringLiteralLike(arg) && KEY_HEADER.test(arg.text));
      return false;
    });
    expect(writers).toEqual(["fi-core/fi-request-policy.ts::readEnvKeys", "traceai_auto/index.ts::init"]);
  });
});
