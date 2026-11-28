import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubText, scrubValue } from "../privacy-runtime";

// Known secrets that cross a JSON owner, a name glued to a bare base64 run, and the name path budget on every path. Each expected
// text is what Reference.scrub (spec/auto-instrumentation/generate_privacy_fixtures.py) gives for the same input. Test values are
// placeholders by shape: a credential value is TAILV followed by hex digits.
const none = indexPrivacyHints([]);
const framedValue = (value: unknown, secrets: string[] = []) => scrubValue(value, secrets, "", 0, false, undefined, none, true);
const proseValue = (value: unknown, secrets: string[] = []) => scrubValue(value, secrets, "", 0, false, undefined, none, false);
const markers = (text: unknown) => new Set(String(text).match(/TAILV[0-9A-F]+/g) ?? []);

// [input, known secrets, framed site, expected (the reference's output)]
const CROSSING: [string, string[], boolean, string][] = [
  ["{\"pwd\":\"TAILV4B524F977\"} tail pwd=TAILV4B524F977", ["ILV4B524F977\"} tail ", "ILV4B524F977\"} tail pwd", "pwd=TAILV4B524F977"], false, "{\"pwd\":\"<redacted>\""],
  ["{\"API_KEY\":\"TAILV06B1E39\"} tail API_KEY=TAILV06B1E39", ["6B1E39\"} tail API_KE"], true, "{\"API_KEY\":\"<redacted>\""],
  ["{\"--Authorization\":\"TAILV6A44BB7B9\"} tail --Authorization=TAILV6A44BB7B9", ["9\"} tail --Authorization"], false, "{\"--Authorization\":\"<redacted>\""],
  ["{\"password\":\"TAILV2776B0A470E\"} tail password=TAILV2776B0A470E", ["V2776B0A470E", "0A470E\"} tail password=", "=TAILV2776B0A47"], false, "{\"password\":\"<redacted>\""],
  ["{\"credential\":\"TAILV62E73774BC\"} tail credential=TAILV62E73774BC", ["V62E73774B", "3774BC\"} tail credentia"], false, "{\"credential\":\"<redacted>\""],
  ["{\"pass\":\"TAILVAFF9F\"} tail pass=TAILVAFF9F", ["l pass=T", "\"TAILVAFF9F\"} tail pas", "ss\":\"TAILVAFF"], false, "{\"pass\":\"<redacted>\""],
  ["{\"--SESSION_ID\":\"TAILV1E9CD\"} tail --SESSION_ID=TAILV1E9CD", ["CD\"} tail --SESSION_I"], true, "{\"--SESSION_ID\":\"<redacted>\""],
  ["{\"_SET-COOKIE\":\"TAILV9C56B0D1FB\"} tail _SET-COOKIE=TAILV9C56B0D1FB", ["OOKIE\":\"TAILV9C56B0D1FB", "1FB\"} tail _SET-COOKIE="], true, "{\"_SET-COOKIE\":\"<redacted>\""],
  ["{\"Cookie\":\"TAILVD80\"} tail Cookie=TAILVD80", ["TAILVD80\"} tail Cookie=", "tail Coo"], false, "{\"Cookie\":\"<redacted>\""],
  ["{\"--pin\":\"TAILV64F8ACED0A\"} tail --pin=TAILV64F8ACED0A", ["CED0A\"} tail --pin"], true, "{\"--pin\":\"<redacted>\""],
  ["{\"secret\":\"TAILVAC4974\"} tail secret=TAILVAC4974", ["974\"} tai", "4\"} tail sec", "AILVAC4974\"} tail sec"], true, "{\"secret\":\"<redacted>\""],
  ["{\".AUTHORIZATION\":\"TAILV0D315F5\"} tail .AUTHORIZATION=TAILV0D315F5", ["\"TAILV0D315F5\"} tail .A", "5\"} tail .AU"], false, "{\".AUTHORIZATION\":\"<redacted>\""],
  ["{\"password\":\"TAILVAF16\"} tail password=TAILVAF16", ["LVAF16\"} tail pa"], false, "{\"password\":\"<redacted>\""],
  ["{\"PRIVATE_KEY\":\"TAILVAC0\"} tail PRIVATE_KEY=TAILVAC0", ["\"PRIVATE_KEY\":\"TAILVA"], true, "{<redacted>C0\"} tail PRIVATE_KEY=<redacted>"],
  ["{\"Set-Cookie\":\"TAILV3AD3E\"} tail Set-Cookie=TAILV3AD3E", ["okie\":\"TAILV3AD3E\"} ", "V3AD3E\"} tail Set-Co"], false, "{\"Set-Cookie\":\"<redacted>\""],
  ["{\"Authorization\":\"TAILV76721E7\"} tail Authorization=TAILV76721E7", ["\"TAILV76721E7\"} t", "7\"} tail Authoriza"], true, "{\"Authorization\":\"<redacted>\""],
  ["{\"CLIENT_SECRET\":\"TAILVF023\"} tail CLIENT_SECRET=TAILVF023", ["\"} tail CLIENT_SECR"], false, "{\"CLIENT_SECRET\":\"<redacted>\""],
  ["{\"secret\":\"TAILVAC4974\"} tail secret=TAILVAC4974", ["974\"} tai", "4\"} tail sec", "AILVAC4974\"} tail sec"], false, "{\"secret\":\"<redacted>\""],
  ["{\"session_id\":\"TAILV7BFF4\"} tail session_id=TAILV7BFF4", ["V7BFF4\"} tail se"], true, "{\"session_id\":\"<redacted>\""],
  ["{\"access_token\":\"TAILVBE03\"} tail access_token=TAILVBE03", ["E03\"} tail access_", "VBE03\"} tail access_to", "token=TAILVBE03"], true, "{\"access_token\":\"<redacted>\""],
  ["{\".CLIENT_SECRET\":\"TAILVCB4A\"} tail .CLIENT_SECRET=TAILVCB4A", ["A\"} tail .CLIENT_SECRET"], true, "{\".CLIENT_SECRET\":\"<redacted>\""],
  ["{\"passwd\":\"TAILV25EAF3B010D\"} tail passwd=TAILV25EAF3B010D", ["V25EAF3B010D\"} tail p"], false, "{\"passwd\":\"<redacted>\""],
  ["{\"PRIVATE_KEY\":\"TAILV18581900\"} tail PRIVATE_KEY=TAILV18581900", ["TAILV18581900", "RIVATE_KEY\":\"TAILV185"], false, "{\"P<redacted>81900\"} tail PRIVATE_KEY=<redacted>"],
  ["{\"apikey\":\"TAILVFBBA3D\"} tail apikey=TAILVFBBA3D", ["VFBBA3D\"} tail apikey="], true, "{\"apikey\":\"<redacted>\""],
  ["{\"_SET-COOKIE\":\"TAILV9C56B0D1FB\"} tail _SET-COOKIE=TAILV9C56B0D1FB", ["OOKIE\":\"TAILV9C56B0D1FB", "1FB\"} tail _SET-COOKIE="], false, "{\"_SET-COOKIE\":\"<redacted>\""],
  ["{\"Cookie\":\"TAILV783A9D6ED\"} tail Cookie=TAILV783A9D6ED", ["83A9D6ED\"} tail Cook", "} tail Cooki", "l Cookie=TAILV783"], true, "{\"Cookie\":\"<redacted>\""],
  ["{\"passphrase\":\"TAILV20B\"} tail passphrase=TAILV20B", ["20B\"} tail passphrase=", " tail passp"], false, "{\"passphrase\":\"<redacted>\""],
  ["{\"pass\":\"TAILV82D1FB\"} tail pass=TAILV82D1FB", ["82D1FB\"} tail pass="], true, "{\"pass\":\"<redacted>\""],
  ["{\"client_secret\":\"TAILVEFDDA20\"} tail client_secret=TAILVEFDDA20", ["DA20\"} tail client_sec", "DA20\"} tail client_secr"], false, "{\"client_secret\":\"<redacted>\""],
  ["{\"-token\":\"TAILVFFE69494743\"} tail -token=TAILVFFE69494743", ["\"} tail -token"], true, "{\"-token\":\"<redacted>\""],
  ["{\"PRIVATE_KEY\":\"TAILV18581900\"} tail PRIVATE_KEY=TAILV18581900", ["TAILV18581900", "RIVATE_KEY\":\"TAILV185"], true, "{\"P<redacted>81900\"} tail PRIVATE_KEY=<redacted>"],
  ["{\"passwd\":\"TAILV25EAF3B010D\"} tail passwd=TAILV25EAF3B010D", ["V25EAF3B010D\"} tail p"], true, "{\"passwd\":\"<redacted>\""],
  ["{\"passphrase\":\"TAILVAF39\"} tail passphrase=TAILVAF39", ["VAF39\"} tail passph", "\"TAILVAF39\"} "], false, "{\"passphrase\":\"<redacted>\""],
  ["{\"Set-Cookie\":\"TAILVB1F59\"} tail Set-Cookie=TAILVB1F59", ["\"} tail Set-Cook", "t-Cookie=TAILV", "kie=TAILVB1F"], true, "{\"Set-Cookie\":\"<redacted>\""],
  ["{\"passphrase\":\"TAILVB788\"} tail passphrase=TAILVB788", ["ase\":\"TAILVB", "B788\"} tail passphr"], true, "{\"passphrase\":\"<redacted>\""],
  ["{\"PRIVATE_KEY\":\"TAILVB75C715\"} tail PRIVATE_KEY=TAILVB75C715", ["ATE_KEY\":\"TA"], true, "{\"PRIV<redacted>ILVB75C715\"} tail PRIVATE_KEY=<redacted>"],
  ["{\"secret\":\"TAILV40A17A\"} tail secret=TAILV40A17A", ["V40A17A\"} tail s", "40A17A\"} ta"], false, "{\"secret\":\"<redacted>\""],
  ["{\"passphrase\":\"TAILV8DC2F0\"} tail passphrase=TAILV8DC2F0", ["se\":\"TAI", "\"TAILV8DC2F0\"} tail pa"], false, "{\"passphrase\":\"<redacted>\""],
  ["{\"pass\":\"TAILVC5929993E\"} tail pass=TAILVC5929993E", ["VC5929993E", "ILVC5929993E\"} tail pass"], false, "{\"pass\":\"<redacted>\""],
  ["{\"apikey\":\"TAILVD57108FE03CD\"} tail apikey=TAILVD57108FE03CD", ["08FE03CD\"} tail apik", "il apikey=TAILVD"], false, "{\"apikey\":\"<redacted>\""],
  ["{\"-Set-Cookie\":\"TAILV5814451\"} tail -Set-Cookie=TAILV5814451", ["4451\"} ta", "l -Set-C"], false, "{\"-Set-Cookie\":\"<redacted>\""],
  ["{\"session_id\":\"TAILV2A3A28A22B\"} tail session_id=TAILV2A3A28A22B", ["} tail sess", "2B\"} tail sess"], false, "{\"session_id\":\"<redacted>\""],
  ["{\"secret\":\"TAILV40A17A\"} tail secret=TAILV40A17A", ["V40A17A\"} tail s", "40A17A\"} ta"], true, "{\"secret\":\"<redacted>\""],
  ["{\"Authorization\":\"TAILV698FF4\"} tail Authorization=TAILV698FF4", [" tail Authorization", "on\":\"TAILV698FF4\"} ta", "TAILV698FF4\"} tail Auth"], true, "{\"Authorization\":\"<redacted>\""],
  ["{\"PASS\":\"TAILV0EC8F\"} tail PASS=TAILV0EC8F", ["LV0EC8F\"} tail PA"], true, "{\"PASS\":\"<redacted>\""],
  ["{\"PWD\":\"TAILV12950\"} tail PWD=TAILV12950", ["il PWD=TAILV12950", "ILV12950\"} tail P", "l PWD=TAILV1295"], true, "{\"PWD\":\"<redacted>\""],
  ["{\"pin\":\"TAILVB96F5D9C0\"} tail pin=TAILVB96F5D9C0", ["9C0\"} tail p"], false, "{\"pin\":\"<redacted>\""],
  ["{\"password\":\"TAILVAF16\"} tail password=TAILVAF16", ["LVAF16\"} tail pa"], true, "{\"password\":\"<redacted>\""],
  ["{\"password\":\"TAILV824\"} tail password=TAILV824", ["l passwor", "\"} tail p"], false, "{\"password\":\"<redacted>\""],
  ["{\"client_secret\":\"TAILVEFDDA20\"} tail client_secret=TAILVEFDDA20", ["DA20\"} tail client_sec", "DA20\"} tail client_secr"], true, "{\"client_secret\":\"<redacted>\""],
  ["{\"pwd\":\"TAILV38180D27\"} tail pwd=TAILV38180D27", ["0D27\"} tail ", "180D27\"} tail pwd="], true, "{\"pwd\":\"<redacted>\""],
  ["{\"PRIVATE_KEY\":\"TAILVAC0\"} tail PRIVATE_KEY=TAILVAC0", ["\"PRIVATE_KEY\":\"TAILVA"], false, "{<redacted>C0\"} tail PRIVATE_KEY=<redacted>"],
  ["{\"pwd\":\"TAILVB215\"} tail pwd=TAILVB215", ["15\"} tail pwd="], false, "{\"pwd\":\"<redacted>\""],
  ["{\"token\":\"TAILV68F86\"} tail token=TAILV68F86", ["token=TAILV68F86", "V68F86\"} tail token=", "\"} tail token="], true, "{\"token\":\"<redacted>\""],
  ["{\"Cookie\":\"TAILV9E20B50\"} tail Cookie=TAILV9E20B50", ["50\"} tail Cookie=TAIL", "AILV9E20B50\"} tail Coo", " tail Cookie=TAILV"], false, "{\"Cookie\":\"<redacted>\""],
  ["{\"session_id\":\"TAILV8BECCC\"} tail session_id=TAILV8BECCC", ["C\"} tail sessi"], false, "{\"session_id\":\"<redacted>\""],
  ["{\"passphrase\":\"TAILV8DC2F0\"} tail passphrase=TAILV8DC2F0", ["se\":\"TAI", "\"TAILV8DC2F0\"} tail pa"], true, "{\"passphrase\":\"<redacted>\""],
  ["{\"x.AUTHORIZATION\":\"TAILV595\"} tail x.AUTHORIZATION=TAILV595", ["ORIZATION=TAILV595", "RIZATION=T", "V595\"} tail x.AUTHORI"], false, "{\"x.AUTHORIZATION\":\"<redacted>\""],
  ["{\"token\":\"TAILVE2EE1\"} tail token=TAILVE2EE1", [":\"TAILVE2EE1\"} tai", "\"TAILVE2EE1\"} tail toke", "\":\"TAILVE2EE1\"} tail "], false, "{\"token\":\"<redacted>\""],
  ["{\"APIKEY\":\"TAILV4B20D2F6A8\"} tail APIKEY=TAILV4B20D2F6A8", ["20D2F6A8\"} tail APIK", "LV4B20D2F6", "4B20D2F6A8"], true, "{\"APIKEY\":\"<redacted>\""],
];
describe("a known secret that crosses a JSON owner never hides a later credential pair", () => {
  test("the passphrase repro gives the reference's text", () => {
    const secrets = ['se":"TAI', '"TAILV8DC2F0"} tail pa'];
    for (const framed of [false, true]) {
      const out = (framed ? framedValue : proseValue)('{"passphrase":"TAILV8DC2F0"} tail passphrase=TAILV8DC2F0', secrets) as string;
      expect(markers(out).size).toBe(0);
    }
  });
  test("a seeded family: no credential value is readable that the reference redacts", () => {
    const readable: string[] = [];
    for (const [value, secrets, framed, expected] of CROSSING) {
      const out = (framed ? framedValue : proseValue)(value, secrets);
      const allowed = markers(expected);
      for (const marker of markers(out)) if (!allowed.has(marker)) readable.push(`${JSON.stringify(value)} ${JSON.stringify(secrets)} ${framed ? "framed" : "prose"}: ${marker} readable in ${JSON.stringify(out)}`);
      if (!framed) expect(markers(scrubText(value, secrets)).size).toBeLessThanOrEqual(markers(expected).size);
    }
    expect(readable).toEqual([]);
  });
});

