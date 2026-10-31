using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using FIInstrumentation.Context;
using OpenTelemetry;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>Observes HttpClient diagnostics; never replaces a handler or eagerly reads application streams.</summary>
internal sealed class HttpCapture : IObserver<DiagnosticListener>, IObserver<KeyValuePair<string, object?>>, IDisposable
{
    private readonly AutoConfig _config;
    private readonly ModelCoverage _coverage;
    private readonly AutoProcessor _privacy;
    private readonly int _attributeLimit;
    private readonly IDisposable _all;
    private readonly ConcurrentBag<IDisposable> _subscriptions = [];
    private readonly ConcurrentDictionary<HttpRequestMessage, Capture> _pending = new();
    private int _disposed;
    internal HttpCapture(AutoConfig config,ModelCoverage? coverage=null,AutoProcessor? privacy=null)
    {
        _config=config; _attributeLimit=AttributeProjection.Limit(config.Options.Exporter);
        _privacy=privacy??new AutoProcessor(config); _coverage=coverage??new ModelCoverage();
        _all=DiagnosticListener.AllListeners.Subscribe(this);
    }
    public void OnNext(DiagnosticListener listener)
    {
        try
        {
            if(listener.Name=="HttpHandlerDiagnosticListener" && Volatile.Read(ref _disposed)==0)
                // HttpClient first checks the operation name (without .Start/.Stop) before emitting either event.
                _subscriptions.Add(listener.Subscribe(this, name=>name.StartsWith("System.Net.Http.HttpRequestOut",StringComparison.Ordinal)||name=="System.Net.Http.Exception"));
        } catch { }
    }
    private static object? Property(object? payload,string name) => payload?.GetType().GetProperty(name)?.GetValue(payload);
    public void OnNext(KeyValuePair<string,object?> e)
    {
        try
        {
            if(Volatile.Read(ref _disposed)!=0 || Property(e.Value,"Request") is not HttpRequestMessage request) return;
            if(e.Key.EndsWith(".Start",StringComparison.Ordinal)) Start(request);
            else if(e.Key.EndsWith(".Exception",StringComparison.Ordinal))
            {
                if(_pending.TryRemove(request,out var capture)) capture.Complete(Property(e.Value,"Exception") as Exception ?? new HttpRequestException());
            }
            else if(e.Key.EndsWith(".Stop",StringComparison.Ordinal) && _pending.TryRemove(request,out var capture))
            {
                if(Property(e.Value,"Response") is HttpResponseMessage response)
                {
                    capture.HeadersObserved();
                    capture.Response.Status=(int)response.StatusCode;
                    capture.Response.Headers=Headers(response.Content.Headers);
                    // Keep the original response AND content object. SocketsHttpHandler's
                    // framework content has one owned stream; tap only that stream, never
                    // call application content/stream factories or read an unread body.
                    var content=response.Content;
                    var type=content.GetType();
                    var field=type.Assembly==typeof(HttpClient).Assembly && type.FullName=="System.Net.Http.HttpConnectionResponseContent"?
                        type.GetField("_stream",System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.NonPublic):null;
                    if(field?.GetValue(content) is Stream stream) field.SetValue(content,new ReadTapStream(stream,capture));
                    // Unknown custom content is deliberately untouched, not silently replaced.
                }
                else capture.Complete(new HttpRequestException());
            }
        }
        catch { /* Diagnostic callbacks execute on application threads and must be fail-open. */ }
    }
    private void Start(HttpRequestMessage request)
    {
        // Capture the transport boundary before any matching, copying or context work.
        var started=DateTimeOffset.UtcNow; long clock=Stopwatch.GetTimestamp();
        bool discoveryIncomplete=ObserveCredentials(request);
        if(InstrumentationSuppression.Active) return;
        var current=Activity.Current;
        var parent=current?.OperationName=="System.Net.Http.HttpRequestOut" ? current.Parent : current;
        if(parent!=null && _coverage.Covered(parent) || current!=null && _coverage.Covered(current))
            return;
        for(var activity=parent;activity!=null;activity=activity.Parent)
        {
            if(activity.GetCustomProperty("traceai.wire.capture")!=null || activity.GetCustomProperty("traceai.model.covered") is true) return;
            if(activity.GetTagItem("gen_ai.span.kind") is "LLM" or "EMBEDDING" or "RERANKER" or "GUARDRAIL") return;
            // SDK-native client spans may intervene, and native providers sometimes
            // set tags after Start. Check every ancestor, never render unknown values.
            if(Agent.IntegrationForSource(activity.Source.Name) is { Name:not "aspnetcore" } integration && _config.Enabled(integration.Name) &&
                (activity.GetTagItem("gen_ai.operation.name") as string??activity.OperationName.Split(' ')[0]) is "chat" or "embedding" or "embeddings" or "generate_content" or "text_completion" or "transcription" or "text_to_speech" or "rerank")
                return;
        }
        var url=request.RequestUri?.AbsoluteUri; if(url==null) return;
        var match=WireCore.Match(request.Method.Method,url); if(match==null) return;
        if(!_config.Enabled("wire")) return;
        var req=new RequestData { Method=request.Method.Method, Url=url, CredentialDiscoveryIncomplete=discoveryIncomplete };
        ReadRequest(request.Content,req,_config.MaxBody);
        foreach(var header in request.Headers) req.Headers[header.Key]=string.Join(",",header.Value);
        var capture=new Capture(match,req,_config.MaxBody,parent?.Context??default,started,
            HostContext.Attributes().ToArray(),()=>Volatile.Read(ref _disposed)==0,clock,_attributeLimit,_privacy);
        if(_pending.TryAdd(request,capture))
        {
            current?.SetCustomProperty("traceai.wire.capture",true);
            if(request.Content is {} content && MultipartObserver.Create(content,req) is {} multipart)
            {
                var observed=new ObservedRequestContent(content,multipart); observed.CopyHeaders(); request.Content=observed;
            }
        }
    }
    private bool ObserveCredentials(HttpRequestMessage request)
    {
        var observation=new RequestData();
        try
        {
            // Observation is independent of matching, sampling, ancestry and the wire
            // switch. Learn separate header values, including non-AI HTTP requests.
            foreach(var secret in WirePrivacy.HeaderSecrets(request.Headers.Concat(request.Content?.Headers.AsEnumerable()??[])
                .SelectMany(h=>h.Value.Select(value=>new KeyValuePair<string,string>(h.Key,value)))))
            {
                if(secret.Length>KnownSecrets.MaxLength) observation.CredentialDiscoveryIncomplete=true;
                else KnownSecrets.Register(secret);
            }
            KnownSecrets.Register(WirePrivacy.QuerySecrets(request.RequestUri?.AbsoluteUri,observation));
            if(request.Content is {} content) ObserveBuffered(content,observation);
        }
        catch { observation.CredentialDiscoveryIncomplete=true; }
        return observation.CredentialDiscoveryIncomplete;
    }
    private void ObserveBuffered(HttpContent content,RequestData observation)
    {
        if(content.GetType()==typeof(MultipartFormDataContent) && content is MultipartFormDataContent multipart)
        {
            int count=0;
            foreach(var part in multipart)
            {
                if(++count>128) throw new InvalidDataException("Credential field budget");
                var disposition=part.Headers.ContentDisposition;
                if(disposition?.FileName!=null || disposition?.FileNameStar!=null) continue;
                var name=disposition?.Name?.Trim('"')??"";
                ObserveBufferedPart(part,name,observation);
            }
        }
        else ObserveBufferedPart(content,null,observation);
    }
    private void ObserveBufferedPart(HttpContent content,string? field,RequestData observation)
    {
        if(!Buffered(content)) return; // Never serialize custom content or consume streams.
        var headers=Headers(content.Headers);
        if(WireCore.IsBinary(content.Headers.ContentType?.MediaType??"")) return;
        var decoded=BodyDecoder.DecodeComplete(BufferedMemory(content),headers,_config.MaxBody);
        if(field!=null && WirePrivacy.CredentialKey(field))
            observation.CredentialDiscoveryIncomplete|=WirePrivacy.ObserveField(field,System.Text.Encoding.UTF8.GetString(decoded.Span));
        else if(content.Headers.ContentType?.MediaType=="application/x-www-form-urlencoded")
            KnownSecrets.Register(WirePrivacy.QuerySecrets("https://credential.invalid/?"+System.Text.Encoding.UTF8.GetString(decoded.Span),observation));
        else KnownSecrets.Register(WirePrivacy.RequestSecrets(observation,decoded));
    }
    internal static Dictionary<string,string> Headers(HttpContentHeaders headers) => headers.ToDictionary(h=>h.Key,h=>string.Join(",",h.Value),StringComparer.OrdinalIgnoreCase);
    private static bool Buffered(HttpContent content) => content.GetType()==typeof(StringContent)||content.GetType()==typeof(ByteArrayContent)||content.GetType()==typeof(ReadOnlyMemoryContent)||content.GetType()==typeof(FormUrlEncodedContent);
    private static ReadOnlyMemory<byte> BufferedMemory(HttpContent content)
    {
        // Only exact buffered BCL types reach this helper. Never call ReadAs*,
        // serialize content or materialize a Content-Length header: even the BCL
        // getter mutates multipart parts after the enclosing length was computed.
        const System.Reflection.BindingFlags flags=System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.NonPublic;
        if(content is ByteArrayContent)
        {
            var type=typeof(ByteArrayContent);
            var bytes=(byte[])type.GetField("_content",flags)!.GetValue(content)!;
            int offset=(int)type.GetField("_offset",flags)!.GetValue(content)!;
            int count=(int)type.GetField("_count",flags)!.GetValue(content)!;
            return bytes.AsMemory(offset,count);
        }
        return (ReadOnlyMemory<byte>)typeof(ReadOnlyMemoryContent).GetField("_content",flags)!.GetValue(content)!;
    }
    private static long? KnownLength(HttpContent content)
    {
        if(content.Headers.TryGetValues("Content-Length",out var values)&&long.TryParse(values.FirstOrDefault(),out var size)) return size;
        // Never invoke custom TryComputeLength or change any part's headers.
        return Buffered(content)?BufferedMemory(content).Length:null;
    }
    internal static void ReadRequest(HttpContent? content,RequestData req,int max)
    {
        req.MaxBody=max;
        if(content==null) return;
        req.Headers=Headers(content.Headers); req.BodySize=KnownLength(content);
        if(content.GetType()==typeof(MultipartFormDataContent) && content is MultipartFormDataContent multipart)
        {
            req.Fields=new();
            foreach(var part in multipart)
            {
                var disposition=part.Headers.ContentDisposition; var name=WireCore.Bounded(disposition?.Name?.Trim('"')??"file",1024);
                if(disposition?.FileName!=null||disposition?.FileNameStar!=null)
                {
                    if(req.Files.Count<128) req.Files.Add(new(name,WireCore.Bounded((disposition.FileNameStar??disposition.FileName)?.Trim('"')??"",1024),WireCore.Bounded(part.Headers.ContentType?.MediaType??"",256),KnownLength(part)));
                    else req.Truncated=true;
                    continue; // File content is never touched, even if already buffered.
                }
                if(req.Fields.Count>=128) { req.Truncated=true; continue; }
                if(!Buffered(part)) { req.BodyUnavailable=true; continue; }
                // Only these exact BCL content types can create the stream: it is an in-memory replay,
                // never a user stream. Restore its position and copy no more than the field budget.
                var task=part.ReadAsStreamAsync();
                if(!task.IsCompletedSuccessfully || !task.Result.CanSeek) { req.BodyUnavailable=true; continue; }
                var memory=task.Result; var position=memory.Position;
                var bytes=new byte[(int)Math.Min(memory.Length,65536)]; int count=0;
                try { memory.Position=0; while(count<bytes.Length) { int copied=memory.Read(bytes,count,bytes.Length-count); if(copied==0) break; count+=copied; } }
                finally { memory.Position=position; }
                if(memory.Length>65536) req.Truncated=true;
                var prefix=bytes.AsSpan(0,count);
                // Drop an incomplete trailing UTF-8 codepoint rather than expanding it past the byte cap.
                var text=System.Text.Encoding.UTF8.GetString(prefix);
                while(System.Text.Encoding.UTF8.GetByteCount(text)>65536) text=text[..^1];
                req.Fields[name]=text;
            }
            return;
        }
        if(WireCore.IsBinary(req.ContentType)) return;
        if(!Buffered(content)) { req.BodyUnavailable=true; return; } // Never invoke custom serialization or consume a stream.
        if(content.Headers.ContentLength is not {} size) { req.BodyUnavailable=true; return; }
        if(size>max) { req.Truncated=true; return; }
        var read=content.ReadAsByteArrayAsync(); if(read.IsCompletedSuccessfully) req.Body=read.Result; else req.BodyUnavailable=true;
    }
    public void OnError(Exception error) { }
    public void OnCompleted() { }
    public void Dispose()
    {
        if(Interlocked.Exchange(ref _disposed,1)!=0) return;
        _all.Dispose(); foreach(var subscription in _subscriptions) subscription.Dispose(); _pending.Clear();
    }
}

