import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import * as ts from "typescript";

/**
 * Static pins for the one decider (fi-request-policy.ts, decideFiRequest). They read the code with the TypeScript compiler API,
 * so arrow functions, object-literal keys, computed template pieces and nested closures are all seen, and assert:
 *   1. the exact set of functions across fi-core/src and traceai_auto/src that mention the key names (as a string,
 *      template piece, regular expression, identifier or property name);
 *   2. the only functions that read FI_API_KEY / FI_SECRET_KEY from an environment object;
 *   3. the only functions that write key-named headers/metadata;
 *   4. every site that hands a headers/metadata container to a transport (fetch, super({ headers | metadata | url }),
 *      new grpc.Metadata, an OTLP delegate) calls the decider first, or is pinned as consuming an already-decided set;
 *   5. the decider is called from exactly the pinned sites, and the loopback decision is made in exactly the pinned places.
 * A new site fails here until it is added with its role. See FI_KEY_ATTACH_SITES.md.
 */
const PACKAGES = join(__dirname, "..", "..", "..");
const ROOTS = [{ pkg: "fi-core", dir: join(PACKAGES, "fi-core", "src") }, { pkg: "traceai_auto", dir: join(PACKAGES, "traceai_auto", "src") }];

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    if (entry.isDirectory()) return entry.name === "__tests__" || entry.name === "generated" ? [] : sourceFiles(join(dir, entry.name));
    return entry.name.endsWith(".ts") && !entry.name.endsWith(".d.ts") ? [join(dir, entry.name)] : [];
  });
}
interface Parsed { id: string; sf: ts.SourceFile }
const PARSED: Parsed[] = ROOTS.flatMap(({ pkg, dir }) => sourceFiles(dir).map(path => ({
  id: `${pkg}/${relative(dir, path).replace(/\\/g, "/")}`, sf: ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true),
})));

/** The outermost named function containing `node`: `Class.method`, a top-level function, or a top-level `const name = (...) =>`. */
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
const sites = (collect: (node: ts.Node, sf: ts.SourceFile) => boolean): string[] => {
  const found = new Set<string>();
  for (const { id, sf } of PARSED) walk(sf, node => { if (collect(node, sf)) found.add(`${id}::${owner(node)}`); });
  return [...found].sort();
};
const textOf = (node: ts.Node): string | undefined => {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isRegularExpressionLiteral(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node)
    || ts.isTemplateTail(node) || ts.isIdentifier(node) || ts.isPrivateIdentifier(node)) return node.text ?? node.getText();
  return undefined;
};

const MENTIONS = /FI_API_KEY|FI_SECRET_KEY|x-api-key|x-secret-key|api[-_]?key|secret[-_]?key/i;
const ENV_KEY = /^(?:FI_API_KEY|FI_SECRET_KEY)$/;
const KEY_HEADER = /^x-(?:api|secret)-key$/i;

describe("the functions that mention the key names are exactly the pinned ones", () => {
  test("fi-core and traceai_auto", () => {
    expect(sites(node => { const text = textOf(node); return text !== undefined && MENTIONS.test(text); })).toEqual([
      // fi-core: the decider module (names, reader), and the privacy placeholder text
      "fi-core/fi-request-policy.ts::<top level>", // KEY_NAMES
      "fi-core/fi-request-policy.ts::assertNoKeysInAmbientHeaders", // reads the key values only to refuse them in ambient OTLP headers; sends nothing
      "fi-core/fi-request-policy.ts::fiBoundHeaders",
      "fi-core/fi-request-policy.ts::hasCompleteKeyPair",
      "fi-core/fi-request-policy.ts::readEnvKeys",
      "fi-core/fi-send-guard.ts::keyValues", // reads the FI key values only to refuse them under another header name; sends nothing
      "fi-core/trace/trace-config/piiRedaction.ts::<top level>",
      // traceai_auto: credential sources and the route-factory call; the decision itself is made in fi-core
      "traceai_auto/config.ts::<top level>",
      "traceai_auto/config.ts::credentialPair",
      "traceai_auto/config.ts::maskConfig",
      "traceai_auto/config.ts::resolveConfig",
      "traceai_auto/index.ts::credentialsWarning",
      "traceai_auto/index.ts::guardedExporter",
      "traceai_auto/index.ts::init",
    ]);
  });
  test("only readEnvKeys reads the FI key variables in fi-core; only resolveConfig reads them in traceai_auto (as one credential source, never sent by itself)", () => {
    const readers = sites(node => {
      if (ts.isPropertyAccessExpression(node)) return ENV_KEY.test(node.name.text);
      if (ts.isElementAccessExpression(node)) return ts.isStringLiteralLike(node.argumentExpression) && ENV_KEY.test(node.argumentExpression.text);
      return false;
    });
    expect(readers).toEqual(["fi-core/fi-request-policy.ts::readEnvKeys", "traceai_auto/config.ts::resolveConfig"]);
  });
  test("only the decider module writes key-named headers or metadata; traceai_auto only hands a pair to the route factory", () => {
    const writers = sites(node => {
      if (ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node)) { const name = node.name; return (ts.isStringLiteralLike(name) || ts.isIdentifier(name)) && KEY_HEADER.test(name.text); }
      if (ts.isElementAccessExpression(node)) return ts.isStringLiteralLike(node.argumentExpression) && KEY_HEADER.test(node.argumentExpression.text);
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && ["set", "add", "append"].includes(node.expression.name.text))
        return node.arguments.some(arg => ts.isStringLiteralLike(arg) && KEY_HEADER.test(arg.text));
      return false;
    });
    expect(writers).toEqual(["fi-core/fi-request-policy.ts::readEnvKeys", "traceai_auto/index.ts::init"]);
  });
});

