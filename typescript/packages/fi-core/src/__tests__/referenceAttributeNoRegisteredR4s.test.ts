import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubValue } from "../privacy-runtime";

// A quoted JSON-string attribute (a JSON text that is one string) with NO registered value, whose raw bytes write characters with escapes:
// scrubAttribute hides every character of a planted credential value that scrubValue hides on the same text, and its output is still one
// JSON string. The raw bytes are read as the References read them: a pair whose name and value are written with escaped quotes runs to
// the end of the text, a key block whose END line is spelled with an escape runs to the end, and an AKIA shape ends where the raw bytes
// end it (before a non-ASCII letter written as an escape).

/** What an output shows: the output, the string it encodes where it is one JSON string, and the output with its escapes read; each split
 * at the placeholders. */
const views = (output: string): string[] => {
  const all = [output];
  try { const node: unknown = JSON.parse(output); if (typeof node === "string") all.push(node); } catch { /* not JSON */ }
  all.push(output.replace(/\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))/g, (whole, hex, ch) => hex ? String.fromCharCode(parseInt(hex, 16)) : JSON.parse(`"\\${ch}"`)));
  return all.flatMap(view => view.split(/<redacted>|<omitted[^<>\n]*>/));
};
/** The 4-character windows of the planted values (every PLNT run in the string the input encodes) that `output` shows. */
const plants = (input: string): string[] => { let text = input; try { const node: unknown = JSON.parse(input); if (typeof node === "string") text = node; } catch { /* not JSON */ } return [...new Set(text.match(/PLNT[0-9A-Za-z]{4,}/g) ?? [])]; };
const windowsShown = (output: string, values: string[]): Set<string> => {
  const parts = views(output), shown = new Set<string>();
  for (const value of values) for (let i = 0; i + 4 <= value.length; i++) if (parts.some(part => part.includes(value.slice(i, i + 4)))) shown.add(value.slice(i, i + 4));
  return shown;
};
const isJSONString = (output: string) => { try { return typeof JSON.parse(output) === "string"; } catch { return false; } };

/** Lens A rows (r4q wf1) read with no registered value: `windows` are planted-value windows that both References (d2dab4e8
 * generate_privacy_fixtures.py sha256 4dbcc945ff312342e50baaec7e5b7e6dc6985281381f492a32da49369b5192c6 and the in-tree 40c761f6, sha256
 * 20a4e2840ace069714abd04a5cd394162c2c3812e3ae3bc3c808212061f32117) and scrubValue hide. */
