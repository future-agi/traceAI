import { createTraceState } from "@opentelemetry/api";
import * as wire from "../src/wire/privacy-runtime";
import * as core from "@traceai/fi-core/privacy";
import { privacyFilter } from "../src/export-privacy";

const hidden = "<redacted>";
const freeText = (value: string, secrets: readonly string[] = []): string =>
  wire.scrubValue(value, secrets, "", 0, false, undefined, undefined, false);

function stableText(raw: string, expected: string, secrets: readonly string[] = []): void {
  for (const run of [wire.scrubText, wire.scrubAttribute, freeText]) {
    expect(run(raw, secrets)).toBe(expected);
    expect(run(expected, secrets)).toBe(expected);
  }
}
function snapshot(attributes: any): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return {
    _spanContext: ctx, spanContext() { return this._spanContext; },
    name: "application", kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2],
    attributes, status: { code: 1 }, resource: { attributes },
    instrumentationScope: { name: "library", attributes },
    events: [{ name: "event", time: [1, 2], attributes }], links: [{ context: ctx, attributes }],
  };
}
function view(span: any): any {
  const context = (ctx: any) => ({ traceId: ctx.traceId, spanId: ctx.spanId, traceFlags: ctx.traceFlags, state: ctx.traceState?.serialize() });
  return {
    name: span.name, kind: span.kind, startTime: span.startTime, endTime: span.endTime,
    duration: span.duration ?? span._duration, context: context(span.spanContext()),
    parent: span.parentSpanContext && context(span.parentSpanContext), attributes: span.attributes,
    status: span.status, resource: span.resource.attributes, scope: span.instrumentationScope,
    events: span.events, links: span.links.map((link: any) => ({ ...link, context: context(link.context) })),
  };
}
function surfaces(span: any): any[] {
  expect(span.events).toHaveLength(1); expect(span.links).toHaveLength(1);
  return [span.attributes, span.resource.attributes, span.instrumentationScope.attributes, span.events[0].attributes, span.links[0].attributes];
}

test("I1-VALUE shared wire/core implementation is the exercised route", () => {
  expect(wire.scrubValue).toBe(core.scrubValue);
  expect(wire.scrubAttribute).toBe(core.scrubAttribute);
  expect(wire.scrubText).toBe(core.scrubText);
});

test("I1-NUMERIC-ROOT-PRESERVES-REGISTERED-SECRET-PRECEDENCE", () => {
  const secret = "27182818", known = [secret], number = 27182818;
  expect(String(number)).toBe(secret); expect(secret).toHaveLength(8);
  stableText(secret, hidden, known);
  expect(wire.scrubValue(number, known)).toBe(hidden);
  expect(wire.scrubValue(hidden, known)).toBe(hidden);
  expect(wire.scrubValue(number, known, "usage.input_tokens")).toBe(hidden);
  const quoted = JSON.stringify(secret), cleanQuoted = JSON.stringify(hidden);
  expect(wire.scrubAttribute(quoted, known)).toBe(cleanQuoted);
  expect(wire.scrubAttribute(cleanQuoted, known)).toBe(cleanQuoted);
  expect(wire.scrubValue(27182819, known)).toBe(27182819);
  stableText("27182819", "27182819", known);
  expect(wire.scrubAttribute("  27182819\n", known)).toBe("  27182819\n");
  for (const value of [NaN, Infinity, -Infinity]) expect(wire.scrubValue(value, known)).toBe(String(value));
  expect(wire.scrubValue([false, null, 7], known)).toEqual([false, null, 7]);
  expect(wire.scrubText("longer-private-value private-value short", ["private-value", "longer-private-value", "short", hidden])).toBe("<redacted> <redacted> short");
});

