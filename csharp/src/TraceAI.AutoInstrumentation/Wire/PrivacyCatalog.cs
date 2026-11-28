using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>S6 recognition contract loaded from the byte-identical shipping catalog.
/// Transport discovery and the detached export boundary use these same predicates.</summary>
internal static partial class PrivacyCatalog
{
    private static readonly JsonNode Catalog=Load();
    private static JsonNode Load()
    {
        using var stream=typeof(PrivacyCatalog).Assembly.GetManifestResourceStream("TraceAI.AutoInstrumentation.Wire.privacy.json")
            ??throw new InvalidDataException("Privacy catalog unavailable");
        return JsonNode.Parse(stream)!;
    }
    private static readonly JsonNode Credentials=Catalog["credentials"]!, Media=Catalog["media"]!, Mime=Media["mime_siblings"]!, Audio=Media["audio"]!;
    private static string[] Strings(JsonNode node)=>node.AsArray().Select(n=>n!.GetValue<string>()).ToArray();
    private static readonly string[] Patterns=Credentials["patterns"]!.AsArray().Select(n=>n!["contains"]!.GetValue<string>()).ToArray();
    // Exclusions are read with Java's Pattern semantics: '.' matches no line terminator (LF, CR, U+0085, U+2028, U+2029).
    private static readonly (Regex Rule,string[] Guards)[] Exclusions=Credentials["exclusions"]!.AsArray().Select(n=>(Rx(@"\A(?:"+n!["regex"]!.GetValue<string>().Replace(".","[^\n\r\u0085\u2028\u2029]")+@")\z"),
        n["unless_contains_patterns"] is {} guards?Strings(guards).Select(id=>Credentials["patterns"]!.AsArray().First(p=>p!["id"]!.GetValue<string>()==id)!["contains"]!.GetValue<string>()).ToArray():Array.Empty<string>())).ToArray();
    private static readonly Regex[] ContextExclusions=Credentials["contextual_exclusions"]!.AsArray().Select(n=>Rx(n!["path_regex"]!.GetValue<string>())).ToArray();
    // Credential-shaped values are redacted wherever they appear in exported text,
    // independent of names. They never exempt anything.
    // The jwt shape is scanned by JwtScan.Replace (exactly equivalent to its catalog regex, in time linear in the text); every other
    // shape runs as its catalog regex. A jwt regex that differs from the one JwtScan implements runs as a regex.
    private const string JwtRegex=@"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}";
    // Each shape gives the spans it matches, left to right and not overlapping, as Regex.Matches reads them.
    private static readonly Func<string,List<(int Start,int End)>>[] ValueShapes=Credentials["value_shapes"]!.AsArray().Select<JsonNode?,Func<string,List<(int Start,int End)>>>(n=>{
        string pattern=n!["regex"]!.GetValue<string>(), flags=n["flags"]!.GetValue<string>();
        if(flags=="" && n["id"]?.GetValue<string>()=="jwt" && pattern==JwtRegex) return JwtScan.Spans;
        Regex rule=flags switch {
            ""=>Rx(ShapePattern(pattern,false)),
            "i"=>new Regex(ShapePattern(pattern,true),RegexOptions.CultureInvariant|RegexOptions.IgnoreCase,TimeSpan.FromMilliseconds(250)),
            _=>throw new InvalidDataException("Unsupported value shape flags")
        };
        return v=>rule.Matches(v).Select(m=>(m.Index,m.Index+m.Length)).ToList();
    }).ToArray();
    private static readonly HashSet<string> Signed=Strings(Catalog["signed_url"]!["parameters"]!).Select(Normalize).ToHashSet(StringComparer.Ordinal);
    private static readonly HashSet<char> Alphabet=Media["base64_alphabet"]!.GetValue<string>().ToHashSet();
    private static readonly HashSet<string> Unrecognized=Strings(Media["unrecognized_base64_fields"]!).ToHashSet(StringComparer.Ordinal);
    private static readonly HashSet<string> MimeNames=Strings(Mime["names"]!).ToHashSet(StringComparer.Ordinal), MimePayload=Strings(Mime["payload_fields"]!).ToHashSet(StringComparer.Ordinal);
    private static readonly string[] MimePrefixes=Strings(Mime["prefixes"]!), MimeExact=Strings(Mime["exact"]!);
    private static readonly HashSet<string> AudioFields=Strings(Audio["fields"]!).ToHashSet(StringComparer.Ordinal), AudioPayload=Strings(Audio["payload_fields"]!).ToHashSet(StringComparer.Ordinal),
        AudioTypes=Strings(Audio["type_names"]!).ToHashSet(StringComparer.Ordinal), Metadata=Strings(Audio["metadata_fields"]!).ToHashSet(StringComparer.Ordinal);
    private sealed record MagicTest(int Offset,byte[] Bytes,byte[] Mask);
    private static readonly MagicTest[][] Magic=Media["magic"]!.AsArray().Select(rule=>rule!["tests"]!.AsArray().Select(test=>{
        var bytes=Convert.FromHexString(test!["hex"]!.GetValue<string>());
        return new MagicTest(test["offset"]!.GetValue<int>(),bytes,test["mask"] is {} mask?Convert.FromHexString(mask.GetValue<string>()):Enumerable.Repeat((byte)255,bytes.Length).ToArray());
    }).ToArray()).ToArray();
    internal static readonly int MaxDepth=Catalog["limits"]!["max_depth"]!.GetValue<int>(), MaxNodes=Catalog["limits"]!["max_nodes"]!.GetValue<int>(), MaxText=Catalog["limits"]!["max_text_chars"]!.GetValue<int>();
    private static readonly int PrefixBytes=Media["base64_prefix_decoded_bytes"]!.GetValue<int>(), Threshold=Media["unrecognized_base64_min_chars_exclusive"]!.GetValue<int>();
    internal static readonly string Redacted=Credentials["replacement"]!.GetValue<string>(), OtherMedia=Media["other_replacement"]!.GetValue<string>();
    private static readonly string DataOther=Media["data_uri"]!["other_replacement"]!.GetValue<string>();
    /// <summary>A catalog value shape as the reference's regular expressions read it: \s is the reference's whitespace (U+001C-U+001F
    /// included), \b is the reference's word boundary (PythonWord: Python's word characters, not .NET's), and under the i flag the letters s, i and k also match the characters that fold to them (U+017F, U+0130, U+0131,
    /// U+212A). Each change only widens what a shape matches.</summary>
    internal static string ShapePattern(string pattern,bool ignoreCase)
    {
        var result=new StringBuilder(pattern.Length+32); bool inClass=false,negated=false;
        for(int k=0;k<pattern.Length;k++)
        {
            char c=pattern[k];
            if(c=='\\'&&k+1<pattern.Length)
            {
                char next=pattern[++k];
                if(next=='s') result.Append(inClass?PrivacyText.SpaceClass:"["+PrivacyText.SpaceClass+"]");
                // The catalog's \b opens a shape before a word character (sk-, AKIA, eyJ, Bearer) or closes one after a word
                // character class: there the reference's boundary is "no word character before" or "none after", written so that
                // the engine still finds the shape's first letter before it reads the assertion.
                else if(next=='b'&&!inClass) result.Append(k==1?"\uFFFF":k==pattern.Length-1?PythonWord.NotBeforeWord:PythonWord.Boundary);
                else result.Append(c).Append(next);
                continue;
            }
            if(c=='['&&!inClass)
            {
                inClass=true; negated=k+1<pattern.Length&&pattern[k+1]=='^'; result.Append(c);
                if(negated) result.Append(pattern[++k]);
                // At the start of the class, where no '-' before them can make a range.
                else if(ignoreCase) result.Append("\\u017F\\u0130\\u0131\\u212A");
                continue;
            }
            if(c==']'&&inClass) { inClass=false; result.Append(c); continue; }
            if(ignoreCase&&!inClass&&char.ToLowerInvariant(c) is 's' or 'i' or 'k')
            {
                result.Append(char.ToLowerInvariant(c) switch { 's'=>"[sS\\u017F]", 'i'=>"[iI\\u0130\\u0131]", _=>"[kK\\u212A]" });
                continue;
            }
            result.Append(c);
        }
        return LeadingBoundary(result.ToString());
    }
    /// <summary>A shape that opens with \b (marked U+FFFF) and a word character: its first atom (a character, a class or a group) is
    /// matched first and then checked to start after no word character, so the engine searches for the atom, not the assertion.</summary>
    private static string LeadingBoundary(string pattern)
    {
        if(pattern.Length==0||pattern[0]!='\uFFFF') return pattern;
        int end=1, depth=0; bool inClass=false;
        do
        {
            char c=pattern[end];
            if(c=='\\') end++;
            else if(inClass) inClass=c!=']';
            else if(c=='[') inClass=true;
            else if(c=='(') depth++;
            else if(c==')') depth--;
            end++;
        } while(depth>0||inClass);
        string atom=pattern[1..end];
        return atom+PythonWord.NotAfterWordBefore(atom)+pattern[end..];
    }
    private static Regex Rx(string pattern)=>new(pattern,RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(250));
    private const string OmissionPattern=@"<omitted: (?:[0-9]+ base64 chars|media|data URI)>";
    private static readonly Regex Omission=Rx(@"\A"+OmissionPattern+@"\z"), SplitPath=Rx(@"[.\[\]]+");
    // Whitespace in these patterns is the reference's (PrivacyText.SpaceClass); the URL and data patterns ignore case throughout.
    private const string DataPattern="(?<![A-Za-z0-9+.\\-_:])data:[^"+PrivacyText.SpaceClass+"\"'<>\\\\]*(?:[\\r\\n]+[A-Za-z0-9+/_=\\-]+)*";
    private const string UrlPattern="(?:https?://|(?<![^"+PrivacyText.SpaceClass+"])[/?])(?:<redacted>|"+OmissionPattern+"|[^"+PrivacyText.SpaceClass+"\"<>\\\\])+";
    private static Regex Ri(string pattern)=>new(pattern,RegexOptions.CultureInvariant|RegexOptions.IgnoreCase,TimeSpan.FromSeconds(2));
    private static readonly Regex Data=Ri(DataPattern), Url=Ri(UrlPattern), ExactUrl=Ri(@"\A(?:"+UrlPattern+@")\z"),
        TextTokens=Ri("(?<url>"+UrlPattern+")|(?<data>"+DataPattern+")");
    internal static string Normalize(string name)
    {
        var result=new StringBuilder(name.Length);
        foreach(char c in name) if(c is not ('-' or '_' or '.')) result.Append(c is >= 'A' and <= 'Z'?(char)(c+32):c);
        return result.ToString();
    }
    private static bool Numeric(string part)=>PrivacyText.Decimal(part);
    private static string[] Parts(string path,bool indices=true)=>SplitPath.Split(path).Where(p=>p.Length>0&&(indices||!Numeric(p))).ToArray();
    internal static string Leaf(string path)=>Normalize(Parts(path,false).LastOrDefault()??"");
    private static bool Full(Regex rule,string name) { var m=rule.Match(name); return m.Success&&m.Length==name.Length; }
    private static bool Excluded(string name)=>Exclusions.Any(e=>e.Rule.IsMatch(name)&&!e.Guards.Any(name.Contains));
    /// <summary>The catalog's name rule: the patterns, exclusions and vocabulary words per path component (an excluded component is
    /// not tested), then on the whole name. A name of more than max_depth components is not read (the whole value is replaced).</summary>
    internal static bool CredentialName(string name)
    {
        PathBudget(name);
        var parts=Parts(name,false); bool excluded=false; var context=new StringBuilder();
        foreach(string part in parts)
        {
            string folded=Normalize(part);
            if(context.Length>0) context.Append('.');
            context.Append(folded);
            if(Excluded(folded)||ContextExclusions.Any(r=>Full(r,context.ToString()))) excluded=true;
            else if(Patterns.Any(folded.Contains)||Vocabulary(part)) return true;
        }
        if(excluded) return false;
        string full=Normalize(name);
        return !Excluded(full)&&(Patterns.Any(full.Contains)||Vocabulary(name));
    }
    internal static bool QueryName(string name)=>Signed.Contains(Normalize(name))||CredentialName(name);
    private static bool Encoded(string value)=>value.Length>0&&value.All(Alphabet.Contains);
    private static string Omitted(int count)=>Media["replacement"]!.GetValue<string>().Replace("{length}",count.ToString(System.Globalization.CultureInfo.InvariantCulture),StringComparison.Ordinal);
    private static bool HasMagic(string value)
    {
        if(!Encoded(value)) return false;
        // Only decode the catalog's prefix budget, even for multi-megabyte media.
        int chars=(PrefixBytes*8+5)/6;
        var prefix=new StringBuilder(chars+3);
        foreach(char c in value)
        {
            if(c is '\r' or '\n') continue;
            prefix.Append(c=='-'?'+':c=='_'?'/':c);
            if(prefix.Length==chars) break;
        }
        // The reference pads the prefix to a multiple of four and accepts padding already in it: decode the characters before a
        // trailing '=' run, padded again.
        int data=prefix.Length; while(data>0&&prefix[data-1]=='=') data--;
        if(data<prefix.Length) prefix.Length=data;
        while(prefix.Length%4!=0) prefix.Append('=');
        byte[] decoded;
        try { decoded=Convert.FromBase64String(prefix.ToString()); } catch(FormatException) { return false; }
        int length=Math.Min(decoded.Length,PrefixBytes);
        return Magic.Any(rule=>rule.All(test=>test.Offset+test.Bytes.Length<=length&&Enumerable.Range(0,test.Bytes.Length).All(i=>(decoded[test.Offset+i]&test.Mask[i])==(test.Bytes[i]&test.Mask[i]))));
    }
    internal readonly record struct Hint(bool Media,bool Audio);
    private static Hint GetHint(string name,object? value,Budget budget)
    {
        string leaf=Leaf(name);
        if(!MimeNames.Contains(leaf)&&leaf!="type") return default;
        if(value is JsonValue json) value=WireCore.S(json);
        if(value is JsonArray array)
        {
            if(array.Count>MaxNodes) throw new InvalidDataException("Privacy hint budget");
            return HintStrings(leaf,array.Select(v=>{ budget.Node(0); return WireCore.S(v); }).OfType<string>(),budget);
        }
        if(value is Array values)
        {
            if(values.Length>MaxNodes) throw new InvalidDataException("Privacy hint budget");
            return HintStrings(leaf,values.Cast<object?>().Select(v=>{ budget.Node(0); return v as string; }).OfType<string>(),budget);
        }
        return value is string text?ScalarHint(leaf,text,budget):default;
    }
    private static Hint HintStrings(string leaf,IEnumerable<string> values,Budget budget)=>Combine(values.Select(text=>ScalarHint(leaf,text,budget)));
    private static Hint ScalarHint(string leaf,string text,Budget budget)
    {
        budget.Text(text.Length);
        if(text.Length>MaxText) throw new InvalidDataException("Privacy hint text budget");
        string mime=text.Split(';',2)[0].Trim().ToLowerInvariant();
        if(MimeNames.Contains(leaf)) return new(MimePrefixes.Any(p=>mime.StartsWith(p,StringComparison.Ordinal))||MimeExact.Contains(mime),mime.StartsWith("audio/",StringComparison.Ordinal));
        return new(false,leaf=="type"&&AudioTypes.Contains(Normalize(text)));
    }
    private static Hint Combine(IEnumerable<Hint> hints) { bool media=false,audio=false; foreach(var hint in hints) { media|=hint.Media; audio|=hint.Audio; } return new(media,audio); }
    internal static Dictionary<string,Hint> IndexHints(IEnumerable<KeyValuePair<string,object?>> items,string path="",Dictionary<string,Hint>? inherited=null)
    {
        var hints=inherited==null?new Dictionary<string,Hint>(StringComparer.Ordinal):new(inherited,StringComparer.Ordinal);
        var budget=new Budget();
        foreach(var pair in items)
        {
            budget.Node(0); budget.Text(pair.Key.Length);
            string fullName=path.Length>0?path+"."+pair.Key:pair.Key;
            // The hint reads the leaf of the full name through the budgeted path parser.
            if(HoldsString(pair.Value)) PathBudget(fullName);
            var hint=GetHint(pair.Key,pair.Value,budget); if(!hint.Media&&!hint.Audio) continue;
            var parts=Parts(fullName).ToList();
            while(parts.Count>0&&Numeric(parts[^1])) parts.RemoveAt(parts.Count-1);
            if(parts.Count>0) parts.RemoveAt(parts.Count-1);
            string parent=string.Join('.',parts); hints.TryGetValue(parent,out var previous);
            hints[parent]=new(previous.Media||hint.Media,previous.Audio||hint.Audio);
        }
        return hints;
    }
    private static bool HoldsString(object? value)=>value switch {
        string=>true,
        JsonValue json=>WireCore.S(json)!=null,
        JsonArray array=>array.Any(v=>WireCore.S(v)!=null),
        Array values=>values.Cast<object?>().Any(v=>v is string),
        _=>false
    };
    internal static (bool Media,bool Audio,bool Payload) Context(string path,Dictionary<string,Hint>? hints=null,bool media=false,bool audio=false)
    {
        hints??=[]; hints.TryGetValue("",out var root); media|=root.Media; audio|=root.Audio;
        bool payload=false,direct=false; var prefix=new List<string>();
        foreach(string part in Parts(path))
        {
            prefix.Add(part); string name=Normalize(part);
            if(!Numeric(part))
            {
                if(Metadata.Contains(name)) { media=false; audio=false; payload=false; }
                direct=AudioFields.Contains(name); audio|=direct;
                if(media&&MimePayload.Contains(name)||audio&&AudioPayload.Contains(name)) payload=true;
            }
            if(hints.TryGetValue(string.Join('.',prefix),out var hint)) { media|=hint.Media; audio|=hint.Audio; }
        }
        return (media,audio,payload||direct);
    }
    internal static string? DataUri(string value)
    {
        if(!value.StartsWith("data:",StringComparison.OrdinalIgnoreCase)) return null;
        int comma=value.IndexOf(','); if(comma<0) return null;
        string metadata=value[5..comma],payload=Uri.UnescapeDataString(value[(comma+1)..]);
        return metadata.EndsWith(";base64",StringComparison.OrdinalIgnoreCase)&&(payload.Length==0||Encoded(payload))?Omitted(payload.Length):DataOther;
    }
    // The only media predicate; path context, magic bytes and exact data URI boundaries
    // are independent rules. Stable omission markers never count as payload bytes.
    internal static string? MediaString(string value,string path="",Dictionary<string,Hint>? hints=null,bool framed=true,bool media=false,bool audio=false)
    {
        if(value==Redacted||value==FIInstrumentation.TraceConfig.RedactedValue||Full(Omission,value)) return null;
        if(framed&&DataUri(value) is {} data) return data;
        bool encoded=Encoded(value);
        if(HasMagic(value)||Context(path,hints,media,audio).Payload) return encoded?Omitted(value.Length):OtherMedia;
        return encoded&&Unrecognized.Contains(Leaf(path))&&value.Length>Threshold?Omitted(value.Length):null;
    }
    /// <summary>Only free text needs delimiter-limited, nonrecursive data URI scanning; a framed value that is one data URI is
    /// replaced whole.</summary>
    private static string DataText(string value,bool framed=false)
    {
        if(framed&&DataUri(value) is {} replacement) return replacement;
        return Substitute(Data,value,false,(original,_,_)=>DataUri(original)??original);
    }
    internal static string DecodeQuery(string value)=>Uri.UnescapeDataString(value.Replace('+',' '));
    internal readonly record struct QueryPair(string Name,string Separator,string Value,string DecodedName,string DecodedValue);
    internal static QueryPair[] QueryPairs(string url)
    {
        string target=url.Split('#',2)[0]; int question=target.IndexOf('?'); if(question<0) return [];
        return target[(question+1)..].Split('&').Select(pair=>{
            int equals=pair.IndexOf('='); string name=equals<0?pair:pair[..equals],value=equals<0?"":pair[(equals+1)..];
            return new QueryPair(name,equals<0?"":"=",value,DecodeQuery(name),DecodeQuery(value));
        }).ToArray();
    }
    /// <summary>The export text rules: the URL catalog and data URIs, known secrets, value shapes and the credential pair rule, in
    /// that order. A pass that does not replace known secrets (shapes: false) runs neither the shapes nor the pair rule on text
    /// that may still hold one. structured: the text is the re-dumped JSON of a structured string, whose leaves each went through
    /// the pair rule on their own, so the rule is not run over it again.</summary>
    internal static string Text(string value,IEnumerable<string>? secrets=null,bool framed=true,bool shapes=true,bool structured=false)
    {
        if(framed&&IsUrlValue(value))
        {
            // A whole URL value: the catalog pass records by position each span it replaced; known secrets and value shapes move
            // those spans (a span one of them overlaps is gone), and the pair rule then runs over the URL text, skipping only the
            // spans still recorded. The skip never looks at placeholder text.
            var (url,spans)=FramedUrl(value);
            var (runs,fragments)=SecretRuns(url,secrets);
            if(!shapes) return ReplaceRuns(url,Union(runs,fragments),spans).Value;
            (url,spans)=ReplaceRuns(url,runs,spans); fragments=Move(fragments,Placeholders(runs));
            foreach(var shape in ValueShapes) { var matched=shape(url); (url,spans)=ReplaceRuns(url,matched,spans); fragments=Move(fragments,Placeholders(matched)); }
            return Fragments(url,fragments,spans);
        }
        string source=value;
        value=Substitute(TextTokens,value,true,(original,url,start)=>{
            if(!url) return DataText(original);
            // Retain enclosing single quotes in prose without treating internal apostrophes as delimiters.
            string quote="";
            if(start>0&&source[start-1]=='\''&&original.EndsWith('\'')) { original=original[..^1]; quote="'"; }
            return FramedUrl(original).Text+quote;
        });
        // Known secrets are replaced first, then the credential value shapes on the result, then the pair rule, in this one call.
        if(!shapes) return ReplaceSecrets(value,secrets);
        var (secretRuns,secretFragments)=SecretRuns(value,secrets);
        value=ReplaceRuns(value,secretRuns,[]).Value; secretFragments=Move(secretFragments,Placeholders(secretRuns));
        foreach(var shape in ValueShapes) { var matched=shape(value); value=ReplaceRuns(value,matched,[]).Value; secretFragments=Move(secretFragments,Placeholders(matched)); }
        if(structured) return ReplaceRuns(value,secretFragments,[]).Value;
        return Fragments(value,secretFragments,null);
    }
    /// <summary>The pair rule over text whose known secrets were applied as the reference applies them, then the fragments of
    /// overlapping secrets that the pair rule left are replaced.</summary>
    private static string Fragments(string value,List<(int Start,int End)> fragments,List<Span>? spans)
    {
        if(fragments.Count==0) return CredentialPairs(value,spans);
        var rewrites=new List<(int Start,int End,int Written)>();
        value=CredentialPairs(value,spans,rewrites);
        return ReplaceRuns(value,Move(fragments,rewrites),[]).Value;
    }
    /// <summary>Known secrets as the reference applies them: one at a time, longest first (then by code point), each occurrence of a
    /// secret in the text the earlier secrets left (never across a placeholder) replaced left to right. Runs: the spans replaced, by
    /// position, sorted and disjoint. Fragments: the parts of an occurrence that an earlier, overlapping secret left visible; the
    /// reference keeps them, and this export replaces them after the pair rule has read the text (no secret character survives).</summary>
    private static (List<(int Start,int End)> Runs,List<(int Start,int End)> Fragments) SecretRuns(string value,IEnumerable<string>? secrets)
    {
        var runs=new List<(int Start,int End)>(); var fragments=new List<(int Start,int End)>();
        if(secrets==null||value.Length<8) return (runs,fragments);
        // A registry snapshot is an immutable array shared by every value of an export batch: prepare it once, not per value.
        var known=secrets is string[] snapshot?Prepared.GetValue(snapshot,Prepare):Prepare(secrets.ToArray());
        if(known.Length==0) return (runs,fragments);
        int offset=0;
        foreach(string part in value.Split(Redacted,StringSplitOptions.None))
        {
            var (partRuns,partFragments)=PartRuns(part,known);
            foreach(var (start,end) in partRuns) runs.Add((offset+start,offset+end));
            foreach(var (start,end) in partFragments) fragments.Add((offset+start,offset+end));
            offset+=part.Length+Redacted.Length;
        }
        return (runs,fragments);
    }
    /// <summary>Replaces every known secret (eight or more characters) as SecretRuns reads them, and the fragments with them. Text
    /// already replaced is left as it is and no secret matches across a placeholder.</summary>
    internal static string ReplaceSecrets(string value,IEnumerable<string>? secrets)
    {
        var (runs,fragments)=SecretRuns(value,secrets);
        return ReplaceRuns(value,Union(runs,fragments),[]).Value;
    }
    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<string[],string[]> Prepared=new();
    private static string[] Prepare(string[] secrets)
    {
        var known=secrets.Where(s=>s.Length>=8&&s!=Redacted).Distinct(StringComparer.Ordinal).ToArray();
        Array.Sort(known,(a,b)=>a.Length!=b.Length?b.Length.CompareTo(a.Length):CodePointOrder(a,b));
        return known;
    }
    /// <summary>Python's string order: by code point (an ordinal UTF-16 compare differs only above U+D7FF).</summary>
    private static int CodePointOrder(string a,string b)
    {
        var x=a.EnumerateRunes().GetEnumerator(); var y=b.EnumerateRunes().GetEnumerator();
        while(true)
        {
            bool more=x.MoveNext(), other=y.MoveNext();
            if(!more||!other) return more==other?0:more?1:-1;
            int c=x.Current.Value.CompareTo(y.Current.Value); if(c!=0) return c;
        }
    }
    private static (List<(int Start,int End)> Runs,List<(int Start,int End)> Fragments) PartRuns(string part,string[] known)
    {
        var runs=new List<(int Start,int End)>();
        foreach(var secret in known)
        {
            if(part.Length<secret.Length||part.IndexOf(secret,StringComparison.Ordinal)<0) continue;
            var added=new List<(int Start,int End)>(); int segment=0;
            for(int k=0;k<=runs.Count;k++)
            {
                int segmentEnd=k<runs.Count?runs[k].Start:part.Length;
                for(int at=segment;segmentEnd-at>=secret.Length;)
                {
                    int found=part.IndexOf(secret,at,segmentEnd-at,StringComparison.Ordinal);
                    if(found<0) break;
                    added.Add((found,found+secret.Length)); at=found+secret.Length;
                }
                if(k<runs.Count) segment=runs[k].End;
            }
            if(added.Count>0) { runs.AddRange(added); runs.Sort(); }
        }
        var fragments=new List<(int Start,int End)>();
        if(runs.Count==0) return (runs,fragments);
        int r=0;
        foreach(var (start,end) in SecretSpans(part,known))
        {
            int position=start;
            while(r<runs.Count&&runs[r].End<=position) r++;
            for(int q=r;q<runs.Count&&runs[q].Start<end;q++)
            {
                if(runs[q].Start>position) fragments.Add((position,runs[q].Start));
                position=Math.Max(position,runs[q].End);
            }
            if(position<end) fragments.Add((position,end));
        }
        return (runs,fragments);
    }
    /// <summary>Every occurrence of every known secret in part, overlapping occurrences merged into one span, sorted.</summary>
    private static List<(int Start,int End)> SecretSpans(string part,string[] known)
    {
        var spans=new List<(int Start,int End)>();
        foreach(var secret in known)
            for(int at=part.IndexOf(secret,StringComparison.Ordinal);at>=0;at=at+1<part.Length?part.IndexOf(secret,at+1,StringComparison.Ordinal):-1)
                spans.Add((at,at+secret.Length));
        if(spans.Count<2) return spans;
        spans.Sort((a,b)=>a.Start!=b.Start?a.Start.CompareTo(b.Start):b.End.CompareTo(a.End));
        var merged=new List<(int Start,int End)>();
        int start=spans[0].Start,end=spans[0].End;
        for(int i=1;i<=spans.Count;i++)
        {
            if(i<spans.Count&&spans[i].Start<end) { end=Math.Max(end,spans[i].End); continue; }
            merged.Add((start,end));
            if(i<spans.Count) { start=spans[i].Start; end=spans[i].End; }
        }
        return merged;
    }
    /// <summary>Two sorted lists of disjoint spans as one sorted list (spans of the two never overlap).</summary>
    private static List<(int Start,int End)> Union(List<(int Start,int End)> a,List<(int Start,int End)> b)
    {
        if(b.Count==0) return a;
        var all=new List<(int Start,int End)>(a.Count+b.Count); all.AddRange(a); all.AddRange(b); all.Sort();
        return all;
    }
    /// <summary>Where the spans are after the text was rewritten: each (start, end, written) of rewrites (sorted, disjoint, in the
    /// old text) became written characters. A span's characters inside a rewrite are gone; the rest of it moves.</summary>
    private static List<(int Start,int End)> Move(List<(int Start,int End)> spans,IReadOnlyList<(int Start,int End,int Written)> rewrites)
    {
        if(spans.Count==0||rewrites.Count==0) return spans;
        var moved=new List<(int Start,int End)>(); int r=0,shift=0;
        foreach(var (start,end) in spans)
        {
            int position=start;
            while(position<end)
            {
                while(r<rewrites.Count&&rewrites[r].End<=position) { shift+=rewrites[r].Written-(rewrites[r].End-rewrites[r].Start); r++; }
                if(r<rewrites.Count&&rewrites[r].Start<=position) { position=rewrites[r].End; continue; }
                int stop=r<rewrites.Count?Math.Min(end,rewrites[r].Start):end;
                moved.Add((position+shift,stop+shift)); position=stop;
            }
        }
        return moved;
    }
    private static List<(int Start,int End,int Written)> Placeholders(List<(int Start,int End)> runs)=>runs.Select(run=>(run.Start,run.End,Redacted.Length)).ToList();
    internal sealed class Budget
    {
        private int nodes=MaxNodes,characters=MaxText;
        internal void Node(int depth) { if(depth>MaxDepth||--nodes<0) throw new InvalidDataException("Privacy structure budget"); }
        internal void Text(int length) { if((characters-=length)<0) throw new InvalidDataException("Privacy text budget"); }
    }
    internal static JsonNode? Scrub(JsonNode? value,string path="",Dictionary<string,Hint>? hints=null,IEnumerable<string>? secrets=null,bool framed=true,Budget? budget=null,int depth=0,bool media=false,bool audio=false,bool shapes=true)
    {
        budget??=new(); budget.Node(depth);
        if(CredentialName(path)) return JsonValue.Create(Redacted);
        if(value is JsonObject obj)
        {
            budget.Text(obj.Sum(p=>p.Key.Length));
            hints=IndexHints(obj.Select(p=>new KeyValuePair<string,object?>(p.Key,p.Value)),path,hints);
            var result=new JsonObject();
            foreach(var pair in obj)
            {
                string child=path.Length>0?path+"."+pair.Key:pair.Key;
                // Preserve the pre-S6 embedding-vector export policy. This is not
                // a credential/media recognition list and never affects harvesting.
                var cleaned=pair.Key.ToLowerInvariant() is "embedding" or "embeddings" && pair.Value is JsonObject or JsonArray
                    ?JsonValue.Create(Redacted):Scrub(pair.Value,child,hints,secrets,true,budget,depth+1,media,audio,shapes);
                string key=MapKey(pair.Key,secrets,shapes);
                // Two keys that are one after scrubbing: one key is kept, in the first one's place, and its value is the placeholder.
                result[key]=result.ContainsKey(key)?JsonValue.Create(Redacted):cleaned;
            }
            return result;
        }
        var context=Context(path,hints,media,audio);
        if(value is JsonArray array)
        {
            if(context.Payload&&array.Count>0&&array.All(IsNumberOrBool)) return JsonValue.Create(OtherMedia);
            var result=new JsonArray();
            for(int i=0;i<array.Count;i++) result.Add(Scrub(array[i],path+"."+i,hints,secrets,true,budget,depth+1,media,audio,shapes));
            return result;
        }
        if(WireCore.S(value) is not {} text) return value!=null&&context.Payload?JsonValue.Create(OtherMedia):value?.DeepClone();
        budget.Text(text.Length);
        return JsonValue.Create(String(text,path,hints,secrets,framed,budget,depth,media,audio,shapes));
    }
    /// <summary>A map key goes through the export rules of a string value at a framed site that need no context: a whole data URI
    /// or magic-byte media value, then the URL catalog and data URIs, known secrets, value shapes and the pair rule.</summary>
    internal static string MapKey(string name,IEnumerable<string>? secrets,bool shapes=true)=>MediaString(name,"",null,true) ?? Text(name,secrets,true,shapes);
    private static bool IsNumberOrBool(JsonNode? value)=>value is JsonValue scalar&&(scalar.GetValueKind() is System.Text.Json.JsonValueKind.Number or System.Text.Json.JsonValueKind.True or System.Text.Json.JsonValueKind.False);
    internal static string String(string value,string path="",Dictionary<string,Hint>? hints=null,IEnumerable<string>? secrets=null,bool framed=true,Budget? budget=null,int depth=0,bool media=false,bool audio=false,bool shapes=true)
    {
        if(CredentialName(path)) return Redacted;
        if(MediaString(value,path,hints,framed,media,audio) is {} replacement) return replacement;
        string trimmed=PrivacyText.TrimStart(value); bool structured=false;
        if(trimmed.Length>0&&trimmed[0] is '{' or '[')
        {
            // Invalid/deep serialized JSON fails closed at S1 rather than being treated as prose.
            var parsed=JsonNode.Parse(value); var context=Context(path,hints,media,audio);
            structured=parsed is JsonObject or JsonArray;
            var clean=Scrub(parsed,"",null,secrets,true,budget,depth+1,context.Media,context.Audio,shapes);
            // Re-dumped as the reference writes JSON text; each leaf went through the pair rule on its own.
            if(!JsonNode.DeepEquals(parsed,clean)) value=PrivacyText.Dumps(clean);
        }
        return Text(value,secrets,framed,shapes,structured);
    }
}