// [input, framed site, expected]
const GLUED: [string, boolean, string][] = [
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret = TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret = TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret=TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret=TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret: TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret: <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret: TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTclient_secret: <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword = TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword = TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword=TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword=TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword: TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword: <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword: TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTpassword: <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key = TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key = TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key=TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key=TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key: TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key: <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key: TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTapi_key: <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken = TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken = TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken = <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken=TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken=TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken=<redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken: TAILV9EB07B1 then more.", false, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken: <redacted>"],
  ["/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken: TAILV9EB07B1 then more.", true, "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRTtoken: <redacted>"],
];
describe("a name glued to a bare base64 run is read before the run", () => {
  test("the value of the glued pair is replaced", () => {
    const readable: string[] = [];
    for (const [value, framed, expected] of GLUED) {
      const out = (framed ? framedValue : proseValue)(value) as string;
      if (markers(out).size) readable.push(`${framed ? "framed" : "prose"}: ${JSON.stringify(out)} (reference ${JSON.stringify(expected)})`);
      expect(out).toContain("<redacted>");
    }
    expect(readable).toEqual([]);
  });
});

describe("the name path budget is read before the depth budget on every path", () => {
  const object = (levels: number, leaf: unknown): unknown => { let value = leaf; for (let i = 0; i < levels; i++) value = { a: value }; return value; };
  const jsonText = (levels: number, leaf = '"x"') => '{"a":'.repeat(levels) + leaf + "}".repeat(levels);
  const longKey = Array.from({ length: 33 }, () => "b").join(".");
  test.each([32, 33, 34, 40])("a %i-level JSON text under a key of 33 components exports the placeholder", levels => {
    const text = `{"${longKey}":${jsonText(levels)}}`;
    for (const out of [framedValue(text), proseValue(text), scrubText(text), framedValue({ k: text }), proseValue({ k: text })]) expect(out).toBe("<redacted>");
  });
  test.each([framedValue, proseValue])("a string leaf at depth 32 is read, not refused", read => {
    expect(read(object(32, "plain text here"))).toEqual(object(32, "plain text here"));
    expect(read(object(32, "see https://h.example/p?a=b next"))).toEqual(object(32, "see https://h.example/p?a=b next"));
  });
  test("a string leaf below depth 32 is still refused by the depth budget", () => {
    expect(() => scrubValue("plain text here", [], "", 33, false, undefined, none, true)).toThrow(/Privacy structure budget/);
  });
});

