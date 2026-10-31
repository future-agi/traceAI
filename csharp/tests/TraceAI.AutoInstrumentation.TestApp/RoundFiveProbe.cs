using System.Diagnostics;
using System.IO.Compression;
using System.Text;

internal static class RoundFiveProbe
{
    internal static async Task Run(string url,string mode)
    {
        using var source=new ActivitySource("OpenAI.Round5");
        if(mode=="round5_s6_catalog")
        {
            const string echo="S6_HEADER_CREDENTIAL_9876 S6_BODY_KEY_9876 S6_ARRAY_KEY_9876 S6_QUERY_CREDENTIAL_9876";
            using(var model=source.StartActivity("chat catalog credentials",ActivityKind.Client,default(ActivityContext),new ActivityTagsCollection { ["gen_ai.span.kind"]="LLM" }))
            using(var native=new Activity("native sdk client").Start())
            using(var retry=new Activity("native retry").Start())
            using(var client=new HttpClient())
            using(var request=new HttpRequestMessage(HttpMethod.Post,url+"?private-token=S6_QUERY_CREDENTIAL_9876") {
                Content=new StringContent("{\"key\":\"S6_BODY_KEY_9876\",\"api_key\":[\"S6_ARRAY_KEY_9876\"],\"messages\":[]}",Encoding.UTF8,"application/json") })
            {
                request.Headers.TryAddWithoutValidation("x-vault-token","S6_HEADER_CREDENTIAL_9876");
                using var response=await client.SendAsync(request); byte[] body=await response.Content.ReadAsByteArrayAsync();
                Console.WriteLine($"response={(int)response.StatusCode};body={Convert.ToBase64String(body)}");
                model?.SetStatus(ActivityStatusCode.Error,echo);
            }
            using(var later=source.StartActivity("chat later catalog",ActivityKind.Client))
            {
                later?.SetTag("later.echo",echo); later?.SetStatus(ActivityStatusCode.Error,echo);
                later?.AddEvent(new ActivityEvent(echo,tags:new ActivityTagsCollection { ["note"]=echo }));
            }
            return;
        }
        if(mode=="round5_registration")
        {
            var runtime=AppDomain.CurrentDomain.GetAssemblies().First(a=>a.GetName().Name=="TraceAI.AutoInstrumentation.Runtime");
            var manual=System.Runtime.Loader.AssemblyLoadContext.Default.LoadFromAssemblyPath(runtime.Location);
            manual.GetType("TraceAI.AutoInstrumentation.TraceAIAuto")!.GetMethod("RegisterSecret")!.Invoke(null,new object[] { "PUBLIC_API_REGISTERED_SECRET_9876" });
            using(var span=source.StartActivity("chat registration",ActivityKind.Client))
            {
                span?.SetTag("public.echo",new[] { "PUBLIC_API_REGISTERED_SECRET_9876","ENV_CREDENTIAL_SECRET_9876" });
                span?.SetStatus(ActivityStatusCode.Error,"PUBLIC_API_REGISTERED_SECRET_9876 ENV_CREDENTIAL_SECRET_9876");
            }
            Console.WriteLine("registration-complete"); return;
        }
        if(mode=="round5_events")
        {
            using(var span=source.StartActivity("chat message events",ActivityKind.Client))
            {
                foreach(string name in new[] { "gen_ai.user.message","gen_ai.system.message","gen_ai.tool.message","gen_ai.assistant.message","gen_ai.choice" })
                    span?.AddEvent(new ActivityEvent(name,tags:new ActivityTagsCollection { ["content"]="PRIVATE_EVENT_CONTENT",["message"]="{\"content\":\"PRIVATE_EVENT_CONTENT\"}",["tool_calls.0.function.arguments"]="PRIVATE_EVENT_CONTENT" }));
                foreach(string name in new[] { "client_secret","authorization_token","password","key","api_key" })
                    span?.SetTag("provider.url."+name,"https://provider.test/?"+name+"=URL_UNDISCOVERED_SECRET_9876");
            }
            Console.WriteLine("events-complete"); return;
        }
        if(mode=="round5_structural_media")
        {
            using(var span=source.StartActivity("chat structural media",ActivityKind.Client))
            {
                span?.SetTag("output.value","{\"audio_base64\":\"QUJDREVG\",\"audio\":{\"bytes\":\"R0hJSktM\",\"transcript\":\"spoken words\"}}");
                span?.SetTag("media.uri","data:audio/wav,%53%54%55%56%57%58");
            }
            Console.WriteLine("media-complete"); return;
        }
        if(mode.StartsWith("round5_media_paths_",StringComparison.Ordinal))
        {
            using(var span=source.StartActivity("chat media paths",ActivityKind.Client))
            {
                span?.SetTag("output.value","{\"parts.1.inlineData.mimeType\":\"audio/wav\",\"parts.1.inlineData.data\":\"QUJDREVG\",\"parts.1.inlineData.transcript\":\"spoken words\"}");
                if(mode.EndsWith("_indexed",StringComparison.Ordinal))
                {
                    span?.SetTag("input.parts.0.inlineData.data.0","R0hJSktM");
                    span?.SetTag("input.parts.0.inlineData.mimeType","image/png");
                }
            }
            Console.WriteLine("media-paths-complete"); return;
        }
        if(mode.StartsWith("round5_multipart_",StringComparison.Ordinal))
        {
            bool streamed=mode.Contains("stream",StringComparison.Ordinal);
            using var file=new ByteArrayContent("multipart-file-bytes"u8.ToArray()); file.Headers.ContentType=new("audio/wav");
            using var multipart=new MultipartFormDataContent("traceai-startup-multipart") { { new StringContent("whisper-1"),"model" },{ file,"file","clip.wav" } };
            if(streamed) multipart.Add(new StreamContent(new MemoryStream("BODY_CREDENTIAL_9876"u8.ToArray())),"client_secret");
            bool before=file.Headers.Contains("Content-Length");
            using(var client=new HttpClient())
            using(var request=new HttpRequestMessage(HttpMethod.Post,url) { Content=multipart })
            using(var response=mode.EndsWith("_sync",StringComparison.Ordinal)?client.Send(request):await client.SendAsync(request))
            {
                byte[] body=await response.Content.ReadAsByteArrayAsync();
                Console.WriteLine($"response={(int)response.StatusCode};body={Convert.ToBase64String(body)};file_header_before={before};file_header_after={file.Headers.Contains("Content-Length")}");
            }
            if(streamed)
                using(var later=source.StartActivity("chat later multipart",ActivityKind.Client))
                {
                    later?.SetTag("later.echo","BODY_CREDENTIAL_9876");
                    later?.SetStatus(ActivityStatusCode.Error,"BODY_CREDENTIAL_9876");
                    later?.AddEvent(new ActivityEvent("BODY_CREDENTIAL_9876",tags:new ActivityTagsCollection { ["note"]="BODY_CREDENTIAL_9876" }));
                }
            return;
        }
        string coding=mode.Split('_')[2];
        byte[] json=Encoding.UTF8.GetBytes("{\"messages\":[],\"mcp_servers\":[{\"authorization_token\":\"COMPRESSED_MCP_TOKEN_9876\"}]}");
        using var memory=new MemoryStream();
        using(Stream encoder=coding switch { "gzip"=>new GZipStream(memory,CompressionLevel.Fastest,true),"br"=>new BrotliStream(memory,CompressionLevel.Fastest,true),_=>new DeflateStream(memory,CompressionLevel.Fastest,true) }) encoder.Write(json);
        using var content=new ByteArrayContent(memory.ToArray()); content.Headers.ContentType=new("application/json"); content.Headers.ContentEncoding.Add(coding);
        byte[] received;
        using(var model=mode.EndsWith("_wire",StringComparison.Ordinal)?null:source.StartActivity("chat compressed credentials",ActivityKind.Client))
        using(var native=model==null?null:new Activity("native sdk client").Start())
        using(var retry=model==null?null:new Activity("native retry").Start())
        using(var client=new HttpClient())
        using(var response=await client.PostAsync(url,content))
        {
            received=await response.Content.ReadAsByteArrayAsync();
            model?.SetStatus(ActivityStatusCode.Error,Encoding.UTF8.GetString(received));
            model?.AddEvent(new ActivityEvent("exception",tags:new ActivityTagsCollection { ["exception.message"]=new[] { "COMPRESSED_MCP_TOKEN_9876" } }));
            Console.WriteLine($"response={(int)response.StatusCode};body={Convert.ToBase64String(received)}");
        }
        // No shared ancestry, and starts after the request: process-wide echo protection.
        using(var later=source.StartActivity("chat later",ActivityKind.Client))
        {
            later?.SetTag("later.echo","COMPRESSED_MCP_TOKEN_9876");
            later?.AddEvent(new ActivityEvent("later COMPRESSED_MCP_TOKEN_9876",tags:new ActivityTagsCollection { ["note"]=Encoding.UTF8.GetString(received) }));
        }
    }
}
