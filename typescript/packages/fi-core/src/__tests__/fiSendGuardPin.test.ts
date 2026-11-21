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
  { pkg: "fi-core", dir: join(PACKAGES, "fi-core", "src"), files: ["otel.ts", "fi-request-policy.ts", "otlp-header-isolation.ts", "fi-route.ts", "fi-route-registry.ts", "fi-send-guard.ts", "fi-transport.ts", "fi-endpoint.ts", "secrets.ts"] },
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

const TRANSPORT_MODULES = /^(?:node:)?(?:http|https|http2|net|tls|dgram|undici)$|^undici\/|^@grpc\/grpc-js$|^@opentelemetry\/(?:exporter-trace-otlp-(?:http|grpc|proto)|otlp-exporter-base(?:\/.*)?|otlp-grpc-exporter-base)$/;
const DISPATCHER_CLASSES = /^(?:Pool|Client|BalancedPool|RoundRobinPool|H2CClient|Agent|ProxyAgent|EnvHttpProxyAgent|Dispatcher|Channel|Server|Socket|WebSocket|EventSource|XMLHttpRequest)$/;
const NETWORK_MEMBERS = /^(?:request|get|fetch|connect|createConnection|stream|pipeline|upgrade|dispatch|createServer)$/;
const GRPC_MEMBERS = /^(?:Client|Channel|makeGenericClientConstructor|makeClientConstructor|credentials)$/;
const TRANSPORT_GLOBALS = /^(?:fetch|WebSocket|EventSource|XMLHttpRequest)$/;

const specifierOf = (node: ts.Node): string | undefined => {
  if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) return node.moduleSpecifier.text;
  if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference) && ts.isStringLiteral(node.moduleReference.expression)) return node.moduleReference.expression.text;
  if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === "require")) && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) return (node.arguments[0] as ts.StringLiteralLike).text;
  return undefined;
};
const isTransportModule = (spec: string | undefined): boolean => spec !== undefined && TRANSPORT_MODULES.test(spec);
/** An element or property access that names a global transport, however the global object is reached. */
const namesTransportGlobal = (node: ts.Node): boolean => {
  // `typeof globalThis.fetch === "function"` only asks whether the global exists; nothing is sent.
  if (node.parent && ts.isTypeOfExpression(node.parent)) return false;
  if (ts.isElementAccessExpression(node)) return ts.isStringLiteralLike(node.argumentExpression) && TRANSPORT_GLOBALS.test(node.argumentExpression.text);
  if (ts.isPropertyAccessExpression(node) && /^(?:globalThis|global|self|window)$/.test(node.expression.getText())) return TRANSPORT_GLOBALS.test(node.name.text);
  return false;
};

/**
 * A reference that can put a request on the wire, in any spelling: a transport global used in any way (call, alias, .call/.apply/.bind,
 * Reflect.apply, destructuring), a transport module imported, required, dynamically imported or `import x = require()`d, a network member of
 * a transport module object, a dispatcher, channel or client constructed, a grpc client, channel or credentials constructor.
 */