/** Whether `name` is declared in the function around `node` with a type or initializer that mentions Metadata. */
function isMetadataBinding(node: ts.Node, name: string): boolean {
  let fn: ts.Node | undefined = node;
  while (fn && !ts.isFunctionLike(fn)) fn = fn.parent;
  let found = false;
  if (fn) walk(fn.getSourceFile(), inner => {
    if (found || inner.pos < fn!.pos || inner.end > fn!.end) return;
    if ((ts.isParameter(inner) || ts.isVariableDeclaration(inner)) && inner.name.getText() === name && /Metadata/.test(`${inner.type?.getText() ?? ""} ${ts.isVariableDeclaration(inner) ? inner.initializer?.getText() ?? "" : ""}`)) found = true;
  });
  return found;
}
/** Sites that hand a headers or metadata container to a transport. */
function isAttach(node: ts.Node): boolean {
  if (ts.isCallExpression(node)) {
    const callee = node.expression;
    if (ts.isIdentifier(callee) && ["fetch", "createOtlpHttpExportDelegate", "createOtlpGrpcExportDelegate"].includes(callee.text)) return true;
    // The fetch captured when the guard loads is the send of a guarded fetch.
    if (ts.isPropertyAccessExpression(callee) && callee.name.text === "fetch" && callee.expression.getText() === "nodeOwn") return true;
    // Any super(...) call with arguments in an exporter class hands its options, whatever they are called, to the OTLP exporter.
    if (callee.kind === ts.SyntaxKind.SuperKeyword) {
      const klass = node.parent && ts.findAncestor(node, ts.isClassDeclaration);
      return node.arguments.length > 0 && /Exporter$/.test(klass?.name?.text ?? "");
    }
    if (ts.isPropertyAccessExpression(callee) && ["set", "add", "append"].includes(callee.name.text)
      && (/metadata/i.test(callee.expression.getText()) || (ts.isIdentifier(callee.expression) && isMetadataBinding(node, callee.expression.text)))) return true;
  }
  if (ts.isNewExpression(node) && /(?:^|\.)Metadata$/.test(node.expression.getText())) return true;
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken && /(?:\._delegate|^this\.headers)$/.test(node.left.getText())) return true;
  return false;
}
const isDecide = (node: ts.Node): boolean => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && ["decide", "decideFiRequest"].includes(node.expression.text);

/**
 * role "decides": calls the decider before the first attach in the function.
 * role "consumes": receives a set the caller already decided; reads no key name and calls no environment reader.
 */