internal sealed class Capture
{
    private readonly WireMatch _match;
    private readonly RequestData _request;
    private readonly int _max;
    private readonly ActivityContext _parent;
    private readonly DateTimeOffset _start;
    private readonly KeyValuePair<string,string>[] _context;
    private readonly Func<bool> _enabled;
    // Spans are deferred until body completion, possibly in another async flow.
    // Sampling/suppression must use the accepted request's context, not its reader's.
    private readonly ExecutionContext _requestContext=CaptureRequestContext();
    private static ExecutionContext CaptureRequestContext()
    {
        if(!ExecutionContext.IsFlowSuppressed()) return ExecutionContext.Capture()!;
        ExecutionContext.RestoreFlow();
        try { return ExecutionContext.Capture()!; }
        finally { ExecutionContext.SuppressFlow(); }
    }
    private readonly long _clock;
    private DateTimeOffset? _lastByte;
    private DateTimeOffset? _headers;
    internal void HeadersObserved() => _headers=DateTimeOffset.UtcNow;
    private MemoryStream? _body;
    private int _completed;
    private readonly object _gate=new();
    internal readonly ResponseData Response=new();
    private readonly int _attributeLimit;
    private readonly AutoProcessor? _privacy;
    internal Capture(WireMatch match,RequestData req,int max,ActivityContext parent,DateTimeOffset start,KeyValuePair<string,string>[] context,Func<bool>? enabled=null,long? clock=null,int? attributeLimit=null,AutoProcessor? privacy=null)
    { _match=match; _request=req; _max=max; _parent=parent; _start=start; _clock=clock??Stopwatch.GetTimestamp(); _context=context; _enabled=enabled??(()=>true); Response.MaxBody=max; _attributeLimit=attributeLimit??AttributeProjection.Limit(); _privacy=privacy; }
    internal void Bytes(ReadOnlySpan<byte> bytes)
    {
        var observed=DateTimeOffset.UtcNow; long observedClock=Stopwatch.GetTimestamp();
        try
        {
            bool consumed;
            lock(_gate)
            {
                if(_completed!=0||bytes.Length==0) return;
                _lastByte=observed;
                Response.ObserveChunk(bytes.Length,Stopwatch.GetElapsedTime(_clock,observedClock).TotalSeconds);
                if(!WireCore.IsBinary(Response.ContentType))
                {
                    if(Response.Size>_max) { Response.Truncated=true; _body?.Dispose(); _body=null; }
                    else (_body??=new MemoryStream()).Write(bytes);
                }
                consumed=ContentLengthConsumed();
            }
            // ReadExactly and manual exact-length reads need not issue another
            // read or dispose. Publish completion on the last byte, including
            // binary/truncated responses, with the byte's timestamp frozen above.
            if(consumed) Complete();
        }
        catch { Response.Truncated=true; }
    }
    private bool ContentLengthConsumed()=>Response.Headers.TryGetValue("Content-Length",out var length) &&
        long.TryParse(length,out var expected) && expected>=0 && Response.Size==expected;
    internal void Complete(Exception? error=null,bool partial=false)
    {
        var observed=DateTimeOffset.UtcNow;
        try
        {
            lock(_gate)
            {
                if(Interlocked.Exchange(ref _completed,1)!=0) return;
                // Content-Length describes the bytes observed by this tee (including
                // encoded bytes). A final zero-byte read is not required by ReadExactly.
                bool consumed=ContentLengthConsumed();
                Response.Error=error; Response.Incomplete=partial && error==null && !consumed;
                Response.Body=_body?.ToArray(); _body?.Dispose(); _body=null;
            }
            // Freeze transport time BEFORE copying/parsing, Activity callbacks or export.
            var ended=error!=null?observed:_lastByte??_headers??observed;
            if(ended<_start) ended=_start;
            if(!_enabled()) return;
            var result=WireCore.Build(_match,_request,Response); if(result==null) return;
            var tags=new ActivityTagsCollection(result.Attributes.Select(p=>new KeyValuePair<string,object?>(p.Key,p.Value)));
            foreach(var (key,value) in _context) tags[key]=value;
            var hints=PrivacyCatalog.IndexHints(tags);
            var operation=result.Attributes.GetValueOrDefault("gen_ai.operation.name") as string;
            // Wire content belongs to the agent even when only a host provider is
            // supplied. Apply the same hide policy before Activity creation, while
            // MIME siblings are still available and before projection drops groups.
            // Host/library spans remain untouched; export snapshots sanitize again.
            var candidates=tags.Select(pair=> {
                try
                {
                    var value=pair.Value==null?null:WirePrivacy.Scrub(pair.Value,path:pair.Key,hints:hints);
                    if(value!=null && _privacy!=null) value=_privacy.Mask(pair.Key,value,operation);
                    return new KeyValuePair<string,object?>(pair.Key,value);
                }
                catch { return new KeyValuePair<string,object?>(pair.Key,FIInstrumentation.TraceConfig.RedactedValue); }
            }).ToArray();
            var projected=AttributeProjection.Project(candidates,_attributeLimit);
            ExecutionContext.Run(_requestContext,_=>
            {
                // Explicitly clear ambient context so a request with no parent stays a root.
                var ambient=Activity.Current;
                try
                {
                    Activity.Current=null;
                    using var activity=TraceAIAuto.Source.StartActivity(result.Name,ActivityKind.Client,_parent,projected,startTime:_start);
                    activity?.SetStatus(result.Ok?ActivityStatusCode.Ok:ActivityStatusCode.Error,result.Description);
                    activity?.SetEndTime(ended.UtcDateTime);
                }
                finally { Activity.Current=ambient; }
            },null);
        }
        catch { }
    }
}