function isTransportPrimitive(node: ts.Node): boolean {
  if (isTransportModule(specifierOf(node))) return true;
  if (namesTransportGlobal(node)) return true;
  // A bare identifier that is a transport global, wherever it appears except as a declaration name or a property name.
  if (ts.isIdentifier(node) && TRANSPORT_GLOBALS.test(node.text)) {
    const parent = node.parent;
    if (ts.isPropertyAccessExpression(parent) && parent.name === node) return false;
    if ((ts.isPropertyAssignment(parent) || ts.isPropertySignature(parent) || ts.isPropertyDeclaration(parent)) && parent.name === node) return false;
    if ((ts.isFunctionDeclaration(parent) || ts.isMethodDeclaration(parent) || ts.isVariableDeclaration(parent) || ts.isParameter(parent) || ts.isImportSpecifier(parent)) && (parent as any).name === node) return false;
    if (ts.isTypeReferenceNode(parent) || ts.isTypeQueryNode(parent)) return false;
    return true;
  }
  if (ts.isCallExpression(node)) {
    const callee = node.expression;
    // ownFetch is fi-core's own transport (undici's agent and request API by path), and newDirectAgent / newProxyAgent build its dispatchers: every key-carrying send and dispatcher goes through them.
    if (ts.isIdentifier(callee)) return /^(?:request|connect|ownFetch|newDirectAgent|newProxyAgent)$/.test(callee.text) || GRPC_MEMBERS.test(callee.text);
    if (ts.isPropertyAccessExpression(callee)) {
      if (GRPC_MEMBERS.test(callee.name.text) || (callee.name.text === "createInsecure" || callee.name.text === "createSsl")) return true;
      const receiver = callee.expression.getText();
      // The captured fetch: Node's own, taken when the guard loads, is the one reference every key-carrying fetch goes through.
      if (receiver === "nodeOwn" && callee.name.text === "fetch") return true;
      if (/^(?:http|https|http2|net|tls|undici|globalThis|global|Reflect)$/.test(receiver) && NETWORK_MEMBERS.test(callee.name.text)) return true;
      if (receiver === "Reflect" && callee.name.text === "apply") return /\b(?:fetch|request|connect)\b/.test(node.arguments[0]?.getText() ?? "");
    }
  }
  if (ts.isNewExpression(node)) {
    const name = ts.isIdentifier(node.expression) ? node.expression.text : ts.isPropertyAccessExpression(node.expression) ? node.expression.name.text : "";
    return DISPATCHER_CLASSES.test(name) || GRPC_MEMBERS.test(name) || /^_?OTLP\w*Exporter$/.test(name);
  }
  if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) && /^grpc$/.test(node.expression.text) && GRPC_MEMBERS.test(node.name.text)) return true;
  return false;
}

describe("every transport primitive in the key-carrying modules is inside the guard", () => {
  test("the exact set of sites", () => {
    expect(sites(isTransportPrimitive)).toEqual([
      "fi-core/fi-request-policy.ts::<top level>", // the grpc import for Metadata, which the decider builds as data
      "fi-core/fi-send-guard.ts::<top level>", // the imports of the transports the guard uses
      "fi-core/fi-send-guard.ts::channelCredentials", // the one place a channel's credentials are constructed
      "fi-core/fi-send-guard.ts::direct", // the one direct transport: fi-core's own undici Agent, TLS verification on
      "fi-core/fi-send-guard.ts::guardOtlpGrpcExporter", // the channel it builds with proxies off
      "fi-core/fi-send-guard.ts::guardOtlpHttpExporter", // the OTLP HTTP exporter's send, through fi-core's own undici fetch
      "fi-core/fi-send-guard.ts::guardedFetch", // the one fetch: fi-core's own undici fetch, never the global slot or globalThis.fetch
      "fi-core/fi-send-guard.ts::through", // the SDK's https proxy: a CONNECT-only undici ProxyAgent of fi-core's own
      "fi-core/fi-transport.ts::<top level>", // the one module that imports undici, by deep path (agent, proxy-agent, api): never the package index or lib/global.js
      "fi-core/otel.ts::<top level>", // the imports of the OTLP exporter packages the SDK's exporter classes extend
      "fi-core/otel.ts::createSpanExporter", // constructs the SDK's own exporter, whose constructor calls its guarded entry point
      "fi-core/otlp-header-isolation.ts::<top level>", // the exporter packages' own configuration functions, used to rebuild a delegate
    ]);
  });
  test("each bypass form has a fixture, and the scanner flags every one", () => {
    const dir = join(__dirname, "pin-bypass-fixtures");
    const files = readdirSync(dir).filter(name => name.endsWith(".txt"));
    expect(files.length).toBeGreaterThanOrEqual(20);
    for (const name of files) {
      const sample = ts.createSourceFile(name, readFileSync(join(dir, name), "utf8"), ts.ScriptTarget.Latest, true);
      let count = 0;
      walk(sample, node => { if (isTransportPrimitive(node)) count++; });
      expect({ fixture: name, flagged: count > 0 }).toEqual({ fixture: name, flagged: true });
    }
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