const ROWS: { id: number; input: string; windows: string[] }[] = [
  {"id": 6458, "input": "\"\\\"password\\\":\\t\\\"PLNTLqy3jE\\u0036gR\\\" x\\u006fxb-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": 12818, "input": "\n  \"marker: \\n\\t/ \\\"password\\\":\\t\\\"PLNTGAdJwsUgBdLszi8iub8T\\\" ; spare ghp_PLNTLZIvJbWZQigA2VcqKPec end\"\t", "windows": ["2Vcq", "A2Vc", "IvJb", "JbWZ", "KPec", "LNTL", "LZIv", "NTLZ", "PLNT", "QigA", "TLZI", "VcqK", "WZQi", "ZIvJ", "ZQig", "bWZQ", "cqKP", "gA2V", "igA2", "qKPe", "vJbW"]},
  {"id": 12941, "input": "\"authorization:'PLNTjcn6AqK2W'\\nxoxb-PLNTrGgjP8r9kfS-PLNTPlheammvrPO ; notice\"\t", "windows": ["8r9k", "9kfS", "GgjP", "LNTP", "LNTr", "NTPl", "NTrG", "P8r9", "PLNT", "Plhe", "TPlh", "TrGg", "ammv", "eamm", "gjP8", "heam", "jP8r", "lhea", "mmvr", "mvrP", "r9kf", "rGgj", "vrPO"]},
  {"id": 13293, "input": "\"note\\nprefix: AKIAPLNTXP9FFPFQD11O\\u00e9more detail\"\t", "windows": ["9FFP", "D11O", "FFPF", "FPFQ", "FQD1", "LNTX", "NTXP", "P9FF", "PFQD", "PLNT", "QD11", "TXP9", "XP9F"]},
  {"id": 17678, "input": "\"\\\"api_key\\\": \\u0020\\\"PLNToNdT\\u00647fOZFNd7a\\\"\u2003ghp_PLNT0n0OniO4GPsgAZ8fanF9 ; notice\"", "windows": ["0Oni", "0n0O", "4GPs", "8fan", "AZ8f", "GPsg", "LNT0", "NT0n", "O4GP", "OniO", "PLNT", "PsgA", "T0n0", "Z8fa", "anF9", "fanF", "gAZ8", "iO4G", "n0On", "niO4", "sgAZ"]},
  {"id": 18772, "input": "\"sta\\u0072t record: -----BEGIN RSA PRIVATE KEY-----\\nPLNTnoL8ZMVGn3AS\\u006fZeWVh\\u0068E2\\u0030P2REO2\\nPLNTE\\u00354KyZEn19AgE077\\u00614b6K3QUV\\u004eRe\\n---\\u002d-END \\u0052SA\\u0020PRIVATE KEY-----\\u0020\\u0064i\\u0061gno\\u0073tic xoxb-P\\u004cNT9N\\u00641qE3bfdw-PLNTsB\\u004aizDvvZC5 ; end note \"", "windows": ["1qE3", "3bfd", "9Nd1", "BJiz", "DvvZ", "E3bf", "JizD", "LNT9", "LNTs", "NT9N", "NTsB", "Nd1q", "PLNT", "T9Nd", "TsBJ", "bfdw", "d1qE", "izDv", "qE3b", "sBJi", "vZC5", "vvZC", "zDvv"]},
  {"id": 19060, "input": "\"start record: -----BEGIN OPENSSH PRIVATE KEY-----\\nPLNT\\u00677pFO2F28\\u0074ifmj30JN\\u00718\\u0033Q4BfnPW\\nPLNTWTj4xk2Lqwo\\u0031bGFin7JLyZfhRlfx\\n--\\u002d--END\\u0020\\u004fPE\\u004eS\\u0053H PRIVATE K\\u0045Y----- diagno\\u0073tic xoxb-PLNTdOW\\u0052uuct\\u0053J\\u004c-\\u0050LNTCV4kaNQLjcc\\u0020; end n\\u006fte \"\n", "windows": ["4kaN", "CV4k", "LNTC", "LNTd", "Ljcc", "NQLj", "NTCV", "NTdO", "OWRu", "PLNT", "QLjc", "Ruuc", "TCV4", "TdOW", "V4ka", "WRuu", "aNQL", "ctSJ", "dOWR", "kaNQ", "tSJL", "uctS", "uuct"]},
  {"id": 19418, "input": "\"\\\"authorization\\\":\\t\\\"PLNTvsMMvvMb9O0ye30W0boOYEqoX8etL\\\" ; xoxb-PLNTSaGhn18rIxe-PLNThYYYSLkH4cT ; notice\"", "windows": ["18rI", "8rIx", "Ghn1", "H4cT", "LNTS", "LNTh", "LkH4", "NTSa", "NThY", "PLNT", "SLkH", "SaGh", "TSaG", "ThYY", "YSLk", "YYSL", "YYYS", "aGhn", "hYYY", "hn18", "kH4c", "n18r", "rIxe"]},
  {"id": 21506, "input": "\"\\\"access_token\\\":\\t\\\"PLNTsY2KPSR8cp9fgc\\\"\\txoxb-PLNT7ysBrQ5Ds9b-PLNTjvvqTu8PTa3 ; notice\"", "windows": ["5Ds9", "7ysB", "8PTa", "BrQ5", "Ds9b", "LNT7", "LNTj", "NT7y", "NTjv", "PLNT", "PTa3", "Q5Ds", "T7ys", "Tjvv", "Tu8P", "jvvq", "qTu8", "rQ5D", "sBrQ", "u8PT", "vqTu", "vvqT", "ysBr"]},
  {"id": 23306, "input": "\"\\\"password\\\":\\t\\\"PLNTXQXDPriqR1X7YDNAbi5r0NUJyARey\\\" / xoxb-PLNTK9lBh3MzRSO-PLNTUXQJSTCO8kB ; notice\"\n", "windows": ["3MzR", "9lBh", "Bh3M", "CO8k", "JSTC", "K9lB", "LNTK", "LNTU", "MzRS", "NTK9", "NTUX", "O8kB", "PLNT", "QJST", "STCO", "TCO8", "TK9l", "TUXQ", "UXQJ", "XQJS", "h3Mz", "lBh3", "zRSO"]},
  {"id": 26042, "input": " \t\"\\\"secret\\\":\\t\\\"PLNTGoYL8Ofc4kjUg0\\\" / xoxb-PLNTSCzMifz8bIo-PLNT43naW4IH2ow ; notice\"", "windows": ["3naW", "43na", "4IH2", "8bIo", "CzMi", "H2ow", "IH2o", "LNT4", "LNTS", "Mifz", "NT43", "NTSC", "PLNT", "SCzM", "T43n", "TSCz", "W4IH", "aW4I", "fz8b", "ifz8", "naW4", "z8bI", "zMif"]},
  {"id": 27400, "input": "\"start record: -----BEGIN RSA PRIVATE KEY-----\\nPLNTcA6EpVTnrsYF7pWZVOCHhz36VYVg\\nPLNTDCGxEAnPQfM5bGnBBnPLJjD0L1e7\\n-\\u002d---END RSA PRIVATE KEY----- diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"  ", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": 27986, "input": "\"\\\"api_key\\\":  \\u0022PLNTFRjqa8Q\\u0078\\u0076jpkia\\u0022   xoxb-PL\\u004e\\u00547Dz1XKIsLbt\\u002dPL\\u004e\\u0054Zh9Bt2g37G1 ; notice\"", "windows": ["1XKI", "2g37", "37G1", "7Dz1", "9Bt2", "Bt2g", "Dz1X", "IsLb", "KIsL", "LNT7", "LNTZ", "NT7D", "NTZh", "PLNT", "T7Dz", "TZh9", "XKIs", "Zh9B", "g37G", "h9Bt", "sLbt", "t2g3", "z1XK"]},
  {"id": 29354, "input": "\"\\\"token\\\":\\t\\\"PLNTgryEmnSCYfYFfD\\\" / xoxb-PLNTxi9pIq7eurO-PLNTq7lBQbthmGP ; notice\"", "windows": ["7eur", "7lBQ", "9pIq", "BQbt", "Iq7e", "LNTq", "LNTx", "NTq7", "NTxi", "PLNT", "Qbth", "Tq7l", "Txi9", "bthm", "eurO", "hmGP", "i9pI", "lBQb", "pIq7", "q7eu", "q7lB", "thmG", "xi9p"]},
  {"id": 32081, "input": "\"start record: -----BEGIN PRIVATE KEY-----\\nPLNTllPs2vwg01shsZI5cxaQStaNtCVl\\u000aPLNTmtIIjEYmhbqu5PMiHFnTIPRWUrs3\\n-----\\u0045ND PRIVATE KEY----- diagnostic ghp_PLNTz8whEEf6RpsM5t0okD1E ; end note \"", "windows": ["0okD", "5t0o", "6Rps", "8whE", "EEf6", "Ef6R", "LNTz", "M5t0", "NTz8", "PLNT", "RpsM", "Tz8w", "f6Rp", "hEEf", "kD1E", "okD1", "psM5", "sM5t", "t0ok", "whEE", "z8wh"]},
  {"id": 36694, "input": "\"note\\nprefix: AKIAPLNT6NNUX8C5JWIR\\u00e9more detail\"", "windows": ["5JWI", "6NNU", "8C5J", "C5JW", "JWIR", "LNT6", "NNUX", "NT6N", "NUX8", "PLNT", "T6NN", "UX8C", "X8C5"]},
  {"id": 39938, "input": "\n  \"\\\"api_key\\\":\\t\\\"PLNT2UWftET6GLJUGRabC0plcSe4uy4R4\\\" ; xoxb-PLNT7VJw15wMYQU-PLNTT6qLJWO4p6W ; notice\"\t", "windows": ["15wM", "4p6W", "5wMY", "6qLJ", "7VJw", "JWO4", "Jw15", "LJWO", "LNT7", "LNTT", "MYQU", "NT7V", "NTT6", "O4p6", "PLNT", "T6qL", "T7VJ", "TT6q", "VJw1", "WO4p", "qLJW", "w15w", "wMYQ"]},
];
/** Rows of the generated family below (one per kind, layout and tail) with the planted-value windows both References hide. */
const FAMILY_ROWS: { id: string; input: string; windows: string[] }[] = [
  {"id": "R4S-00260", "input": "\"note: detail  \\\"pass\\u0077ord\\\":\\t\\\"PLNT100104QzWx7Rk2Vb9TyLm3\\\" x xoxb-1234-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-00261", "input": "\"note: detail  \\\"pass\\u0077ord\\\":\\t\\\"PLNT100105QzWx7Rk2Vb9TyLm3\\\" diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-00262", "input": "\"note: detail  \\\"pass\\u0077ord\\\":\\t\\\"PLNT100106QzWx7Rk2Vb9TyLm3\\\" x xoxb-1234-PL\\u004eTRd9n9IpDQlK\\u002dPLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-00263", "input": "\"note: detail  \\\"pass\\u0077ord\\\":\\t\\\"PLNT100107QzWx7Rk2Vb9TyLm3\\\" diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-03971", "input": "\"start record: -----BEGIN RSA PRIVATE KEY-----\\nPLNT100f83QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END RSA PRIVATE KEY----- x xoxb-1234-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04012", "input": "\"start record: -----BEGIN RSA PRIVATE KEY-----\\nPLNT100facQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END RSA PRIVATE KEY----- diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-04053", "input": "\"start record: -----BEGIN RSA PRIVATE KEY-----\\nPLNT100fd5QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END RSA PRIVATE KEY----- x xoxb-1234-PL\\u004eTRd9n9IpDQlK\\u002dPLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04094", "input": "\"start record: -----BEGIN RSA PRIVATE KEY-----\\nPLNT100ffeQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END RSA PRIVATE KEY----- diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-04213", "input": "\"start record: -----BEGIN EC PRIVATE KEY-----\\nPLNT101075QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END EC PRIVATE KEY----- x xoxb-1234-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04252", "input": "\"start record: -----BEGIN EC PRIVATE KEY-----\\nPLNT10109cQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END EC PRIVATE KEY----- diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-04291", "input": "\"start record: -----BEGIN EC PRIVATE KEY-----\\nPLNT1010c3QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END EC PRIVATE KEY----- x xoxb-1234-PL\\u004eTRd9n9IpDQlK\\u002dPLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04330", "input": "\"start record: -----BEGIN EC PRIVATE KEY-----\\nPLNT1010eaQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END EC PRIVATE KEY----- diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-04461", "input": "\"start record: -----BEGIN OPENSSH PRIVATE KEY-----\\nPLNT10116dQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END OPENSSH PRIVATE KEY----- x xoxb-1234-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04507", "input": "\"start record: -----BEGIN OPENSSH PRIVATE KEY-----\\nPLNT10119bQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END OPENSSH PRIVATE KEY----- diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-04553", "input": "\"start record: -----BEGIN OPENSSH PRIVATE KEY-----\\nPLNT1011c9QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END OPENSSH PRIVATE KEY----- x xoxb-1234-PL\\u004eTRd9n9IpDQlK\\u002dPLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04599", "input": "\"start record: -----BEGIN OPENSSH PRIVATE KEY-----\\nPLNT1011f7QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END OPENSSH PRIVATE KEY----- diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-04715", "input": "\"start record: -----BEGIN PRIVATE KEY-----\\nPLNT10126bQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END PRIVATE KEY----- x xoxb-1234-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04750", "input": "\"start record: -----BEGIN PRIVATE KEY-----\\nPLNT10128eQzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END PRIVATE KEY----- diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-04785", "input": "\"start record: -----BEGIN PRIVATE KEY-----\\nPLNT1012b1QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END PRIVATE KEY----- x xoxb-1234-PL\\u004eTRd9n9IpDQlK\\u002dPLNTOwOsCUtpsMp ; notice\"", "windows": ["9IpD", "9n9I", "CUtp", "DQlK", "IpDQ", "LNTO", "LNTR", "NTOw", "NTRd", "OsCU", "OwOs", "PLNT", "Rd9n", "TOwO", "TRd9", "Utps", "d9n9", "n9Ip", "pDQl", "psMp", "sCUt", "tpsM", "wOsC"]},
  {"id": "R4S-04820", "input": "\"start record: -----BEGIN PRIVATE KEY-----\\nPLNT1012d4QzWx7Rk2Vb9TyLm3\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\n\\u002d----END PRIVATE KEY----- diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": "R4S-06564", "input": "\"diagnostic AKIAPLNT1019A4QZWX7R\\u00e9more detail\"", "windows": ["019A", "1019", "19A4", "4QZW", "9A4Q", "A4QZ", "LNT1", "NT10", "PLNT", "QZWX", "T101", "WX7R", "ZWX7"]},
  {"id": "R4S-06565", "input": "\"diagnostic AKIAPLNT1019A5QZWX7R\\u00e9more detail ; end note\"", "windows": ["019A", "1019", "19A5", "5QZW", "9A5Q", "A5QZ", "LNT1", "NT10", "PLNT", "QZWX", "T101", "WX7R", "ZWX7"]},
  {"id": "R4S-06566", "input": "\"diagnostic AKIAPLNT1019A6QZWX7R\\u00e9more detail x xoxb-1234-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "windows": ["019A", "1019", "19A6", "6QZW", "9A6Q", "A6QZ", "LNT1", "NT10", "QZWX", "T101", "WX7R", "ZWX7"]},
  {"id": "R4S-06567", "input": "\"diagnostic AKIAPLNT1019A7QZWX7R\\u00e9more detail diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["019A", "1019", "19A7", "7QZW", "9A7Q", "A7QZ", "LNT1", "NT10", "QZWX", "T101", "WX7R", "ZWX7"]},
  {"id": "R4S-06568", "input": "\"diagnostic AKIAPLNT1019A8QZWX7R\\u00e9more detail x xoxb-1234-PL\\u004eTRd9n9IpDQlK\\u002dPLNTOwOsCUtpsMp ; notice\"", "windows": ["019A", "1019", "19A8", "8QZW", "9A8Q", "A8QZ", "LNT1", "NT10", "QZWX", "T101", "WX7R", "ZWX7"]},
  {"id": "R4S-06569", "input": "\"diagnostic AKIAPLNT1019A9QZWX7R\\u00e9more detail diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"", "windows": ["019A", "1019", "19A9", "9A9Q", "9QZW", "A9QZ", "LNT1", "NT10", "QZWX", "T101", "WX7R", "ZWX7"]},
];

/** JSON-string attributes with no registered value: pair names in 8 layouts x escape spellings (\\uXXXX of each name character, or the
 * separator before the name written \\t \\n \\/ \\\\ \\"); PEM blocks x an escape at each END-line character, the line break before it, or a
 * BEGIN-line character; value shapes x a non-ASCII neighbour written raw or as an escape x the shape head plain or with one escaped
 * character; each x 6 tails (none, prose, a trailing xoxb- or ghp_ value, plain or with an escaped character). 8,550 cases. */
function familyR4s(): { id: string; input: string; kind: string }[] {
  const NAMES = ["password", "secret", "token", "api_key", "access_token", "authorization"];
  const LAYOUTS = [["eq", "{n} = {v}"], ["colon-quote", "{n}: '{v}'"], ["json-member", "\"{n}\": \"{v}\""], ["json-tab", "\"{n}\":\t\"{v}\""],
    ["json-2sp", "\"{n}\":  \"{v}\""], ["dq-value", "{n}=\"{v}\""], ["query", "https://h.invalid/x?mode=ok&{n}={v}&keep=world"], ["header", "{n}:\t{v}"]];
  const SEPS = [["t", "\t"], ["n", "\n"], ["slash", "/"], ["bs", "\\"], ["q", "\""]];
  const TAILS = ["", " ; end note", " x xoxb-1234-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice", " diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note ",
    " x xoxb-1234-PL\\u004eTRd9n9IpDQlK\\u002dPLNTOwOsCUtpsMp ; notice", " diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note "];
  const NEIGHBOURS = ["é", "ü", "ß", "Ж", "中", " ", "\u{1F600}"];
  const SHAPES: [string, (v: string) => string][] = [["ghp_", (v: string) => "ghp_" + v], ["sk-proj-", (v: string) => "sk-proj-" + v], ["xoxb-", (v: string) => "xoxb-1234-" + v],
    ["AKIA", (v: string) => "AKIA" + v.toUpperCase().slice(0, 16)], ["Bearer ", (v: string) => "Bearer " + v], ["github_pat_", (v: string) => "github_pat_" + v]];
  const PEMS = ["RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY"];
  const hex4 = (c: number) => "\\u" + c.toString(16).padStart(4, "0");
  const lit = (ch: string, escape: boolean) => {
    if (!escape) return JSON.stringify(ch).slice(1, -1);
    if (ch === "/") return "\\/";
    if ("\t\n\\\"".includes(ch)) return JSON.stringify(ch).slice(1, -1);
    const cp = ch.codePointAt(0)!;
    return cp > 0xffff ? hex4(ch.charCodeAt(0)) + hex4(ch.charCodeAt(1)) : hex4(cp);
  };
  const spell = (decoded: string, escaped: Set<number>) => "\"" + Array.from(decoded).map((ch, i) => lit(ch, escaped.has(i))).join("") + "\"";
  const decodeTail = (tail: string) => tail.replace(/\\u([0-9a-f]{4})/g, (w: string, h: string) => String.fromCharCode(parseInt(h, 16)));
  const cases: { id: string; input: string; kind: string }[] = [];
  const emit = (decoded: string, escaped: Set<number>, tail: string, kind: string) => {
    const dt = decodeTail(tail);
    const input = dt ? spell(decoded.slice(0, decoded.length - dt.length), escaped).slice(0, -1) + tail + "\"" : spell(decoded, escaped);
    cases.push({ id: "R4S-" + String(cases.length).padStart(5, "0"), input, kind });
  };
  const plant = () => "PLNT" + (0x100000 + cases.length).toString(16) + "QzWx7Rk2Vb9TyLm3";
  for (const name of NAMES) for (const [, t] of LAYOUTS) for (const [esc, sep] of [["u", " "], ...SEPS]) {
    for (const k of esc === "u" ? [...name].keys() : [0]) for (const tail of TAILS) {
      const value = plant(), ctx = "detail " + sep, body = t.replace("{n}", name).replace("{v}", value);
      const lead = esc === "u" && k % 2 === 1 ? "" : "note: " + ctx;
      const decoded = lead + body + decodeTail(tail), at = decoded.indexOf(name, Math.max(0, lead.length - 1));
      emit(decoded, new Set([esc === "u" ? at + k : lead.length - 1]), tail, "pair");
    }
  }
  for (const label of PEMS) for (const tail of TAILS) {
    const begin = `-----BEGIN ${label}-----`, end = `-----END ${label}-----`;
    const wheres = ["nl", ...[...end].map((_: string, i: number) => "end" + i), ...[...begin].map((_: string, i: number) => i).filter((i: number) => i % 3 === 0).map((i: number) => "begin" + i)];
    for (const where of wheres) {
      const value = plant(), body = begin + "\n" + value + "\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\n" + end;
      const decoded = "start record: " + body + decodeTail(tail), b0 = decoded.indexOf(begin), e0 = decoded.indexOf(end);
      const at = where === "nl" ? e0 - 1 : where.startsWith("end") ? e0 + Number(where.slice(3)) : b0 + Number(where.slice(5));
      emit(decoded, new Set([at]), tail, "pem");
    }
  }
  for (const [shape, f] of SHAPES) for (const nb of NEIGHBOURS) for (const nbEsc of [false, true]) {
    for (const headEsc of [-1, ...[...shape.trim()].keys()]) for (const tail of TAILS) {
      const value = plant(), body = f(value), decoded = "diagnostic " + body + nb + "more detail" + decodeTail(tail), s0 = "diagnostic ".length;
      const escaped = new Set<number>();
      if (nbEsc) escaped.add(s0 + body.length);
      if (headEsc >= 0) escaped.add(s0 + headEsc);
      emit(decoded, escaped, tail, "shape");
    }
  }
  return cases;
}

describe("scrubAttribute: quoted roots with no registered value hide what scrubValue and both References hide", () => {
  test("lens A rows read with no registered value: 0 windows shown", () => {
    const shown = ROWS.filter(row => { const out = scrubAttribute(row.input, []); const s = windowsShown(out, plants(row.input)); return row.windows.some(w => s.has(w)) || !isJSONString(out); });
    expect(shown.map(row => row.id)).toEqual([]);
  });
  test("family rows: 0 windows both References hide", () => {
    const shown = FAMILY_ROWS.filter(row => { const s = windowsShown(scrubAttribute(row.input, []), plants(row.input)); return row.windows.some(w => s.has(w)); });
    expect(shown.map(row => row.id)).toEqual([]);
  });
  test("an escaped quoted pair runs to the end; a trailing value is hidden", () => {
    const input = "\"\\\"password\\\":\\t\\\"PLNTLqy3jE\\u0036gR\\\" x\\u006fxb-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"";
    const out = scrubAttribute(input, []), byValue = scrubValue(input, []);
    expect([...windowsShown(out, plants(input))]).toEqual([...windowsShown(byValue, plants(input))]);
    expect(windowsShown(out, ["PLNTRd9n9IpDQlK", "PLNTOwOsCUtpsMp"]).size).toBe(0);
    expect(isJSONString(out)).toBe(true);
  });
  test("a key block whose END line is spelled with an escape runs to the end", () => {
    const input = "\"start record: -----BEGIN RSA PRIVATE KEY-----\\nPLNTcA6EpVTnrsYF7pWZVOCHhz36VYVg\\n-\\u002d---END RSA PRIVATE KEY----- diagnostic ghp_PLNTcu518Yncdgzvk61DWJ3D ; end note \"";
    const out = scrubAttribute(input, []);
    expect(windowsShown(out, ["PLNTcA6EpVTnrsYF7pWZVOCHhz36VYVg", "PLNTcu518Yncdgzvk61DWJ3D"]).size).toBe(0);
    expect(isJSONString(out)).toBe(true);
  });
  test("an AKIA shape before an escaped non-ASCII letter is hidden", () => {
    const input = "\"note\\nprefix: AKIAPLNTXP9FFPFQD11O\\u00e9more detail\"";
    const out = scrubAttribute(input, []);
    expect(windowsShown(out, ["PLNTXP9FFPFQD11O"]).size).toBe(0);
    expect(out).toContain("more detail");
    expect(isJSONString(out)).toBe(true);
  });
  test("generated family (8,550): scrubAttribute shows no planted window that scrubValue hides, and stays one JSON string", () => {
    const cases = familyR4s();
    expect(cases.length).toBe(8550);
    const above: string[] = [], invalid: string[] = [];
    for (const c of cases) {
      const out = scrubAttribute(c.input, []), byValue = scrubValue(c.input, []), values = plants(c.input);
      const hidden = windowsShown(byValue, values);
      if ([...windowsShown(out, values)].some(w => !hidden.has(w))) above.push(c.id);
      if (!isJSONString(out)) invalid.push(c.id);
    }
    expect({ above: above.length, invalid: invalid.length, first: above.slice(0, 5) }).toEqual({ above: 0, invalid: 0, first: [] });
  }, 120000);
});
