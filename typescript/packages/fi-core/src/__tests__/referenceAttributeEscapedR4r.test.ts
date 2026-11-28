import { describe, expect, test } from "@jest/globals";
import { scrubAttribute, scrubValue } from "../privacy-runtime";

// A quoted JSON-string attribute (a JSON text that is one string) whose raw bytes write a character with an escape (\uXXXX, \t, \n, \/,
// \\, \") while a registered value is spelled as the decoded string spells it: the registered value can take part of a credential name or
// a value shape in the read of the string only, so that read no longer hides what follows. scrubAttribute hides every character of a
// credential value that scrubValue hides on the same text, and its output is still one JSON string.

/** What an output shows: the output, the string it encodes where it is one JSON string, and the output with its escapes read; each split
 * at the placeholders. */
const views = (output: string): string[] => {
  const all = [output];
  try { const node: unknown = JSON.parse(output); if (typeof node === "string") all.push(node); } catch { /* not JSON */ }
  all.push(output.replace(/\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))/g, (whole, hex, ch) => hex ? String.fromCharCode(parseInt(hex, 16)) : JSON.parse(`"\\${ch}"`)));
  return all.flatMap(view => view.split(/<redacted>|<omitted[^<>\n]*>/));
};
/** The 4-character windows of `value` that `output` shows. */
const windowsShown = (output: string, value: string): string[] => {
  const parts = views(output), shown: string[] = [];
  for (let i = 0; i + 4 <= value.length; i++) if (parts.some(part => part.includes(value.slice(i, i + 4)))) shown.push(value.slice(i, i + 4));
  return shown;
};
const isJSONString = (output: string) => { try { return typeof JSON.parse(output) === "string"; } catch { return false; } };

/** Lens A rows: the planted-value windows both References hide that scrubAttribute showed (`windows`); and quoted-root attributes whose
 * scrubAttribute output was not one JSON string. */
