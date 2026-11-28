using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;
using Xunit.Abstractions;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>Every credential value shape in spec/auto-instrumentation/privacy.json must scrub adversarial repeated-start input in
/// bounded time (no quadratic backtracking), and a real JWT must be replaced whole. Entry point: WirePrivacy.ScrubString
/// (which applies PrivacyCatalog.ValueShapes). Note ScrubString swallows RegexMatchTimeoutException and returns the placeholder,
/// so a catastrophic input shows up here as an over-budget time (about the 250 ms regex timeout), not as an exception.</summary>
public class ValueShapeBoundedTimeTests(ITestOutputHelper output)
{
    private const int Repeats=10_000;
    private const double BudgetMs=100;
    private static string Rep(string unit,int n=Repeats,string tail="")=>new StringBuilder(unit.Length*n+tail.Length).Insert(0,unit,n).Append(tail).ToString();
    private static string Scrub(string value)=>WirePrivacy.ScrubString(value);
    private void Bounded(string name,string input)
    {
        Scrub(Rep("warm-up ",16)+"eyJ-"); // warm up the JIT and the static catalog
        double ms=double.MaxValue;
        for(int run=0;run<3;run++){ var clock=Stopwatch.StartNew(); Scrub(input); clock.Stop(); ms=Math.Min(ms,clock.Elapsed.TotalMilliseconds); } // the fastest of three, so one slow run does not decide
        output.WriteLine($"{name}: {input.Length} chars, {ms:F1} ms (budget {BudgetMs} ms)");
        Assert.True(ms<BudgetMs,$"{name}: {input.Length} chars took {ms:F1} ms, budget {BudgetMs} ms");
    }
    [Fact] public void JwtDotFreeRepeatedStartIsBounded()=>Bounded("jwt eyJ-*N",Rep("eyJ-"));
    [Fact] public void JwtRepeatedStartEndingInDotIsBounded()=>Bounded("jwt eyJ-*N+.",Rep("eyJ-",Repeats,"."));
    [Fact] public void JwtTwoDotRepeatedStartIsBounded()=>Bounded("jwt (eyJ.eyJ-)*5000",Rep("eyJ.eyJ-",5_000));
    [Fact] public void JwtTwoSegmentRepeatedStartIsBounded()=>Bounded("jwt (eyJaaaaaaaa.)*N",Rep("eyJaaaaaaaa.",Repeats));
    [Fact] public void ProviderKeyRepeatedStartIsBounded()=>Bounded("sk-*N",Rep("sk-"));
    [Fact] public void AwsAccessKeyAkiaRepeatedStartIsBounded()=>Bounded("AKIA*N",Rep("AKIA"));
    [Fact] public void AwsAccessKeyAsiaRepeatedStartIsBounded()=>Bounded("ASIA*N",Rep("ASIA"));
    [Fact] public void AuthorizationBearerRepeatedStartIsBounded()=>Bounded("Bearer *N",Rep("Bearer "));
    [Fact] public void AuthorizationBasicRepeatedStartIsBounded()=>Bounded("Basic *N",Rep("Basic "));
    [Fact] public void PrivateKeyPemRepeatedStartIsBounded()=>Bounded("BEGIN PRIVATE KEY*N",Rep("-----BEGIN PRIVATE KEY-----"));
    [Fact] public void PrivateKeyPemRepeatedStartWithQualifierIsBounded()=>Bounded("BEGIN RSA PRIVATE KEY*N",Rep("-----BEGIN RSA PRIVATE KEY-----\n"));

