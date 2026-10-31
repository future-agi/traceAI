using System.Diagnostics;
using System.Globalization;
using System.Net.Http.Headers;
using System.Text;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;

namespace TraceAI.AutoInstrumentation;

/// <summary>
/// OTel 1.15.3's protobuf serializer directly calls DiagnosticSource 10 APIs
/// (ActivitySource.Tags/TelemetrySchemaUrl, Activity.HasRemoteParent). A hook
/// cannot upgrade the application's TPA binding. Keep the patched SDK and shared
/// Activity identity, but encode the stable OTLP trace schema on DS 8/9 instead.
/// Only already-sanitized activities enter this transport. No retries/read-ahead.
/// </summary>
internal sealed class CompatibilityOtlpTransport : IDisposable
{
    private readonly HttpClient _client;
    private readonly Uri _endpoint;
    private readonly int _spanAttributeLimit=SpanAttributeLimit();
    private static int SpanAttributeLimit()
    {
        int? Read(string name)=>int.TryParse(Environment.GetEnvironmentVariable(name),out var value)&&value>=0?value:null;
        return Read("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT")??Read("OTEL_ATTRIBUTE_COUNT_LIMIT")??128;
    }
    internal CompatibilityOtlpTransport(OtlpExporterOptions options)
    {
        _endpoint=options.Endpoint;
        _client=options.HttpClientFactory();
        _client.Timeout=TimeSpan.FromMilliseconds(options.TimeoutMilliseconds);
        foreach(var header in (options.Headers??"").Split(',',StringSplitOptions.RemoveEmptyEntries))
        {
            int split=header.IndexOf('=');
            if(split>0) _client.DefaultRequestHeaders.TryAddWithoutValidation(header[..split].Trim(),Uri.UnescapeDataString(header[(split+1)..].Trim()));
        }
    }
    internal bool Export(Resource resource,Activity[] spans)
    {
        using var request=new HttpRequestMessage(HttpMethod.Post,_endpoint) { Content=new ByteArrayContent(Encode(resource,spans,_spanAttributeLimit)) };
        request.Content.Headers.ContentType=new MediaTypeHeaderValue("application/x-protobuf");
        using var response=_client.Send(request,HttpCompletionOption.ResponseHeadersRead);
        return response.IsSuccessStatusCode;
    }
    internal static byte[] Encode(Resource resource,IEnumerable<Activity> spans,int? spanAttributeLimit=null)
    {
        var resourceSpans=new Proto();
        resourceSpans.Message(1,Attributes(resource.Attributes.Select(p=>new KeyValuePair<string,object?>(p.Key,p.Value)),1));
        foreach(var group in spans.GroupBy(a=>a.Source))
        {
            var scope=new Proto(); var identity=new Proto(); identity.String(1,group.Key.Name); identity.String(2,group.Key.Version);
            if(typeof(ActivitySource).GetProperty("Tags")?.GetValue(group.Key) is IEnumerable<KeyValuePair<string,object?>> tags)
                identity.Append(Attributes(tags,3));
            scope.Message(1,identity);
            scope.String(3,typeof(ActivitySource).GetProperty("TelemetrySchemaUrl")?.GetValue(group.Key) as string);
            foreach(var activity in group) scope.Message(2,Span(activity,spanAttributeLimit??SpanAttributeLimit()));
            resourceSpans.Message(2,scope);
        }
        var request=new Proto(); request.Message(1,resourceSpans); return request.Bytes();
    }
    private static Proto Span(Activity activity,int attributeLimit)
    {
        var span=new Proto();
        span.Data(1,Convert.FromHexString(activity.TraceId.ToHexString()));
        span.Data(2,Convert.FromHexString(activity.SpanId.ToHexString()));
        span.String(3,activity.TraceStateString);
        if(activity.ParentSpanId!=default) span.Data(4,Convert.FromHexString(activity.ParentSpanId.ToHexString()));
        span.String(5,activity.DisplayName); span.Int(6,(ulong)activity.Kind+1);
        span.Fixed(7,Nanos(activity.StartTimeUtc)); span.Fixed(8,Nanos(activity.StartTimeUtc+activity.Duration));
        var attributes=activity.TagObjects.Where(p=>p.Value!=null && p.Key is not "otel.status_code" and not "otel.status_description").ToArray();
        span.Append(Attributes(attributes.Take(attributeLimit),9));
        if(attributes.Length>attributeLimit) span.Int(10,(ulong)(attributes.Length-attributeLimit));
        foreach(var item in activity.Events)
        {
            var e=new Proto(); e.Fixed(1,Nanos(item.Timestamp.UtcDateTime)); e.String(2,item.Name);
            e.Append(Attributes(item.Tags,3)); span.Message(11,e);
        }
        foreach(var item in activity.Links)
        {
            var link=new Proto(); link.Data(1,Convert.FromHexString(item.Context.TraceId.ToHexString()));
            link.Data(2,Convert.FromHexString(item.Context.SpanId.ToHexString())); link.String(3,item.Context.TraceState);
            link.Append(Attributes(item.Tags??[],4));
            link.UInt32(6,(uint)item.Context.TraceFlags|0x100u|(item.Context.IsRemote?0x200u:0)); span.Message(13,link);
        }
        var status=new Proto(); status.String(2,activity.StatusDescription); status.Int(3,(ulong)activity.Status);
        span.Message(15,status); span.UInt32(16,(uint)activity.ActivityTraceFlags);
        return span;
    }
    private static ulong Nanos(DateTime timestamp) => checked((ulong)Math.Max(0,timestamp.ToUniversalTime().Ticks-DateTime.UnixEpoch.Ticks)*100);
    private static Proto Attributes(IEnumerable<KeyValuePair<string,object?>> attributes,int field)
    {
        var result=new Proto();
        foreach(var pair in attributes)
        {
            if(pair.Value==null) continue;
            var attribute=new Proto(); attribute.String(1,pair.Key); attribute.Message(2,Value(pair.Value)); result.Message(field,attribute);
        }
        return result;
    }
    private static Proto Value(object? value)
    {
        var result=new Proto();
        switch(value)
        {
            case null: break;
            case string text: result.String(1,text); break;
            case bool boolean: result.Int(2,boolean?1u:0u); break;
            case byte or sbyte or short or ushort or int or uint or long:
                result.Int(3,unchecked((ulong)Convert.ToInt64(value,CultureInfo.InvariantCulture))); break;
            case ulong integer when integer<=long.MaxValue: result.Int(3,integer); break;
            case float or double or decimal:
                result.Fixed(4,unchecked((ulong)BitConverter.DoubleToInt64Bits(Convert.ToDouble(value,CultureInfo.InvariantCulture)))); break;
            case Array array when array.Rank==1:
                var items=new Proto(); foreach(var item in array) items.Message(1,Value(item)); result.Message(5,items); break;
            default: result.String(1,value.GetType().FullName??value.GetType().Name); break;
        }
        return result;
    }
    public void Dispose()=>_client.Dispose();