test("I1-NUMERIC-JSON-DEPTH-AND-LEXEMES", () => {
  const secret = "31415926", known = [secret];
  const raw = { n: 31415926, values: [1, 31415926, 3], text: secret, arguments: JSON.stringify({ n: 31415926, city: "Oslo" }) };
  const before = JSON.stringify(raw);
  expect(before).toContain(secret);
  const expected = { n: hidden, values: [1, hidden, 3], text: hidden, arguments: JSON.stringify({ n: hidden, city: "Oslo" }) };
  expect(wire.scrubValue(raw, known)).toEqual(expected);
  expect(wire.scrubValue(expected, known)).toEqual(expected);
  for (const run of [wire.scrubAttribute, wire.scrubValue]) {
    const clean = run(before, known);
    expect(JSON.parse(clean)).toEqual(expected);
    expect(run(clean, known)).toBe(clean);
    const double = run(JSON.stringify(before), known);
    expect(JSON.parse(JSON.parse(double))).toEqual(expected);
    expect(run(double, known)).toBe(double);
  }
  for (const encoded of [before, JSON.stringify(before), JSON.stringify(JSON.stringify(before))]) {
    const clean = wire.scrubText("401 " + encoded + " retry", known);
    expect(clean.startsWith("401 ")).toBe(true); expect(clean.endsWith(" retry")).toBe(true);
    expect(clean).not.toContain(secret);
    let decoded: any = JSON.parse(clean.slice(4, -6));
    while (typeof decoded === "string") decoded = JSON.parse(decoded);
    expect(decoded).toEqual(expected);
    expect(wire.scrubText(clean, known)).toBe(clean);
  }
  expect(JSON.stringify(raw)).toBe(before);
  expect(wire.scrubAttribute('{"n":9314159260,"city": "Oslo"}', known)).toBe('{"n":"9<redacted>0","city": "Oslo"}');
  expect(wire.scrubAttribute('{"city": "Oslo"}', known)).toBe('{"city": "Oslo"}');
});

test.each(["10000000000000001", "1.25000000e+3", "-16180339", "0.12345678", "1.23456789e+400"])
("I1-NUMERIC-ORIGINAL-TOKEN %s", secret => {
  expect(secret.length).toBeGreaterThanOrEqual(8);
  const raw = ` { "n" : ${secret}, "values": [1, ${secret}, 3], "city": "Oslo" } `;
  const expected = ' { "n" : "<redacted>", "values": [1, "<redacted>", 3], "city": "Oslo" } ';
  expect(raw).toContain(secret);
  for (const run of [wire.scrubAttribute, wire.scrubValue]) {
    expect(run(raw, [secret])).toBe(expected);
    expect(run(expected, [secret])).toBe(expected);
  }
  expect(wire.scrubAttribute(secret, [secret])).toBe(hidden);
  expect(wire.scrubText("401 " + raw, [secret])).toBe("401 " + expected);
  expect(wire.scrubText("401 " + expected, [secret])).toBe("401 " + expected);
});

test("I1-NUMERIC-LOSSY-ALIASES-AND-STRUCTURAL-EDITS", () => {
  const exact = "10000000000000001", rounded = "10000000000000000";
  const untouched = '{"n":10000000000000000,"city": "Oslo"}';
  expect(wire.scrubAttribute(untouched, [exact])).toBe(untouched);
  // A different field changing must not round an unregistered lexeme into a secret.
  const input = '{"n":10000000000000001,"exp":1.25000000e+3,"minus":-0,"password":"private","city":"Oslo"}';
  const expected = '{"n":10000000000000001,"exp":1.25000000e+3,"minus":-0,"password":"<redacted>","city":"Oslo"}';
  expect(input).toContain("private");
  for (const run of [wire.scrubAttribute, wire.scrubValue]) {
    expect(run(input, [rounded])).toBe(expected);
    expect(run(expected, [rounded])).toBe(expected);
  }
  expect(wire.scrubText("401 " + input, [rounded])).toBe("401 " + expected);
  expect(wire.scrubText("401 " + expected, [rounded])).toBe("401 " + expected);
  const duplicate = '{"n":10000000000000001,"n":7,"city":"Oslo"}';
  expect(wire.scrubAttribute(duplicate, [exact])).toBe('{"n":"<redacted>","n":7,"city":"Oslo"}');
  const ordered = '{"2":1.25000000e+3,"1":10000000000000001,"password":"private"}';
  const orderedClean = wire.scrubAttribute(ordered, [rounded]);
  expect(orderedClean).toContain('"1":10000000000000001');
  expect(orderedClean).toContain('"2":1.25000000e+3');
  expect(JSON.parse(orderedClean).password).toBe(hidden);
  expect(wire.scrubAttribute(orderedClean, [rounded])).toBe(orderedClean);
});