const ROWS: { escaped: { id: number; input: string; registered: string[]; windows: string[] }[]; json: { from: string; id: number; input: string; registered: string[] }[] } = {"escaped": [
  {"id": 5882, "input": "\n  \"start record: -----BEGIN RSA PRI\\u0056ATE KEY-----\\nPLNTZJQklpwVzQfr9vtljsDJS8I2zZ7z\\nPLNTT8LJ028QkP25gYsUkZ0mEFCTJtcS\\u000a-----END RSA PRIVATE KEY----- diagnostic xoxb-PLNTYQOiwBsDs8a-PLNT9ftSpHI\\u004ezHu ; end note \"  ", "registered": ["a-PLNT9ftSpHINzHu ; end note", "PLNT9ftSpHINz", "8a-PLNT9ftS"], "windows": ["sDs8"]},
  {"id": 6014, "input": "\n  \"note\\\\prefix: ghp_PLNTz8D\\u004c\\u0066bLXC1FgWvJdDIhq\\u00e9more de\\u0074ail\"\t", "registered": [" ghp_PLN", "note\\prefix: ghp_"], "windows": ["LNTz", "NTz8", "PLNT"]},
  {"id": 6458, "input": "\"\\\"password\\\":\\t\\\"PLNTLqy3jE\\u0036gR\\\" x\\u006fxb-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"", "registered": ["OwOsCUtpsM"], "windows": ["9IpD", "9n9I", "DQlK", "IpDQ", "LNTR", "NTRd", "PLNT", "Rd9n", "TRd9", "d9n9", "n9Ip", "pDQl"]},
  {"id": 7033, "input": "\"diagnostic xo\\u0078b-PLN\\u0054SbLCd7rxDWW\\u002dPLNTtpTPIy3l1W1 ; end note\"  ", "registered": ["NTtpTPIy", "xDWW-PLNTtpT"], "windows": ["Iy3l", "PIy3", "y3l1"]},
  {"id": 7405, "input": "\"BEARER\\tPLNT1nudUpC3bogzLW6uyMVz \\/ secret : 'PLNTBgVYzcpDWUNoJo'\"", "registered": ["T1nudUpC3bogzLW6uyMVz / secre", "EARER\tPLNT1nudUp"], "windows": ["BgVY", "DWUN", "LNTB", "NTBg", "NoJo", "TBgV", "UNoJ", "VYzc", "WUNo", "Yzcp", "cpDW", "gVYz", "pDWU", "zcpD"]},
  {"id": 8462, "input": " \"detail \\t\\t ghp_PLNTemux06eQE2Ni9PxycfPY ; \\t\\ud83e\\uddea fin\"\n", "registered": ["PLNTemux", " \t\t ghp_PLNTem"], "windows": ["ux06", "x06e"]},
  {"id": 12065, "input": " \t\"note\\\\prefix: ghp_PLNTGMjyvHD7l3Yche3yOQpw/more detail\"\t", "registered": ["prefix: ghp_PLNT", "note\\prefix: ghp_P"], "windows": ["LNTG", "NTGM", "TGMj"]},
  {"id": 12818, "input": "\n  \"marker: \\n\\t/ \\\"password\\\":\\t\\\"PLNTGAdJwsUgBdLszi8iub8T\\\" ; spare ghp_PLNTLZIvJbWZQigA2VcqKPec end\"\t", "registered": ["spare ghp_PLNTLZIvJbWZQi"], "windows": ["2Vcq", "A2Vc", "KPec", "VcqK", "cqKP", "gA2V", "qKPe"]},
  {"id": 12941, "input": "\"authorization:'PLNTjcn6AqK2W'\\nxoxb-PLNTrGgjP8r9kfS-PLNTPlheammvrPO ; notice\"\t", "registered": ["xb-PLNTrGgjP8"], "windows": ["9kfS", "LNTP", "NTPl", "PLNT", "Plhe", "TPlh", "ammv", "eamm", "heam", "lhea", "mmvr", "mvrP", "r9kf", "vrPO"]},
  {"id": 13145, "input": " \t\"marker: \\n\\u2003/ ghp_PLNTWbG9MW10m1LFBZfocmTu ; spare AKIAPLNTUNG7J43DMZ2Y end\"", "registered": ["\n\u2003/ ghp_PLNTWbG9MW10m1LFBZ", "TWbG9MW10m1LFBZf"], "windows": ["focm"]},
  {"id": 13293, "input": "\"note\\nprefix: AKIAPLNTXP9FFPFQD11O\\u00e9more detail\"\t", "registered": ["note\nprefix: AK"], "windows": ["9FFP", "D11O", "FFPF", "FPFQ", "FQD1", "LNTX", "NTXP", "P9FF", "PFQD", "PLNT", "QD11", "TXP9", "XP9F"]},
  {"id": 17678, "input": "\"\\\"api_key\\\": \\u0020\\\"PLNToNdT\\u00647fOZFNd7a\\\"\u2003ghp_PLNT0n0OniO4GPsgAZ8fanF9 ; notice\"", "registered": [":  \"PLNToNdTd7fOZFNd7a\"\u2003", "\":  \"PLNToNdTd7fOZF", "y\":  \"PLNToNdTd7fOZFNd7a\"\u2003ghp"], "windows": ["0Oni", "0n0O", "4GPs", "8fan", "AZ8f", "GPsg", "LNT0", "NT0n", "O4GP", "OniO", "PLNT", "PsgA", "T0n0", "Z8fa", "anF9", "fanF", "gAZ8", "iO4G", "n0On", "niO4", "sgAZ"]},
  {"id": 18491, "input": " \"eyJPLNT82Mp7m6c78tA7AG9.PLNTLvwdbVTmAmwaGE3F.PLNThTbgMRUjQcFGBqAQ\\nauthorization  :  'PLNTa7Y9fXATDhJJ0eqWcxrxICI9my5EB'\"", "registered": [".PLNThTbgMRUjQcFGBqAQ\naut"], "windows": ["0eqW", "7Y9f", "9fXA", "9my5", "ATDh", "CI9m", "DhJJ", "I9my", "ICI9", "J0eq", "JJ0e", "LNTa", "NTa7", "PLNT", "TDhJ", "Ta7Y", "Wcxr", "XATD", "Y9fX", "a7Y9", "cxrx", "eqWc", "fXAT", "hJJ0", "my5E", "qWcx", "rxIC", "xICI", "xrxI", "y5EB"]},
  {"id": 18547, "input": "\"\\u0041KIAPLNT3OKA7BPBIN40\\n--pass PLNTjz0mcf4Z96nllWvtXnQ8\"  ", "registered": ["AKIAPLNT3OKA7BPBI", "AKIAPLNT3OKA7BPBIN40\n"], "windows": ["0mcf", "4Z96", "6nll", "96nl", "LNTj", "NTjz", "Tjz0", "WvtX", "XnQ8", "Z96n", "cf4Z", "f4Z9", "jz0m", "lWvt", "llWv", "mcf4", "nllW", "tXnQ", "vtXn", "z0mc"]},
  {"id": 18772, "input": "\"sta\\u0072t record: -----BEGIN RSA PRIVATE KEY-----\\nPLNTnoL8ZMVGn3AS\\u006fZeWVh\\u0068E2\\u0030P2REO2\\nPLNTE\\u00354KyZEn19AgE077\\u00614b6K3QUV\\u004eRe\\n---\\u002d-END \\u0052SA\\u0020PRIVATE KEY-----\\u0020\\u0064i\\u0061gno\\u0073tic xoxb-P\\u004cNT9N\\u00641qE3bfdw-PLNTsB\\u004aizDvvZC5 ; end note \"", "registered": ["NTsBJizDvvZC5 ; end not", "oxb-PLNT9Nd1qE3bfdw-PLNT"], "windows": ["BJiz", "DvvZ", "JizD", "izDv", "sBJi", "vZC5", "vvZC", "zDvv"]},
  {"id": 19060, "input": "\"start record: -----BEGIN OPENSSH PRIVATE KEY-----\\nPLNT\\u00677pFO2F28\\u0074ifmj30JN\\u00718\\u0033Q4BfnPW\\nPLNTWTj4xk2Lqwo\\u0031bGFin7JLyZfhRlfx\\n--\\u002d--END\\u0020\\u004fPE\\u004eS\\u0053H PRIVATE K\\u0045Y----- diagno\\u0073tic xoxb-PLNTdOW\\u0052uuct\\u0053J\\u004c-\\u0050LNTCV4kaNQLjcc\\u0020; end n\\u006fte \"\n", "registered": ["Q4BfnPW\nPLNTWTj4xk2Lqwo1b", "WTj4xk2Lqwo1bGFin"], "windows": ["4kaN", "CV4k", "LNTC", "LNTd", "Ljcc", "NQLj", "NTCV", "NTdO", "OWRu", "PLNT", "QLjc", "Ruuc", "TCV4", "TdOW", "V4ka", "WRuu", "aNQL", "ctSJ", "dOWR", "kaNQ", "tSJL", "uctS", "uuct"]},
  {"id": 19262, "input": " \"detail /\\\" ghp_PLNTAd0frB8tCuMim8C1ze7I ; /\\\\ fin\"\n", "registered": ["CuMim8C1ze", "Mim8C1ze7I ; /\\ fin"], "windows": ["8tCu", "B8tC"]},
  {"id": 19418, "input": "\"\\\"authorization\\\":\\t\\\"PLNTvsMMvvMb9O0ye30W0boOYEqoX8etL\\\" ; xoxb-PLNTSaGhn18rIxe-PLNThYYYSLkH4cT ; notice\"", "registered": ["LNThYYYSLkH4cT ; no", "xb-PLNTSaGhn18rIxe-PLNThY"], "windows": ["H4cT", "LkH4", "SLkH", "YSLk", "YYSL", "kH4c"]},
  {"id": 20283, "input": "\"x\\u006fxb-PLNTwyRZ68xoOUO\\u002dPLNTWZDZKVtSOF9\"\n", "registered": ["xb-PLNTwy", "xoxb-PLNTw"], "windows": ["yRZ6"]},
  {"id": 20846, "input": "\"ghp_PLNTioIcTs3UXIgiuKwj28K6 \\/ api_key : 'PLNTWcoyynFjo85aYfAS7l0mxx52VkE4j'\"\n", "registered": [" / api_ke", "8K6 / api_key"], "windows": ["0mxx", "2VkE", "52Vk", "5aYf", "7l0m", "85aY", "AS7l", "Fjo8", "LNTW", "NTWc", "S7l0", "TWco", "VkE4", "Wcoy", "YfAS", "aYfA", "coyy", "fAS7", "jo85", "kE4j", "l0mx", "mxx5", "nFjo", "o85a", "oyyn", "x52V", "xx52", "ynFj", "yynF"]},
  {"id": 21506, "input": "\"\\\"access_token\\\":\\t\\\"PLNTsY2KPSR8cp9fgc\\\"\\txoxb-PLNT7ysBrQ5Ds9b-PLNTjvvqTu8PTa3 ; notice\"", "registered": ["ysBrQ5Ds9b-PLNTjvvq", "b-PLNT7ysBrQ5Ds9b", "Ds9b-PLNTjvvqTu8PTa3 ; notice"], "windows": ["7ysB", "BrQ5", "LNT7", "NT7y", "PLNT", "T7ys", "sBrQ", "ysBr"]},
  {"id": 23306, "input": "\"\\\"password\\\":\\t\\\"PLNTXQXDPriqR1X7YDNAbi5r0NUJyARey\\\" / xoxb-PLNTK9lBh3MzRSO-PLNTUXQJSTCO8kB ; notice\"\n", "registered": ["/ xoxb-PLNTK9lBh3MzR", "-PLNTK9lBh3MzRSO", "PLNTK9lB"], "windows": ["CO8k", "JSTC", "LNTU", "NTUX", "O8kB", "PLNT", "QJST", "STCO", "TCO8", "TUXQ", "UXQJ", "XQJS"]},
  {"id": 26042, "input": " \t\"\\\"secret\\\":\\t\\\"PLNTGoYL8Ofc4kjUg0\\\" / xoxb-PLNTSCzMifz8bIo-PLNT43naW4IH2ow ; notice\"", "registered": ["cret\":\t\"PLNTGoYL8Ofc4kjUg0\" ", "\"secret\":\t", "\"secret\":\t\"PLNTGoY"], "windows": ["3naW", "43na", "4IH2", "8bIo", "CzMi", "H2ow", "IH2o", "LNT4", "LNTS", "Mifz", "NT43", "NTSC", "PLNT", "SCzM", "T43n", "TSCz", "W4IH", "aW4I", "fz8b", "ifz8", "naW4", "z8bI", "zMif"]},
  {"id": 27400, "input": "\"start record: -----BEGIN RSA PRIVATE KEY-----\\nPLNTcA6EpVTnrsYF7pWZVOCHhz36VYVg\\nPLNTDCGxEAnPQfM5bGnBBnPLJjD0L1e7\\n-\\u002d---END RSA PRIVATE KEY----- diagnostic ghp_PLN\\u0054cu518Yncdgzvk61DWJ3D ; end note \"  ", "registered": ["LNTDCGxEAn"], "windows": ["18Yn", "1DWJ", "518Y", "61DW", "8Ync", "DWJ3", "LNTc", "NTcu", "PLNT", "Tcu5", "WJ3D", "Yncd", "cdgz", "cu51", "dgzv", "gzvk", "k61D", "ncdg", "u518", "vk61", "zvk6"]},
  {"id": 27986, "input": "\"\\\"api_key\\\":  \\u0022PLNTFRjqa8Q\\u0078\\u0076jpkia\\u0022   xoxb-PL\\u004e\\u00547Dz1XKIsLbt\\u002dPL\\u004e\\u0054Zh9Bt2g37G1 ; notice\"", "registered": ["t2g37G1 ; notice", "-PLNTZh9Bt2g", "1 ; notice"], "windows": ["1XKI", "7Dz1", "Dz1X", "IsLb", "KIsL", "LNT7", "LNTZ", "NT7D", "NTZh", "PLNT", "T7Dz", "TZh9", "XKIs", "Zh9B", "sLbt", "z1XK"]},
  {"id": 29059, "input": " \t\"AKIAPLNTI6BHH5OK09MJ\\n\\\"pass\\t=\\tPLNTuXlLIe4iBKYRjbmGRwe0xkeKfX7qq\\\"\"\n", "registered": ["PLNTI6BHH5OK09MJ\n\"pass\t"], "windows": ["0xke", "4iBK", "BKYR", "GRwe", "Ie4i", "KYRj", "KfX7", "LIe4", "LNTu", "NTuX", "PLNT", "Rjbm", "Rwe0", "TuXl", "X7qq", "XlLI", "YRjb", "bmGR", "e0xk", "e4iB", "eKfX", "fX7q", "iBKY", "jbmG", "keKf", "lLIe", "mGRw", "uXlL", "we0x", "xkeK"]},
  {"id": 29354, "input": "\"\\\"token\\\":\\t\\\"PLNTgryEmnSCYfYFfD\\\" / xoxb-PLNTxi9pIq7eurO-PLNTq7lBQbthmGP ; notice\"", "registered": ["q7lBQbthmGP ;", "i9pIq7eurO-PLNTq7lBQbthmG"], "windows": ["LNTx", "PLNT"]},
  {"id": 29716, "input": "\"note\\u000aprefix: \\u0078oxb-PLNTD9i9y68MvpL-PLNTCMFbria0c\\u0055h\\/more detail\"", "registered": ["refix: xoxb-PLNTD9i", "-PLNTD9i9y"], "windows": ["9y68", "y68M"]},
  {"id": 30227, "input": "\n  \"eyJPLNTQmaOFJP3SnkDfidr.PLNTvqQmOUAnpZfm8S75.PLNTeuJs3qwZtkI6zcmB\\u2003password\\t:\\t'PLNT7LMgvWuew0XWgEIpCp3ZRfoobCs9T'\"", "registered": ["PLNTeuJs3qwZtkI6zcmB\u2003password"], "windows": ["0XWg", "3ZRf", "7LMg", "Cp3Z", "Cs9T", "EIpC", "IpCp", "LMgv", "LNT7", "MgvW", "NT7L", "PLNT", "Rfoo", "T7LM", "WgEI", "Wuew", "XWgE", "ZRfo", "bCs9", "ew0X", "foob", "gEIp", "gvWu", "obCs", "oobC", "p3ZR", "pCp3", "uew0", "vWue", "w0XW"]},
  {"id": 32081, "input": "\"start record: -----BEGIN PRIVATE KEY-----\\nPLNTllPs2vwg01shsZI5cxaQStaNtCVl\\u000aPLNTmtIIjEYmhbqu5PMiHFnTIPRWUrs3\\n-----\\u0045ND PRIVATE KEY----- diagnostic ghp_PLNTz8whEEf6RpsM5t0okD1E ; end note \"", "registered": ["nostic ghp_PLNTz8whEEf6R", "ic ghp_PL"], "windows": ["0okD", "5t0o", "M5t0", "kD1E", "okD1", "psM5", "sM5t", "t0ok"]},
  {"id": 35101, "input": "\"note\\tprefix: ghp_PLNTc2Tsk6iV5b1ZRUAy2Cvr\\ud83e\\uddeamore detail\"", "registered": ["Tc2Tsk6iV", "\tprefix: ghp_PLNTc2Tsk"], "windows": ["6iV5", "V5b1", "iV5b"]},
  {"id": 36694, "input": "\"note\\nprefix: AKIAPLNT6NNUX8C5JWIR\\u00e9more detail\"", "registered": ["e detail"], "windows": ["5JWI", "6NNU", "8C5J", "C5JW", "JWIR", "LNT6", "NNUX", "NT6N", "NUX8", "PLNT", "T6NN", "UX8C", "X8C5"]},
  {"id": 39938, "input": "\n  \"\\\"api_key\\\":\\t\\\"PLNT2UWftET6GLJUGRabC0plcSe4uy4R4\\\" ; xoxb-PLNT7VJw15wMYQU-PLNTT6qLJWO4p6W ; notice\"\t", "registered": ["\"api_key\":\t\"PLNT2UWftET6", "\"api_key\":\t", "\"api_key\":\t\"PLNT2UWftET6G"], "windows": ["15wM", "4p6W", "5wMY", "6qLJ", "7VJw", "JWO4", "Jw15", "LJWO", "LNT7", "LNTT", "MYQU", "NT7V", "NTT6", "O4p6", "PLNT", "T6qL", "T7VJ", "TT6q", "VJw1", "WO4p", "qLJW", "w15w", "wMYQ"]}], "json": [
  {"from": "wf2", "id": 25, "input": "\" details api_key=\\t'PLNTlL4YysAgcmzLb5uGH' + crumb\\nmosaicJasper\"\n", "registered": ["_key=\\t'PLNT", "LNTlL4YysAgcmzLb5uGH", "crumb\\nmosaicJasper", "_key=\t'PLNTlL4YysAg"]},
  {"from": "wf2", "id": 147, "input": "\t\"\\\"access_token=\\u0009PLNTX0Sz0Gnj4YCJvKoG1\\\"\\tcrumb\\nmosaicJasper tail \"", "registered": ["\\\"access", "ess_toke", "crumb\\nmosaicJasper"]},
  {"from": "wf2", "id": 350, "input": "\"http://beta.invalid/doc?mode=ready&password=PL\\u004eT3O\\u0073GeP\\u0048zal&tag=mosaic&tail=quartz#preview=ok + crumb\\\\mosaicQuartz tail \"\n", "registered": ["=PL\\u004eT3", "crumb\\\\mosaicQuartz"]},
  {"from": "wf2", "id": 357, "input": "\"http://gamma.invalid/doc?mode=ready&secret=PLNTeahvXgia0G8vfTmDn&tag=mosaic&tail=quartz#preview=ok\\tcrumb\\nmosaicQuartz\"\n", "registered": ["t=PLNTeahvXgia0G8vfTmDn", "crumb\\nmosaicQuartz", "=PLNTeahvXgia0G8vfTm"]},
  {"from": "wf2", "id": 395, "input": "\"http://alpha.invalid/doc?mode=ready&authorization=PLNT33RNvkGZDM5c9GZxWPgdwum&tag=mosaic&tail=quartz#preview=ok\\tcrum\\u0062\\u000amosaicQuartz ; done\" ", "registered": ["ion=PLNT33R", "PLNT33RNvkGZDM5c9GZxWPgdwum", "crum\\u0062\\u000amosaicQuartz", "ion=PLNT33RNvk"]},
  {"from": "wf2", "id": 420, "input": "\" http://alpha.invalid/doc?page=demo#mode=ready&token=PLN\\u0054pzjAEsQ01GvlGK25o&tag=mosaic&tail=quartz\\tcrumb\\n\\u0022mosaicQuartz ; done\" ", "registered": ["=PLN\\u0054pzj", "oken=PLNTpzjA", "crumb\n\"mosaicQuartz"]},
  {"from": "wf2", "id": 424, "input": "\"notice: https://alpha.invalid/doc?page=demo#mode=ready&api_key=PLNTufmKrn8ZNShGQrt21IrKc58fOIH&tag=mosaic&tail=quartz / crumb\\n\\\"mosaicStone ; done\" ", "registered": ["ey=PLNTufmKrn8ZNShGQrt21IrKc58fOIH", "crumb\\n\\\"mosaicStone", "_key=PLNTufm"]},
  {"from": "wf2", "id": 427, "input": "\"notice: https://gamma.invalid/doc?page=demo#mode=ready&api_key=PL\\u004ET3rrXd\\u0062v\\u004fZ2&tag=mosaic&tail=quartz / crumb\\t/mosaicBirch\" ", "registered": ["y=PL\\u004ET3rr", "eady&api_key=PL", "crumb\\t/mosaicBirch"]},
  {"from": "wf2", "id": 444, "input": "\" details http://beta.invalid/doc?page=demo#mode=ready&authorization=\\u0050LNTaG\\u0079Gb0\\u007aGyc&tag=mosaic&tail=quartz ; crumb\\tmosaicJasper\"\n", "registered": ["thorization=\\u0050LNTaG", "zation=\\u00", "on=\\u0050LNTa", "crumb\\tmosaicJasper"]},
  {"from": "wf2", "id": 576, "input": "\"crumb\\\"mosaicBirch https://alpha.invalid/x?token=PLNTDT783gsK0a4VWcOV#state=demo&access_token=PLNT4h2MdldjJk4Q more \\\"password = PLNTrydOLPolES\\\"\\tcrumb\\u0022mosaicBirch\"\n", "registered": ["rd = PLN", "d = PLNTrydOLPolES", "crumb\\u0022mosaicBirch", "rd = PLNTrydOLPolES\"\tc"]},
  {"from": "wf2", "id": 587, "input": "\"diagnostic crumb\\t/mosaicJasper https://alpha.invalid/x?token=PLNTo0Kjb9Mv9Du86yIZ#state=demo&access_token=PLNTGqsQ19whlCV3 more \\\"t\\u006Fken = PLNTLjLNrMO2I65iNtWOn\\\"\\tcrumb\\t\\/mosaicJasper ; done\"\n", "registered": ["\\u006Fken = PLNT", "crumb\\t\\/mosaicJasper"]},
  {"from": "wf2", "id": 602, "input": "\"notice: crumb\\\"mosaicStone https://alpha.invalid/x?token=PLNTpdBuPig04NIDon5o#state=demo&access_token=PLNTf7S7DE46PgXI more \\\"access_token=PLNTVmXsFg1TTCK0SxoKvREnWOnXnQV\\\" ; crumb\\u0022mosaicStone\" ", "registered": ["ore \\\"ac", "crumb\\u0022mosaic", "u0022mosaicStone", "crumb\"mosaicStone"]},
  {"from": "wf2", "id": 606, "input": "\"notice: crumb\\t\\/mosaicBirch https://alpha.invalid/x?token=PLNTdXrTQ6rAlHdg13aG#state=demo&access_token=PLNTYab4J4DQBjeE more \\u0022access_token=\\tPLNTjRVZYlEjtTffO8Bz33BT8ITxdnb\\u0022\"\n", "registered": ["re \\u0022access", "crumb\\t\\/mosaicBirch"]},
  {"from": "wf2", "id": 620, "input": "\" details crumb\\\\mosaicJasper https://alpha.invalid/x?token=PLNTXbOkI64nUx2fo6qQ#state=demo&access_token=PLNTCkxRLHaxrWw8 more \\\"authorization=\\tPLNTWBGx22tRQdOUeXjIW\\\" crumb\\\\mosaicJasper\" ", "registered": ["zation=\tPLNTWBGx22tRQ", "\\\"authorizatio", "crumb\\\\mosaicJasper"]},
  {"from": "wf2", "id": 622, "input": "\" crumb\\t\\/mosaicQuartz https://alpha.invalid/x?token=PLNTRS9V6eRAQ3fVO1Az#state=demo&access_token=PLNTaCeVLCgUsKLV more \\\"authorization =PLNT1\\u0050qBUVhW6V7v4\\u006EzB6Ts7FNBQEif\\\"\" ", "registered": ["thorization ", "tion =PLNT1\\u0050qB", "crumb\\t\\/mosaicQuartz", " \"authoriza"]},
  {"from": "wf2", "id": 632, "input": "\t\"crumb\\nmosaicStone -----BEGIN PRIVATE KEY-----\\nPLNTnqYnl0NSB4yzEhAzGrceEJlNjObTqC6\\n-----END PRIVATE KEY----- note \\u0022password\\u0020=PLNTnBgfsSXDog\\\"\"\n", "registered": ["----- note \\u0022passwor", "ord\\u0020=PLNTnBgf", "crumb\\nmosaicStone", " note \"passw"]},
  {"from": "wf2", "id": 638, "input": "\"crumb\\t/mosaicJasper -----BEGIN PRIVATE KEY-----\\nPLNTf0BDnoBCUJYu2fx0D0h4MqW3vFgjWms\\n-----END PRIVATE KEY----- note \\\"se\\u0063ret =PLNTY5aYdiH5OfO0sJvdn\\\" / crumb\\t\\/mosaicJasper\" ", "registered": ["\\u0063ret ", " \"secret =PLNTY5aYdi", "crumb\t/mosaicJasper"]},
  {"from": "wf2", "id": 680, "input": "\"crumb\\n\\\"mosaicQuartz -----BEGIN PRIVATE KEY-----\\nPLNT2v55TTM5CSJlnHYa39iWOmqUNk2fmMd\\n-----END PRIVATE KEY----- note \\\"authorization = PLNTEu9yljyXgp8I0nLZo\\\"\"\n", "registered": ["ion = PLNTEu", " PLNTEu9yljyXgp8I0nLZo", "crumb\\n\\\"mosaicQuartz", "ion = PL"]},
  {"from": "wf2", "id": 687, "input": "\" details crumb/mosaicBirch secret = PLNTfSvYszZ2v4pgNM98jm0VK\\t ; password=PLNTq9jNRzW92Myd8QsFJtXRJwf + crumb\\u002fmosaicBirch\" ", "registered": ["sword=PLNTq9jNR", "K\\t ; password", "crumb\\u002fmosaicBirch"]},
  {"from": "wf2", "id": 695, "input": "\" crumb\\\"mosaicBirch secret = PLNTdFmqmx1C1dta1SiTqHkmt\\t ; secret\\t= 'PLNTVMKZlVku6VvPYIgrV' + crumb\\u0022mosaicBirch ; done\" ", "registered": ["cret\\t= 'PLNTVMKZlVku6V", " ; secret\t= '", "crumb\"mosaicBirch"]},
  {"from": "wf2", "id": 698, "input": "\"diagnostic crumb\\tmosaicBirch secret = PLNT6a4GMcUcEGBGulZWt8rlX; more secre\\u0074='PLNTII7T32qRgS4CvNsVu8c8WoQnRVr' + crumb\\tmosaicBirch\"\n", "registered": ["ore secre\\u007", "\\u0074='PLNTI", "crumb\\tmosaicBirch", "more secre"]},
  {"from": "wf2", "id": 719, "input": "\t\"crumb\\\\mosaicQuartz secret = PLNT1hik40qCFUwUojayRDTYG; more access_token = \\\"PLNTAnp1rXdf0DvTybMcv\\\"\"", "registered": ["s_token = \\\"", "en = \\\"PL", "\\\"PLNTAnp1rXdf0Dv", "crumb\\\\mosaicQuartz"]},
  {"from": "wf2", "id": 724, "input": "\" details crumb\\nmosaicBirch secret = PLNTpVrCwjmjeVSv6VEgVF8En\\nnext access_token=\\\"PLNTSzaymIr\\u0044d4\\\"\\tcrumb\\nmosaicBirch tail \"\n", "registered": ["en=\\\"PLNTSzaymIr\\u0044d", "_token=\"PLNT", "crumb\nmosaicBirch"]},
  {"from": "wf2", "id": 726, "input": "\"diagnostic crumb\\u0009mosaicStone secret = PLNTIp4vbVFlrbtu8037fBBKK access_token = \\u0050LNTHyPXSSGYdQoqHlL3ZEsvlwIosAV\"\n", "registered": [" = \\u0050LNTHyPXSSG", "crumb\\u0009mosaic", "\\u0009mosaicStone", "crumb\tmosaicStone"]},
  {"from": "wf1", "id": 326, "input": "\"marker: /\\ud83e\\uddea\\t ghp_PLNTJAlRZXlGxqzPraSLsyxN ; spare https://example.invalid/x?safe=ready&api%5Fkey=PLNTmukAIYko6JdqtSX6CPXC&keep=world#anchor end\"\n", "registered": [" ghp_PLNTJAlRZXlGxqzPr", "/\\ud83e\\uddea\\t ghp_PLNTJ"]},
  {"from": "wf1", "id": 406, "input": "\"\\\"secret\\t=\\tPLNTSouAPlnCDwR5irdegfZc\\\"\\tAKIAPLNTKL8VEFCH6NUF ; notice\"\t", "registered": ["Zc\\\"\\tAKIA"]},
  {"from": "wf1", "id": 672, "input": "\n  \"sk-PLNT8npmxeDOrtzNb3jqGIdk / \\u0061pi_key \\u003d PLNTJL2j6cWaz\"", "registered": ["jqGIdk / \\u0061pi_k", "\\u0061pi_key \\u003d"]},
  {"from": "wf1", "id": 886, "input": "\"start record: -----BEGIN PRIVATE KEY-----\\nPLNTJa0HkrSjYhqIoxs1t96NXHTj1Hxb\\nPLNTLizo8uEtZaY5OvQmtGiAdlXcz2Cv\\n-----END PRIVATE KEY----- diagnostic https://example.invalid/x?mode=debug#tag=ok&access%5Ftoken=PLNTln221SM7681PkV40Y59N&keep=world ; end note \"\t", "registered": ["qIoxs1t96NXHTj1Hxb\\nPLNT", "KEY-----\\nPLNTJa0HkrSjYhqIoxs1t96"]},
  {"from": "wf1", "id": 897, "input": "\"sk-proj-P\\u004cNTP2u2i\\u006eMvqnFEM3qfCK8k / password  =  PLNTZfq\\u004byHdPg\\u004ddIIH4898uM3NYzFdjny\"\n", "registered": ["k-proj-P\\u004cNTP2u2i\\u006", "\"sk-proj-"]},
  {"from": "wf1", "id": 925, "input": "\"api_key: PLNTuSVSEpnTBBiTiaUjMItR18Mcokmnf\\nBasic\\tPLNTQGVWhYWEngibOv8JS3Di ; notice\"  ", "registered": ["8JS3Di ; notice", "Mcokmnf\\nBasic\\tPLNTQGVWhYWEngibOv8JS3Di ", "S3Di ; n"]},
  {"from": "wf1", "id": 1015, "input": "\"password: PLNTibOQ4ZV6e4dstqgLSBF8\\u2003https://PLNToDr8Z3F40I:PLNTj23yYje3ng0RKKjDUx8a@example.invalid/x?mode=none ; notice\"\n", "registered": ["LNTibOQ4ZV6e4dstqgLSBF8\\u2003h", "\"password: PLNTib"]},
  {"from": "wf1", "id": 1326, "input": "\"sk-proj-PLNTSVroYusUg\\u0051ALc3NlhM00 / authorization=PLNTZRPFhm5O\\u00578WWDrQ6AyEKerFQc3xoa\"\t", "registered": ["k-proj-PLNTSVroYusUg\\"]},
  {"from": "wf1", "id": 1464, "input": "\"sk-PLNT4g9ZRer6dHuFDu1FWz4P\\n\\\"access_token  =  PLNTFjAqL1AoW\\\"\"  ", "registered": ["z4P\n\"access_toke", "er6dHuFDu1FWz4P\\n\\\"acce"]},
  {"from": "wf1", "id": 1516, "input": "\"\\\"pwd  =  PLNTAP4h4EzlFg1Z50FYPvpIxkRFNBD4o\\\" / --pwd PLNTUaj7CdsQavTYST\"  ", "registered": [" --pwd PLNTUaj7CdsQavTYST\"", "TAP4h4EzlFg1Z50FYPvpIxkRFNBD4o\\\" / --pwd", "xkRFNBD4"]},
  {"from": "wf1", "id": 1550, "input": "\"ghp_PLNTGA\\u006c3ecCOL9LM1BJfrT17 --token PLNTu\\u004a\\u006fgSz\\u0073ZGNkaOPVGzWr6\"\n", "registered": ["NTGA\\u006c3ecCOL9LM1BJfrT1"]},
  {"from": "wf1", "id": 2087, "input": "\"-----BEGIN OPENSSH PRIVATE KEY-----\\nPLNTd4hQAInx2GDu0gZ6AmRec4BWlQXZ\\nPLNTMnFzUiKq3cysXscNwzROkob0e8hz\\n-----END OPENSSH PRIVATE KEY----- / secret:PLNTm0JplLp6uw0GhOL7GX3JdfXCAOfF9\"\t", "registered": ["D OPENSSH PR", "z\\n-----END OPENSSH PRIVATE KEY-----"]},
  {"from": "wf1", "id": 2278, "input": "\"AKIAPLNTRFDQ5V4IRK2D\\npwd  =  PLNTOGADYR8oLwKSWENx\\u004dXSg\"\t", "registered": ["OGADYR8oLwKSWENx\\u0"]},
  {"from": "wf1", "id": 2449, "input": "\"secret:\\tPLNT\\u0051NM\\u0041f5re4\\u0055uAzBG13Z\\u0041\\u0032qoYe6Hm\\u004b\\u004e / \\\"authori\\u007aation = \\u0050LNT8QJiDpSFMuO\\u0079omrbmhea\\\"\"  ", "registered": ["ori\\u007aation = \\u0050LNT", "authori\\u007aation = \\u0050"]},
  {"from": "wf1", "id": 2661, "input": " \"-----BEGIN PRIVATE KEY-----\\nPLNTXuhSCuzeGkzo6nT0BIcR0qwfUMqb\\nPLNTRjoKdqHlRVzTIvwFQ8VMbnuSXtax\\n-----END PRIVATE KEY-----\\tpwd = PLNTvF6JbVKx1DzWwMZToPD7rjG06mcCT\"", "registered": ["---\\nPLNT"]},
  {"from": "wf1", "id": 2846, "input": " \t\"note\\u2003prefix: ghp_PLNTYVkxMYHdLYFGPqOQ9l3p\\nmore detail\"", "registered": ["hp_PLNTYVkxM", "note\u2003prefix: ghp_PLNTYV"]}]};

describe("lens A rows: a quoted JSON-string attribute with escapes and registered values", () => {
  test("scrubAttribute shows none of the planted-value windows both References hide", () => {
    const failures = ROWS.escaped.filter(row => {
      const parts = views(scrubAttribute(row.input, row.registered));
      return row.windows.some(w => parts.some(part => part.includes(w)));
    }).map(row => row.id);
    expect(ROWS.escaped.length).toBe(33);
    expect(failures).toEqual([]);
  });

  test("a registered value spelled as the string spells it that takes a pair's name", () => {
    const input = "\"BEARER\\tPLNT1nudUpC3bogzLW6uyMVz \\/ secret : 'PLNTBgVYzcpDWUNoJo'\"";
    const secrets = ["T1nudUpC3bogzLW6uyMVz / secre", "EARER\tPLNT1nudUp"];
    expect(windowsShown(scrubAttribute(input, secrets), "PLNTBgVYzcpDWUNoJo")).toEqual([]);
    expect(isJSONString(scrubAttribute(input, secrets))).toBe(true);
  });

  test("a registered value spelled as the string spells it that takes the head of a value shape", () => {
    const input = "\n  \"note\\\\prefix: ghp_PLNTz8D\\u004c\\u0066bLXC1FgWvJdDIhq\\u00e9more de\\u0074ail\"\t";
    const secrets = [" ghp_PLN", "note\\prefix: ghp_"];
    const value = "PLNTz8DLfbLXC1FgWvJdDIhq", hidden = windowsShown(scrubValue(input, secrets), value);
    expect(windowsShown(scrubAttribute(input, secrets), value).filter(w => !hidden.includes(w))).toEqual([]);
    expect(windowsShown(scrubAttribute(input, secrets), value)).not.toContain("PLNT");
  });

  test("an escaped pair whose value the raw bytes read to the end of the text", () => {
    const input = "\"\\\"password\\\":\\t\\\"PLNTLqy3jE\\u0036gR\\\" x\\u006fxb-PLNTRd9n9IpDQlK-PLNTOwOsCUtpsMp ; notice\"";
    const secrets = ["OwOsCUtpsM"];
    for (const value of ["PLNTLqy3jE6gR", "PLNTRd9n9IpDQlK", "PLNTOwOsCUtpsMp"]) expect(windowsShown(scrubAttribute(input, secrets), value)).toEqual(windowsShown(scrubValue(input, secrets), value));
    expect(isJSONString(scrubAttribute(input, secrets))).toBe(true);
  });
});

describe("a generated family: escape spellings x pair names and value shapes x registered values cut at each offset", () => {
  // As harness r4r family/gen_r4r.py: the context character before the name is written with an escape (or one character of the name as
  // \uXXXX), and a registered value starts 3 or 6 characters before the name and cuts it at each offset, spelled as the decoded string
  // spells it or as the raw bytes do.
  const names = ["password", "secret", "token", "api_key", "access_token", "authorization"];
  const layouts: Record<string, (name: string, value: string) => string> = {
    "eq": (n, v) => `${n} = ${v}`, "colon-quote": (n, v) => `${n}: '${v}'`, "json-member": (n, v) => `"${n}": "${v}"`,
    "query": (n, v) => `https://h.invalid/x?mode=ok&${n}=${v}&keep=world`,
  };
  const shapes: Record<string, (value: string) => string> = { "ghp_": v => "ghp_" + v, "sk-proj-": v => "sk-proj-" + v, "xoxb-": v => "xoxb-" + v, "AKIA": v => "AKIA" + v.toUpperCase().slice(0, 16), "Bearer ": v => "Bearer " + v };
  const seps: Record<string, string> = { t: "\t", n: "\n", slash: "/", bs: "\\", q: "\"" };
  const spell = (decoded: string, escaped: Set<number>) => [...decoded].map((ch, i) => !escaped.has(i) ? JSON.stringify(ch).slice(1, -1) : ch === "/" ? "\\/" : "\t\n\\\"".includes(ch) ? JSON.stringify(ch).slice(1, -1) : "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0")).join("");
  const heads: [string, (value: string) => string][] = [];
  for (const name of names) for (const layout of Object.values(layouts)) heads.push([name, v => layout(name, v)]);
  for (const [shape, body] of Object.entries(shapes)) heads.push([shape, body]);
  const cases: { label: string; input: string; secrets: string[]; value: string }[] = [];
  let n = 0;
  for (const [name, body] of heads) for (const esc of ["u", ...Object.keys(seps)]) for (let cut = 1; cut < name.length; cut++) for (const before of [3, 6]) for (const spelling of ["decoded", "raw"]) {
    const value = "PLNT" + (100000 + n++).toString(16) + "QzWx7Rk2Vb9TyLm3";
    const context = "note: detail " + (esc === "u" ? " " : seps[esc]);
    const decoded = context + body(value) + " ; end", at = decoded.indexOf(name, context.length - 1), sep = context.length - 1;
    const escaped = new Set([esc === "u" ? at + (cut - 1) % name.length : sep]);
    const input = "\"" + spell(decoded, escaped) + "\"", start = at - before, registered = decoded.slice(start, at + cut);
    const secret = spelling === "decoded" ? registered : spell(registered, new Set([...escaped].filter(i => i >= start && i < at + cut).map(i => i - start)));
    cases.push({ label: `${name} / ${esc} / cut ${cut} / ${before} before / ${spelling}`, input, secrets: [secret], value: name === "AKIA" ? value.toUpperCase().slice(0, 16) : value });
  }

  test("scrubAttribute shows no credential value window that scrubValue hides", () => {
    expect(cases.length).toBe(4872);
    const failures = cases.filter(c => { const hidden = windowsShown(scrubValue(c.input, c.secrets), c.value); return windowsShown(scrubAttribute(c.input, c.secrets), c.value).some(w => !hidden.includes(w)); }).map(c => c.label);
    expect(failures).toEqual([]);
  });

  test("every scrubAttribute output is one JSON string", () => {
    expect(cases.filter(c => !isJSONString(scrubAttribute(c.input, c.secrets))).map(c => c.label)).toEqual([]);
  });
});