describe("a known secret inside embedded JSON or brackets is still replaced", () => {
  test.each([
    ['x "k": "s3cr3t_k token: TAILV876X"', ["s3cr3t_k"], "s3cr3t_k"],
    ['upstream said "detail": "key s3cr3t_k rejected, password=TAILV876X"', ["s3cr3t_k"], "s3cr3t_k"],
    ['x {"PWD":"TAILV1", "note":"SECRETVALUE9"}', ['"PWD":"TAILV1", "note":"SECRETVALUE9'], "SECRETVALUE9"],
    ['upstream error: {"type":"service_account","private_key":"TAILVABCDEF12","client_email":"SECRETEMAIL9@x.iam"}',
      ['{"type":"service_account","private_key":"TAILVABCDEF12","client_email":"SECRETEMAIL9@x.iam"}'], "SECRETEMAIL9"],
    ["x { foobarxxpwd=TAILV1 } end", ["foobarxx"], "TAILV1"],
  ] as [string, string[], string][])("%s", (value, secrets, hidden) => {
    for (const read of [framedValue, proseValue]) expect(read(value, secrets) as string).not.toContain(hidden);
    expect(scrubText(value, secrets)).not.toContain(hidden);
  });
});

describe("a secret that crosses an owner stays linear in the number of pairs", () => {
  test.each([20_000, 40_000])("%i credential pairs after one crossing secret", pairs => {
    let text = 'abcdefg {"k":"v"}';
    for (let i = 0; i < pairs; i++) text += ` pwd=TAILV${i}`;
    const start = Date.now();
    const out = scrubText(text, ['defg {"k']);
    expect(Date.now() - start).toBeLessThan(5_000);
    expect(out).not.toContain("TAILV");
  });
});
