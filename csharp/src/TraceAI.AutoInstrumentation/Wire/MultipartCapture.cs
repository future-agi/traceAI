using System.Net;
using System.Net.Http.Headers;
using System.Text;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>Parses only bytes the transport has already sent. File bodies are counted, never buffered.</summary>
internal sealed class MultipartObserver
{
    private readonly RequestData _request;
    private readonly byte[] _first, _delimiter;
    private readonly byte[] _buffer=new byte[16384];
    private readonly MemoryStream _field=new();
    private readonly Dictionary<string,string> _fields=new();
    private readonly List<FilePart> _files=[];
    private int _count, _state; // first boundary, headers, body, boundary suffix, done
    private string _name="";
    private FilePart? _file;
    private long _size;
    internal MultipartObserver(RequestData request,string boundary)
    {
        _request=request; _first=Encoding.UTF8.GetBytes("--"+boundary); _delimiter=Encoding.UTF8.GetBytes("\r\n--"+boundary);
        request.BodyUnavailable=true;
    }
    internal static MultipartObserver? Create(HttpContent content,RequestData request)
    {
        if(content.Headers.ContentType?.MediaType?.Equals("multipart/form-data",StringComparison.OrdinalIgnoreCase)!=true) return null;
        var boundary=content.Headers.ContentType.Parameters.FirstOrDefault(p=>p.Name.Equals("boundary",StringComparison.OrdinalIgnoreCase))?.Value?.Trim('"');
        return boundary is { Length:>0 and <=200 }?new(request,boundary):null;
    }
    internal void Bytes(ReadOnlySpan<byte> bytes)
    {
        try
        {
            while(!bytes.IsEmpty && _state<4)
            {
                int take=Math.Min(bytes.Length,_buffer.Length-_count);
                if(take==0) { _request.Truncated=true; _state=4; return; }
                bytes[..take].CopyTo(_buffer.AsSpan(_count)); _count+=take; bytes=bytes[take..];
                Process();
            }
        }
        catch { _request.Truncated=true; _state=4; }
    }
    private void Remove(int count)
    { _buffer.AsSpan(count,_count-count).CopyTo(_buffer); _count-=count; }
    private void Process()
    {
        while(_state<4)
        {
            var bytes=_buffer.AsSpan(0,_count);
            if(_state==0)
            {
                int index=bytes.IndexOf(_first);
                if(index<0) { Remove(Math.Max(0,_count-_first.Length)); return; }
                Remove(index+_first.Length); _state=3;
            }
            else if(_state==1)
            {
                int index=bytes.IndexOf("\r\n\r\n"u8);
                if(index<0) { if(_count>8192) { _request.Truncated=true; _state=4; } return; }
                if(index>8192) { _request.Truncated=true; _state=4; return; }
                StartPart(Encoding.UTF8.GetString(bytes[..index])); Remove(index+4); _state=2;
            }
            else if(_state==2)
            {
                int index=bytes.IndexOf(_delimiter);
                if(index<0)
                {
                    int safe=Math.Max(0,_count-_delimiter.Length-2);
                    Payload(bytes[..safe]); Remove(safe); return;
                }
                // A matching prefix in file data is not a delimiter without a valid suffix.
                if(_count<index+_delimiter.Length+2) { Payload(bytes[..index]); Remove(index); return; }
                var suffix=bytes.Slice(index+_delimiter.Length,2);
                if(!suffix.SequenceEqual("--"u8)&&!suffix.SequenceEqual("\r\n"u8)) { Payload(bytes[..(index+2)]); Remove(index+2); continue; }
                Payload(bytes[..index]); EndPart(); Remove(index+_delimiter.Length); _state=3;
            }
            else
            {
                if(_count<2) return;
                if(bytes.StartsWith("--"u8))
                {
                    _request.Fields=_fields; _request.Files=_files; _request.BodyUnavailable=false; _request.Body=null; _state=4; _count=0; Array.Clear(_buffer); _field.Dispose();
                }
                else if(bytes.StartsWith("\r\n"u8)) { Remove(2); _state=1; }
                else { _request.Truncated=true; _state=4; }
            }
        }
    }
    private void StartPart(string headers)
    {
        _name=""; _file=null; _size=0; _field.SetLength(0);
        string? mime=null; ContentDispositionHeaderValue? disposition=null;
        foreach(var line in headers.Split("\r\n"))
        {
            var pair=line.Split(':',2); if(pair.Length!=2) continue;
            if(pair[0].Equals("content-disposition",StringComparison.OrdinalIgnoreCase)) ContentDispositionHeaderValue.TryParse(pair[1].Trim(),out disposition);
            else if(pair[0].Equals("content-type",StringComparison.OrdinalIgnoreCase)) mime=WireCore.Bounded(pair[1].Trim().Split(';')[0],256);
        }
        if(disposition?.Name is not {} name) return;
        _name=WireCore.Bounded(name.Trim('"'),1024);
        if((disposition.FileNameStar??disposition.FileName) is {} filename) _file=new(_name,WireCore.Bounded(filename.Trim('"'),1024),mime,null);
    }
    private void Payload(ReadOnlySpan<byte> bytes)
    {
        _size+=bytes.Length;
        if(_file!=null||_name.Length==0) return;
        if(_fields.Count>=128 && !_fields.ContainsKey(_name)) { _request.Truncated=true; return; }
        int take=(int)Math.Min(bytes.Length,65536-_field.Length);
        if(take<bytes.Length) _request.Truncated=true;
        if(take>0) _field.Write(bytes[..take]);
    }
    private void EndPart()
    {
        if(_file!=null)
        {
            if(_files.Count<128) _files.Add(_file with { Size=_size }); else _request.Truncated=true;
        }
        else if(_name.Length>0 && (_fields.Count<128||_fields.ContainsKey(_name)))
        {
            var text=Encoding.UTF8.GetString(_field.GetBuffer(),0,(int)_field.Length);
            while(Encoding.UTF8.GetByteCount(text)>65536) text=text[..^1];
            _fields[_name]=text;
            _request.CredentialDiscoveryIncomplete|=WirePrivacy.ObserveField(_name,text,_size==_field.Length);
        }
    }
}

