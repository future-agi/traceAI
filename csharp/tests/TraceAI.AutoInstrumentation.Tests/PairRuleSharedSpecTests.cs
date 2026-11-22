using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

// RED tests for the credential pair rule and the URL catalog extensions of the shared spec.
// Every expected value below was computed by the Python Reference (spec/auto-instrumentation/generate_privacy_fixtures.py)
// with Reference(catalog).scrub(value, path, None, (), framed=site != "free_text_urls"). Inputs are synthetic markers.
public class PairRuleSharedSpecTests : InitIsolation
{
    private const string Free = "free_text_urls";
    private const string Framed = "whole_url_site";
    private const string JsonPrefix = "@json:";

    // Same evaluation as PrivacyCatalogTests.Actual: objects and arrays through WirePrivacy.Scrub then JSON-parse, strings through ScrubString.
    private static void Check(string site, string path, string input, string expected)
    {
        bool framed = site != Free;
        if (input.StartsWith(JsonPrefix, StringComparison.Ordinal))
        {
            var node = JsonNode.Parse(input.Substring(JsonPrefix.Length))!;
            string clean = (string)WirePrivacy.Scrub(node, [], path, null, framed);
            JsonNode? actual = clean.StartsWith('<') ? JsonValue.Create(clean) : JsonNode.Parse(clean);
            JsonNode? want = expected.StartsWith(JsonPrefix, StringComparison.Ordinal) ? JsonNode.Parse(expected.Substring(JsonPrefix.Length)) : JsonValue.Create(expected);
            Assert.True(JsonNode.DeepEquals(want, actual), "expected " + want?.ToJsonString() + " actual " + actual?.ToJsonString());
            return;
        }
        Assert.Equal(expected, WirePrivacy.ScrubString(input, [], path, null, framed));
    }