    private const string Header="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", Payload="eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ";
    private static readonly string Redacted=PrivacyCatalog.Redacted;
    public static TheoryData<string,string,string> Jwts()
    {
        string sig="SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", dashSig="SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQss-", underSig="abcDEF123_-xyz987654321_";
        string jwt=$"{Header}.{Payload}.{sig}";
        return new()
        {
            { "plain",jwt,Redacted },
            { "signature ends in dash",$"{Header}.{Payload}.{dashSig}",Redacted },
            { "signature ends in underscore",$"{Header}.{Payload}.{underSig}_",Redacted },
            { "preceded by dash",$"x-{jwt}","x-"+Redacted },
            { "preceded by underscore-free word dash",$"access-token-{Header}.{Payload}.{dashSig}","access-token-"+Redacted },
            { "in surrounding text",$"token: {jwt}, expires soon.",$"token: {Redacted}, expires soon." },
            { "in surrounding text, dash-ending signature",$"got {Header}.{Payload}.{dashSig} here",$"got {Redacted} here" },
            { "quoted in JSON-ish text",$"{{\"id_token\":\"{Header}.{Payload}.{dashSig}\"}}","{\"id_token\":\""+Redacted+"\"}" },
        };
    }
    [Theory,MemberData(nameof(Jwts))]
    public void RealJwtIsReplacedWhole(string name,string input,string expected)
    {
        string actual=Scrub(input);
        Assert.True(expected==actual,$"{name}: expected '{expected}' but got '{actual}'");
        Assert.DoesNotContain("eyJ",actual); Assert.DoesNotContain(Payload,actual);
    }

    // ---- Large real tokens: the jwt shape must be unbounded, so a big header or payload is replaced whole. ----
    private static string B64(int n){ var sb=new StringBuilder(n); const string a="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"; for(int i=0;i<n;i++) sb.Append(a[(i*7+i/13)%a.Length]); return sb.ToString(); }
    public static TheoryData<string,string> LargeJwts()
    {
        string sig="SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
        string bigHeader="eyJ"+B64(2048), bigPayload="eyJ"+B64(12*1024);
        string a=$"{bigHeader}.{Payload}.{sig}", b=$"{Header}.{bigPayload}.{sig}";
        return new() { { "2KB header",a }, { "12KB payload",b }, { "Bearer + 2KB header","Bearer "+a }, { "Bearer + 12KB payload","Bearer "+b } };
    }
    [Theory,MemberData(nameof(LargeJwts))]
    public void LargeJwtIsReplacedWhole(string name,string input)
    {
        // The jwt shape runs before the authorization shape (catalog order), so "Bearer <token>" becomes "Bearer <redacted>".
        string expected=input.StartsWith("Bearer ",StringComparison.Ordinal)?"Bearer "+Redacted:Redacted;
        string actual=Scrub(input);
        Assert.True(expected==actual,$"{name}: expected whole redaction but got {actual.Length} chars: '{(actual.Length>80?actual[..80]:actual)}...'");
        Assert.DoesNotContain("eyJ",actual);
    }

