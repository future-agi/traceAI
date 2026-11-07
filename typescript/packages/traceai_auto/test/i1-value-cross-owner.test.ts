import * as wire from "../src/wire/privacy-runtime";
import * as core from "@traceai/fi-core/privacy";

const hidden = "<redacted>";
const runs = [wire.scrubText, wire.scrubAttribute, wire.scrubValue];
function stable(input: string, expected: string, secrets: string[] = []): void {
  for (const run of runs) {
    expect(run(input, secrets)).toBe(expected);
    expect(run(expected, secrets)).toBe(expected);
  }
}

test("I1-CROSS shared bridge identity", () => {
  expect(wire.scrubText).toBe(core.scrubText);
  expect(wire.scrubAttribute).toBe(core.scrubAttribute);
  expect(wire.scrubValue).toBe(core.scrubValue);
});

test("I1-CROSS duplicate overwritten string and escaped string secrets", () => {
  const secret = "31415926";
  // Construct the escaped source explicitly: the runtime receives six source
  // characters for the escape, not the already decoded digit.
  const escaped = '3141' + String.fromCharCode(92) + 'u0035926';
  for (const token of [secret, escaped]) {
    const source = '{"n":"' + token + '","n":7,"safe":10000000000000001}';
    stable(source, '{"n":7,"safe":10000000000000001}', [secret]);
    stable("401 " + source + " retry", '401 {"n":7,"safe":10000000000000001} retry', [secret]);
    const nested = JSON.stringify({ diagnostic: "401 " + source + " retry" });
    const expected = JSON.stringify({ diagnostic: '401 {"n":7,"safe":10000000000000001} retry' });
    stable(nested, expected, [secret]);
  }
  stable('{"password":"private","password":"safe"}', '{"password":"<redacted>"}');
  stable('{"a":{"n":"31415926"},"a":{"n":7}}', '{"a":{"n":7}}', [secret]);
  stable('{"a":[{"n":"31415926","n":7}]}', '{"a":[{"n":7}]}', [secret]);
});

test("I1-CROSS audited numeric duplicates retain original lexemes", () => {
  stable('{"n":10000000000000001,"n":7,"city":"Oslo"}', '{"n":"<redacted>","n":7,"city":"Oslo"}', ["10000000000000001"]);
  stable('{"audio":7,"audio":31415926}', '{"audio":"<redacted>"}', ["31415926"]);
  stable('{"n":1.25000000e+3,"n":7}', '{"n":"<redacted>","n":7}', ["1.25000000e+3"]);
  stable('{ "n":7, "safe":10000000000000001 }', '{ "n":7, "safe":10000000000000001 }', ["10000000000000000"]);
  stable('{"n":"31415926"}', '{"n":"<redacted>"}', ["31415926"]);
});

test("I1-CROSS original ranges span array and quoted owners", () => {
  stable("diagnostic abc[12345678] retry", "diagnostic <redacted> retry", ["abc[12345678]"]);
  // Consume the complete intersected quote owner; retaining a dangling quote
  // is not required to retain the safe surrounding diagnostic context.
  stable('401 abcd"efgh" retry', "401 <redacted> retry", ['abcd"efgh']);
  stable('401 [12345678]tail retry', "401 <redacted> retry", ['[12345678]tail']);
  stable('401 {"x":7}suffix retry', "401 <redacted> retry", ['{"x":7}suffix']);
  stable('401 {"safe":"value"} retry', '401 {"safe":"value"} retry', ["abc[12345678]"]);
  stable('diagnostic abc[12345678] retry', 'diagnostic abc[12345678] retry');
  stable('401 abcd"efgh" retry', '401 abcd"efgh" retry');
});

test("I1-CROSS longest-first minimum length and sentinel stability", () => {
  stable('diagnostic prefix-abc[12345678]-suffix retry', 'diagnostic <redacted> retry', ['abc[12345678]', 'prefix-abc[12345678]-suffix']);
  stable('diagnostic abc[12345678] abc[12345678] short retry', 'diagnostic <redacted> <redacted> short retry', ['abc[12345678]', 'short', hidden]);
  stable('prefix abcdefg suffix', 'prefix abcdefg suffix', ['abcdefg']);
  stable('prefix abcdefgh suffix', 'prefix <redacted> suffix', ['abcdefgh']);
  stable('prefix <redacted> suffix', 'prefix <redacted> suffix', ['redacted', hidden]);
});

