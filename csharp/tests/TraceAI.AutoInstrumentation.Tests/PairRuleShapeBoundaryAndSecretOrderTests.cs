using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>Value-shape word boundaries as the spec's reference reads them (Python's word characters), known secrets applied one
/// at a time before the pair rule, and JSON text that holds more than one value. Each expected text is the reference's output
/// (Reference.scrub in spec/auto-instrumentation/generate_privacy_fixtures.py) for the same input, or more redacted where noted.</summary>
public class PairRuleShapeBoundaryAndSecretOrderTests
{
    // A combining mark, a zero-width non-joiner, a zero-width joiner and connector punctuation are no word characters for the
    // reference, so each shape next to one of them starts or ends at a word boundary and is replaced.
    public static TheoryData<string,string,bool,string> ShapeNextToNonWordCharacters=>new()
    {
        { "\u0301sk-SYNTHETICabcdefghij0123", "input.value", true, "\u0301<redacted>" },
        { "note \u0301sk-SYNTHETICabcdefghij0123\u0301 end", "input.value", true, "note \u0301<redacted>\u0301 end" },
        { "\u0301sk-SYNTHETICabcdefghij0123", "", false, "\u0301<redacted>" },
        { "note \u0301sk-SYNTHETICabcdefghij0123\u0301 end", "", false, "note \u0301<redacted>\u0301 end" },
        { "\u200Csk-SYNTHETICabcdefghij0123", "input.value", true, "\u200C<redacted>" },
        { "note \u200Csk-SYNTHETICabcdefghij0123\u200C end", "input.value", true, "note \u200C<redacted>\u200C end" },
        { "\u200Csk-SYNTHETICabcdefghij0123", "", false, "\u200C<redacted>" },
        { "note \u200Csk-SYNTHETICabcdefghij0123\u200C end", "", false, "note \u200C<redacted>\u200C end" },
        { "\u200Dsk-SYNTHETICabcdefghij0123", "input.value", true, "\u200D<redacted>" },
        { "note \u200Dsk-SYNTHETICabcdefghij0123\u200D end", "input.value", true, "note \u200D<redacted>\u200D end" },
        { "\u200Dsk-SYNTHETICabcdefghij0123", "", false, "\u200D<redacted>" },
        { "note \u200Dsk-SYNTHETICabcdefghij0123\u200D end", "", false, "note \u200D<redacted>\u200D end" },
        { "\u203Fsk-SYNTHETICabcdefghij0123", "input.value", true, "\u203F<redacted>" },
        { "note \u203Fsk-SYNTHETICabcdefghij0123\u203F end", "input.value", true, "note \u203F<redacted>\u203F end" },
        { "\u203Fsk-SYNTHETICabcdefghij0123", "", false, "\u203F<redacted>" },
        { "note \u203Fsk-SYNTHETICabcdefghij0123\u203F end", "", false, "note \u203F<redacted>\u203F end" },
        { "\u0301AKIASYNTHETIC0123ABC", "input.value", true, "\u0301<redacted>" },
        { "note \u0301AKIASYNTHETIC0123ABC\u0301 end", "input.value", true, "note \u0301<redacted>\u0301 end" },
        { "\u0301AKIASYNTHETIC0123ABC", "", false, "\u0301<redacted>" },
        { "note \u0301AKIASYNTHETIC0123ABC\u0301 end", "", false, "note \u0301<redacted>\u0301 end" },
        { "\u200CAKIASYNTHETIC0123ABC", "input.value", true, "\u200C<redacted>" },
        { "note \u200CAKIASYNTHETIC0123ABC\u200C end", "input.value", true, "note \u200C<redacted>\u200C end" },
        { "\u200CAKIASYNTHETIC0123ABC", "", false, "\u200C<redacted>" },
        { "note \u200CAKIASYNTHETIC0123ABC\u200C end", "", false, "note \u200C<redacted>\u200C end" },
        { "\u200DAKIASYNTHETIC0123ABC", "input.value", true, "\u200D<redacted>" },
        { "note \u200DAKIASYNTHETIC0123ABC\u200D end", "input.value", true, "note \u200D<redacted>\u200D end" },
        { "\u200DAKIASYNTHETIC0123ABC", "", false, "\u200D<redacted>" },
        { "note \u200DAKIASYNTHETIC0123ABC\u200D end", "", false, "note \u200D<redacted>\u200D end" },
        { "\u203FAKIASYNTHETIC0123ABC", "input.value", true, "\u203F<redacted>" },
        { "note \u203FAKIASYNTHETIC0123ABC\u203F end", "input.value", true, "note \u203F<redacted>\u203F end" },
        { "\u203FAKIASYNTHETIC0123ABC", "", false, "\u203F<redacted>" },
        { "note \u203FAKIASYNTHETIC0123ABC\u203F end", "", false, "note \u203F<redacted>\u203F end" },
        { "\u0301Bearer SYNTHETICabc123", "input.value", true, "\u0301<redacted>" },
        { "note \u0301Bearer SYNTHETICabc123\u0301 end", "input.value", true, "note \u0301<redacted>\u0301 end" },
        { "\u0301Bearer SYNTHETICabc123", "", false, "\u0301<redacted>" },
        { "note \u0301Bearer SYNTHETICabc123\u0301 end", "", false, "note \u0301<redacted>\u0301 end" },
        { "\u200CBearer SYNTHETICabc123", "input.value", true, "\u200C<redacted>" },
        { "note \u200CBearer SYNTHETICabc123\u200C end", "input.value", true, "note \u200C<redacted>\u200C end" },
        { "\u200CBearer SYNTHETICabc123", "", false, "\u200C<redacted>" },
        { "note \u200CBearer SYNTHETICabc123\u200C end", "", false, "note \u200C<redacted>\u200C end" },
        { "\u200DBearer SYNTHETICabc123", "input.value", true, "\u200D<redacted>" },
        { "note \u200DBearer SYNTHETICabc123\u200D end", "input.value", true, "note \u200D<redacted>\u200D end" },
        { "\u200DBearer SYNTHETICabc123", "", false, "\u200D<redacted>" },
        { "note \u200DBearer SYNTHETICabc123\u200D end", "", false, "note \u200D<redacted>\u200D end" },
        { "\u203FBearer SYNTHETICabc123", "input.value", true, "\u203F<redacted>" },
        { "note \u203FBearer SYNTHETICabc123\u203F end", "input.value", true, "note \u203F<redacted>\u203F end" },
        { "\u203FBearer SYNTHETICabc123", "", false, "\u203F<redacted>" },
        { "note \u203FBearer SYNTHETICabc123\u203F end", "", false, "note \u203F<redacted>\u203F end" },
        { "\u0301eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "input.value", true, "\u0301<redacted>" },
        { "note \u0301eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u0301 end", "input.value", true, "note \u0301<redacted>\u0301 end" },
        { "\u0301eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "", false, "\u0301<redacted>" },
        { "note \u0301eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u0301 end", "", false, "note \u0301<redacted>\u0301 end" },
        { "\u200CeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "input.value", true, "\u200C<redacted>" },
        { "note \u200CeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u200C end", "input.value", true, "note \u200C<redacted>\u200C end" },
        { "\u200CeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "", false, "\u200C<redacted>" },
        { "note \u200CeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u200C end", "", false, "note \u200C<redacted>\u200C end" },
        { "\u200DeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "input.value", true, "\u200D<redacted>" },
        { "note \u200DeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u200D end", "input.value", true, "note \u200D<redacted>\u200D end" },
        { "\u200DeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "", false, "\u200D<redacted>" },
        { "note \u200DeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u200D end", "", false, "note \u200D<redacted>\u200D end" },
        { "\u203FeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "input.value", true, "\u203F<redacted>" },
        { "note \u203FeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u203F end", "input.value", true, "note \u203F<redacted>\u203F end" },
        { "\u203FeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz", "", false, "\u203F<redacted>" },
        { "note \u203FeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz\u203F end", "", false, "note \u203F<redacted>\u203F end" },
    };
    [Theory, MemberData(nameof(ShapeNextToNonWordCharacters))]
    public void AValueShapeNextToACombiningMarkJoinerOrConnectorIsReplacedAsTheReferenceReplacesIt(string value,string path,bool framed,string expected)
        =>Assert.Equal(expected,WirePrivacy.ScrubString(value,null,path,null,framed));

    // Known secrets are applied one at a time, longest first, as the reference applies them, so a credential name that a shorter
    // secret overlaps stays readable for the pair rule and its value is replaced. Where the reference leaves the visible part of
    // an overlapping secret, that part is replaced too (no secret character survives): more redacted than the reference.
    public static TheoryData<string,string[],string> OverlappingSecretsBeforeAPair=>new()
    {
        { "D1XDZ11Bauth=TAILV6", ["XDZ11Bau","D1XDZ11B"], "<redacted><redacted>th=<redacted>" },
        { "note YXXBDC1api_key: TAILV43 tail", ["api_key:","YXXBDC1ap"], "note <redacted><redacted> <redacted>" },
        { "DZ-B-0api_key=\"TAILV393", ["pi_key=\"","DZ-B-0api_"], "<redacted><redacted><redacted>" },
    };
    [Theory, MemberData(nameof(OverlappingSecretsBeforeAPair))]
    public void OverlappingKnownSecretsLeaveTheCredentialPairReadable(string value,string[] secrets,string expected)
    {
        foreach(var (path,framed) in new[]{ ("",false),("input.value",true) })
        {
            string actual=WirePrivacy.ScrubString(value,secrets,path,null,framed);
            Assert.Equal(expected,actual);
            Assert.DoesNotContain("TAILV",actual);
            Assert.Equal(actual,WirePrivacy.ScrubString(actual,secrets,path,null,framed));
        }
    }

    // A JSON value followed by more text is no JSON text: the pair rule reads the whole string (here it exports as the placeholder,
    // the boundary's fail-closed policy for text that starts like JSON and does not parse), so no marker is left at any site.
    [Theory]
    [InlineData("{} password=TAILV tail")]
    [InlineData("[1] token: TAILV")]
    [InlineData("{\"a\":1} trailing password=TAILV")]
    [InlineData("{\"a\":\"x\"} password=TAILV")]
    [InlineData("{}api_keydata:TAILVTAILV")]
    public void JsonFollowedByTextKeepsNoMarker(string value)
    {
        foreach(var path in new[]{ "","input.value","http.request.body","gen_ai.prompt" })
            foreach(var framed in new[]{ true,false })
                Assert.DoesNotContain("TAILV",WirePrivacy.ScrubString(value,null,path,null,framed));
        Assert.DoesNotContain("TAILV",WirePrivacy.ScrubString("{\"m\":"+System.Text.Json.JsonSerializer.Serialize(value)+"}",null,"input.value"));
    }
}
