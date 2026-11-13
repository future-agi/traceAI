using System.IO.Compression;

namespace TraceAI.AutoInstrumentation.Wire;

internal static class BodyDecoder
{
    internal static byte[]? Decode(byte[]? bytes,Dictionary<string,string> headers,int max,out bool truncated,out string? undecoded)
    {
        truncated=false; undecoded=null;
        if(bytes==null) return null;
        if(bytes.Length>max) { truncated=true; return null; }
        var encoding=headers.FirstOrDefault(h=>h.Key.Equals("content-encoding",StringComparison.OrdinalIgnoreCase)).Value;
        if(string.IsNullOrWhiteSpace(encoding)||encoding.Equals("identity",StringComparison.OrdinalIgnoreCase)) return bytes;
        foreach(var coding in encoding.Split(',',StringSplitOptions.TrimEntries|StringSplitOptions.RemoveEmptyEntries).Reverse())
        {
            using var input=new MemoryStream(bytes,false); using var output=new MemoryStream();
            try
            {
                using Stream decoder=coding.ToLowerInvariant() switch {
                    "gzip"=>new GZipStream(input,CompressionMode.Decompress,true),
                    "deflate"=>IsZlib(bytes)?new ZLibStream(input,CompressionMode.Decompress,true):new DeflateStream(input,CompressionMode.Decompress,true),
                    "br"=>new BrotliStream(input,CompressionMode.Decompress,true),
                    "identity"=>input,
                    _=>throw new NotSupportedException()
                };
                var buffer=new byte[8192]; int read;
                while((read=decoder.Read(buffer,0,(int)Math.Min(buffer.Length,(long)max-output.Length+1)))>0)
                {
                    if(output.Length+read>max) { truncated=true; return null; }
                    output.Write(buffer,0,read);
                }
            }
            catch
            {
                undecoded=encoding;
                // A truncated compressed stream may still contain complete SSE events.
                if(output.Length==0) return null;
            }
            bytes=output.ToArray();
        }
        return bytes;
    }
    /// <summary>Credential discovery cannot accept a decoded prefix. Exact buffered
    /// bodies are observed without copying encoded bytes; every coding must finish.</summary>
    internal static ReadOnlyMemory<byte> DecodeComplete(ReadOnlyMemory<byte> bytes,Dictionary<string,string> headers,int max)
    {
        max=Math.Min(max,4*1024*1024);
        if(bytes.Length>max) throw new InvalidDataException("Credential body budget");
        string? encoding=headers.FirstOrDefault(h=>h.Key.Equals("content-encoding",StringComparison.OrdinalIgnoreCase)).Value;
        var codings=(encoding??"").Split(',',StringSplitOptions.TrimEntries|StringSplitOptions.RemoveEmptyEntries);
        if(codings.Length>8) throw new InvalidDataException("Credential encoding budget");
        foreach(var coding in codings.Reverse())
        {
            if(coding.Equals("identity",StringComparison.OrdinalIgnoreCase)) continue;
            if(coding.Equals("br",StringComparison.OrdinalIgnoreCase))
            {
                byte[] output=new byte[max+1];
                using var decoder=new BrotliDecoder();
                var status=decoder.Decompress(bytes.Span,output,out int consumed,out int written);
                if(status!=System.Buffers.OperationStatus.Done || consumed!=bytes.Length || written>max)
                    throw new InvalidDataException("Incomplete credential encoding");
                bytes=output.AsMemory(0,written); continue;
            }
            bool gzip=coding.Equals("gzip",StringComparison.OrdinalIgnoreCase),deflate=coding.Equals("deflate",StringComparison.OrdinalIgnoreCase);
            if(!gzip&&!deflate) throw new InvalidDataException("Unsupported credential encoding");
            bool zlib=deflate && bytes.Length>=2 && (bytes.Span[0]&15)==8 && ((bytes.Span[0]<<8)+bytes.Span[1])%31==0;
            using var input=new BufferedInput(bytes,failAtEnd:!gzip);
            using Stream stream=gzip?new GZipStream(input,CompressionMode.Decompress):zlib?new ZLibStream(input,CompressionMode.Decompress):new DeflateStream(input,CompressionMode.Decompress);
            using var decoded=new MemoryStream(); var buffer=new byte[8192];
            int read;
            while((read=stream.Read(buffer,0,(int)Math.Min(buffer.Length,(long)max-decoded.Length+1)))>0)
            {
                if(decoded.Length+read>max) throw new InvalidDataException("Credential decoded budget");
                decoded.Write(buffer,0,read);
            }
            var result=decoded.ToArray();
            // GZipStream tolerates missing trailers. Validate both trailer fields;
            // no prefix can be trusted when a credential may follow that prefix.
            if(gzip && (bytes.Length<18 || System.Buffers.Binary.BinaryPrimitives.ReadUInt32LittleEndian(bytes.Span[^4..])!=result.Length ||
                System.Buffers.Binary.BinaryPrimitives.ReadUInt32LittleEndian(bytes.Span[^8..^4])!=Crc32(result)))
                throw new InvalidDataException("Incomplete credential encoding");
            bytes=result;
        }
        return bytes;
    }
    private static uint Crc32(ReadOnlySpan<byte> bytes)
    {
        uint crc=uint.MaxValue;
        foreach(byte value in bytes)
        {
            crc^=value;
            for(int bit=0;bit<8;bit++) crc=(crc>>1)^((crc&1)==0?0:0xedb88320u);
        }
        return ~crc;
    }
    private sealed class BufferedInput(ReadOnlyMemory<byte> bytes,bool failAtEnd) : Stream
    {
        private int _position;
        public override int Read(byte[] buffer,int offset,int count)=>Read(buffer.AsSpan(offset,count));
        public override int Read(Span<byte> buffer)
        {
            if(buffer.Length==0) return 0;
            if(_position==bytes.Length && failAtEnd) throw new InvalidDataException("Incomplete credential encoding");
            int count=Math.Min(buffer.Length,bytes.Length-_position); bytes.Span.Slice(_position,count).CopyTo(buffer); _position+=count; return count;
        }
        public override bool CanRead=>true; public override bool CanSeek=>false; public override bool CanWrite=>false;
        public override long Length=>bytes.Length; public override long Position { get=>_position; set=>throw new NotSupportedException(); }
        public override void Flush() { }
        public override long Seek(long offset,SeekOrigin origin)=>throw new NotSupportedException();
        public override void SetLength(long value)=>throw new NotSupportedException();
        public override void Write(byte[] buffer,int offset,int count)=>throw new NotSupportedException();
    }
    private static bool IsZlib(byte[] bytes) => bytes.Length>=2 && (bytes[0]&15)==8 && ((bytes[0]<<8)+bytes[1])%31==0;
}