test("I1-NUMERIC-MALFORMED-JSON-AND-MARKER-CONTROLS", () => {
  const secret = "14142135", known = [secret], malformed = '401 {"n":14142135';
  expect(malformed).toContain(secret);
  stableText(malformed, "401 <omitted: incomplete audio>", known);
  stableText('{"city":', '{"city":', known);
  const payload = { n: 14142135, marker: "__REDACTED__", nested: JSON.stringify({ n: 14142135, marker: "__REDACTED__" }) };
  const raw = JSON.stringify(JSON.stringify(payload)), clean = wire.scrubAttribute(raw, known);
  expect(raw).toContain(secret);
  expect(JSON.parse(JSON.parse(clean))).toEqual({ n: hidden, marker: "__REDACTED__", nested: JSON.stringify({ n: hidden, marker: "__REDACTED__" }) });
  expect(wire.scrubAttribute(clean, known)).toBe(clean);
  // Complete registered strings remain authoritative even if they are valid JSON.
  const whole = '{"city":"Oslo"}';
  expect(wire.scrubAttribute(whole, [whole])).toBe(hidden);
  expect(wire.scrubValue(whole, [whole])).toBe(hidden);
  expect(wire.scrubValue({ arguments: whole }, [whole])).toEqual({ arguments: hidden });
});

test("I1-NUMERIC-SNAPSHOT-ALL-SURFACES-WITH-SAFE-COUNTERS", () => {
  const secret = "87654321", n = 87654321;
  const attrs = { n, numbers: [1, n, 3], literal: secret, json: JSON.stringify({ n, ok: false, none: null }), safe: "ordinary text", usage: 7, "usage.input_tokens": n, flag: false };
  const source = snapshot(attrs);
  source._spanContext.traceState = createTraceState(`vendor=${secret}`);
  source.parentSpanContext = { ...source._spanContext };
  source.instrumentationScope = { name: secret, version: secret, schemaUrl: secret, attributes: attrs };
  source.status = { code: 2, message: secret }; source.events[0].name = secret;
  const before = JSON.stringify(view(source)); expect(before).toContain(secret);
  const filter = privacyFilter({ hideErrorDetails: false }, [secret]);
  const assertMap = (surface: any) => {
    expect(surface.n).toBe(hidden); expect(surface.numbers).toEqual([1, hidden, 3]);
    expect(surface.literal).toBe(hidden); expect(JSON.parse(surface.json)).toEqual({ n: hidden, ok: false, none: null });
    expect(surface.safe).toBe("ordinary text"); expect(surface.usage).toBe(7); expect(typeof surface.usage).toBe("number");
    expect(surface["usage.input_tokens"]).toBe(hidden); expect(surface.flag).toBe(false);
  };
  const direct = filter.attributes(attrs); assertMap(direct); expect(filter.attributes(direct)).toEqual(direct);
  const output = filter.span(source, [], { extraNumber: n, extraSafe: "extra text" });
  for (const surface of surfaces(output)) assertMap(surface);
  expect(output.resource.attributes.extraNumber).toBe(hidden); expect(output.resource.attributes.extraSafe).toBe("extra text");
  expect(output.name).toBe("application"); expect(output.startTime).toEqual([1, 2]); expect(output.endTime).toEqual([3, 4]);
  expect(output.duration).toEqual([2, 2]); expect(output.spanContext().traceId).toBe(source._spanContext.traceId);
  expect(output.spanContext().spanId).toBe(source._spanContext.spanId);
  expect(output.spanContext().traceState?.serialize()).toBe("vendor=<redacted>");
  expect(output.parentSpanContext?.traceState?.serialize()).toBe("vendor=<redacted>");
  expect(output.status.message).toBe(hidden); expect(output.events[0].name).toBe(hidden);
  expect(output.instrumentationScope.name).toBe(hidden); expect(output.instrumentationScope.version).toBe(hidden);
  expect(output.instrumentationScope.schemaUrl).toBe(hidden);
  expect(JSON.stringify(view(output))).not.toContain(secret);
  expect(view(filter.span(output))).toEqual(view(output));
  expect(JSON.stringify(view(source))).toBe(before);
  const named = filter.span({ ...source, name: secret }); expect(named.name).toBe(hidden);
  expect(named.attributes.safe).toBe("ordinary text");
});

