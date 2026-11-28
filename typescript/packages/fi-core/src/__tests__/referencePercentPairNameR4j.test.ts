import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubText, scrubValue } from "../privacy-runtime";

// A pair whose name is a credential name only once its percent forms are read (tok%65n is token, pass%77ord is password) has its
// value replaced, as a pair spelled plainly does: in prose, in query strings, after other pairs, as a quoted name, as a --flag, as a
// list header (the rest of the line), in a JSON string leaf and in a text that spells the '%' as a JSON escape. The name is read once:
// a name that still holds a percent form after one read is not decided, and its pair is replaced too. A name that is no credential
// name once read (n%61me, tok%65ns, tok%65n_count, a name that decodes to a non-ASCII letter) keeps its value. Every expected value is
// the reference's output for the same input.

const SECRET = "PLNTq7Zr4kXw9";

/** Every reading of an output: the text, each JSON text and JSON string literal in it decoded, and its escapes read, again and again. */
function views(out: unknown): string[] {
  const seen = new Set<string>();
  let layer = [typeof out === "string" ? out : JSON.stringify(out)];
  for (let depth = 0; depth < 8 && layer.length; depth++) {
    const next: string[] = [];
    for (const view of layer) {
      if (seen.has(view)) continue;
      seen.add(view);
      const trimmed = view.trim();
      if (/^[[{"]/.test(trimmed)) {
        try {
          const strings: string[] = [];
          const walk = (node: unknown): void => {
            if (typeof node === "string") strings.push(node);
            else if (node && typeof node === "object") for (const [key, child] of Object.entries(node)) { strings.push(key); walk(child); }
          };
          const parsed: unknown = JSON.parse(trimmed);
          walk(parsed);
          next.push(...strings);
          if (parsed && typeof parsed === "object") next.push(JSON.stringify(parsed));
        } catch { /* not JSON */ }
      }
      for (const match of view.matchAll(/"(?:[^"\\\n]|\\.)*"/g)) { try { const s = JSON.parse(match[0]); if (typeof s === "string") next.push(s); } catch { /* not a literal */ } }
      if (view.includes("\\")) next.push(view.replace(/\\(u[0-9a-fA-F]{4}|["\\/bfnrt])/g, (_, g: string) => g[0] === "u" ? String.fromCharCode(parseInt(g.slice(1), 16)) : ({ b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" } as Record<string, string>)[g] ?? g));
    }
    layer = next;
  }
  return [...seen].map(view => view.replace(/<redacted>|<omitted(?:: [^<>]*)?>/g, "\0"));
}
/** The four-character runs of a secret that some reading of the output shows. */
function shownRuns(out: unknown, secret: string): string[] {
  const runs = new Set<string>();
  for (let i = 0; i + 4 <= secret.length; i++) runs.add(secret.slice(i, i + 4));
  const shown = new Set<string>();
  for (const view of views(out)) for (const run of runs) if (view.includes(run)) shown.add(run);
  return [...shown];
}

// [input, the reference's output]
const PROSE: [string, string][] = [
  ["log tok%65n=PLNTq7Zr4kXw9 end", "log tok%65n=<redacted>"],
  ["tok%65n=PLNTq7Zr4kXw9", "tok%65n=<redacted>"],
  ["tok%65n: PLNTq7Zr4kXw9", "tok%65n: <redacted>"],
  ["tok%65n:PLNTq7Zr4kXw9", "tok%65n:<redacted>"],
  ["tok%65n = PLNTq7Zr4kXw9", "tok%65n = <redacted>"],
  ["Tok%65n=PLNTq7Zr4kXw9", "Tok%65n=<redacted>"],
  ["tok%65N=PLNTq7Zr4kXw9", "tok%65N=<redacted>"],
  ["%54OKEN=PLNTq7Zr4kXw9", "%54OKEN=<redacted>"],
  ["TOK%45N=PLNTq7Zr4kXw9", "TOK%45N=<redacted>"],
  ["to%6Ben=PLNTq7Zr4kXw9", "to%6Ben=<redacted>"],
  ["to%6ben=PLNTq7Zr4kXw9", "to%6ben=<redacted>"],
  ["%70assword=PLNTq7Zr4kXw9", "%70assword=<redacted>"],
  ["pass%77ord=PLNTq7Zr4kXw9", "pass%77ord=<redacted>"],
  ["s%65cret=PLNTq7Zr4kXw9", "s%65cret=<redacted>"],
  ["s%65cr%65t=PLNTq7Zr4kXw9", "s%65cr%65t=<redacted>"],
  ["access%54oken=PLNTq7Zr4kXw9", "access%54oken=<redacted>"],
  ["refresh_tok%65n=PLNTq7Zr4kXw9", "refresh_tok%65n=<redacted>"],
  ["sessi%6Fn_id=PLNTq7Zr4kXw9", "sessi%6Fn_id=<redacted>"],
  ["auth%6Frization=PLNTq7Zr4kXw9", "auth%6Frization=<redacted>"],
  ["api%4Bey=PLNTq7Zr4kXw9", "api%4Bey=<redacted>"],
  ["X-Api-K%65y: PLNTq7Zr4kXw9", "X-Api-K%65y: <redacted>"],
  ["api+k%65y=PLNTq7Zr4kXw9", "api+k%65y=<redacted>"],
  ["token%2=PLNTq7Zr4kXw9", "token%2=<redacted>"],
  ["tok%65n%=PLNTq7Zr4kXw9", "tok%65n%=<redacted>"],
  ["tok%25%36%35n=PLNTq7Zr4kXw9", "tok%25%36%35n=<redacted>"],
  ["%C3%A9tok%65n=PLNTq7Zr4kXw9", "%C3%A9tok%65n=<redacted>"],
  ["my%C3%A9tok%65n=PLNTq7Zr4kXw9", "my%C3%A9tok%65n=<redacted>"],
  ["%E4%BD%A0s%65cret=PLNTq7Zr4kXw9", "%E4%BD%A0s%65cret=<redacted>"],
  ["%E0%A5%A6tok%65n=PLNTq7Zr4kXw9", "%E0%A5%A6tok%65n=<redacted>"],
  ["tok%65n%3DPLNTq7Zr4kXw9", "tok%65n%3D<redacted>"],
  ["tok%65n%3dPLNTq7Zr4kXw9", "tok%65n%3d<redacted>"],
  ["tok%65n%20=PLNTq7Zr4kXw9", "tok%65n%20=<redacted>"]
];
// [input, known secrets, path, the reference's output]
const CONTEXT: [unknown, string[], string, unknown][] = [
  ["a=1&tok%65n=PLNTq7Zr4kXw9&b=2", [], "", "a=1&tok%65n=<redacted>"],
  ["user=al&pass%77ord=PLNTq7Zr4kXw9&next=1", [], "", "user=al&pass%77ord=<redacted>"],
  ["x=1;tok%65n=PLNTq7Zr4kXw9;y=2", [], "", "x=1;tok%65n=<redacted>;y=2"],
  ["name=bob tok%65n=PLNTq7Zr4kXw9 next", [], "", "name=bob tok%65n=<redacted>"],
  ["name=bob, tok%65n=PLNTq7Zr4kXw9, id=3", [], "", "name=bob, tok%65n=<redacted>, id=3"],
  ["tok%65n=PLNTq7Zr4kXw9\nname=bob", [], "", "tok%65n=<redacted>\nname=bob"],
  ["first tok%65n=PLNTq7Zr4kXw9 then s%65cret=PLNTk3Vb8mQz5", [], "", "first tok%65n=<redacted>"],
  ["tok%65n=PLNTq7Zr4kXw9&tok%65n=PLNTk3Vb8mQz5", [], "", "tok%65n=<redacted>"],
  ["tok%65n=\"PLNTq7Zr4kXw9 more\" end", [], "", "tok%65n=<redacted>"],
  ["GET /p?q=1 tok%65n=PLNTq7Zr4kXw9", [], "", "GET /p?q=1 tok%65n=<redacted>"],
  ["see https://h.example/p?a=1&s%65cret=PLNTq7Zr4kXw9 now", [], "", "see https://h.example/p?a=1&s%65cret=<redacted>"],
  ["\"tok%65n\": \"PLNTq7Zr4kXw9\"", [], "", "\"tok%65n\": \"<redacted>\""],
  ["log \"tok%65n\": \"PLNTq7Zr4kXw9\" end", [], "", "log \"tok%65n\": \"<redacted>\" end"],
  ["\"tok%65n\"=PLNTq7Zr4kXw9", [], "", "\"tok%65n\"=<redacted>"],
  ["'tok%65n': 'PLNTq7Zr4kXw9'", [], "", "'tok%65n': '<redacted>'"],
  ["\\\"tok%65n\\\": \\\"PLNTq7Zr4kXw9\\\"", [], "", "\\\"tok%65n\\\": \\\"<redacted>\\\""],
  ["&quot;tok%65n&quot;: &quot;PLNTq7Zr4kXw9&quot;", [], "", "&quot;tok%65n&quot;: &quot;<redacted>&quot;"],
  ["--tok%65n PLNTq7Zr4kXw9", [], "", "--tok%65n <redacted>"],
  ["run --pass%77ord PLNTq7Zr4kXw9 now", [], "", "run --pass%77ord <redacted> now"],
  ["-tok%65n PLNTq7Zr4kXw9", [], "", "-tok%65n <redacted>"],
  ["--tok%65n=PLNTq7Zr4kXw9", [], "", "--tok%65n=<redacted>"],
  ["Cook%69e: a=1; b=PLNTq7Zr4kXw9", [], "", "Cook%69e: <redacted>"],
  ["Set-Cook%69e: sid=PLNTq7Zr4kXw9; Path=/", [], "", "Set-Cook%69e: <redacted>"],
  ["cook%69e: a=PLNTq7Zr4kXw9\nnext=1", [], "", "cook%69e: <redacted>\nnext=1"],
  ["Authoriz%61tion: Bearer PLNTq7Zr4kXw9 more", [], "", "Authoriz%61tion: <redacted>"],
  ["Proxy-Authoriz%61tion: Basic PLNTq7Zr4kXw9 x", [], "", "Proxy-Authoriz%61tion: <redacted>"],
  ["tok%65n=PLNTq7Zr4kXw9", [], "message", "tok%65n=<redacted>"],
  ["log s%65cret=PLNTq7Zr4kXw9 end", [], "llm.input_messages.0.content", "log s%65cret=<redacted>"],
  ["{\"m\": \"log tok%65n=PLNTq7Zr4kXw9\"}", [], "", "{\"m\":\"log tok%65n=<redacted>\"}"],
  ["{\"note\": \"tok%65n=PLNTq7Zr4kXw9\", \"id\": 3}", [], "", "{\"note\":\"tok%65n=<redacted>\",\"id\":3}"],
  ["[\"tok%65n=PLNTq7Zr4kXw9\", \"ok\"]", [], "", "[\"tok%65n=<redacted>\",\"ok\"]"],
  [{"m": "log tok%65n=PLNTq7Zr4kXw9"}, [], "", {"m": "log tok%65n=<redacted>"}],
  [["tok%65n=PLNTq7Zr4kXw9", "ok"], [], "", ["tok%65n=<redacted>", "ok"]],
  [{"a": {"b": ["x pass%77ord=PLNTq7Zr4kXw9"]}}, [], "", {"a": {"b": ["x pass%77ord=<redacted>"]}}],
  ["[\"s\\u002565cret: PLNTq7Zr4kXw9\",]", [], "", "<redacted>"],
  ["[\"log tok\\u002565n=PLNTq7Zr4kXw9\"] x", [], "", "<redacted>"]
];
// [input]: an unclosed array tail that holds such a pair. The reference replaces the pair's value and keeps the rest; this export
// may take out more (the unclosed tail is read as a whole, as it is for the same pair spelled plainly), but never shows the value.
const UNCLOSED: string[] = ["[\"log tok%65n=PLNTq7Zr4kXw9\"] x", "[\"log token=PLNTq7Zr4kXw9\"] x"];
// [input, the reference's output (the input unchanged)]
const KEEP: [string, string][] = [
  ["n%61me=PLNTq7Zr4kXw9", "n%61me=PLNTq7Zr4kXw9"],
  ["us%65r=PLNTq7Zr4kXw9", "us%65r=PLNTq7Zr4kXw9"],
  ["%6Eame=PLNTq7Zr4kXw9", "%6Eame=PLNTq7Zr4kXw9"],
  ["%6E%61%6D%65=PLNTq7Zr4kXw9", "%6E%61%6D%65=PLNTq7Zr4kXw9"],
  ["us%65r_id=PLNTq7Zr4kXw9", "us%65r_id=PLNTq7Zr4kXw9"],
  ["c%6Funt=PLNTq7Zr4kXw9", "c%6Funt=PLNTq7Zr4kXw9"],
  ["tok%65ns=PLNTq7Zr4kXw9", "tok%65ns=PLNTq7Zr4kXw9"],
  ["tok%65n_count=PLNTq7Zr4kXw9", "tok%65n_count=PLNTq7Zr4kXw9"],
  ["tok%65n%5Fcount=12", "tok%65n%5Fcount=12"],
  ["max_tok%65ns=4096", "max_tok%65ns=4096"],
  ["max%5Ftokens=PLNTq7Zr4kXw9", "max%5Ftokens=PLNTq7Zr4kXw9"],
  ["tok%65ns=12&c%6Funt=3", "tok%65ns=12&c%6Funt=3"],
  ["us%65r=bob&n%61me=al", "us%65r=bob&n%61me=al"],
  ["n%61me: bob", "n%61me: bob"],
  ["--n%61me bob", "--n%61me bob"],
  ["--us%65r bob", "--us%65r bob"],
  ["tok%2Ben=PLNTq7Zr4kXw9", "tok%2Ben=PLNTq7Zr4kXw9"],
  ["to+ken=PLNTq7Zr4kXw9", "to+ken=PLNTq7Zr4kXw9"],
  ["to+k%65n=PLNTq7Zr4kXw9", "to+k%65n=PLNTq7Zr4kXw9"],
  ["https://h.example/p?to+k%65n=PLNTq7Zr4kXw9", "https://h.example/p?to+k%65n=PLNTq7Zr4kXw9"],
  ["p%C3%A9ssword=PLNTq7Zr4kXw9", "p%C3%A9ssword=PLNTq7Zr4kXw9"],
  ["tok%C3%A9n=PLNTq7Zr4kXw9", "tok%C3%A9n=PLNTq7Zr4kXw9"],
  ["%EF%BD%94oken=PLNTq7Zr4kXw9", "%EF%BD%94oken=PLNTq7Zr4kXw9"],
  ["tok%EF%BD%85n=PLNTq7Zr4kXw9", "tok%EF%BD%85n=PLNTq7Zr4kXw9"],
  ["s%C4%B1cret=PLNTq7Zr4kXw9", "s%C4%B1cret=PLNTq7Zr4kXw9"],
  ["%C5%BFecret=PLNTq7Zr4kXw9", "%C5%BFecret=PLNTq7Zr4kXw9"],
  ["tok%FFn=PLNTq7Zr4kXw9", "tok%FFn=PLNTq7Zr4kXw9"],
  ["tok%C3n=PLNTq7Zr4kXw9", "tok%C3n=PLNTq7Zr4kXw9"],
  ["tok%6=PLNTq7Zr4kXw9", "tok%6=PLNTq7Zr4kXw9"],
  ["tok%=PLNTq7Zr4kXw9", "tok%=PLNTq7Zr4kXw9"],
  ["tok%256=PLNTq7Zr4kXw9", "tok%256=PLNTq7Zr4kXw9"],
  ["tok%65n%3aPLNTq7Zr4kXw9", "tok%65n%3aPLNTq7Zr4kXw9"]
];
// [input, the reference's output]
const ALREADY: [string, string][] = [
  ["tok%2565n=PLNTq7Zr4kXw9", "tok%2565n=<redacted>"],
  ["api%5Fkey=PLNTq7Zr4kXw9", "api%5Fkey=<redacted>"],
  ["x%2Dapi%2Dkey=PLNTq7Zr4kXw9", "x%2Dapi%2Dkey=<redacted>"],
  ["client%5Fsecret=PLNTq7Zr4kXw9", "client%5Fsecret=<redacted>"],
  ["priv%61te_key=PLNTq7Zr4kXw9", "priv%61te_key=<redacted>"],
  ["passw%64=PLNTq7Zr4kXw9", "passw%64=<redacted>"],
  ["%73%65%63%72%65%74=PLNTq7Zr4kXw9", "%73%65%63%72%65%74=<redacted>"],
  ["%E2%80%8Btoken=PLNTq7Zr4kXw9", "%E2%80%8Btoken=<redacted>"],
  ["tok%65n%C3%A9=PLNTq7Zr4kXw9", "tok%65n%C3%A9=<redacted>"],
  ["https://h.example/p?a=1&tok%65n=PLNTq7Zr4kXw9&b=2", "https://h.example/p?a=1&tok%65n=<redacted>&b=2"],
  ["https://h.example/p#tok%65n=PLNTq7Zr4kXw9", "https://h.example/p#tok%65n=<redacted>"],
  ["log https://h.example/p?n%61me=bob&tok%65n=PLNTq7Zr4kXw9", "log https://h.example/p?n%61me=bob&tok%65n=<redacted>"],
  ["https://h.example/p?a=1&api+k%65y=PLNTq7Zr4kXw9", "https://h.example/p?a=1&api+k%65y=<redacted>"],
  ["{\"tok%65n\": \"PLNTq7Zr4kXw9\"} x", "<redacted>"]
];
// Texts that begin as a JSON object or array, wrapped as a whole value, an object leaf, a JSON string leaf or an array, at several
// depths, each holding a pair whose name is a credential name once its percent forms (or a '%' spelled %) are read:
// [input, known secrets, path, depth, framed, the four-character runs of its markers that no reading of the reference's output shows]
const MIXED: [unknown, string[], string, number, boolean, string][] = [
  ["\u000b[\"user\",   [ \"refresh_tok\\u002565n: TAILJ44L5PP\"  ],   \"Bearer TAILUF8F65LJL\", \t[\r\n\"Bearer TAILJSH8GU\",\n\t\"TAILDKREVXGP6Z\"\n]\r\n],", ["TAILJSH8GU", "TAILDKREVXGP6Z"], "input", 0, true, "44L5 4L5P 5LJL 65LJ 8F65 AILD AILJ AILU DKRE EVXG F65L F8F6 GP6Z H8GU ILDK ILJ4 ILJS ILUF J44L JSH8 KREV L5PP LDKR LJ44 LJSH LUF8 REVX SH8G TAIL UF8F VXGP XGP6"],
  [{"m": "\t{\n\"content\"  \r\n\"\\\"p%61sswd\\u003a TAILJT426NY4YA\"}"}, [], "tool.arguments", 0, true, "26NY 426N 6NY4 AILJ ILJT JT42 LJT4 NY4Y T426 TAIL Y4YA"],
  ["{\"m\": \"\\u2028{\\r\\n\\\"count\\\":  \\\"log pa%73swd=TAILUDBNRDA\\\",,  \\\"tokens\\\" :  \\\"{\\\\\\\"b\\\\\\\": \\\\\\\"TAIL3E4HS5FV\\\\\\\"}\\\"\\n}\"}", [], "message", 1, true, "3E4H 4HS5 AIL3 AILU BNRD DBNR E4HS HS5F IL3E ILUD L3E4 LUDB NRDA S5FV TAIL UDBN"],
  [" [\"\\\"pas\\u002573word: TAIL4PHQKG\\\"\"} password=TAILU7HEYKQ2", [], "llm.input_messages.0.content", 30, true, "4PHQ 7HEY AIL4 AILU EYKQ HEYK HQKG IL4P ILU7 L4PH LU7H PHQK TAIL U7HE YKQ2"],
  ["\u001f{\r\n\"data\"  :\"Bearer TAILTVKEHXQLY\",\r\n\"Cook%69e\"  =\"TAILT6U66DB2\" ]", [], "", 1, true, "66DB 6DB2 6U66 AILT EHXQ HXQL ILT6 ILTV KEHX LT6U LTVK T6U6 TAIL TVKE U66D VKEH XQLY"],
  [" \ufeff [\r\n\"\\u12\",{  ],\"\\\"%73ession_id\\u003a TAILS6H4BKL\\\"\",  []\n]\n{}", [], "output.value", 0, true, "4BKL 6H4B AILS H4BK ILS6 LS6H S6H4 TAIL"],
  ["\u001c[\t[  [\"Bearer TAIL579RJ6\",  [\"TAILBXBB4R\" ]\t}, \r\n\"{\\\"accessToken\\\": \\\"TAILT8BUXJ2P6\\\"}}\", \"x TAIL9AVMJ6WZWA\",   {  ,}\r\n], \r\n[\r\n{},\t\"TAILKMBF4DN5WX\"\r\n,], \"a\\u002575thorization: TAIL5XRJGGL5B end\", \n[\t[\n},\n[\r\n[{ \"user\"\u00a0:  \"a\u0001b\", \"count\" :  \"Bearer TAILCXVWXYXEK\"}], \n\"x TAILQZTT7TDJEW\", \"x TAILVJB4E8\", true]\n] ]", ["TAIL579RJ6", "TAILBXBB4R", "TAIL9AVMJ6WZWA", "TAIL5XRJGGL5B", "TAILQZTT7TDJEW", "TAILVJB4E8"], "", 1, true, "4DN5 579R 5XRJ 6WZW 79RJ 7TDJ 8BUX 9AVM 9RJ6 AIL5 AIL9 AILB AILC AILK AILQ AILT AILV AVMJ B4E8 BB4R BF4D BUXJ BXBB CXVW DJEW DN5W F4DN GGL5 GL5B IL57 IL5X IL9A ILBX ILCX ILKM ILQZ ILT8 ILVJ J2P6 J6WZ JB4E JGGL KMBF L579 L5XR L9AV LBXB LCXV LKMB LQZT LT8B LVJB MBF4 MJ6W N5WX QZTT RJGG T7TD T8BU TAIL TDJE TT7T UXJ2 VJB4 VMJ6 VWXY WXYX WZWA XBB4 XJ2P XRJG XVWX XYXE YXEK ZTT7"],
  [{"m": "\ufeff{ \"content\" = \"\\\"accessT%6Fken\\\":\\\"TAILRENP7S9V8U\\\"\",\r\n\"content\"\n:\"{\\\"id\\\": \\\"TAILXVBL4Q\\\"}\"\n]}"}, [], "tool.arguments", 30, true, "7S9V 9V8U AILR AILX BL4Q ENP7 ILRE ILXV LREN LXVB NP7S P7S9 RENP S9V8 TAIL VBL4 XVBL"],
  ["\u0085{\r\n\"refre\\u0073h_token\" \"\\\"a%75th\\u003dTAILZ45XT2FK end\"}", [], "message", 5, true, "45XT 5XT2 AILZ ILZ4 LZ45 T2FK TAIL XT2F Z45X"],
  ["\u001c[ \"\\\"to%6Ben\\u003dTAILHFQMZ4JPQM\\\"\"\r\n] password=TAILPSCTYH", [], "message", 5, true, "4JPQ AILH AILP CTYH FQMZ HFQM ILHF ILPS JPQM LHFQ LPSC MZ4J PSCT QMZ4 SCTY TAIL Z4JP"],
  ["\u0085[\t\"pas\\u002573wd\\u003a TAILH6TAYZSNKG&x=1\"\n,]", [], "data", 1, true, "6TAY AILH AYZS H6TA ILH6 LH6T SNKG TAIL TAYZ YZSN ZSNK"],
  ["{\"m\": \"\\u3000{\\\"content\\\"  :\\n\\\"Coo%6Bie\\\\u003a TAILKVET2B&x=1\\\"\\r\\n}\"}", [], "message", 30, true, "AILK ET2B ILKV KVET LKVE TAIL VET2"],
  ["\u0085[ infinity,   \"log private_%6Bey\\u003dTAILJVYPXTZ35&x=1\" ", [], "", 29, true, "AILJ ILJV JVYP LJVY PXTZ TAIL TZ35 VYPX XTZ3 YPXT"],
  ["{\"m\": \" \\ufeff {\\\"b\\\"\\\"log a%75thorization\\\\\\\":\\\\\\\"TAIL5SARUNFK7P end\\\",\\\"b\\\"  =  \\\"TAILK8QGH9M\\\"}\"}", ["TAILK8QGH9M"], "input", 5, true, "5SAR 8QGH AIL5 AILK ARUN FK7P GH9M IL5S ILK8 K8QG L5SA LK8Q NFK7 QGH9 RUNF SARU TAIL UNFK"],
  [{"m": "{\t\"model\" :  \"token%2=TAILSWYX6NRD8C end\",\n\r\n\"name\" :  1.}"}, [], "output.value", 30, false, "6NRD AILS ILSW LSWY NRD8 RD8C SWYX TAIL WYX6 X6NR YX6N"],
  [" \ufeff [  \"apiKey%2=TAILYZM2VH\\\"\" ],", [], "output.value", 31, true, "AILY ILYZ LYZM M2VH TAIL YZM2 ZM2V"],
  [["\f{  \"data\" \u0661,  \"a\"  :[  \"\\\"Coo%6Bie\\u003a TAIL524B2V&x=1\", false,{  }  ]\r\n}", "ok"], [], "tool.arguments", 0, true, "24B2 4B2V 524B AIL5 IL52 L524 TAIL"],
  ["\u2028[\n\u0661\u0662,\n  [\n{  }, \n\"a\\xb\", [ \"%61uthorization\\u003a TAILJX8PTSZJ end\",\n\n[\t,],]\r\n],", [], "a.b", 0, true, "8PTS AILJ ILJX JX8P LJX8 PTSZ TAIL TSZJ X8PT"],
  ["\u001c[ true, [{  ,},{\t\"data\":\"log Coo%6Bie\\\":\\\"TAILQGTPSAA7M end\"\n},\r\ntrue,\n0x1F\n], \t12.5]", [], "message", 31, true, "AA7M AILQ GTPS ILQG LQGT PSAA QGTP SAA7 TAIL TPSA"],
  [" [{,}, \"\\\"pas%73word: TAILTCCFYC2GR\\\"\",   \"Bearer TAIL7GJ8D65\"\t,] [ ,", ["TAIL7GJ8D65"], "a.b", 30, true, "7GJ8 8D65 AIL7 AILT C2GR CCFY CFYC FYC2 GJ8D IL7G ILTC J8D6 L7GJ LTCC TAIL TCCF YC2G"],
  ["\u2028{ \"a\"  \"\\\"%74oken\\u003a TAILC7XACSB7K\\\"\"\t,}", [], "input", 1, true, "7XAC ACSB AILC C7XA CSB7 ILC7 LC7X SB7K TAIL XACS"],
  [{"m": "\u000b{\"msg\"\n\"\\u12G4\",,\r\n\"model\": \"\\\"private_k\\u002565y=TAILD3GQEHFDX\\\"\",}"}, [], "message", 1, true, "3GQE AILD D3GQ EHFD GQEH HFDX ILD3 LD3G QEHF TAIL"],
  ["\n{\n\"text\"  : \"log tok%65n=TAILK9QK2DHM2L\\\"\", \t\"b\"\t:\"tokens\", \"data\":\t[] ,}", [], "output.value", 0, true, "2DHM 9QK2 AILK DHM2 HM2L ILK9 K2DH K9QK LK9Q QK2D TAIL"],
  ["\u00a0[ \"au\\u002574h=TAILC5CVJHV&x=1\"\n] trailing", [], "", 29, false, "5CVJ AILC C5CV CVJH ILC5 LC5C TAIL VJHV"],
  ["{\"m\": \"\\u000b{\\n\\\"tokens\\\"  \\\"\\\\\\\"secr%65t\\\\\\\":\\\\\\\"TAILCJS3Y9AWHQ\\\",\\n\\\"model\\\"\\t1E+400}\\u00a0\"}", [], "", 0, true, "3Y9A 9AWH AILC AWHQ CJS3 ILCJ JS3Y LCJS S3Y9 TAIL Y9AW"],
  [" \ufeff [ [],\n[\r\n],\r\n\"\\\"x-api-k%65y\\u003a TAILXAWK7F\"\r\n,]", [], "llm.input_messages.0.content", 5, true, "AILX AWK7 ILXA LXAW TAIL WK7F XAWK"],
  [" \ufeff [  \"\\\"session\\u00255fid\\u003dTAILE37RTHNS\"\n,]", [], "input", 0, false, "37RT 7RTH AILE E37R ILE3 LE37 RTHN TAIL THNS"],
  ["\u001f[\n\"s\\u002565cret: TAILMQQNFGB772\",]", [], "input", 30, true, "AILM B772 FGB7 GB77 ILMQ LMQQ MQQN NFGB QNFG QQNF TAIL"],
  ["\r[\u00a0\"{\\\"token_count\\\": \\\"TAILKHLQ5BQ\\\"}\",  \"log accessTo%6Ben: TAILCLXDMQPWF\\\"\"]", [], "", 0, true, "AILC CLXD DMQP ILCL LCLX LXDM MQPW QPWF XDMQ"],
  ["\u3000{\t\"a\" :\"TAILT3YGD4R\",\n\r\n\"count\"   [  \"token_count\", None,\"log tok%65n=\\\"TAIL3436N3\\\"\",\"x TAIL9HNGNA\"\t]}", ["TAILT3YGD4R", "TAIL3436N3"], "tool.arguments", 0, true, "3436 36N3 3YGD 436N 9HNG AIL3 AIL9 AILT GD4R HNGN IL34 IL9H ILT3 L343 L9HN LT3Y NGNA T3YG TAIL YGD4"],
  [" {\n\"data\"\n\n\"model\",\"id\": \"au%74h: TAILELJNDT\"\t} x", [], "a.b", 0, false, "AILE ELJN ILEL JNDT LELJ LJND TAIL"],
  ["{\t\"data\"\r\n : \t\"x TAILRRYE97B7ED\",,  \"tokens\"  :  \"log x-api-k%65y\\u003a TAILBJ4YPQS end\"\r\n}", [], "input", 0, false, "4YPQ 7B7E 97B7 AILB AILR B7ED BJ4Y E97B ILBJ ILRR J4YP LBJ4 LRRY RRYE RYE9 TAIL YE97 YPQS"],
  ["[[  ], \n\"\\\"p\\u002561ssword\\\":\\\"TAILC2Z7LQNP7 end\"\t]\n{}", [], "llm.input_messages.0.content", 30, true, "2Z7L 7LQN AILC C2Z7 ILC2 LC2Z LQNP QNP7 TAIL Z7LQ"],
  [{"m": "\ufeff\ufeff[\r\n\"TAIL9KMHNPCBF\", \u00a0[\t\"%73ecret=\\\"TAIL5V7YJF7&x=1\", { },]]"}, ["TAIL9KMHNPCBF"], "output.value", 0, true, "5V7Y 7YJF 9KMH AIL5 AIL9 HNPC IL5V IL9K KMHN L5V7 L9KM MHNP NPCB PCBF TAIL V7YJ YJF7"],
  ["\u00a0{  \"msg\"  :\n\"log %70asswd\\u003dTAILCNVGWPYLC5\"\r\n}", [], "", 30, true, "AILC CNVG GWPY ILCN LCNV NVGW PYLC TAIL VGWP WPYL YLC5"],
  [" [{\n\"token_count\"\n:\"TAIL5YCC447\"},\t\"s%65ssion_id: TAILC4J9NXBH\" }", [], "", 31, true, "4J9N 5YCC 9NXB AIL5 AILC C447 C4J9 CC44 IL5Y ILC4 J9NX L5YC LC4J NXBH TAIL YCC4"],
  [{"m": "[\t[\r\n{\"user\"=\r\n[\t\"content\",\n \"\\\"sessi%6Fn_id=TAIL22BWEDCM4J\\\"\",\n[\ntrue\t]\t,],\n \"user\"= null\r\n}, \"TAILL36FA9V\" }]"}, [], "", 5, true, "22BW 2BWE 36FA 6FA9 AIL2 AILL BWED CM4J DCM4 EDCM FA9V IL22 ILL3 L22B L36F LL36 TAIL WEDC"],
  ["\u001c[\"api%4Bey: TAILGBDT3DH8J\\\"\",\r\n-Infinity,\t{\r\n]\r\n]", [], "output.value", 30, true, "3DH8 AILG BDT3 DH8J DT3D GBDT ILGB LGBD T3DH TAIL"],
  ["[\"\\u000b[ \\\"\\\\\\\"apiK%65y=TAIL2QY7NHFLA9 end\\\",\\n  [\\r\\nundefined,\\\"TAILW2LY6UA\\\",\\t\\\"\\\\\\\"pas%73word=TAILLYLJX9WA\\\\\\\"\\\",\\u00a0\\\"Bearer TAILQU6B2PA\\\",],\\n  \\\"\\\\u12\\\",\\n[ \\\"x TAIL22M3J8P\\\"] ] x\"]", ["TAILW2LY6UA", "TAILQU6B2PA"], "", 1, true, "22M3 2LY6 2M3J 2QY7 3J8P 6B2P 7NHF AIL2 AILL AILQ AILW B2PA FLA9 HFLA IL22 IL2Q ILLY ILQU ILW2 JX9W L22M L2QY LJX9 LLYL LQU6 LW2L LY6U LYLJ M3J8 NHFL QU6B QY7N TAIL U6B2 W2LY X9WA Y6UA Y7NH YLJX"],
  ["\ufeff\ufeff{\n\"token_count\":\r\n\"log pa%73swd\\\":\\\"TAILWRD66C9\"\t] \u2028{\"token_count\"\t:\t{\r\n}\r\n} password=TAILZV4NFTXJD5", [], "message", 0, true, "4NFT 66C9 AILW AILZ D66C FTXJ ILWR ILZV LWRD LZV4 NFTX RD66 TAIL TXJD V4NF WRD6 XJD5 ZV4N"],
  ["\u001c[\"pas\\u002573wd=TAILPZ3BNSKA25 end\"  }", [], "a.b", 31, false, "3BNS AILP BNSK ILPZ KA25 LPZ3 NSKA PZ3B SKA2 TAIL Z3BN"],
  ["\u0085[\t[  \"x TAIL7J5PQNAY\"\r\n],\r\n\"au%74horization: TAILYCEUF9&x=1\"\n] trailing", ["TAIL7J5PQNAY"], "", 1, true, "5PQN 7J5P AIL7 AILY CEUF EUF9 IL7J ILYC J5PQ L7J5 LYCE PQNA QNAY TAIL YCEU"],
  ["\u2028[\r\ntrue,\t\"\ud800\",  [\n\"au%74h\\u003a TAILLCXUNPUCP end\"],\r\n'single'  ,]", [], "", 1, true, "AILL CXUN ILLC LCXU LLCX NPUC PUCP TAIL UNPU XUNP"],
  ["[\" [1e, \\r\\n{\\t,}, \\\"{\\\\\\\"api_key\\\\\\\": \\\\\\\"TAILHSFE5LK\\\\\\\"}\\\", \\\"log x-api-k%65y=\\\\\\\"TAILKXA3CLZ8\\\\\\\"\\\"\\n,]\"]", [], "data", 0, true, "3CLZ A3CL AILH AILK CLZ8 E5LK FE5L HSFE ILHS ILKX KXA3 LHSF LKXA SFE5 TAIL XA3C"]
];

describe("a pair whose name is a credential name once its percent forms are read, in plain prose", () => {
  test.each(PROSE)("%j has its value replaced as the reference does", (input, expected) => {
    expect(scrubValue(input)).toBe(expected);
    expect(shownRuns(scrubValue(input), SECRET)).toEqual([]);
  });
  test.each(PROSE)("%j is exported the same way as free text and as an attribute", (input, expected) => {
    expect(scrubText(input)).toBe(expected);
    expect(scrubAttribute(input)).toBe(expected);
  });
});

describe("a percent-spelled credential pair name in a query string, after other pairs, quoted, as a flag, as a list header or in a JSON leaf", () => {
  test.each(CONTEXT)("%j (known secrets %j, path %j) is exported as the reference exports it", (input, secrets, path, expected) => {
    const out = scrubValue(input, secrets, path);
    expect(out).toEqual(expected);
    expect(shownRuns(out, SECRET)).toEqual([]);
  });
});

describe("an unclosed array tail that holds a pair named with percent forms", () => {
  test.each(UNCLOSED)("%j shows no run of the value", input => {
    for (const out of [scrubValue(input, []), scrubText(input, []), scrubAttribute(input, [])]) expect(shownRuns(out, SECRET)).toEqual([]);
  });
});
describe("a percent-spelled pair name that is no credential name once read keeps its value", () => {
  test.each(KEEP)("%j is shown unchanged", (input, expected) => {
    expect(expected).toBe(input);
    expect(scrubValue(input)).toBe(expected);
  });
});

describe("a percent-spelled pair name read as a credential name as written or as an undecided name", () => {
  test.each(ALREADY)("%j has its value replaced as the reference does", (input, expected) => {
    expect(scrubValue(input)).toBe(expected);
    expect(shownRuns(scrubValue(input), SECRET)).toEqual([]);
  });
});

describe("texts that begin as a JSON object or array and hold a pair named with percent forms", () => {
  test.each(MIXED.map((row, n) => [n, ...row] as const))("text %i shows no run of a marker that the reference hides", (_n, input, secrets, path, depth, framed, hidden) => {
    let out: unknown;
    try { out = scrubValue(input, secrets, path, depth, false, undefined, undefined, framed); }
    catch (error) { expect(String(error)).toMatch(/Privacy/); return; } // a value the scrubber refuses to export shows nothing
    const shown = views(out).flatMap(view => hidden.split(" ").filter(run => view.includes(run)));
    expect([...new Set(shown)]).toEqual([]);
  });
});
