using System.Text;
using System.Text.RegularExpressions;
using static TraceAI.AutoInstrumentation.Wire.PrivacyText;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>A name of more than limits.max_depth components: the whole exported value becomes the placeholder (fail closed).</summary>
internal sealed class PrivacyPathBudgetException : Exception
{
    internal PrivacyPathBudgetException() : base("Privacy path budget") { }
}

/// <summary>The credential pair rule and the URL catalog of the shared privacy spec (catalog version 4). The executable reference is
/// spec/auto-instrumentation/generate_privacy_fixtures.py (class Reference); this is a port of its behaviour. Every exported string
/// passes known-secret replacement, then the catalog's value shapes, then the pair rule. Each read is bounded or moves forward, so the
/// rule is linear in the text.</summary>
internal static partial class PrivacyCatalog
{
    // A name, wherever its length is bounded, is read through its last NameCap characters; a name that runs on past that window is
    // not decided and its pair is replaced (fail closed).
    private const int NameCap=128;
    // A name after a '?' that holds more '?' marks than this is not decided either (fail closed).
    private const int QuestionCap=8;
    // The longest readable gap between a pair name and its separator: plain spaces only.
    private const int GapCap=8;
    private const string EscapedQuote="\\\"", EntityQuote="&quot;", EncodedNbsp="%C2%A0";
    private static readonly HashSet<string> ListHeaders=new(StringComparer.Ordinal) { "cookie","setcookie","authorization","proxyauthorization" };
    private static readonly string[] Words=Strings(Credentials["vocabulary"]!["words"]!);
    private static readonly HashSet<string> WordSet=new(Words,StringComparer.Ordinal);
    // The catalog's value shapes as read at a --name's value start: the value after an encoded gap is read as after a plain one,
    // where the shape's leading word boundary holds.
    private static readonly Regex[] WordShapes=Credentials["value_shapes"]!.AsArray().Select(n=>{
        string pattern=n!["regex"]!.GetValue<string>(), flags=n["flags"]!.GetValue<string>();
        if(pattern.StartsWith("\\b",StringComparison.Ordinal)) pattern=pattern[2..];
        bool ignoreCase=flags.Contains('i');
        var options=RegexOptions.CultureInvariant|(ignoreCase?RegexOptions.IgnoreCase:RegexOptions.None);
        return new Regex("\\G(?:"+ShapePattern(pattern,ignoreCase)+")",options,TimeSpan.FromSeconds(1));
    }).ToArray();