describe("a quoted-root attribute stays one JSON string", () => {
  test("lens A rows whose output took a quote of the root or a part of an escape", () => {
    expect(ROWS.json.length).toBe(40);
    expect(ROWS.json.filter(row => !isJSONString(scrubAttribute(row.input, row.registered))).map(row => `${row.from} ${row.id}`)).toEqual([]);
  });

  test("a registered value that runs over the closing quote: the quote and the whitespace after it are kept", () => {
    const input = " \"diagnostic \\\"password = PLNTs8VbQ2LmWx7Rk\\\" ; end\" \n";
    const out = scrubAttribute(input, ["Q2LmWx7Rk\\\" ; end\" "]);
    expect(isJSONString(out)).toBe(true);
    expect(windowsShown(out, "PLNTs8VbQ2LmWx7Rk")).toEqual([]);
  });

  test("a registered value that ends inside an escape hides the escape whole", () => {
    const input = "\"note PLNTq3Vb8LmWx7Rk2\\u00e9 \\u0074oken = PLNTa9Ck2Rv7Lm3Wx8Qz\"";
    const out = scrubAttribute(input, ["PLNTq3Vb8LmWx7Rk2\\u00", "n = PLNTa9Ck2Rv"]);
    expect(isJSONString(out)).toBe(true);
    expect(windowsShown(out, "PLNTq3Vb8LmWx7Rk2")).toEqual([]);
    expect(windowsShown(out, "PLNTa9Ck2Rv7Lm3Wx8Qz")).toEqual([]);
  });
});