test("I1-URL-OWNERSHIP-NONFIXTURE-AUTHORITIES", () => {
  const suffix = "&safe=%2f+YES&dup=one&dup=two#anchor?token=ordinary";
  for (const host of ["abcdef.invalid", "longordinary.example", "cdn.test"]) for (const scheme of ["http", "https"]) {
    const url = `${scheme}://${host}/path?access_token=private-value${suffix}`;
    const expected = `${scheme}://${host}/path?access_token=<redacted>${suffix}`;
    expect(url).toContain("private-value");
    stableText(url, expected);
    stableText(`see "${url}" after`, `see "${expected}" after`);
    stableText(`${url} then ${url} retry`, `${expected} then ${expected} retry`);
    expect(wire.scrubValue([url, "ordinary text"])).toEqual([expected, "ordinary text"]);
    const raw = JSON.stringify({ url, arguments: JSON.stringify({ url }) });
    const clean = wire.scrubAttribute(raw);
    expect(JSON.parse(clean)).toEqual({ url: expected, arguments: JSON.stringify({ url: expected }) });
    expect(wire.scrubAttribute(clean)).toBe(clean);
    expect(JSON.parse(JSON.parse(wire.scrubAttribute(JSON.stringify(raw)))).url).toBe(expected);
    expect(url).toContain("private-value");
  }
});

test("I1-URL-COMPONENT-PRIVACY-WITHOUT-NORMALIZATION", () => {
  const suffix = "&model=author's-model&safe=%2f+YES&dup=1&dup=2&empty=#fragment?token=ordinary";
  const url = "https://user:pass@abcdef.invalid/p?%74oken=secret-value" + suffix;
  const expected = "https://<redacted>@abcdef.invalid/p?%74oken=<redacted>" + suffix;
  expect(url).toContain("secret-value"); stableText(url, expected);
  stableText(`see '${url}' after`, `see '${expected}' after`);
  stableText("see /path?%74oken=private-value&safe=yes next", "see /path?%74oken=<redacted>&safe=yes next");
  stableText("see ?token=private-value&safe=yes next", "see ?token=<redacted>&safe=yes next");
  stableText("see #fragment?token=ordinary next", "see #fragment?token=ordinary next");
  stableText("https://abcdef.invalid/?%invalid=safe&model=%252Fplain", "https://abcdef.invalid/?%invalid=safe&model=%252Fplain");
  stableText("https://abcdef.invalid/?next=https%3A%2F%2Fnested.invalid%2F%3Ftoken%3Dordinary", "https://abcdef.invalid/?next=https%3A%2F%2Fnested.invalid%2F%3Ftoken%3Dordinary");
  const secret = "path-private-value";
  stableText(`https://abcdef.invalid/${secret}#anchor-${secret}`, "https://abcdef.invalid/<redacted>#anchor-<redacted>", [secret]);
  stableText(`see #fragment?token=${secret} next`, "see #fragment?token=<redacted> next", [secret]);
  const shaped = "sk-" + "SYNTHETIC".repeat(4);
  stableText(`https://abcdef.invalid/p#${shaped}`, "https://abcdef.invalid/p#<redacted>");
  stableText(`token=https://abcdef.invalid/#fragment?token=${secret}`, 'token="<redacted>"');
  stableText('password={"city":"Oslo"} after', 'password="<redacted>"');
  stableText('prefix token="https://abcdef.invalid/p" suffix', 'prefix token="<redacted>" suffix');
});

test.each(["iVBORw0KGgoAAAANSUhEUg==", "/9j/4AAQ", "_9j_4AAQ", "//4AAAAA"])
("I1-URL-AND-BARE-MEDIA-IN-THE-SAME-DIAGNOSTIC %s", payload => {
  const url = "https://abcdef.invalid/?token=private-value&safe=yes";
  const raw = `Cannot decode ${payload} at ${url} please retry ordinary-id-123 __REDACTED__ data:audio/wav;base64,AQIDBA== end`;
  const expected = `Cannot decode <omitted: ${payload.length} base64 chars> at https://abcdef.invalid/?token=<redacted>&safe=yes please retry ordinary-id-123 __REDACTED__ <omitted: 8 base64 chars> end`;
  expect(raw).toContain(payload); expect(raw).toContain("AQIDBA==");
  stableText(raw, expected);
  expect(wire.scrubValue(raw)).toBe(expected);
  expect(wire.scrubValue(expected)).toBe(expected);
});

