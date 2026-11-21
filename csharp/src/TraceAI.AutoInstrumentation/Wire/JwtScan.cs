using System.Text;
using System.Text.RegularExpressions;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>Replaces what the catalog jwt shape <c>\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}</c> matches, with the
/// same result as <c>Regex.Replace</c> and in time linear in the text, for a token of any length.
/// The class excludes '.', so each segment is one maximal run of [A-Za-z0-9_-]. Every maximal run of [A-Za-z0-9_.-] is split at its
/// dots and its segments are examined left to right; each segment is read a bounded number of times (as header, then as second or
/// third segment of a failed triple), so the total work is linear. In a header segment the leftmost eyJ that starts at a regex word
/// boundary is the only candidate that can match (a later one has fewer characters after it). A match ends at the end of its
/// third segment and scanning resumes with the segment after it.</summary>
internal static class JwtScan
{
    private const int MinSegment=8;
    // The word-boundary test of the regex engine itself, for the one character that can be outside the ASCII run alphabet.
    private static readonly Regex Boundary=new(@"\G\b",RegexOptions.CultureInvariant,TimeSpan.FromSeconds(1));
    private static bool Run(char c)=>c is >='a' and <='z' or >='A' and <='Z' or >='0' and <='9' or '_' or '-';
    private static bool RunOrDot(char c)=>c=='.'||Run(c);
    private static bool StartsAt(string s,int p,int runStart)
    {
        if(p>0 && s[p-1]>=128 && p==runStart) return Boundary.IsMatch(s,p);
        if(p==0) return true;
        char prev=s[p-1];
        return prev=='-'||prev=='.'||!Run(prev); // '_' and alphanumerics are word characters; a non-run ASCII char is not
    }
    internal static string Replace(string s)
    {
        if(s.Length<29||s.IndexOf("eyJ",StringComparison.Ordinal)<0) return s;
        StringBuilder? result=null; int copied=0, i=0, n=s.Length;
        while(i<n)
        {
            while(i<n&&!RunOrDot(s[i])) i++;
            int runStart=i;
            while(i<n&&RunOrDot(s[i])) i++;
            int runEnd=i;
            if(runEnd-runStart<29) continue;
            // segments of [runStart,runEnd): [a,b) with b at a dot or runEnd
            int a=runStart;
            while(a<runEnd)
            {
                int b=s.IndexOf('.',a,runEnd-a); if(b<0) break; // no dot after this segment: it cannot be a header
                int c=s.IndexOf('.',b+1,runEnd-b-1); if(c<0) break;
                int d=s.IndexOf('.',c+1,runEnd-c-1); if(d<0) d=runEnd;
                if(b-a>=3+MinSegment && c-b-1>=MinSegment && d-c-1>=MinSegment)
                {
                    int p=a;
                    for(;;)
                    {
                        p=s.IndexOf("eyJ",p,b-p,StringComparison.Ordinal);
                        if(p<0||b-p-3<MinSegment) { p=-1; break; }
                        if(StartsAt(s,p,runStart)) break;
                        p++;
                    }
                    if(p>=0)
                    {
                        (result??=new StringBuilder(s.Length)).Append(s,copied,p-copied).Append(PrivacyCatalog.Redacted);
                        copied=d; a=d+1; continue; // the third segment ends the match; the next token may start after its dot
                    }
                }
                a=b+1;
            }
        }
        return result==null?s:result.Append(s,copied,n-copied).ToString();
    }
}