internal sealed class ReadTapStream(Stream inner,Capture capture) : Stream
{
    private int Observe(Span<byte> bytes,int count) { if(count==0) capture.Complete(); else capture.Bytes(bytes[..count]); return count; }
    public override int Read(byte[] buffer,int offset,int count)
    {
        try { int read=inner.Read(buffer,offset,count); return count==0?read:Observe(buffer.AsSpan(offset,count),read); }
        catch(Exception ex) { capture.Complete(ex); throw; }
    }
    public override int Read(Span<byte> buffer)
    {
        try { var count=inner.Read(buffer); return buffer.Length==0?count:Observe(buffer,count); }
        catch(Exception ex) { capture.Complete(ex); throw; }
    }
    public override Task<int> ReadAsync(byte[] buffer,int offset,int count,CancellationToken token)
    {
        // Invoke the original overload before entering an async method, preserving
        // synchronous argument-validation exceptions as well as the original error.
        try { return ObserveTask(inner.ReadAsync(buffer,offset,count,token),buffer,offset,count); }
        catch(Exception ex) { capture.Complete(ex); throw; }
    }
    private async Task<int> ObserveTask(Task<int> pending,byte[] buffer,int offset,int count)
    {
        try { int read=await pending.ConfigureAwait(false); return count==0?read:Observe(buffer.AsSpan(offset,count),read); }
        catch(Exception ex) { capture.Complete(ex); throw; }
    }
    public override ValueTask<int> ReadAsync(Memory<byte> buffer,CancellationToken token=default)
    {
        try { return ObserveMemory(inner.ReadAsync(buffer,token),buffer); }
        catch(Exception ex) { capture.Complete(ex); throw; }
    }
    private async ValueTask<int> ObserveMemory(ValueTask<int> pending,Memory<byte> buffer)
    {
        try { int count=await pending.ConfigureAwait(false); return buffer.Length==0?count:Observe(buffer.Span,count); }
        catch(Exception ex) { capture.Complete(ex); throw; }
    }
    public override void CopyTo(Stream destination,int bufferSize)
    {
        ArgumentNullException.ThrowIfNull(destination);
        try { using var tap=new WriteTapStream(destination,capture); base.CopyTo(tap,bufferSize); capture.Complete(); }
        catch(Exception ex) { capture.Complete(ex); throw; }
    }
    public override Task CopyToAsync(Stream destination,int bufferSize,CancellationToken token)
    {
        ArgumentNullException.ThrowIfNull(destination);
        var tap=new WriteTapStream(destination,capture);
        try { return ObserveCopy(base.CopyToAsync(tap,bufferSize,token),tap); }
        catch(Exception ex) { tap.Dispose(); capture.Complete(ex); throw; }
    }
    private async Task ObserveCopy(Task pending,Stream tap)
    {
        using(tap)
        {
            try { await pending.ConfigureAwait(false); capture.Complete(); }
            catch(Exception ex) { capture.Complete(ex); throw; }
        }
    }
    protected override void Dispose(bool disposing) { if(disposing) { capture.Complete(partial:true); inner.Dispose(); } base.Dispose(disposing); }
    public override async ValueTask DisposeAsync() { capture.Complete(partial:true); await inner.DisposeAsync().ConfigureAwait(false); GC.SuppressFinalize(this); }
    public override bool CanRead=>inner.CanRead; public override bool CanSeek=>inner.CanSeek; public override bool CanWrite=>inner.CanWrite;
    public override bool CanTimeout=>inner.CanTimeout;
    public override int ReadTimeout { get=>inner.ReadTimeout; set=>inner.ReadTimeout=value; }
    public override int WriteTimeout { get=>inner.WriteTimeout; set=>inner.WriteTimeout=value; }
    public override long Length=>inner.Length;
    public override long Position { get=>inner.Position; set=>inner.Position=value; }
    public override long Seek(long offset,SeekOrigin origin)=>inner.Seek(offset,origin);
    public override void Flush()=>inner.Flush(); public override Task FlushAsync(CancellationToken token)=>inner.FlushAsync(token);
    public override void SetLength(long value)=>inner.SetLength(value);
    public override void Write(byte[] buffer,int offset,int count)=>inner.Write(buffer,offset,count);
}