test.each([undefined, 4096])("I1-URL-QUERY-MEDIA-DATA-URI-AND-DETACHED-SURFACES (image cap %s)", base64ImageMaxLength => {
  const url = "https://abcdef.invalid/?content=AQIDBA%3D%3D&mime_type=audio%2Fpcm&client_secret=synthetic-private&model=keep#anchor";
  const expectedUrl = "https://abcdef.invalid/?content=<omitted: 8 base64 chars>&mime_type=audio%2Fpcm&client_secret=<redacted>&model=keep#anchor";
  const svg = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg">literal payload</svg>';
  const svgUrl = `https://abcdef.invalid/?content=${svg}&safe=yes#anchor`;
  const attrs = { url, literal: svg, svgUrl, png: "iVBORw0KGgoAAAANSUhEUg==", safe: "ordinary text",
    "parts.0.inlineData.mimeType": ["text/plain", "audio/wav"], "parts.0.inlineData.data": "AQIDBA==", "parts.1.inlineData.data": "AQIDBA==" };
  const source = snapshot(attrs), before = JSON.stringify(view(source));
  expect(before).toContain("AQIDBA"); expect(before).toContain("synthetic-private");
  const filter = privacyFilter({ base64ImageMaxLength }), output = filter.span(source);
  // F2 cap-0 whole-scope replacement precedes I1 media markers. The explicit
  // positive-cap twin keeps every original marker and the SVG URL's safe suffix.
  for (const surface of surfaces(output)) {
    expect(surface.url).toBe(expectedUrl);
    expect(surface.literal).toBe(base64ImageMaxLength === undefined ? "[image-redacted]" : "<omitted: data URI>");
    expect(surface.svgUrl).toBe(base64ImageMaxLength === undefined ? "[image-redacted]" : "https://abcdef.invalid/?content=<omitted: data URI>&safe=yes#anchor");
    expect(surface.png).toBe(base64ImageMaxLength === undefined ? "[image-redacted]" : "<omitted: 24 base64 chars>");
    expect(surface.safe).toBe("ordinary text");
    expect(surface["parts.0.inlineData.data"]).toBe("<omitted: 8 base64 chars>");
    expect(surface["parts.1.inlineData.data"]).toBe("AQIDBA==");
    for (const value of ["synthetic-private", "literal payload", attrs.png]) expect(JSON.stringify(surface)).not.toContain(value);
    expect(surface.url).not.toContain("AQIDBA");
  }
  expect(output.name).toBe("application"); expect(output.spanContext().traceId).toBe(source._spanContext.traceId);
  expect(output.startTime).toEqual([1, 2]); expect(output.endTime).toEqual([3, 4]);
  expect(view(filter.span(output))).toEqual(view(output)); expect(JSON.stringify(view(source))).toBe(before);
  const hints = wire.indexPrivacyHints(Object.entries(attrs));
  expect(wire.scrubValue(attrs["parts.0.inlineData.data"], [], "parts.0.inlineData.data", 0, false, undefined, hints)).toBe("<omitted: 8 base64 chars>");
  expect(wire.scrubValue(attrs["parts.1.inlineData.data"], [], "parts.1.inlineData.data", 0, false, undefined, hints)).toBe("AQIDBA==");
  const duplicate = "https://abcdef.invalid/?content=AQIDBA==&mime_type=text%2Fplain&mime_type=audio%2Fpcm&safe=yes";
  stableText(duplicate, "https://abcdef.invalid/?content=<omitted: 8 base64 chars>&mime_type=text%2Fplain&mime_type=audio%2Fpcm&safe=yes");
  const once = "https://abcdef.invalid/?content=AQIDBA%253D%253D&mime_type=audio%252Fpcm&safe=yes";
  stableText(once, once);
  const incomplete = "https://abcdef.invalid/?content=AQIDBA==&mime_type=text%2Fplain&safe=ok";
  // Lost MIME context also subjects the MIME value to the existing ambiguous
  // base64 rule; this packet must not relax that conservative URL policy.
  const incompleteClean = "https://abcdef.invalid/?content=<omitted: 8 base64 chars>&mime_type=<omitted: 10 base64 chars>&safe=ok";
  expect(wire.scrubAttribute(incomplete, [], true)).toBe(incompleteClean);
  expect(wire.scrubAttribute(incompleteClean, [], true)).toBe(incompleteClean);
  const nested = JSON.stringify({ url: incomplete, safe: "ordinary text", n: 7 });
  const nestedClean = wire.scrubAttribute(nested, [], true);
  expect(JSON.parse(nestedClean)).toEqual({ url: incompleteClean, safe: "ordinary text", n: 7 });
  expect(wire.scrubAttribute(nestedClean, [], true)).toBe(nestedClean);
  const diagnostic = "401 " + JSON.stringify(nested) + " retry";
  const diagnosticClean = wire.scrubAttribute(diagnostic, [], true);
  expect(JSON.parse(JSON.parse(diagnosticClean.slice(4, -6)))).toEqual({ url: incompleteClean, safe: "ordinary text", n: 7 });
  expect(wire.scrubAttribute(diagnosticClean, [], true)).toBe(diagnosticClean);
  const keyURL = JSON.stringify({ [incomplete]: "ordinary text" });
  const keyClean = wire.scrubAttribute(keyURL, [], true);
  expect(JSON.parse(keyClean)).toEqual({ [incompleteClean]: "ordinary text" });
  expect(wire.scrubAttribute(keyClean, [], true)).toBe(keyClean);
});