describe("a pair whose value is written with escaped quotes, after a registered value spelled with the decoded tab", () => {
  // As harness r4r review/lensA/repro_cases.py: the read of the raw bytes with no registered value writes the pair's value as
  // '<redacted>' with the quotes it replaces; it is still joined, so the value is hidden as scrubValue and both References hide it.
  const cases: { label: string; input: string; secrets: string[] }[] = [];
  for (const key of ["access_token", "password", "secret", "token", "pwd"]) for (let cut = 0; cut < key.length; cut++) for (const sep of ["\\t:\\t", ":\\t", "\\t:", ":", " = "]) for (const lead of ["", "note ", "note\\/ "]) for (const tail of ["", ";x"]) {
    const registered = JSON.parse("\"" + key.slice(cut) + "'" + (sep.startsWith("\\t") ? "\\t" : "") + "\"");
    if (registered.length < 8) continue;
    cases.push({ label: `${key} / ${cut} / ${JSON.stringify(sep)} / ${JSON.stringify(lead)} / ${JSON.stringify(tail)}`, input: "\"" + lead + "'" + key + "'" + sep + "\\\"PLNTABCD\\\"" + tail + "\"", secrets: [registered] });
  }
  test("scrubAttribute shows no window of the value that scrubValue hides, and stays one JSON string", () => {
    expect(cases.length).toBe(276);
    const failures = cases.filter(c => { const hidden = windowsShown(scrubValue(c.input, c.secrets), "PLNTABCD"); return !isJSONString(scrubAttribute(c.input, c.secrets)) || windowsShown(scrubAttribute(c.input, c.secrets), "PLNTABCD").some(w => !hidden.includes(w)); }).map(c => c.label);
    expect(failures).toEqual([]);
  });
  test("one row: the access_token pair after a tab", () => {
    const input = "\"'access_token'\\t:\\t\\\"PLNTABCD\\\"\"";
    expect(windowsShown(scrubAttribute(input, ["access_token'\t"]), "PLNTABCD")).toEqual([]);
    expect(isJSONString(scrubAttribute(input, ["access_token'\t"]))).toBe(true);
  });
});

describe("a quoted root whose added read runs out of a budget", () => {
  // A registered value that is the whole string as it decodes, spelled with an escape in the raw bytes; the read with no registered value
  // would read 10,000 JSON objects. The attribute alone fails closed as the quoted placeholder, as before; no error is thrown.
  test("10,000 JSON objects inside the string, the registered value the whole decoded string", () => {
    const text = "PLNTBudget " + " {\"note\":\"PLNTbudgetpayload\"} | ".repeat(10000), input = JSON.stringify(text).replace("P", "\\u0050");
    expect(scrubAttribute(input, [text])).toBe("\"<redacted>\"");
  });
});