    private static bool PairNameChar(char c)=>c is (>= 'a' and <= 'z') or (>= 'A' and <= 'Z') or (>= '0' and <= '9') or '_' or '.' or '-' or '[' or ']';
    private static bool PairNameStart(char c)=>c is (>= 'a' and <= 'z') or (>= 'A' and <= 'Z') or '_';
    private static bool PaddingFollower(char c)=>c is '=' or ' ' or '\t' or '\n' or '\v' or '\f' or '\r' or ',' or ';' or ')' or ']' or '}' or '"' or '\'';
    private static bool LineBreak(char c)=>c is '\n' or '\r' or '\u0085' or '\u2028' or '\u2029' or '\f' or '\v';
    private static bool JsonSpaceEscape(char c)=>c is 't' or 'n' or 'r' or 'f' or 'b';
    private static bool EncodedEquals(string text,int at)=>at>=0&&at+2<=text.Length&&text[at]=='3'&&text[at+1] is 'D' or 'd';
    private static char Upper(char c)=>c is >= 'a' and <= 'z'?(char)(c-32):c;
    private static bool EncodedSpaceDigits(string text,int at)
    {
        if(at<0||at+2>text.Length) return false;
        char a=Upper(text[at]),b=Upper(text[at+1]);
        return a=='2'&&b=='0'||a=='0'&&b is '9' or 'A' or 'B' or 'C' or 'D';
    }
    private static bool EncodedNbspAt(string text,int at)
    {
        if(at<0||at+6>text.Length) return false;
        for(int k=0;k<6;k++) if(Upper(text[at+k])!=EncodedNbsp[k]) return false;
        return true;
    }
    private static bool UnicodeSpaceEscape(string text,int digits)
    {
        if(digits<0||digits+4>text.Length) return false;
        for(int k=0;k<4;k++) if(!Hex(text[digits+k])) return false;
        return Space((char)Convert.ToInt32(text.Substring(digits,4),16));
    }
    // ---- Names --------------------------------------------------------------------------------------------------------------
    /// <summary>A name of more than max_depth components (each non-empty run between '.', '[' and ']') is not read.</summary>
    internal static void PathBudget(string name)
    {
        int marks=0; foreach(char c in name) if(c is '.' or '[' or ']') marks++;
        if(marks<MaxDepth) return;
        int parts=0; bool inside=false;
        foreach(char c in name)
        {
            if(c is '.' or '[' or ']') inside=false;
            else if(!inside) { inside=true; parts++; }
        }
        if(parts>MaxDepth) throw new PrivacyPathBudgetException();
    }
    /// <summary>Whether a segment of the name is one of the catalog's vocabulary words. A segment is a run of UTF-16 units that are
    /// letters or decimal digits (Lu, Ll, Lt, Lm, Lo, Nd); every other unit delimits it, and so does a lowercase unit followed by an
    /// uppercase one. Words compare in lowercase (the simple one-to-one mapping).</summary>
    internal static bool Vocabulary(string name)
    {
        var lowered=new StringBuilder(name.Length);
        foreach(char c in name) lowered.Append(JavaLower(c));
        string folded=lowered.ToString();
        if(!Words.Any(w=>folded.Contains(w,StringComparison.Ordinal))) return false;
        var word=new StringBuilder(); char? previous=null;
        for(int k=0;k<=name.Length;k++)
        {
            char c=k<name.Length?name[k]:'-';
            bool surrogate=char.IsSurrogate(c);
            bool letter=!surrogate&&LetterOrDigit(c);
            bool boundary=!letter||(previous is char p&&char.IsUpper(c)&&char.IsLower(p));
            if(boundary&&word.Length>0)
            {
                if(WordSet.Contains(word.ToString())) return true;
                word.Clear();
            }
            if(letter) word.Append(JavaLower(c));
            previous=surrogate?null:c;
        }
        return false;
    }
    private static char JavaLower(char c)=>c=='\u0130'?'i':char.ToLowerInvariant(c);
    private static bool LetterOrDigit(char c)=>char.GetUnicodeCategory(c) is System.Globalization.UnicodeCategory.UppercaseLetter
        or System.Globalization.UnicodeCategory.LowercaseLetter or System.Globalization.UnicodeCategory.TitlecaseLetter
        or System.Globalization.UnicodeCategory.ModifierLetter or System.Globalization.UnicodeCategory.OtherLetter
        or System.Globalization.UnicodeCategory.DecimalDigitNumber;
    /// <summary>A credential name for the pair rule, with the command-line and bracket spellings undone first: --password,
    /// -Ddb.password and user[password].</summary>
    private static bool PairName(string name)
    {
        if(name.Length==0) return false;
        string stripped=name.TrimStart('-');
        if(stripped.StartsWith('D')&&stripped.Length>1&&name.StartsWith("-D",StringComparison.Ordinal)) stripped=stripped[1..];
        stripped=stripped.Replace('[','.').Replace(']','.');
        return stripped.Length>0&&(CredentialName(stripped)||CredentialName(name));
    }
    // ---- Readers ------------------------------------------------------------------------------------------------------------
    /// <summary>Whether another pair starts at start: [ \t]{0,8} (spaced only), -{0,2}, a name of 1 to 128 characters that starts
    /// with a letter or underscore, [ ]{0,8} (spaced only), then ':' or an '=' that is not padding or an empty value.</summary>
    private static bool NextPair(string text,int start,bool spaced)
    {
        int length=text.Length,i=start;
        if(spaced)
        {
            while(i<length&&text[i] is ' ' or '\t'&&i-start<=8) i++;
            if(i-start>8) return false;
        }
        int dashes=i;
        while(i<length&&text[i]=='-'&&i-dashes<=2) i++;
        if(i-dashes>2||i>=length||!PairNameStart(text[i])) return false;
        int name=i; i++;
        while(i<length&&PairNameChar(text[i])&&i-name<128) i++;
        if(i<length&&PairNameChar(text[i])) return false;
        if(spaced)
        {
            int gap=i;
            while(i<length&&text[i]==' '&&i-gap<=8) i++;
            if(i-gap>8) return false;
        }
        if(i>=length) return false;
        if(text[i]==':') return true;
        if(text[i]!='=') return false;
        int rest=i+1;
        if(rest>=length||PaddingFollower(text[rest])) return false;
        if(length-rest<=2)
        {
            string tail=text[rest..];
            if(tail is "\r\n" or "\n" or "\r" or "\u0085" or "\u2028" or "\u2029") return false;
        }
        return true;
    }
    /// <summary>The true end of a name=value value: a pair boundary with no bracket open, or the end of the text.</summary>
    private static int PairValueEnd(string text,int start)
    {
        int length=text.Length;
        if(start>=length) return length;
        if(StartsAt(text,start,"-----BEGIN"))
        {
            int endLine=text.IndexOf("-----END",start,StringComparison.Ordinal);
            if(endLine<0) return length;
            int close=text.IndexOf("-----",endLine+8,StringComparison.Ordinal);
            return close<0?length:close+5;
        }
        char first=text[start]; int i=start;
        if(first is '"' or '\'')
        {
            i=start+1;
            while(i<length&&text[i]!=first) i+=text[i]=='\\'?2:1;
            if(i>=length) return length;
            i++;
        }
        int depth=0;
        while(i<length)
        {
            char c=text[i];
            if(c is '(' or '[' or '{') depth++;
            else if(c is ')' or ']' or '}') depth=Math.Max(depth-1,0);
            else if(depth==0)
            {
                if(c is ',' or ';' or '\n' or '\r'&&NextPair(text,i+1,true)) return i;
                if(c==' '&&NextPair(text,i+1,false)) return i;
            }
            i++;
        }
        return length;
    }
    /// <summary>The width of the encoded whitespace that ends at end, read back no further than floor, or 0.</summary>
    private static int EncodedSpaceWidth(string text,int end,int floor)
    {
        if(end-3>=floor&&text[end-3]=='%'&&EncodedSpaceDigits(text,end-2)) return 3;
        if(end-6>=floor&&EncodedNbspAt(text,end-6)) return 6;
        int width=0;
        if(end-2>=floor&&text[end-2]=='\\'&&JsonSpaceEscape(text[end-1])) width=2;
        else if(end-6>=floor&&text[end-6]=='\\'&&text[end-5]=='u'&&UnicodeSpaceEscape(text,end-4)) width=6;
        if(width==0) return 0;
        int slashes=1;
        while(end-width-slashes>=floor&&text[end-width-slashes]=='\\') slashes++;
        return slashes%2==1?width:0;
    }
    /// <summary>The start of the gap that ends at end: whitespace and encoded whitespace, in any order.</summary>
    private static int GapStart(string text,int end,int floor)
    {
        while(end>floor)
        {
            if(Space(text[end-1])) { end--; continue; }
            int width=EncodedSpaceWidth(text,end,floor);
            if(width==0) break;
            end-=width;
        }
        return end;
    }
    private static bool EscapeBefore(string text,int end,int floor)=>end-3>=floor&&text[end-3]=='%'&&Hex(text[end-2])&&Hex(text[end-1]);
    /// <summary>The name before a separator that ends at a percent escape, read back over name characters and %XX escapes and
    /// percent-decoded once: (decoded, undecided), or null when the name does not end at an escape.</summary>
    private static (string Decoded,bool Undecided)? EscapedName(string text,int nameStart,int nameEnd,int floor)
    {
        if(!((EscapeBefore(text,nameEnd,floor)&&!EncodedEquals(text,nameEnd-2))||
             (nameStart>floor&&EscapeBefore(text,nameStart,floor)&&text[nameStart-2]=='2'&&text[nameStart-1]=='5')))
            return null;
        int start=nameEnd;
        while(start>floor&&nameEnd-start<NameCap)
        {
            if(EscapeBefore(text,start,floor))
            {
                if(EncodedEquals(text,start-2)) break;
                start-=3;
            }
            else if(PairNameChar(text[start-1])) start--;
            else break;
        }
        bool overflow=nameEnd-start>=NameCap&&start>floor&&(PairNameChar(text[start-1])||EscapeBefore(text,start,floor));
        string decoded=Unquote(text[start..nameEnd]);
        bool undecided=overflow||HasEscape(decoded)||!AnyWordCharacter(decoded);
        return (decoded,undecided);
    }
    private static bool DashesSpace(char c)=>Space(c)&&!LineBreak(c);
    private static bool WordSpace(char c)=>Space(c);
    private static bool EncodedControl(string text,int start,int width)
    {
        if(text[start]=='%') return width==3&&!(text[start+1]=='2'&&text[start+2]=='0');
        if(width==2) return true;
        return char.GetUnicodeCategory((char)Convert.ToInt32(text.Substring(start+2,4),16))==System.Globalization.UnicodeCategory.Control;
    }
    /// <summary>The width of the pair separator at k ('=', ':' that does not open "://", or %3D / %3d), or 0.</summary>
    private static int SeparatorAt(string text,int k)
    {
        char c=text[k];
        if(c=='='||(c==':'&&!StartsAt(text,k+1,"//"))) return 1;
        return c=='%'&&EncodedEquals(text,k+1)?3:0;
    }
    private static int SeparatorValueEnd(string text,int k,int size)
    {
        int value=k+size;
        while(value<text.Length&&value-k-size<8&&text[value]==' ') value++;
        int name=k;
        while(name>0&&k-name<NameCap&&PairNameChar(text[name-1])) name--;
        if(ListHeaders.Contains(Normalize(text[name..k])))
        {
            while(value<text.Length&&text[value] is not ('\n' or '\r')) value++;
            return value;
        }
        return PairValueEnd(text,value);
    }
    /// <summary>No rule leaves an '=', ':' or %3D unread at the end of the text it consumed: while text[start..stop] ends at a
    /// separator that is a credential or undecided pair, the replacement runs on to that pair's value end.</summary>
    private static int ConsumedEnd(string text,int start,int stop,Func<int,int,int,bool,int> decide)
    {
        while(stop>start)
        {
            int size;
            if(text[stop-1] is '=' or ':'&&SeparatorAt(text,stop-1)!=0) size=1;
            else if(stop-3>=start&&SeparatorAt(text,stop-3)==3) size=3;
            else return stop;
            int end=decide(stop-size,size,start,true);
            if(end<=stop) return stop;
            stop=end;
        }
        return stop;
    }
    private static int EncodedSpaceAhead(string text,int start)
    {
        if(start>=text.Length) return 0;
        if(text[start]=='%')
        {
            if(EncodedSpaceDigits(text,start+1)) return 3;
            return EncodedNbspAt(text,start)?6:0;
        }
        if(text[start]=='\\')
        {
            if(start+1<text.Length&&JsonSpaceEscape(text[start+1])) return 2;
            if(start+1<text.Length&&text[start+1]=='u'&&UnicodeSpaceEscape(text,start+2)) return 6;
        }
        return 0;
    }
    private static bool DashesSeparator(string text,int start)=>start<text.Length&&(text[start] is '=' or ':'||(text[start]=='%'&&EncodedEquals(text,start+1)));
    private static int FlagDashes(string text,int i)
    {
        if(StartsAt(text,i,"--")) return 2;
        return StartsAt(text,i,"-")&&i+1<text.Length&&PairNameStart(text[i+1])?1:0;
    }
    private static bool ReadableGap(string text,int start,int end)
    {
        if(end-start>GapCap) return false;
        for(int k=start;k<end;k++) if(text[k]!=' ') return false;
        return true;
    }
    private static string? QuoteBefore(string text,int end)
    {
        if(end>=EntityQuote.Length&&StartsAt(text,end-EntityQuote.Length,EntityQuote)) return EntityQuote;
        if(end<1||text[end-1] is not ('"' or '\'')) return null;
        if(text[end-1]=='"')
        {
            int slashes=0;
            while(end-2-slashes>=0&&text[end-2-slashes]=='\\'&&slashes<NameCap) slashes++;
            if(slashes%2==1) return EscapedQuote;
        }
        return text[end-1]=='"'?"\"":"'";
    }
    private readonly record struct QuotedName(int Opening,string Quote,int Close,bool Overflow);
    /// <summary>A quoted name that ends, after a whitespace gap, before the separator at colon, or null.</summary>
    private static QuotedName? JsonName(string text,int colon)
    {
        int end=GapStart(text,colon,0);
        string? quote=QuoteBefore(text,end);
        if(quote==null||(quote.Length==1&&end<3)) return null;
        int close=end-quote.Length, floor=Math.Max(0,close-130);
        int opening=LastIndex(text,quote,floor,close);
        if(opening>=0) return new(opening,quote,close,!ReadableGap(text,end,colon));
        return floor>0?new(-1,quote,close,true):null;
    }
    private static int JsonStringEnd(string text,int start)
    {
        char quote=text[start]; int i=start+1;
        while(i<text.Length)
        {
            if(text[i]=='\\') { i+=2; continue; }
            if(text[i]==quote) return i+1;
            i++;
        }
        return text.Length;
    }
    private static bool JsonMemberBoundary(string text,int end)
    {
        while(end<text.Length&&JavaSpace(text[end])) end++;
        return end>=text.Length||text[end] is ',' or '}' or ']';
    }
    private static int EscapedStringEnd(string text,int start)
    {
        int length=text.Length,i=start+EscapedQuote.Length; bool innerEscape=false;
        while(i<length)
        {
            if(text[i]=='\\'&&i+1<length)
            {
                char outer=text[i+1]; i+=2;
                if(innerEscape) innerEscape=false;
                else if(outer=='\\') innerEscape=true;
                else if(outer=='"') return i;
            }
            else { i++; innerEscape=false; }
        }
        return length;
    }
    private static int EntityStringEnd(string text,int start)
    {
        int length=text.Length,size=EntityQuote.Length,i=start+size;
        while(i<length)
        {
            if(text[i]=='\\') i+=1+(StartsAt(text,i+1,EntityQuote)?size:1);
            else if(StartsAt(text,i,EntityQuote)) return i+size;
            else i++;
        }
        return length;
    }
    private static readonly Regex JsonNumber=new(@"\A-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\z",RegexOptions.CultureInvariant);
    private static bool JsonScalarChar(char c)=>c is (>= '0' and <= '9') or '+' or '-' or '.' or 'e' or 'E' or 't' or 'r' or 'u' or 'f' or 'a' or 'l' or 's' or 'n';
    /// <summary>The end of the value of a quoted credential name: (a) a quoted string ends at its closing quote; (b) a number, true,
    /// false or null ends only before ',', '}', ']' or the end of the text; (c) anything else is prose to its true end.</summary>
    private static int JsonValueEnd(string text,int start,bool listHeader,string quote)
    {
        int length=text.Length;
        if(start>=length) return start;
        if(quote==EntityQuote&&StartsAt(text,start,EntityQuote)) return EntityStringEnd(text,start);
        if(quote==EscapedQuote&&StartsAt(text,start,EscapedQuote)) return EscapedStringEnd(text,start);
        char first=text[start];
        if(first is '"' or '\'') return JsonStringEnd(text,start);
        int end=start;
        while(end<length&&JsonScalarChar(text[end])) end++;
        if((end>=length||text[end] is ',' or '}' or ']' or ' ' or '\t' or '\r' or '\n')&&JsonMemberBoundary(text,end))
        {
            string scalar=text[start..end];
            if(scalar is "true" or "false" or "null"||JsonNumber.IsMatch(scalar)) return end;
        }
        if(listHeader)
        {
            while(start<length&&text[start] is not ('\n' or '\r')) start++;
            return start;
        }
        return PairValueEnd(text,start);
    }
    // ---- The pair rule ------------------------------------------------------------------------------------------------------
    private readonly record struct SeparatorDecision(bool Pair,string Name,bool Overflow);
    private static SeparatorDecision SeparatorPair(string text,int i,int size,int floor,Func<string,bool> credential)
    {
        int nameEnd=GapStart(text,i,floor);
        bool readable=ReadableGap(text,nameEnd,i);
        int nameStart=nameEnd;
        while(nameStart>floor&&nameEnd-nameStart<NameCap&&PairNameChar(text[nameStart-1])&&!EscapeBefore(text,nameStart,floor)) nameStart--;
        bool overflow=nameEnd-nameStart>=NameCap&&nameStart>floor&&PairNameChar(text[nameStart-1])&&!EscapeBefore(text,nameStart,floor);
        if(overflow) PathBudget(text[nameStart..nameEnd]);
        var escaped=overflow?null:EscapedName(text,nameStart,nameEnd,floor);
        string name=escaped==null?text[nameStart..nameEnd]:escaped.Value.Decoded;
        bool pair=(nameEnd-nameStart>=2&&(overflow||!readable||credential(text[nameStart..nameEnd])))
            ||(escaped!=null&&(escaped.Value.Undecided||!readable||credential(escaped.Value.Decoded)));
        return new(pair,name,overflow);
    }
    private static bool FlagAhead(string text,int start,Func<string,bool> credential)
    {
        int dashes=FlagDashes(text,start);
        if(dashes!=1) return dashes==2;
        int end=start+1;
        while(end<text.Length&&PairNameChar(text[end])&&end-start<=NameCap) end++;
        if(end-start<3||end-start>NameCap||end>=text.Length||!Space(text[end])) return false;
        int after=end;
        while(after<text.Length&&Space(text[after])) after++;
        return after<text.Length&&credential(text[start..end]);
    }
    private static bool LineStartsPair(string text,int start,Func<string,bool> credential)
    {
        int length=text.Length,k=-1;
        foreach(string quote in new[] { EntityQuote,EscapedQuote,"\"","'" })
        {
            if(!StartsAt(text,start,quote)) continue;
            int from=start+quote.Length, limit=Math.Min(length,start+quote.Length+130);
            int close=-1;
            for(int p=from;p+quote.Length<=limit;p++) if(string.CompareOrdinal(text,p,quote,0,quote.Length)==0) { close=p; break; }
            if(close<0) return false;
            k=close+quote.Length;
            break;
        }
        if(k<0)
        {
            if(FlagAhead(text,start,credential)) return true;
            int name=start;
            while(name<length&&text[name]=='-'&&name-start<2) name++;
            k=name;
            while(k<length&&k-name<NameCap)
            {
                if(PairNameChar(text[k])) k++;
                else if(k+3<=length&&EscapeBefore(text,k+3,k)&&!EncodedEquals(text,k+1)&&EncodedSpaceAhead(text,k)==0) k+=3;
                else break;
            }
            if(k==name) return false;
        }
        while(k<length&&(DashesSpace(text[k])||EncodedSpaceAhead(text,k)!=0)) k+=EncodedSpaceAhead(text,k) is var w&&w>0?w:1;
        return k<length&&SeparatorAt(text,k)!=0;
    }
    /// <summary>Replaces the value of every credential-named pair with the placeholder, in one left-to-right pass. skip holds the
    /// spans (start, end) the catalog itself replaced in a whole URL value, recorded by position during the catalog pass; a pair
    /// whose value starts inside one is left as the catalog wrote it. An omission span shields nothing.</summary>
    internal static string CredentialPairs(string text,IReadOnlyList<Span>? skipSpans=null,List<(int Start,int End,int Written)>? rewrites=null)
    {
        int length=text.Length,copied=0,i=0,dashesColon=-1;
        var output=new StringBuilder(); bool wrote=false;
        var known=new Dictionary<string,bool>(StringComparer.Ordinal);
        var skip=(skipSpans??[]).Where(s=>!s.Omission).ToArray();
        var starts=skip.Select(s=>s.Start).ToArray();
        bool Credential(string name)
        {
            if(!known.TryGetValue(name,out bool result)) known[name]=result=PairName(name);
            return result;
        }
        int Skipped(int position)
        {
            int index=Array.BinarySearch(starts,position);
            int k=index>=0?index:~index-1;
            if(index>=0) while(k+1<starts.Length&&starts[k+1]<=position) k++;
            return k>=0&&position<skip[k].End?skip[k].End:-1;
        }
        (bool Pair,string Name) QuotedPair(QuotedName quoted)
        {
            string name=quoted.Opening<0?"":text[(quoted.Opening+quoted.Quote.Length)..quoted.Close];
            int plain=name.Length;
            while(plain>0&&PairNameChar(name[plain-1])&&!EscapeBefore(name,plain,0)) plain--;
            var escaped=quoted.Opening>=0?EscapedName(name,plain,name.Length,0):null;
            return (quoted.Overflow||Credential(name)||(escaped!=null&&(escaped.Value.Undecided||Credential(escaped.Value.Decoded))),name);
        }
        int EqualsValueEnd(int k,int size,string name)
        {
            int value=k+size;
            while(value<length&&value-k-size<8&&text[value]==' ') value++;
            if(ListHeaders.Contains(Normalize(name)))
            {
                while(value<length&&text[value] is not ('\n' or '\r')) value++;
                return value;
            }
            return PairValueEnd(text,value);
        }
        int Decide(int k,int size,int floor,bool readEnd)
        {
            var quoted=JsonName(text,k);
            if(quoted!=null&&quoted.Value.Opening>=floor)
            {
                var (pair,name)=QuotedPair(quoted.Value);
                if(size==1&&text[k]==':')
                {
                    if(!pair) return -1;
                    if(!readEnd) return k;
                    int value=k+1;
                    while(value<length&&JavaSpace(text[value])) value++;
                    return JsonValueEnd(text,value,ListHeaders.Contains(Normalize(name)),quoted.Value.Quote);
                }
                if(pair) return readEnd?EqualsValueEnd(k,size,name):k;
            }
            if(!SeparatorPair(text,k,size,floor,Credential).Pair) return -1;
            return readEnd?SeparatorValueEnd(text,k,size):k;
        }
        bool QuotedEqualsAt(int k)
        {
            var quoted=k>0?JsonName(text,k):null;
            return quoted!=null&&QuotedPair(quoted.Value).Pair;
        }
        int WordEnd(int begin,int value)
        {
            int valueEnd=value,word=value;
            if(text[value] is '"' or '\'')
            {
                valueEnd=value+1;
                while(valueEnd<length&&text[valueEnd]!=text[value]) valueEnd+=text[valueEnd]=='\\'?2:1;
                valueEnd=word=Math.Min(length,valueEnd+1);
            }
            int shapeEnd=value;
            foreach(var shape in WordShapes)
            {
                var match=shape.Match(text,value);
                if(match.Success) shapeEnd=Math.Max(shapeEnd,match.Index+match.Length);
            }
            int first=-1,last=-1;
            while(valueEnd<length&&(valueEnd<shapeEnd||!WordSpace(text[valueEnd])))
            {
                int size=valueEnd>=word?SeparatorAt(text,valueEnd):0;
                if(size!=0&&Decide(valueEnd,size,begin,false)>=0)
                {
                    if(first<0) first=valueEnd;
                    last=valueEnd;
                }
                valueEnd++;
            }
            int ahead=valueEnd;
            while(ahead<length&&(DashesSpace(text[ahead])||EncodedSpaceAhead(text,ahead)!=0)) ahead+=EncodedSpaceAhead(text,ahead) is var w&&w>0?w:1;
            if(ahead>valueEnd&&ahead<length&&SeparatorAt(text,ahead)!=0&&Decide(ahead,SeparatorAt(text,ahead),begin,false)>=0)
            {
                if(first<0) first=ahead;
                last=ahead;
            }
            foreach(int k in new SortedSet<int>(new[] { first,last }.Where(x=>x!=-1)))
                valueEnd=Math.Max(valueEnd,Decide(k,SeparatorAt(text,k),begin,true));
            return ConsumedEnd(text,begin,valueEnd,Decide);
        }
        while(i<length)
        {
            char c=text[i];
            int start=-1,stop=-1; string replacement=Redacted; bool userinfo=false;
            var quotedName=c==':'&&i>0?JsonName(text,i):null;
            if(quotedName!=null)
            {
                var (pair,name)=QuotedPair(quotedName.Value);
                if(pair)
                {
                    int value=i+1;
                    while(value<length&&JavaSpace(text[value])) value++;
                    if(skip.Length>0&&Skipped(value)>=0) { i=Math.Max(Skipped(value),i+1); continue; }
                    bool header=ListHeaders.Contains(Normalize(name));
                    int end=JsonValueEnd(text,value,header,quotedName.Value.Quote);
                    if(!header) end=ConsumedEnd(text,value,end,Decide);
                    if(end>value) { start=value; stop=end; replacement=quotedName.Value.Quote+Redacted+quotedName.Value.Quote; }
                }
                if(start<0) { i++; continue; }
            }
            else if(c==':'&&StartsAt(text,i+1,"//")&&i!=dashesColon)
            {
                int limit=Math.Min(length,i+3+512),end=i+3;
                while(end<limit&&text[end] is not ('/' or '?' or '#' or ' ' or '\t' or '\r' or '\n')) end++;
                int at=LastIndex(text,'@',i+3,end);
                if(at<i+3)
                {
                    int word=i+3;
                    while(word<length&&word-i<1024&&!JavaSpace(text[word])) word++;
                    int later=LastIndex(text,'@',i+3,word);
                    int colon=later>i+3?Index(text,':',i+3,later):-1;
                    if(later>i+3&&colon>i+3) at=later;
                }
                if(at<i+3) { i+=3; continue; }
                start=i+3; stop=at; userinfo=true;
            }
            else if((c=='='||(c=='%'&&EncodedEquals(text,i+1)))&&QuotedEqualsAt(i))
            {
                int size=c=='='?1:3, value=i+size;
                while(value<length&&value-i-size<8&&text[value]==' ') value++;
                if(skip.Length>0&&Skipped(value)>=0) { i=Math.Max(Skipped(value),i+1); continue; }
                string name=QuotedPair(JsonName(text,i)!.Value).Name;
                start=value;
                stop=EqualsValueEnd(i,size,name);
                if(!ListHeaders.Contains(Normalize(name))) stop=ConsumedEnd(text,value,stop,Decide);
            }
            else if(c is '=' or ':'||(c=='%'&&EncodedEquals(text,i+1)))
            {
                int size=c is '=' or ':'?1:3;
                var decision=SeparatorPair(text,i,size,copied,Credential);
                if(decision.Pair)
                {
                    int value=i+size;
                    while(value<length&&value-i-size<8&&text[value]==' ') value++;
                    if(skip.Length>0&&Skipped(value)>=0) { i=Math.Max(Skipped(value),i+1); continue; }
                    start=value;
                    if(!decision.Overflow&&ListHeaders.Contains(Normalize(decision.Name)))
                    {
                        stop=value;
                        while(stop<length&&text[stop] is not ('\n' or '\r')) stop++;
                    }
                    else stop=ConsumedEnd(text,value,PairValueEnd(text,value),Decide);
                }
            }
            else if(c=='-'&&(i==0||Space(text[i-1])||EncodedSpaceWidth(text,i,0)!=0)&&FlagDashes(text,i)!=0)
            {
                int dashes=FlagDashes(text,i), end=i+dashes;
                while(end<length&&PairNameChar(text[end])&&end-i<NameCap+dashes) end++;
                bool overflow=end-i>=NameCap+dashes&&end<length&&PairNameChar(text[end]);
                while(overflow&&end<length&&PairNameChar(text[end])) end++;
                if(overflow) PathBudget(text[i..end]);
                if(end<length&&end-i-dashes>=(dashes==2?1:2)
                    &&(DashesSpace(text[end])||LineBreak(text[end])||EncodedSpaceAhead(text,end)!=0)
                    &&(overflow||Credential(text[i..end])))
                {
                    int value=end,control=-1,gapEnd=end; bool spaced=false, handled=false;
                    while(value<length)
                    {
                        if(DashesSpace(text[value])) { value++; spaced=spaced||control>=0; continue; }
                        int width=EncodedSpaceAhead(text,value);
                        if(width==0) break;
                        if(control<0&&EncodedControl(text,value,width)) control=value;
                        value+=width; gapEnd=value;
                    }
                    if(DashesSeparator(text,value))
                    {
                        dashesColon=text[value]==':'?value:-1;
                        i=value;
                        continue;
                    }
                    int begin=control>=0?control:value, valueEnd=-1;
                    if(value<length&&LineBreak(text[value]))
                    {
                        int line=value+(StartsAt(text,value,"\r\n")?2:1);
                        bool encoded=false;
                        while(line<length)
                        {
                            if(DashesSpace(text[line])) { line++; continue; }
                            if(encoded&&LineBreak(text[line])) { line+=StartsAt(text,line,"\r\n")?2:1; encoded=false; continue; }
                            int width=EncodedSpaceAhead(text,line);
                            if(width==0) break;
                            if(control<0&&EncodedControl(text,line,width)) control=line;
                            line+=width; encoded=true;
                        }
                        if(line>=length||LineBreak(text[line])||DashesSeparator(text,line))
                        {
                            if(line>=length||LineBreak(text[line])) i=end;
                            else { dashesColon=text[line]==':'?line:-1; i=line; }
                            continue;
                        }
                        if(LineStartsPair(text,line,Credential))
                        {
                            if(control>=0) { start=control; stop=gapEnd; handled=true; }
                            else { i=line; continue; }
                        }
                        else
                        {
                            begin=control>=0?control:line;
                            valueEnd=WordEnd(begin,line);
                        }
                    }
                    else if(control>=0&&!spaced&&value<length) valueEnd=WordEnd(begin,value);
                    else if(value>=length||FlagAhead(text,value,Credential))
                    {
                        if(control>=0) { start=control; stop=gapEnd; handled=true; }
                    }
                    else valueEnd=WordEnd(begin,value);
                    if(!handled&&valueEnd>begin) { start=begin; stop=valueEnd; }
                }
                if(start<0) { i=end; continue; }
            }
            if(start>=0&&skip.Length>0)
            {
                int spanEnd=Skipped(start);
                if(spanEnd>=0&&!(userinfo&&stop>spanEnd)) { i=Math.Max(spanEnd,i+1); continue; }
            }
            if(start>=copied&&stop>=start)
            {
                output.Append(text,copied,start-copied).Append(replacement);
                rewrites?.Add((start,stop,replacement.Length));
                wrote=true; copied=stop; i=Math.Max(stop,i+1);
                continue;
            }
            i++;
        }
        if(!wrote) return text;
        return output.Append(text,copied,length-copied).ToString();
    }
    // ---- The URL catalog ----------------------------------------------------------------------------------------------------
    /// <summary>A span (start, end) the catalog replaced, by position; Omission is true for an omission the catalog wrote (a data URI
    /// or a media value), false for a placeholder. The kind is recorded when the span is written, never read back from its text.</summary>
    internal readonly record struct Span(int Start,int End,bool Omission=false);
    /// <summary>An output text built from kept and replaced pieces, recording by position every span the catalog replaced.</summary>
    private sealed class Rewrite
    {
        private readonly StringBuilder parts=new();
        internal readonly List<Span> Spans=[];
        internal void Keep(string text) => parts.Append(text);
        internal void Keep(string text,int start,int count) => parts.Append(text,start,count);
        internal void Put(string text,bool omission=false) { Spans.Add(new(parts.Length,parts.Length+text.Length,omission)); parts.Append(text); }
        /// <summary>Writes a part of a URL: each run of runs (sorted, disjoint; an empty run inserts the placeholder) becomes the
        /// placeholder, and each data URI in the original text its omission. Where a run and a data URI overlap, the run's
        /// placeholder is written and the data URI's omission covers the rest of it.</summary>
        internal void Region(string text,IReadOnlyList<(int Start,int End)>? runs=null)
        {
            runs??=[];
            var data=new List<(int Start,int End,string Replacement)>();
            foreach(var (s,e,_) in ScanText(Data,text,false))
                if(DataUri(text[s..e]) is {} replacement) data.Add((s,e,replacement));
            int position=0,length=text.Length,r=0,d=0,lastKind=0,lastIndex=-1;
            while(true)
            {
                while(r<runs.Count&&runs[r].End<=position&&!(runs[r].Start==runs[r].End&&runs[r].End==position)) r++;
                if(r<runs.Count&&runs[r].Start==runs[r].End&&runs[r].End==position)
                {
                    Put(Redacted); lastKind=1; lastIndex=r; r++;
                    continue;
                }
                while(d<data.Count&&data[d].End<=position) d++;
                if(position>=length) break;
                if(r<runs.Count&&runs[r].Start<=position)
                {
                    if(!(lastKind==1&&lastIndex==r)) { Put(Redacted); lastKind=1; lastIndex=r; }
                    position=runs[r].End;
                }
                else if(d<data.Count&&data[d].Start<=position)
                {
                    int end=r<runs.Count?Math.Min(data[d].End,runs[r].Start):data[d].End;
                    if(!(lastKind==2&&lastIndex==d)) { Put(data[d].Replacement,true); lastKind=2; lastIndex=d; }
                    position=end;
                }
                else
                {
                    int end=length;
                    if(r<runs.Count) end=Math.Min(end,runs[r].Start);
                    if(d<data.Count) end=Math.Min(end,data[d].Start);
                    Keep(text,position,end-position);
                    position=end; lastKind=0; lastIndex=-1;
                }
            }
        }
        internal string Text => parts.ToString();
    }
    /// <summary>Replaces each run (sorted, disjoint) with the placeholder. A span a run overlaps is dropped; the others move.</summary>
    private static (string Value,List<Span> Spans) ReplaceRuns(string value,List<(int Start,int End)> runs,List<Span> spans)
    {
        if(runs.Count==0) return (value,spans);
        var output=new StringBuilder(value.Length); var moved=new List<Span>();
        int copied=0,shift=0,k=0;
        foreach(var (start,end) in runs)
        {
            while(k<spans.Count&&spans[k].End<=start) { moved.Add(spans[k] with { Start=spans[k].Start+shift,End=spans[k].End+shift }); k++; }
            while(k<spans.Count&&spans[k].Start<end) k++;
            output.Append(value,copied,start-copied).Append(Redacted);
            copied=end;
            shift+=Redacted.Length-(end-start);
        }
        for(;k<spans.Count;k++) moved.Add(spans[k] with { Start=spans[k].Start+shift,End=spans[k].End+shift });
        output.Append(value,copied,value.Length-copied);
        return (output.ToString(),moved);
    }
    /// <summary>The matches of pattern (Data, or TextTokens with its data group) in text, left to right and not overlapping, with
    /// each data URI cut where a continuation line stops belonging to it: (start, end, whether the match is a URL).</summary>
    private static IEnumerable<(int Start,int End,bool Url)> ScanText(Regex pattern,string text,bool tokens)
    {
        int position=0;
        while(position<=text.Length)
        {
            var match=pattern.Match(text,position);
            if(!match.Success) yield break;
            bool url=tokens&&match.Groups["url"].Success;
            int end=url?match.Index+match.Length:DataUriEnd(text,match.Index,match.Index+match.Length);
            yield return (match.Index,end,url);
            position=end>match.Index?end:match.Index+1;
        }
    }
    private static string Substitute(Regex pattern,string text,bool tokens,Func<string,bool,int,string> replace)
    {
        StringBuilder? output=null; int copied=0;
        foreach(var (start,end,url) in ScanText(pattern,text,tokens))
        {
            output??=new StringBuilder(text.Length);
            output.Append(text,copied,start-copied).Append(replace(text[start..end],url,start));
            copied=end;
        }
        return output==null?text:output.Append(text,copied,text.Length-copied).ToString();
    }
    private const string Base64Index="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    private static int Base64Value(char c)=>c=='-'?62:c=='_'?63:Base64Index.IndexOf(c);
    private static bool Base64Alphabet(char c)=>c is (>= 'A' and <= 'Z') or (>= 'a' and <= 'z') or (>= '0' and <= '9') or '+' or '/' or '_' or '-';
    private static bool DataLineChar(char c)=>Base64Alphabet(c)||c=='=';
    private static bool Padding(string body,int pad,bool base64,bool padded,int count)
    {
        if(!base64||padded||pad>2||body.Length==0||body.StartsWith('-')||(count+body.Length+pad)%4!=0) return false;
        return Base64Value(body[^1])%(pad==1?4:16)==0;
    }
    /// <summary>The end of the data URI matched at text[start..end]: a base64 continuation line belongs to it only if its '='
    /// characters are trailing base64 padding, and a line followed by a separator is a pair name, never base64.</summary>
    private static int DataUriEnd(string text,int start,int end)
    {
        int head=start+5;
        while(head<end&&text[head] is not ('\r' or '\n')) head++;
        string header=text[(start+5)..head];
        int comma=header.IndexOf(',');
        string metadata=comma<0?header:header[..comma], payload=comma<0?"":header[(comma+1)..];
        bool base64=comma>=0&&AsciiLower(metadata).EndsWith(";base64",StringComparison.Ordinal);
        string decoded=Unquote(payload);
        int count=decoded.Count(Base64Alphabet); bool padded=decoded.Contains('=');
        int position=head;
        while(position<end)
        {
            int p=position;
            while(p<end&&text[p] is '\r' or '\n') p++;
            if(p==position) break;
            int groupStart=p;
            while(p<end&&DataLineChar(text[p])) p++;
            if(p==groupStart) break;
            int after=p,gap=p;
            while(gap<text.Length&&(DashesSpace(text[gap])||EncodedSpaceAhead(text,gap)!=0)) gap+=EncodedSpaceAhead(text,gap) is var w&&w>0?w:1;
            if(gap<text.Length&&(text[gap] is '=' or ':'||StartsAt(text,gap,"%3D")||StartsAt(text,gap,"%3d"))) break;
            string group=text[groupStart..p], body=group.TrimEnd('=');
            int pad=group.Length-body.Length;
            if(body.StartsWith("--",StringComparison.Ordinal)||body.Contains('=')||(pad>0&&!Padding(body,pad,base64,padded,count))) break;
            count+=body.Length; padded=padded||pad>0; position=after;
        }
        return position;
    }
    private static string AsciiLower(string text)
    {
        var builder=new StringBuilder(text.Length);
        foreach(char c in text) builder.Append(c is >= 'A' and <= 'Z'?(char)(c+32):c);
        return builder.ToString();
    }
    /// <summary>The user information of an http(s) URL, as ^(https?://)[^\s/@]+:[^\s/@]+@ reads it, in one linear scan: (the scheme
    /// length, the index after the '@'), or (0, -1). The run after the scheme holds no whitespace, '/' or '@', ends at an '@', and has
    /// a ':' with at least one character on each side.</summary>
    private static (int Scheme,int End) UserInfo(string path)
    {
        int scheme=path.StartsWith("http://",StringComparison.OrdinalIgnoreCase)?7:path.StartsWith("https://",StringComparison.OrdinalIgnoreCase)?8:0;
        if(scheme==0) return (0,-1);
        int k=scheme;
        while(k<path.Length&&!Space(path[k])&&path[k] is not ('/' or '@')) k++;
        if(k>=path.Length||path[k]!='@') return (0,-1);
        // A ':' with at least one character of the run before it and one after it, before the '@'.
        for(int p=scheme+1;p<k-1;p++) if(path[p]==':') return (scheme,k+1);
        return (0,-1);
    }
    // Names after a '?' already read in this URL: each is read once (its name test is the costly part of the second-'?' read).
    [ThreadStatic] private static Dictionary<string,bool>? questionNames;
    private static bool QuestionQueryName(string raw)
    {
        var cache=questionNames??=new Dictionary<string,bool>(StringComparer.Ordinal);
        if(cache.TryGetValue(raw,out bool known)) return known;
        bool result=QueryName(UnquotePlus(raw));
        if(cache.Count>=65536) cache.Clear();
        cache[raw]=result;
        return result;
    }
    private static bool AfterQuestion(string text,List<int> marks,int end)
    {
        if(marks.Count==0) return false;
        if(end-marks[0]-1>NameCap||marks.Count>QuestionCap) return true;
        foreach(int q in marks) if(QuestionQueryName(text[(q+1)..end])) return true;
        return false;
    }
    private static bool QuestionName(string rawName)
    {
        if(QueryName(UnquotePlus(rawName))) return true;
        var marks=new List<int>(); int at=rawName.IndexOf('?');
        while(at>=0&&marks.Count<=QuestionCap) { marks.Add(at); at=rawName.IndexOf('?',at+1); }
        return AfterQuestion(rawName,marks,rawName.Length);
    }
    private static (string Head,bool Found,string Tail) Partition(string text,char separator)
    {
        int at=text.IndexOf(separator);
        return at<0?(text,false,""):(text[..at],true,text[(at+1)..]);
    }
    /// <summary>The part of a URL after its '#': split at '&amp;' like a query; a credential-named pair's value is replaced to the next
    /// '&amp;'; in any other pair, a credential pair after a second '?' is.</summary>
    private static void TailPairs(string segment,Rewrite output)
    {
        var runs=new List<(int,int)>(); int offset=0;
        foreach(string piece in segment.Split('&'))
        {
            var (rawName,equals,rawValue)=Partition(piece,'=');
            int value=offset+rawName.Length+(equals?1:0), end=offset+piece.Length;
            if(equals&&QuestionName(rawName)) runs.Add((value,end));
            else if(equals)
            {
                int cut=SecondQuestionCut(rawValue);
                if(cut>=0) runs.Add((value+cut,end));
            }
            offset=end+1;
        }
        output.Region(segment,runs);
    }
    /// <summary>Where a value that holds '?name=...' with a credential name is cut (after that pair's '='), or -1. One forward pass.</summary>
    private static int SecondQuestionCut(string rawValue)
    {
        int i=rawValue.IndexOf('?');
        if(i<0) return -1;
        var marks=new List<int>();
        for(;i<rawValue.Length;i++)
        {
            char c=rawValue[i];
            if(c=='?') { if(marks.Count<=QuestionCap) marks.Add(i); }
            else if(c=='=')
            {
                if(marks.Count>0&&AfterQuestion(rawValue,marks,i)) return i+1;
                marks.Clear();
            }
        }
        return -1;
    }
    private static bool PathPairSeparator(char c)=>c is ';' or ',' or '&';
    /// <summary>The URL path pair rule: each path segment is read whole, every '=' and every %3D in it; a credential name's value is
    /// replaced to the next pair in the segment or the segment end.</summary>
    private static void PathPairs(string path,Rewrite output)
    {
        int start=0;
        if(path.StartsWith("http://",StringComparison.OrdinalIgnoreCase)||path.StartsWith("https://",StringComparison.OrdinalIgnoreCase))
        {
            start=path.IndexOf('/',path.IndexOf("//",StringComparison.Ordinal)+2);
            if(start<0) start=path.Length;
        }
        var runs=new List<(int,int)>(); int position=start;
        while(position<path.Length)
        {
            int end=position+1<path.Length?path.IndexOf('/',position+1):-1;
            if(end<0) end=path.Length;
            int first=path[position]=='/'?position+1:position;
            int k=first,name=first; bool decided=false;
            while(k<end)
            {
                char c=path[k];
                if(PathPairSeparator(c)) { k++; name=k; continue; }
                int size=c=='='?1:c=='%'&&EncodedEquals(path,k+1)?3:0;
                if(size==0) { k++; continue; }
                bool credential=k>name&&CredentialName(Unquote(path[name..k]));
                if(!decided&&!credential&&k>first) credential=CredentialName(Unquote(path[first..k]));
                decided=true;
                if(credential)
                {
                    int valueEnd=PathValueEnd(path,k+size,end);
                    runs.Add((k+size,valueEnd));
                    k=name=valueEnd;
                }
                else k=name=k+size;
            }
            position=end;
        }
        output.Region(path,runs);
    }
    private static int PathValueEnd(string path,int start,int end)
    {
        int k=start;
        while(k<end)
        {
            if(!PathPairSeparator(path[k])) { k++; continue; }
            int j=k+1;
            while(j<end&&!PathPairSeparator(path[j])&&path[j]!='='&&!(path[j]=='%'&&EncodedEquals(path,j+1))) j++;
            if(j<end&&j>k+1&&!PathPairSeparator(path[j])) return k;
            k=j;
        }
        return end;
    }
    /// <summary>Scrubs one already-framed URL. Returns the text and the spans the catalog itself replaced in it, by position.</summary>
    private static (string Text,List<Span> Spans) FramedUrl(string original)
    {
        questionNames?.Clear();
        var output=new Rewrite();
        var (target,hashmark,fragment)=Partition(original,'#');
        var (path,question,rawQuery)=Partition(target,'?');
        var (scheme,userinfoEnd)=UserInfo(path);
        if(userinfoEnd>0)
        {
            output.Keep(path,0,scheme);
            output.Put(Redacted);
            output.Keep("@");
            path=path[userinfoEnd..];
        }
        PathPairs(path,output);
        if(question) output.Keep("?");
        var parsed=rawQuery.Split('&').Select(pair=>{
            var (rawName,equals,rawValue)=Partition(pair,'=');
            return (RawName:rawName,HasEquals:equals,RawValue:rawValue,Name:UnquotePlus(rawName),Decoded:UnquotePlus(rawValue));
        }).ToArray();
        var hints=IndexHints(parsed.Where(p=>p.HasEquals).Select(p=>new KeyValuePair<string,object?>(p.Name,p.Decoded)));
        for(int index=0;index<parsed.Length;index++)
        {
            var pair=parsed[index];
            if(index>0) output.Keep("&");
            output.Keep(pair.RawName);
            if(!pair.HasEquals) continue;
            output.Keep("=");
            if(QueryName(pair.Name)||QuestionName(pair.RawName)) { output.Put(Redacted); continue; }
            if(MediaString(pair.Decoded,pair.Name,hints) is {} replacement) { output.Put(replacement,true); continue; }
            int cut=SecondQuestionCut(pair.RawValue);
            if(cut>=0) { output.Keep(pair.RawValue,0,cut); output.Put(Redacted); }
            else if(DataText(pair.Decoded,true)!=pair.Decoded) output.Region(pair.Decoded);
            else output.Keep(pair.RawValue);
        }
        if(hashmark) { output.Keep("#"); TailPairs(fragment,output); }
        return (output.Text,output.Spans);
    }
    private static readonly Regex UrlStart=new(@"\A(?:https?://|[/?])",RegexOptions.CultureInvariant|RegexOptions.IgnoreCase);
    /// <summary>Validates the whole URL, not just a URL prefix in captured prose. A literal data-URI query value owns its payload's
    /// delimiters: only those values are masked for the lexical check.</summary>
    private static bool IsUrlValue(string value)
    {
        if(Full(ExactUrl,value)) return true;
        if(!UrlStart.IsMatch(value)) return false;
        var (target,hashmark,fragment)=Partition(value,'#');
        var (path,question,rawQuery)=Partition(target,'?');
        if(!question) return false;
        var pairs=rawQuery.Split('&').Select(pair=>{
            var (rawName,equals,rawValue)=Partition(pair,'=');
            if(equals&&DataUri(UnquotePlus(rawValue))!=null) rawValue=DataOther;
            return rawName+(equals?"=":"")+rawValue;
        });
        string candidate=path+"?"+string.Join('&',pairs)+(hashmark?"#"+fragment:"");
        return candidate!=value&&Full(ExactUrl,candidate);
    }
}