const ATTACH: Record<string, "decides" | "consumes"> = {
  "fi-core/otel.ts::HTTPSpanExporter.constructor": "decides",
  "fi-core/otel.ts::OTLPHTTPSpanExporter.constructor": "decides",
  "fi-core/otel.ts::FITracerProvider.constructor": "decides", // keeps the decided headers; the default exporter decides again on them
  "fi-core/otel.ts::GRPCSpanExporter.constructor": "decides",
  "fi-core/fi-send-guard.ts::guardedFetch": "consumes", // the one fetch: judges the final headers it is given, which the callers decided
  "fi-core/fi-send-guard.ts::guardOtlpGrpcExporter": "consumes", // the one gRPC client: the interceptor judges the final metadata against the decided set
  "fi-core/otlp-header-isolation.ts::isolateHttpExporterHeaders": "consumes", // rebuilds the delegate from the decided headers
  "fi-core/otlp-header-isolation.ts::isolateGrpcExporterHeaders": "consumes", // rebuilds the delegate from the decided metadata
  "fi-core/fi-request-policy.ts::decideFiRequest": "decides", // builds the final metadata
  "fi-core/fi-request-policy.ts::copyMetadata": "consumes", // only called by the decider
};
describe("every site that hands headers or metadata to a transport goes through the decider", () => {
  test("the set of attach sites is exactly the pinned one", () => {
    expect(sites(isAttach)).toEqual(Object.keys(ATTACH).sort());
  });
  const byId = (id: string): { sf: ts.SourceFile; nodes: ts.Node[] } => {
    const [file, name] = id.split("::");
    const sf = PARSED.find(p => p.id === file)!.sf, nodes: ts.Node[] = [];
    walk(sf, node => { if (owner(node) === name) nodes.push(node); });
    return { sf, nodes };
  };
  test.each(Object.entries(ATTACH).filter(([, role]) => role === "decides"))("%s calls the decider before it attaches", id => {
    const { nodes } = byId(id);
    const decide = nodes.filter(isDecide).map(node => node.getStart()), attach = nodes.filter(isAttach).map(node => node.getStart());
    if (id.endsWith("::decideFiRequest")) return; // the decider itself
    expect(decide.length).toBeGreaterThan(0);
    expect(Math.min(...decide)).toBeLessThan(Math.min(...attach));
  });
  test.each(Object.entries(ATTACH).filter(([, role]) => role === "consumes"))("%s reads no key name and never calls the decider's environment reader", id => {
    const { nodes } = byId(id);
    expect(nodes.filter(node => { const text = textOf(node); return text !== undefined && MENTIONS.test(text); })).toEqual([]);
    expect(nodes.filter(node => ts.isCallExpression(node) && /readEnvKeys|fiBoundHeaders/.test(node.expression.getText()))).toEqual([]);
  });
});

describe("the decider is called from the pinned sites only", () => {
  test("decideFiRequest is referenced only by its module, by the send guard and by the one wrapper in otel.ts", () => {
    expect(sites(node => ts.isIdentifier(node) && node.text === "decideFiRequest")).toEqual(["fi-core/fi-request-policy.ts::decideFiRequest", "fi-core/fi-send-guard.ts::<top level>", "fi-core/fi-send-guard.ts::guardSend", "fi-core/otel.ts::<top level>", "fi-core/otel.ts::decide"]);
  });
  test("the wrapper is called by every exporter constructor and eval check, and nothing else", () => {
    expect(sites(node => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "decide")).toEqual([
      "fi-core/otel.ts::FITracerProvider.constructor", "fi-core/otel.ts::GRPCSpanExporter.constructor", "fi-core/otel.ts::HTTPSpanExporter.constructor",
      "fi-core/otel.ts::OTLPHTTPSpanExporter.constructor", "fi-core/otel.ts::checkCustomEvalConfigExists", "fi-core/otel.ts::checkCustomEvalTemplateExists",
    ]);
  });
  test("the route factory reaches the decider only through createSpanExporter, and traceai_auto only through the factory", () => {
    const calls = (name: string) => sites(node => ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === name);
    expect(calls("createSpanExporter")).toContain("fi-core/fi-route.ts::createFiRouteSpanExporter");
    expect(calls("createFiRouteSpanExporter")).toEqual(["traceai_auto/index.ts::init"]);
    // traceai_auto never imports a transport or a header builder of its own for the FI route
    expect(sites(node => ts.isIdentifier(node) && /^(?:decideFiRequest|createSpanExporter|GRPCSpanExporter|HTTPSpanExporter)$/.test(node.text) && node.getSourceFile().fileName.includes("traceai_auto"))).toEqual([]);
  });
});

describe("loopback is decided on the host as written, in the pinned places only", () => {
  test("only these functions name the loopback set or call the literal-loopback decider", () => {
    expect(sites(node => ts.isIdentifier(node) && /^(?:LOOPBACK_HOSTS|isLiteralLoopback)$/.test(node.text))).toEqual([
      "fi-core/fi-endpoint.ts::<top level>", "fi-core/fi-endpoint.ts::isLiteralLoopback", "fi-core/fi-request-policy.ts::<top level>" /* the import */,
      "fi-core/fi-request-policy.ts::decideFiRequest", "fi-core/fi-request-policy.ts::hostClass", "fi-core/fi-request-policy.ts::plaintextRule"]);
  });
  test("the decider reads the written host and agrees it with the parsed one; it never reads the parsed host alone", () => {
    const parsed = PARSED.find(p => p.id === "fi-core/fi-endpoint.ts")!.sf;
    let body = "";
    walk(parsed, node => { if (ts.isFunctionDeclaration(node) && node.name?.text === "isLiteralLoopback") body = node.getText(); });
    expect(body).toMatch(/writtenHost\(/);
    expect(body).toMatch(/hostsAgree\(/);
    expect(body).not.toMatch(/\.hostname/);
  });
});