internal sealed class WriteTapStream(Stream inner,Capture capture) : Stream
{
    // Only the source's read failures are provider/transport errors. Finalize a
    // destination failure here, before HttpContent wraps it in HttpRequestException.
    public override void Write(byte[] buffer,int offset,int count)
    {
        try { inner.Write(buffer,offset,count); }
        catch { capture.Complete(partial:true); throw; }
    }
    public override void Write(ReadOnlySpan<byte> buffer)
    {
        try { inner.Write(buffer); }
        catch { capture.Complete(partial:true); throw; }
    }
    public override Task WriteAsync(byte[] buffer,int offset,int count,CancellationToken token) => WriteAsync(buffer.AsMemory(offset,count),token).AsTask();
    public override async ValueTask WriteAsync(ReadOnlyMemory<byte> buffer,CancellationToken token=default)
    {
        try { await inner.WriteAsync(buffer,token).ConfigureAwait(false); }
        catch { capture.Complete(partial:true); throw; }
    }
    // Do not close the application's destination stream when this tap is disposed.
    public override bool CanRead=>inner.CanRead; public override bool CanSeek=>inner.CanSeek; public override bool CanWrite=>inner.CanWrite;
    public override long Length=>inner.Length; public override long Position { get=>inner.Position; set=>throw new NotSupportedException(); }
    public override void Flush() { try { inner.Flush(); } catch { capture.Complete(partial:true); throw; } }
    public override async Task FlushAsync(CancellationToken token)
    { try { await inner.FlushAsync(token).ConfigureAwait(false); } catch { capture.Complete(partial:true); throw; } }
    public override int Read(byte[] b,int o,int c)=>throw new NotSupportedException(); public override long Seek(long o,SeekOrigin s)=>throw new NotSupportedException(); public override void SetLength(long v)=>throw new NotSupportedException();
}