    [Theory]
    [InlineData(Framed, "out.text", "note password=TAILV rest of the line here", "note password=<redacted>")]
    [InlineData(Free, "out.text", "run --password TAILV more words after", "run --password <redacted> more words after")]
    [InlineData(Framed, "out.text", "call -password TAILV and then TAILX", "call -password <redacted> and then TAILX")]
    [InlineData(Free, "out.text", "say \"password\": TAILV tail words", "say \"password\": \"<redacted>\"")]
    [InlineData(Framed, "out.text", "login: user pass: TAILV, then TAILX; ok", "login: user pass: <redacted>")]
    [InlineData(Free, "out.text", "secret=TAILV and TAILX; other=1", "secret=<redacted>; other=1")]
    [InlineData(Framed, "out.text", "{\"n\":\"x password=TAILV tail\",\"m\":\"{\\\"k\\\":1,\\\"token\\\":\\\"TAILV\\\"}\"}", "{\"n\":\"x password=<redacted>\",\"m\":\"{\\\"k\\\":1,\\\"token\\\":\\\"<redacted>\\\"}\"}")]
    [InlineData(Framed, "out.text", "api_key: TAILV two words, three", "api_key: <redacted>")]
    [InlineData(Framed, "out", "@json:{\"a\": \"x password=TAILV tail\", \"b\": \"{\\\"n\\\": 1, \\\"token\\\": \\\"TAILV\\\"}\", \"c\": \"plain\"}", "@json:{\"a\": \"x password=<redacted>\", \"b\": \"{\\\"n\\\":1,\\\"token\\\":\\\"<redacted>\\\"}\", \"c\": \"plain\"}")]
    [InlineData(Free, "out", "@json:{\"msg\": \"api_key: TAILV z\", \"keep\": [\"one password=TAILX end\", 2]}", "@json:{\"msg\": \"api_key: <redacted>\", \"keep\": [\"one password=<redacted>\", 2]}")]
    public void PairRuleGroup1_CredentialPairValueRunsToTrueEnd(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "http.url", "https://h.invalid/a/note password=TAILV more/b?x=1", "https://h.invalid/a/note password=<redacted>")]
    [InlineData(Framed, "http.url", "https://h.invalid/p#the password=TAILV tail words", "https://h.invalid/p#the password=<redacted>")]
    [InlineData(Framed, "http.url", "https://h.invalid/?token=TAILV#password=TAILX tail", "https://h.invalid/?token=<redacted>")]
    [InlineData(Framed, "http.url", "https://h.invalid/p q?x=1#note token=TAILV more", "https://h.invalid/p q?x=1#note token=<redacted>")]
    [InlineData(Framed, "http.url", "http://h.invalid/a b password=TAILV c/d", "http://h.invalid/a b password=<redacted>")]
    [InlineData(Framed, "http.url", "https://h.invalid/?q=see password=TAILV here&n=1", "https://h.invalid/?q=see password=<redacted>")]
    [InlineData(Framed, "http.url", "/rel/path note secret=TAILV rest?x=1", "/rel/path note secret=<redacted>")]
    public void PairRuleGroup2_WholeUrlSkipsOnlyCatalogSpans(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "http.url", "https://h.invalid/p?a=1#token=TAILV&b=2", "https://h.invalid/p?a=1#token=<redacted>&b=2")]
    [InlineData(Framed, "http.url", "https://h.invalid/p?a=1?token=TAILV&b=2", "https://h.invalid/p?a=1?token=<redacted>&b=2")]
    [InlineData(Framed, "http.url", "https://h.invalid/password=TAILV/x", "https://h.invalid/password=<redacted>/x")]
    [InlineData(Framed, "http.url", "https://h.invalid/a/token%3DTAILV/x", "https://h.invalid/a/token%3D<redacted>/x")]
    [InlineData(Free, "http.url", "see https://h.invalid/p#password=TAILV now", "see https://h.invalid/p#password=<redacted>")]
    [InlineData(Free, "http.url", "see https://h.invalid/p?a=1?secret=TAILV&b=2 now", "see https://h.invalid/p?a=1?secret=<redacted>")]
    [InlineData(Free, "http.url", "visit https://h.invalid/a/password=TAILV/b ok", "visit https://h.invalid/a/password=<redacted>")]
    [InlineData(Framed, "http.url", "https://h.invalid/p?a=1#x=2&password=TAILV", "https://h.invalid/p?a=1#x=2&password=<redacted>")]
    public void PairRuleGroup3_UrlFragmentSecondQuestionAndPathPairs(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "http.url", "https://h.invalid/?dbPass=TAILV&a=1", "https://h.invalid/?dbPass=<redacted>&a=1")]
    [InlineData(Framed, "http.url", "https://h.invalid/p#db.pwd=TAILV&a=1", "https://h.invalid/p#db.pwd=<redacted>&a=1")]
    [InlineData(Framed, "http.url", "https://h.invalid/user-pin=TAILV/x", "https://h.invalid/user-pin=<redacted>/x")]
    [InlineData(Framed, "headers.X-Pin", "TAILV", "<redacted>")]
    [InlineData(Free, "out.text", "x passcode=TAILV y", "x passcode=<redacted>")]
    [InlineData(Free, "out.text", "a dbPass=TAILV b", "a dbPass=<redacted>")]
    [InlineData(Framed, "out", "@json:{\"dbPass\": \"TAILV\", \"user-pin\": \"TAILX\", \"keep\": \"k\"}", "@json:{\"dbPass\": \"<redacted>\", \"user-pin\": \"<redacted>\", \"keep\": \"k\"}")]
    [InlineData(Framed, "out.text", "note passphrase=TAILV more", "note passphrase=<redacted>")]
    [InlineData(Framed, "out.text", "x passwd: TAILV z", "x passwd: <redacted>")]
    [InlineData(Free, "out.text", "use X-Pin=TAILV now", "use X-Pin=<redacted>")]
    public void PairRuleGroup4_VocabularyWordsAtEverySite(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out.text", "say \"password\": \"TAILV x\" rest", "say \"password\": \"<redacted>\" rest")]
    [InlineData(Framed, "out.text", "say password: 'TAILV x' rest here", "say password: <redacted>")]
    [InlineData(Free, "out.text", "say \\\"password\\\": \\\"TAILV x\\\" rest", "say \\\"password\\\": \\\"<redacted>\\\" rest")]
    [InlineData(Free, "out.text", "say &quot;password&quot;: &quot;TAILV x&quot; rest", "say &quot;password&quot;: &quot;<redacted>&quot; rest")]
    [InlineData(Framed, "out.text", "x \"token\": 12345, y", "x \"token\": \"<redacted>\", y")]
    [InlineData(Framed, "out.text", "x \"token\": true} y", "x \"token\": \"<redacted>\"} y")]
    [InlineData(Framed, "out.text", "x \"token\": null] y", "x \"token\": \"<redacted>\"] y")]
    [InlineData(Framed, "out.text", "x \"token\": {\"a\":1, \"b\":2} tail words", "x \"token\": \"<redacted>\"")]
    [InlineData(Framed, "out.text", "x \"token\": [1, 2] tail words", "x \"token\": \"<redacted>\"")]
    public void PairRuleGroup5_JsonQuotedNameValueEnds(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out", "@json:{\"p secret: TAILV z\": \"1\", \"q\": \"2\"}", "@json:{\"p secret: <redacted>\": \"<redacted>\", \"q\": \"2\"}")]
    [InlineData(Framed, "out", "@json:{\"x password=TAILV\": \"v\", \"keep\": \"k\"}", "@json:{\"x password=<redacted>\": \"<redacted>\", \"keep\": \"k\"}")]
    [InlineData(Framed, "out", "@json:{\"a password=TAILV\": \"1\", \"a password=TAILX\": \"2\", \"b\": \"3\"}", "@json:{\"a password=<redacted>\": \"<redacted>\", \"b\": \"3\"}")]
    [InlineData(Framed, "out", "@json:{\"k https://h.invalid/?secret=TAILV#f\": \"1\", \"k https://h.invalid/?secret=TAILX#f\": \"2\"}", "@json:{\"k https://h.invalid/?secret=<redacted>\": \"<redacted>\"}")]
    [InlineData(Free, "out", "@json:{\"n token=TAILV\": {\"in\": \"x\"}, \"n token=TAILX\": \"y\"}", "@json:{\"n token=<redacted>\": \"<redacted>\"}")]
    public void PairRuleGroup6_MapKeysAndCollisions(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out.text", "x a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.password=TAILV tail", "<redacted>")]
    [InlineData(Free, "out.text", "x a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.token=TAILV tail", "<redacted>")]
    [InlineData(Framed, "a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.note", "TAILV", "<redacted>")]
    [InlineData(Framed, "a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.note", "plain TAILV text", "<redacted>")]
    [InlineData(Free, "a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.x", "password=TAILV", "<redacted>")]
    public void PairRuleGroup7_DepthBudgetExportsPlaceholder(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out.text", "x nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn=TAILV tail", "x nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn=<redacted>")]
    [InlineData(Free, "out.text", "x nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn=TAILV tail words", "x nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn=<redacted>")]
    [InlineData(Framed, "out.text", "x wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww=TAILV z", "x wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww=<redacted>")]
    [InlineData(Framed, "out.text", "https://h.invalid/p?a?b?c?d?e?f?g?h?i?j=TAILV&k=1", "https://h.invalid/p?a?b?c?d?e?f?g?h?i?j=<redacted>&k=1")]
    [InlineData(Free, "out.text", "see https://h.invalid/p?a?b?c?d?e?f?g?h?i?j=TAILV&k=1 now", "see https://h.invalid/p?a?b?c?d?e?f?g?h?i?j=<redacted>&k=1 now")]
    [InlineData(Framed, "out.text", "https://h.invalid/p??????????x=TAILV&k=1", "https://h.invalid/p??????????x=<redacted>&k=1")]
    public void PairRuleGroup8_BoundedNamesFailClosed(string site, string path, string input, string expected) => Check(site, path, input, expected);

    private static string Repeat(string prefix, string unit, int length)
    {
        var sb = new StringBuilder(prefix, length + unit.Length);
        while (sb.Length < length) sb.Append(unit);
        return sb.ToString(0, length);
    }
    private static string Adversarial(string kind) => kind switch
    {
        "question_marks" => Repeat("", "?", 1_600_000),
        "url_question_chain" => Repeat("http://h.invalid/p?", "a?b?c?d?e?f?g?h?i?j=", 1_600_000),
        "long_names" => Repeat("x ", new string('n', 200) + "=", 1_600_000),
        "many_pairs" => Repeat("x ", "password=TAILV ", 1_600_000),
        "query_pairs" => Repeat("http://h.invalid/p?", "a=b&token=TAILV&", 1_600_000),
        "long_query_pairs" => Repeat("http://h.invalid/p?", "a=" + new string('v', 185) + "&token=TAILV&", 1_600_000),
        _ => throw new ArgumentException(kind),
    };

    // 1.6M characters of adversarial input must finish within 10 seconds and equal the Python reference output (SHA-256 prefix, 16 hex).
    [Theory]
    [InlineData("question_marks", Free, "9e6fbc86f1fb4d00")]
    [InlineData("question_marks", Framed, "9e6fbc86f1fb4d00")]
    [InlineData("url_question_chain", Free, "23dcc224e6ef9997")]
    [InlineData("url_question_chain", Framed, "23dcc224e6ef9997")]
    [InlineData("long_names", Free, "2f4768b5b1796b11")]
    [InlineData("long_names", Framed, "2f4768b5b1796b11")]
    [InlineData("many_pairs", Free, "092a1df4341050a7")]
    [InlineData("many_pairs", Framed, "092a1df4341050a7")]
    // 100,000 query pairs: more than limits.max_nodes (16,384), the query hint index budget of this SDK, so the whole value is the
    // placeholder (fail closed; the reference has no such budget for query pairs). Under the budget the output is the reference's.
    [InlineData("query_pairs", Free, "3a2d1afa44d10120")]
    [InlineData("query_pairs", Framed, "3a2d1afa44d10120")]
    [InlineData("long_query_pairs", Free, "491bba268a61d04e")]
    [InlineData("long_query_pairs", Framed, "b493501e4c31f83b")]
    public void PairRuleGroup9_LinearTimeOnAdversarialInput(string kind, string site, string sha256Prefix)
    {
        string input = Adversarial(kind);
        Assert.Equal(1_600_000, input.Length);
        var watch = Stopwatch.StartNew();
        string actual = WirePrivacy.ScrubString(input, [], "out.text", null, site != Free);
        watch.Stop();
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(10), $"{kind} took {watch.Elapsed.TotalSeconds:F1}s");
        string digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(actual))).ToLowerInvariant().Substring(0, 16);
        Assert.Equal(sha256Prefix, digest);
    }

    // User information, value shapes and magic bytes read as the reference reads them: whitespace includes U+001C-U+001F, the i
    // flag folds dotless i and long s, and padding already in a base64 prefix is accepted.
    [Theory]
    [InlineData(Framed, "x", "http://u:p@h/x", "http://<redacted>@h/x")]
    [InlineData(Framed, "x", "https://u:@h/x", "https://<redacted>@h/x")]
    [InlineData(Framed, "x", "https://:p@h/x", "https://<redacted>@h/x")]
    [InlineData(Framed, "x", "http://a:b:c@h", "http://<redacted>@h")]
    [InlineData(Free, "x", "HTTPS://u:p@h?x=1", "HTTPS://<redacted>@h?x=1")]
    [InlineData(Framed, "x", "http://a:@b@c", "http://<redacted>@c")]
    [InlineData(Framed, "x", "BEARER\u001cabcdefghTAILVTAILV", "<redacted>")]
    [InlineData(Free, "x", "Bearer abcdefgh\u0131TAILVTAIL99", "<redacted>")]
    [InlineData(Framed, "x", "Ba\u017Fic abcdefgh12345678", "<redacted>")]
    [InlineData(Framed, "x", "//QQ=", "<omitted: 5 base64 chars>")]
    [InlineData(Free, "x", "//AA==", "<omitted: 6 base64 chars>")]
    public void PairRuleGroup1_UserInformationShapesAndMagicBytesAsTheReferenceReadsThem(string site, string path, string input, string expected) => Check(site, path, input, expected);

    // A run of ':' after a scheme is read in linear time (no user information, the text is kept), and names after a '?' are each
    // read once per URL: a 1.6M-character chain of '?' names equals the reference output.
    [Fact]
    public void PairRuleGroup9_ColonRunsAndQuestionNameChainsStayLinear()
    {
        string colons = "http://" + new string(':', 400_000);
        var watch = Stopwatch.StartNew();
        Assert.Equal(colons, WirePrivacy.ScrubString(colons, [], "x", null, true));
        Assert.Equal("see " + colons, WirePrivacy.ScrubString("see " + colons, [], "x", null, false));
        string chain = Repeat("http://a/?x=", "????????a=", 1_600_012);
        string actual = WirePrivacy.ScrubString(chain, [], "x", null, true);
        watch.Stop();
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(10), $"took {watch.Elapsed.TotalSeconds:F1}s");
        Assert.Equal("c2eb75fbbf9d5277", Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(actual))).ToLowerInvariant().Substring(0, 16));
    }

    [Theory]
    [InlineData(Framed, "out.text", "x password   =TAILV rest", "x password   =<redacted>")]
    [InlineData(Free, "out.text", "x password : TAILV rest", "x password : <redacted>")]
    [InlineData(Framed, "out.text", "x token \t= TAILV rest here", "x token \t= <redacted>")]
    [InlineData(Free, "out.text", "x secret        : TAILV rest", "x secret        : <redacted>")]
    [InlineData(Framed, "out.text", "x password        =TAILV rest", "x password        =<redacted>")]
    public void PairRuleGroup10_GapsBetweenNameAndSeparator(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "http.url", "https://h.invalid/a/password%3DTAILV/b", "https://h.invalid/a/password%3D<redacted>/b")]
    [InlineData(Free, "http.url", "see https://h.invalid/a/token%3dTAILV/b ok", "see https://h.invalid/a/token%3d<redacted>")]
    [InlineData(Framed, "http.url", "https://h.invalid/a/x;password%3DTAILV;y=1/b", "https://h.invalid/a/x;password%3D<redacted>;y=1/b")]
    [InlineData(Framed, "http.url", "https://h.invalid/a/secret%3DTAILV", "https://h.invalid/a/secret%3D<redacted>")]
    public void PairRuleGroup11_PathSegmentEncodedEquals(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out.text", "x user%5Bpassword%5D=TAILV&n=1", "x user%5Bpassword%5D=<redacted>")]
    [InlineData(Free, "out.text", "x user[password]=TAILV tail", "x user[password]=<redacted>")]
    [InlineData(Free, "out.text", "x user%5Bsecret%5D=TAILV&n=1 tail", "x user%5Bsecret%5D=<redacted>")]
    [InlineData(Free, "out.text", "x user%5btoken%5d=TAILV tail", "x user%5btoken%5d=<redacted>")]
    [InlineData(Framed, "out.text", "x account%5Bcred%5D%5Bsecret%5D=TAILV tail", "x account%5Bcred%5D%5Bsecret%5D=<redacted>")]
    public void PairRuleGroup12_NameReadBackOfFormEncodedBracketNames(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out.text", "run --password\nTAILV more", "run --password\n<redacted> more")]
    [InlineData(Free, "out.text", "run --password    TAILV x", "run --password    <redacted> x")]
    [InlineData(Framed, "out.text", "run -password TAILV more", "run -password <redacted> more")]
    [InlineData(Free, "out.text", "run -secret\t TAILV words\nnext line", "run -secret\t <redacted> words\nnext line")]
    [InlineData(Framed, "out.text", "run --secret \t TAILV rest", "run --secret \t <redacted> rest")]
    [InlineData(Free, "out.text", "run -token TAILV more words", "run -token <redacted> more words")]
    public void PairRuleGroup13_DashNameGapsLineBreaksAndSingleDashFlags(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out.text", "x \"password\"=TAILV tail", "x \"password\"=<redacted>")]
    [InlineData(Free, "out.text", "x \"password\"%3DTAILV rest", "x \"password\"%3D<redacted>")]
    [InlineData(Framed, "out.text", "x 'token'=TAILV x", "x 'token'=<redacted>")]
    [InlineData(Free, "out.text", "x \"secret\" = TAILV rest", "x \"secret\" = <redacted>")]
    [InlineData(Framed, "out.text", "x \\\"password\\\"=TAILV tail", "x \\\"password\\\"=<redacted>")]
    public void PairRuleGroup14_QuotedNamesWithEqualsOrEncodedEquals(string site, string path, string input, string expected) => Check(site, path, input, expected);

    [Theory]
    [InlineData(Framed, "out.text", "x data:text/plain;base64,QUJD\npassword=TAILV rest", "x <omitted: 4 base64 chars>\npassword=<redacted>")]
    [InlineData(Free, "out.text", "data:text/plain;base64,QUJD\nQUJD\npassword: TAILV x", "<omitted: 9 base64 chars>\npassword: <redacted>")]
    [InlineData(Framed, "out.text", "x data:text/plain,abc\ntoken%3DTAILV y", "x <omitted: data URI>\ntoken%3D<redacted>")]
    [InlineData(Free, "out.text", "x data:text/plain;base64,QUJD\nsecret = TAILV end", "x <omitted: 4 base64 chars>\nsecret = <redacted>")]
    [InlineData(Framed, "out.text", "x data:text/plain;base64,QUJD\n--password TAILV end", "x <omitted: 4 base64 chars>\n--password <redacted> end")]
    public void PairRuleGroup15_DataUriContinuationLines(string site, string path, string input, string expected) => Check(site, path, input, expected);
}