internal sealed class ObservedRequestContent(HttpContent inner,MultipartObserver observer) : HttpContent
{
    internal void CopyHeaders() { foreach(var header in inner.Headers) Headers.TryAddWithoutValidation(header.Key,header.Value); }
    protected override bool TryComputeLength(out long length)
    { length=inner.Headers.ContentLength??0; return inner.Headers.ContentLength!=null; }
    protected override void SerializeToStream(Stream stream,TransportContext? context,CancellationToken token)
    { using var tap=new RequestTapStream(stream,observer); inner.CopyTo(tap,context,token); }
    protected override Task SerializeToStreamAsync(Stream stream,TransportContext? context) => SerializeToStreamAsync(stream,context,CancellationToken.None);
    protected override async Task SerializeToStreamAsync(Stream stream,TransportContext? context,CancellationToken token)
    { using var tap=new RequestTapStream(stream,observer); await inner.CopyToAsync(tap,context,token).ConfigureAwait(false); }
    protected override void Dispose(bool disposing) { if(disposing) inner.Dispose(); base.Dispose(disposing); }
}

internal sealed class RequestTapStream(Stream inner,MultipartObserver observer) : Stream
{
    public override void Write(byte[] buffer,int offset,int count) { inner.Write(buffer,offset,count); observer.Bytes(buffer.AsSpan(offset,count)); }
    public override void Write(ReadOnlySpan<byte> buffer) { inner.Write(buffer); observer.Bytes(buffer); }
    public override async Task WriteAsync(byte[] buffer,int offset,int count,CancellationToken token) { await inner.WriteAsync(buffer.AsMemory(offset,count),token).ConfigureAwait(false); observer.Bytes(buffer.AsSpan(offset,count)); }
    public override async ValueTask WriteAsync(ReadOnlyMemory<byte> buffer,CancellationToken token=default) { await inner.WriteAsync(buffer,token).ConfigureAwait(false); observer.Bytes(buffer.Span); }
    public override bool CanRead=>false; public override bool CanSeek=>false; public override bool CanWrite=>inner.CanWrite;
    public override long Length=>inner.Length; public override long Position { get=>inner.Position; set=>throw new NotSupportedException(); }
    public override void Flush()=>inner.Flush(); public override Task FlushAsync(CancellationToken token)=>inner.FlushAsync(token);
    public override int Read(byte[] b,int o,int c)=>throw new NotSupportedException(); public override long Seek(long o,SeekOrigin s)=>throw new NotSupportedException(); public override void SetLength(long v)=>throw new NotSupportedException();
}