test("I1-PROVIDER-NUMERIC-AND-URL-RECONSTRUCTION-CONTROLS", () => {
  const secret = "22360679", url = "https://abcdef.invalid/p?model=keep#anchor";
  const token = { token: "ordinary-word", bytes: [111, 114], logprob: -0.25 };
  const safe = { n: 7, choices: [{ index: 0, message: { content: url }, logprobs: { content: [token] } }] };
  const before = JSON.stringify(safe);
  expect(wire.scrubProviderResponse(safe, [secret])).toEqual(safe);
  expect(wire.scrubProviderResponse(wire.scrubProviderResponse(safe, [secret]), [secret])).toEqual(safe);
  const numeric = { ...safe, n: 22360679 };
  const numericClean = wire.scrubProviderResponse(numeric, [secret]);
  expect(numeric.n).toBe(22360679); expect(numericClean.n).toBe(hidden); expect(numericClean.choices).toEqual(safe.choices);
  const dirty = { n: 7, choices: [{ index: 0, message: { content: secret }, logprobs: { content: [{ token: secret, bytes: [50, 50], logprob: -0.5 }] } }] };
  expect(JSON.stringify(dirty)).toContain(secret);
  const clean = wire.scrubProviderResponse(dirty, [secret]);
  expect(clean.choices[0].message.content).toBe(hidden); expect(clean.choices[0].logprobs).toBe(hidden); expect(clean.n).toBe(7);
  expect(wire.scrubProviderResponse(clean, [secret])).toEqual(clean);
  const tokenOnly = { choices: [{ message: { content: "ordinary answer" }, logprobs: { content: [{ token: secret, bytes: [50, 50], logprob: -0.5 }] } }] };
  const tokenClean = wire.scrubProviderResponse(tokenOnly, [secret]);
  expect(tokenClean.choices[0].message.content).toBe("ordinary answer");
  expect(tokenClean.choices[0].logprobs.content[0]).toEqual({ token: hidden, bytes: hidden, logprob: -0.5 });
  const chunks = [secret.slice(0, 4), secret.slice(4)].map(content => ({ choices: [{ index: 0, delta: { content }, logprobs: { content: [token] } }] }));
  expect(chunks.map(chunk => chunk.choices[0].delta.content).join("")).toBe(secret);
  const cleanChunks = wire.scrubProviderResponse(chunks, [secret]);
  for (const chunk of cleanChunks) { expect(chunk.choices[0].delta.content).toBe(hidden); expect(chunk.choices[0].logprobs).toBe(hidden); }
  expect(wire.scrubProviderResponse(cleanChunks, [secret])).toEqual(cleanChunks);
  expect(JSON.stringify(safe)).toBe(before);
});

test("I1-VALUE shared budgets remain fail-closed and scalar media policy is not rewritten", () => {
  const raw = '{"n":24494897,"safe":"ordinary text"}';
  expect(raw).toContain("24494897");
  expect(() => wire.scrubValue(raw, ["24494897"], "", 0, false, { nodes: 2, text: 1000, seen: new WeakSet() })).toThrow();
  expect(() => wire.scrubValue(raw, ["24494897"], "", 0, false, { nodes: 100, text: 2, seen: new WeakSet() })).toThrow();
  expect(wire.scrubValue(7, [], "usage.input_tokens")).toBe(7);
  expect(wire.scrubValue(24494897, ["24494897"], "usage.input_tokens")).toBe(hidden);
  expect(wire.mediaReplacement([1, 2, 3], "audio")).toBe("<omitted: media>");
  expect(wire.scrubValue({ audio: [], input_audio: null })).toEqual({ audio: [], input_audio: null });
  // Explicit numeric/bool payload-vs-counter policy remains an external gate.
  // Do not ratify either conflicting scalar interpretation in this repair file.
});
