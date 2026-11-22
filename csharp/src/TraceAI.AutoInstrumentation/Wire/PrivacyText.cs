using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>Character classes, percent decoding and JSON text as the shared privacy reference reads them
/// (spec/auto-instrumentation/generate_privacy_fixtures.py). Positions are UTF-16 units.</summary>
internal static class PrivacyText
{
    /// <summary>Whitespace as the reference reads it (str.isspace): U+0009-U+000D, U+001C-U+0020, U+0085, U+00A0, U+1680,
    /// U+2000-U+200A, U+2028, U+2029, U+202F, U+205F and U+3000.</summary>
    internal static bool Space(char c)=>c is (>= '\t' and <= '\r') or (>= '\x1c' and <= ' ') or '\x85' or '\xa0' or '\u1680'
        or (>= '\u2000' and <= '\u200a') or '\u2028' or '\u2029' or '\u202f' or '\u205f' or '\u3000';
    /// <summary>The same set as a regular expression class body, for the patterns that read whitespace.</summary>
    internal const string SpaceClass="\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
    /// <summary>Java's Character.isWhitespace: U+0009-U+000D, U+001C-U+001F and every space, line or paragraph separator except
    /// the no-break spaces U+00A0, U+2007 and U+202F.</summary>
    internal static bool JavaSpace(char c)
    {
        if(c is (>= '\t' and <= '\r') or (>= '\x1c' and <= '\x1f')) return true;
        if(c is '\xa0' or '\u2007' or '\u202f') return false;
        return CharUnicodeInfo.GetUnicodeCategory(c) is UnicodeCategory.SpaceSeparator or UnicodeCategory.LineSeparator or UnicodeCategory.ParagraphSeparator;
    }
    internal static bool Hex(char c)=>c is (>= '0' and <= '9') or (>= 'a' and <= 'f') or (>= 'A' and <= 'F');
    internal static bool StartsAt(string text,int at,string value)=>at>=0&&at+value.Length<=text.Length&&string.CompareOrdinal(text,at,value,0,value.Length)==0;
    /// <summary>The last index of value that lies wholly inside [start, end), or -1.</summary>
    internal static int LastIndex(string text,string value,int start,int end)
    {
        for(int p=Math.Min(end,text.Length)-value.Length;p>=Math.Max(start,0);p--)
            if(string.CompareOrdinal(text,p,value,0,value.Length)==0) return p;
        return -1;
    }
    internal static int LastIndex(string text,char value,int start,int end)
    {
        for(int p=Math.Min(end,text.Length)-1;p>=Math.Max(start,0);p--) if(text[p]==value) return p;
        return -1;
    }
    internal static int Index(string text,char value,int start,int end)
    {
        end=Math.Min(end,text.Length);
        return start>=end?-1:text.IndexOf(value,start,end-start);
    }
    /// <summary>Whether text holds a percent escape (a '%' and two hex digits).</summary>
    internal static bool HasEscape(string text)
    {
        for(int i=text.IndexOf('%');i>=0&&i+2<text.Length;i=text.IndexOf('%',i+1))
            if(Hex(text[i+1])&&Hex(text[i+2])) return true;
        return false;
    }
    /// <summary>Percent-decodes once, as the reference's unquote: each run of ASCII text is decoded to bytes ('%' and two hex digits
    /// is one byte, any other '%' stays) and read as UTF-8 with U+FFFD for an invalid sequence; other characters are kept.</summary>
    internal static string Unquote(string text)
    {
        if(text.IndexOf('%')<0) return text;
        var result=new StringBuilder(text.Length);
        var bytes=new List<byte>();
        int i=0;
        while(i<text.Length)
        {
            if(text[i]>=128) { result.Append(text[i]); i++; continue; }
            bytes.Clear();
            while(i<text.Length&&text[i]<128)
            {
                if(text[i]=='%'&&i+2<text.Length&&Hex(text[i+1])&&Hex(text[i+2])&&text[i+1]<128&&text[i+2]<128)
                { bytes.Add((byte)Convert.ToInt32(text.Substring(i+1,2),16)); i+=3; }
                else { bytes.Add((byte)text[i]); i++; }
            }
            result.Append(Encoding.UTF8.GetString(bytes.ToArray()));
        }
        return result.ToString();
    }
    internal static string UnquotePlus(string text)=>Unquote(text.Replace('+',' '));
    /// <summary>Whether a character of the decoded text is a letter, a number or '_' (str.isalnum or '_').</summary>
    internal static bool AnyWordCharacter(string text)
    {
        foreach(var rune in text.EnumerateRunes()) if(Rune.IsLetter(rune)||Rune.IsNumber(rune)||rune.Value=='_') return true;
        return false;
    }
    internal static bool Decimal(string part)
    {
        if(part.Length==0) return false;
        foreach(char c in part) if(!char.IsDigit(c)) return false;
        return true;
    }
    internal static string TrimStart(string value)
    {
        int i=0; while(i<value.Length&&Space(value[i])) i++;
        return value[i..];
    }
    /// <summary>JSON text as json.dumps(value, ensure_ascii=False, separators=(",", ":")) writes it: only '"', '\' and controls
    /// below U+0020 are escaped; integers keep their digits and other numbers are written as the shortest round trip.</summary>
    internal static string Dumps(JsonNode? node)
    {
        var builder=new StringBuilder();
        Dump(node,builder);
        return builder.ToString();
    }
    private static void Dump(JsonNode? node,StringBuilder builder)
    {
        switch(node)
        {
            case null: builder.Append("null"); return;
            case JsonObject obj:
                builder.Append('{'); bool first=true;
                foreach(var pair in obj) { if(!first) builder.Append(','); first=false; DumpString(pair.Key,builder); builder.Append(':'); Dump(pair.Value,builder); }
                builder.Append('}'); return;
            case JsonArray array:
                builder.Append('[');
                for(int i=0;i<array.Count;i++) { if(i>0) builder.Append(','); Dump(array[i],builder); }
                builder.Append(']'); return;
        }
        var value=(JsonValue)node;
        if(value.TryGetValue<string>(out var text)) { DumpString(text,builder); return; }
        switch(value.GetValueKind())
        {
            case JsonValueKind.String: DumpString(value.GetValue<string>(),builder); return;
            case JsonValueKind.True: builder.Append("true"); return;
            case JsonValueKind.False: builder.Append("false"); return;
            case JsonValueKind.Null: builder.Append("null"); return;
            case JsonValueKind.Number: builder.Append(Number(value.ToJsonString())); return;
            default: builder.Append(value.ToJsonString()); return;
        }
    }
    private static string Number(string raw)
    {
        if(raw.IndexOfAny(['.','e','E'])<0&&BigInteger.TryParse(raw,NumberStyles.AllowLeadingSign,CultureInfo.InvariantCulture,out var integer))
            return integer.ToString(CultureInfo.InvariantCulture);
        double number=double.Parse(raw,NumberStyles.Float,CultureInfo.InvariantCulture);
        if(double.IsPositiveInfinity(number)) return "Infinity";
        if(double.IsNegativeInfinity(number)) return "-Infinity";
        if(double.IsNaN(number)) return "NaN";
        return Repr(number);
    }
    /// <summary>The shortest round-trip text of a double in Python's repr form: fixed notation when the decimal point position is
    /// in -3..16, else d.ddde+XX.</summary>
    internal static string Repr(double number)
    {
        if(number==0) return double.IsNegative(number)?"-0.0":"0.0";
        string shortest=Math.Abs(number).ToString("R",CultureInfo.InvariantCulture);
        string digits; int exponent;
        int e=shortest.IndexOfAny(['E','e']);
        string mantissa=e<0?shortest:shortest[..e];
        int power=e<0?0:int.Parse(shortest[(e+1)..],CultureInfo.InvariantCulture);
        int dot=mantissa.IndexOf('.');
        string whole=dot<0?mantissa:mantissa[..dot], fraction=dot<0?"":mantissa[(dot+1)..];
        digits=(whole+fraction).TrimStart('0');
        int leading=(whole+fraction).Length-(whole+fraction).TrimStart('0').Length;
        // value = 0.digits x 10^decpt
        int decpt=whole.Length+power-leading;
        digits=digits.TrimEnd('0'); if(digits.Length==0) digits="0";
        exponent=decpt;
        var builder=new StringBuilder();
        if(number<0) builder.Append('-');
        if(decpt> -4&&decpt<=16)
        {
            if(decpt<=0) builder.Append("0.").Append('0',-decpt).Append(digits);
            else if(decpt>=digits.Length) builder.Append(digits).Append('0',decpt-digits.Length).Append(".0");
            else builder.Append(digits,0,decpt).Append('.').Append(digits,decpt,digits.Length-decpt);
        }
        else
        {
            builder.Append(digits[0]);
            if(digits.Length>1) builder.Append('.').Append(digits,1,digits.Length-1);
            int x=exponent-1;
            builder.Append('e').Append(x<0?'-':'+').Append(Math.Abs(x).ToString("00",CultureInfo.InvariantCulture));
        }
        return builder.ToString();
    }
    private static void DumpString(string text,StringBuilder builder)
    {
        builder.Append('"');
        foreach(char c in text)
        {
            switch(c)
            {
                case '"': builder.Append("\\\""); break;
                case '\\': builder.Append("\\\\"); break;
                case '\n': builder.Append("\\n"); break;
                case '\r': builder.Append("\\r"); break;
                case '\t': builder.Append("\\t"); break;
                case '\b': builder.Append("\\b"); break;
                case '\f': builder.Append("\\f"); break;
                default:
                    if(c<0x20) builder.Append("\\u").Append(((int)c).ToString("x4",CultureInfo.InvariantCulture));
                    else builder.Append(c);
                    break;
            }
        }
        builder.Append('"');
    }
}