    // OTLP field numbers follow opentelemetry/proto/{trace,common,resource}/v1.
    // A per-message cap bounds serialization even with application-added attributes.
    private sealed class Proto
    {
        private const int Limit=16*1024*1024;
        private readonly MemoryStream _bytes=new();
        private void Budget(int count) { if(count>Limit-_bytes.Length) throw new InvalidDataException("OTLP message limit"); }
        private void Varint(ulong value)
        {
            do { Budget(1); byte next=(byte)(value&127); value>>=7; _bytes.WriteByte((byte)(next|(value==0?0:128))); } while(value!=0);
        }
        internal void Int(int field,ulong value) { Varint((ulong)field<<3); Varint(value); }
        internal void Fixed(int field,ulong value)
        {
            Varint(((ulong)field<<3)|1); Budget(8);
            Span<byte> bytes=stackalloc byte[8]; System.Buffers.Binary.BinaryPrimitives.WriteUInt64LittleEndian(bytes,value); _bytes.Write(bytes);
        }
        internal void UInt32(int field,uint value)
        {
            Varint(((ulong)field<<3)|5); Budget(4);
            Span<byte> bytes=stackalloc byte[4]; System.Buffers.Binary.BinaryPrimitives.WriteUInt32LittleEndian(bytes,value); _bytes.Write(bytes);
        }
        internal void String(int field,string? value) { if(value!=null) { if(Encoding.UTF8.GetByteCount(value)>Limit) throw new InvalidDataException("OTLP string limit"); Data(field,Encoding.UTF8.GetBytes(value)); } }
        internal void Data(int field,ReadOnlySpan<byte> bytes) { Budget(bytes.Length+10); Varint(((ulong)field<<3)|2); Varint((ulong)bytes.Length); _bytes.Write(bytes); }
        internal void Message(int field,Proto value)=>Data(field,value._bytes.GetBuffer().AsSpan(0,(int)value._bytes.Length));
        internal void Append(Proto value) { Budget((int)value._bytes.Length); _bytes.Write(value._bytes.GetBuffer().AsSpan(0,(int)value._bytes.Length)); }
        internal byte[] Bytes()=>_bytes.ToArray();
    }
}