test("I1-CROSS JSON token ownership covers escaped raw matches and syntax ranges", () => {
  const rawEscape = String.fromCharCode(92) + 'u0061bcdefgh';
  const source = '{"note":"' + rawEscape + '","safe":7}';
  stable(source, '{"note":"<redacted>","safe":7}', [rawEscape]);
  const rawMime = 'audio' + String.fromCharCode(92) + 'u002fwav';
  stable('{"mime_type":"' + rawMime + '","data":"AQIDBA=="}', '{"mime_type":"<redacted>","data":"<omitted: 8 base64 chars>"}', [rawMime]);
  const rawKey = 'api_' + String.fromCharCode(92) + 'u006bey';
  stable('{"' + rawKey + '":"private","safe":7}', '"<redacted>"', [rawKey]);
  stable('{"left":"abc","right":7}', '"<redacted>"', ['abc","right']);
  stable('401 {"left":"abc","right":7} retry', '401 "<redacted>" retry', ['abc","right']);
  stable('{"safe":"ordinary words","n":7}', '{"safe":"ordinary words","n":7}');
});

test("I1-CROSS nested diagnostic and provider text cannot regain raw secrets", () => {
  const secret = 'abcd"efgh', raw = '401 abcd"efgh" retry', clean = '401 <redacted> retry';
  stable(JSON.stringify({ message: raw }), JSON.stringify({ message: clean }), [secret]);
  stable(JSON.stringify(JSON.stringify({ message: raw })), JSON.stringify(JSON.stringify({ message: clean })), [secret]);
  const token = { token: "ordinary-word", bytes: [111, 114], logprob: -0.25 };
  const value = { safe: 7, choices: [{ message: { content: raw }, logprobs: { content: [token] } }] };
  const expected = { safe: 7, choices: [{ message: { content: clean }, logprobs: hidden }] };
  expect(wire.scrubProviderResponse(value, [secret])).toEqual(expected);
  expect(wire.scrubProviderResponse(expected, [secret])).toEqual(expected);
  expect(value.choices[0].message.content).toBe(raw);
  const safe = { safe: 7, choices: [{ message: { content: "ordinary answer" }, logprobs: { content: [token] } }] };
  expect(wire.scrubProviderResponse(safe, [secret])).toEqual(safe);
});

test("I1-CROSS URL syntax is not bare media and original URL secrets precede rewriting", () => {
  const url = "https://abcdef.invalid/p?model=keep#anchor";
  stable('401 ' + url + ' {"n":7} retry', '401 ' + url + ' {"n":7} retry');
  stable('401 ' + url + ' iVBORw0KGgoAAAANSUhEUg== retry', '401 ' + url + ' <omitted: 24 base64 chars> retry');
  stable('401 https://abcdef.invalid/?echo=31415926&model=keep retry', '401 https://abcdef.invalid/?echo=<redacted>&model=keep retry', ['31415926']);
  stable('401 https://abcdef.invalid/?token=private&model=keep retry', '401 https://abcdef.invalid/?token=<redacted>&model=keep retry');
  stable('401 prefixhttps://abcdef.invalid/p retry', '401 <redacted> retry', ['prefixhttps://abcdef.invalid/p']);
  stable(JSON.stringify({ url }), JSON.stringify({ url }));
});

test("I1-CROSS shared budgets still bound source matching", () => {
  expect(() => wire.scrubValue('401 abc[12345678] retry', ['abc[12345678]'], '', 0, false, { nodes: 1, text: 1000, seen: new WeakSet() })).toThrow();
  expect(() => wire.scrubValue('{"n":"31415926","n":7}', ['31415926'], '', 0, false, { nodes: 100, text: 2, seen: new WeakSet() })).toThrow();
});