    // ---- Differential: the jwt shape alone against the plain unbounded reference regex (backtracking, no timeout). ----
    private static readonly Regex Reference=new(@"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",RegexOptions.CultureInvariant);
    private static void SameAsReference(string input,string label)
    {
        string expected=Reference.Replace(input,"<redacted>"), actual=JwtScan.Replace(input);
        Assert.True(expected==actual,$"{label}: input '{(input.Length>200?input[..200]+"...":input)}' expected '{(expected.Length>200?expected[..200]:expected)}' actual '{(actual.Length>200?actual[..200]:actual)}'");
    }
    [Fact] public void JwtShapeMatchesUnboundedReferenceOnRandomStrings()
    {
        string[] pieces={"eyJ","eyJaaaaaaaa","aaaaaaaa",".","-","_"," ","x","\u00e9eyJ","\u00e9","\u0301eyJ","\u200deyJ","eyJ-","eyJ_","a.","7","eyJaaaaaaaa.aaaaaaaa.aaaaaaaa","eyJaaaaaaaa.aaaaaaaa."};
        var rng=new Random(20260101); int changed=0; const int Rounds=30_000;
        for(int round=0;round<Rounds;round++)
        {
            var sb=new StringBuilder(); int n=rng.Next(1,25);
            for(int i=0;i<n;i++) sb.Append(pieces[rng.Next(pieces.Length)]);
            string input=sb.ToString();
            if(Reference.Replace(input,"<redacted>")!=input) changed++;
            SameAsReference(input,$"random round {round}");
            string whole=PrivacyCatalog.Text(input,null,false); // through the catalog entry point (no Bearer/Basic/sk-/AKIA/PEM text in these pieces)
            Assert.True(Reference.Replace(input,"<redacted>")==whole,$"random round {round} via Text: '{input}'");
        }
        Assert.True(changed>1000,$"only {changed} rounds exercised a match");
        output.WriteLine($"differential: {Rounds} random rounds, {changed} where the reference redacted something");
    }
    [Fact] public void JwtShapeMatchesUnboundedReferenceWhenAValidRunIsBrokenAtEveryPosition()
    {
        string run=$"{Header}.{Payload}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
        int cases=0;
        foreach(var wrap in new[]{ ("",""),("Bearer ",""),("x-"," tail"),("\u00e9","."),("a.","-eyJ") })
            for(int k=0;k<run.Length;k++)
            {
                foreach(var replacement in new[]{ " ","!","\u00e9","\n" }) { SameAsReference(wrap.Item1+run[..k]+replacement+run[(k+1)..]+wrap.Item2,$"replaced at {k}"); cases++; }
                SameAsReference(wrap.Item1+run.Remove(k,1)+wrap.Item2,$"removed at {k}"); cases++;
                SameAsReference(wrap.Item1+run.Insert(k,".")+wrap.Item2,$"dot added at {k}"); cases++;
                SameAsReference(wrap.Item1+run.Insert(k,"eyJ")+wrap.Item2,$"eyJ added at {k}"); cases++;
                SameAsReference(wrap.Item1+run.Insert(k,"-")+wrap.Item2,$"dash added at {k}"); cases++;
            }
        output.WriteLine($"broken-run differential: {cases} cases");
    }
    [Fact] public void JwtShapeMatchesUnboundedReferenceOnMixedDotDashUnderscoreRuns()
    {
        string[] pieces={"eyJ","eyJabcdefgh","abcdefgh",".","-","_","..","eyJ-","-eyJ","_eyJ","e","J","y"};
        var rng=new Random(77); int cases=0;
        for(int round=0;round<20_000;round++)
        {
            var sb=new StringBuilder(); int n=rng.Next(1,30);
            for(int i=0;i<n;i++) sb.Append(pieces[rng.Next(pieces.Length)]);
            SameAsReference(sb.ToString(),$"mixed round {round}"); cases++;
        }
        output.WriteLine($"mixed-run differential: {cases} rounds");
    }
    [Fact] public void JwtShapeMatchesUnboundedReferenceNextToNonAsciiLetters()
    {
        foreach(var lead in new[]{ "\u00e9","\u00c9","\u0301","\u200c","\u200d","\u4e2d","\u0663","\u203f","\ud83d\ude00","\u00a0","\u2013","\u00b2" })
            foreach(var tail in new[]{ "","\u00e9" })
            {
                string jwt=$"{Header}.{Payload}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
                SameAsReference(lead+jwt+tail,"lead U+"+((int)lead[0]).ToString("X4"));
                SameAsReference("a"+lead+jwt+tail+" "+lead+jwt,"twice U+"+((int)lead[0]).ToString("X4"));
                SameAsReference(lead+"eyJ"+jwt,"lead U+"+((int)lead[0]).ToString("X4"));
            }
    }
    // 4 MiB (the 4 * 1024 * 1024 character text budget): the backtracking reference finishes this input in the test.
    [Fact] public void JwtShapeMatchesUnboundedReferenceOnOneLargeInput()
    {
        string unit=$"{Header}.{Payload}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
        var sb=new StringBuilder(); var rng=new Random(5);
        while(sb.Length<4*1024*1024)
        {
            sb.Append(rng.Next(6) switch { 0=>unit, 1=>"eyJ-eyJ.", 2=>"Bearer "+unit, 3=>"eyJ"+B64(rng.Next(3000)), 4=>"."+B64(rng.Next(40)), _=>" x\u00e9 " });
        }
        string input=sb.ToString();
        var clock=Stopwatch.StartNew(); string expected=Reference.Replace(input,"<redacted>"); var referenceMs=clock.ElapsedMilliseconds;
        clock.Restart(); string actual=PrivacyCatalog.Text(input,null,false); var oursMs=clock.ElapsedMilliseconds;
        output.WriteLine($"large input: {input.Length} chars, reference {referenceMs} ms, scan {oursMs} ms");
        Assert.True(expected==actual,"large input differs from the reference");
    }
}
