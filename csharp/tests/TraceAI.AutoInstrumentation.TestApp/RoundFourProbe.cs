using System.Diagnostics;
using System.Text;
using System.Text.Json;

internal static class RoundFourProbe
{
    internal static async Task Run(string url,string mode)
    {
        if(mode.StartsWith("round4_term_",StringComparison.Ordinal)) { await Termination(url,mode); return; }
        if(mode.StartsWith("round4_credentials_",StringComparison.Ordinal))
        {
            bool aws=mode.Contains("_aws_",StringComparison.Ordinal);
            string secret=aws?"AWS_SESSION_SECRET_9876":"MCP_BODY_SECRET_9876";
            using var source=new ActivitySource("OpenAI.Round4.BodyCredentials");
            using var model=mode.EndsWith("_wire",StringComparison.Ordinal)?null:source.StartActivity("chat credentials",ActivityKind.Client);
            using var native=model==null?null:new Activity("native sdk client").Start();
            using var retry=model==null?null:new Activity("native retry").Start();
            using var credentialClient=new HttpClient();
            if(aws) credentialClient.DefaultRequestHeaders.Add("X-Amz-Security-Token",secret);
            string json=aws?"{\"messages\":[]}":"{\"messages\":[],\"mcp_servers\":[{\"authorization_token\":\""+secret+"\"}]}";
            using var credentialResponse=await credentialClient.PostAsync(url,new StringContent(json,Encoding.UTF8,"application/json"));
            byte[] body=await credentialResponse.Content.ReadAsByteArrayAsync();
            model?.SetStatus(ActivityStatusCode.Error,Encoding.UTF8.GetString(body));
            model?.AddEvent(new ActivityEvent("exception",tags:new ActivityTagsCollection { ["exception.message"]=new[] { secret },["exception.stacktrace"]="provider error "+secret }));
            Console.WriteLine($"response={(int)credentialResponse.StatusCode};body={Convert.ToBase64String(body)}"); return;
        }
        if(mode=="round4_second_privacy")
        {
            using var source=new ActivitySource("OpenAI.Round4.SecondPrivacy");
            using(var span=source.StartActivity("chat privacy",ActivityKind.Client))
            {
                span?.SetTag("input.text","PRIVATE_DOT_TEXT"); span?.SetTag("output.text",new[] { "PRIVATE_DOT_TEXT" });
                span?.SetTag("gen_ai.input.messages.0.parts.0.text","PRIVATE_DOT_TEXT");
                span?.SetTag("media","data:image/png;base64,%51%55%4A%44%52%45%56%47%53%41%3D%3D");
                span?.SetTag("http.request.header.x_amz_security_token",new[] { "AWS_SESSION_SECRET_9876" });
            }
            Console.WriteLine("privacy-complete"); return;
        }
        if(mode.StartsWith("round4_exact_",StringComparison.Ordinal))
        {
            // Deliberately do not dispose any response/stream, or issue an EOF read.
            // Both explicit Flush and ProcessExit must see the already-completed span.
            var exactClient=new HttpClient(); var exactRequest=new HttpRequestMessage(HttpMethod.Post,url) {
                Content=new StringContent("{\"messages\":[],\"input\":\"hello\"}",Encoding.UTF8,"application/json") };
            var exactResponse=await exactClient.SendAsync(exactRequest,HttpCompletionOption.ResponseHeadersRead);
            var exactStream=await exactResponse.Content.ReadAsStreamAsync();
            byte[] received=new byte[checked((int)exactResponse.Content.Headers.ContentLength!.Value)];
            if(mode.EndsWith("_sync",StringComparison.Ordinal)) exactStream.ReadExactly(received);
            else if(mode.EndsWith("_memory",StringComparison.Ordinal)) await exactStream.ReadExactlyAsync(received);
            else
            {
                int count=0;
                while(count<received.Length)
                {
                    int read=await exactStream.ReadAsync(received,count,received.Length-count);
                    if(read==0) throw new EndOfStreamException(); count+=read;
                }
            }
            var entry=AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(a=>a.GetName().Name=="TraceAI.AutoInstrumentation.Runtime")?.GetType("TraceAI.AutoInstrumentation.TraceAIAuto");
            if(entry?.GetMethod("Flush")!.Invoke(null,new object[] { 3000 }) is false) throw new Exception("flush failed");
            Console.WriteLine(Convert.ToBase64String(received));
            GC.KeepAlive(exactResponse); GC.KeepAlive(exactStream); GC.KeepAlive(exactClient); return;
        }
        if(mode.StartsWith("round4_query_",StringComparison.Ordinal))
        {
            using var source=new ActivitySource("OpenAI.Round4.QueryCredentials");
            using var model=mode=="round4_query_wire"?null:source.StartActivity("chat query",ActivityKind.Client);
            using var queryClient=new HttpClient();
            using var queryResponse=await queryClient.PostAsync(url,new StringContent("{\"contents\":[]}",Encoding.UTF8,"application/json"));
            byte[] body=await queryResponse.Content.ReadAsByteArrayAsync();
            model?.SetStatus(ActivityStatusCode.Error,Encoding.UTF8.GetString(body));
            Console.WriteLine($"response={(int)queryResponse.StatusCode};body={Convert.ToBase64String(body)}"); return;
        }
        if(mode=="round4_header")
        {
            using var source=new ActivitySource("OpenAI.Round4.HeaderCredentials");
            using(var span=source.StartActivity("chat header",ActivityKind.Client))
                span?.SetTag("http.request.header.Ocp-Apim-Subscription-Key",new[] { "SUBSCRIPTION_SECRET_9876" });
            Console.WriteLine("header-complete"); return;
        }
        if(mode=="round4_once")
        {
            var hook=AppDomain.CurrentDomain.GetAssemblies().First(a=>a.GetName().Name=="TraceAI.AutoInstrumentation").GetType("StartupHook")!;
            hook.GetMethod("Initialize")!.Invoke(null,null); hook.GetMethod("Initialize")!.Invoke(null,null);
            using var onceClient=new HttpClient(); using var onceResponse=await onceClient.PostAsync(url,new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
            Console.WriteLine(await onceResponse.Content.ReadAsStringAsync()); return;
        }
        if(mode=="round4_privacy")
        {
            using var source=new ActivitySource("OpenAI.Round4.explicit-api-key","explicit-secret-key");
            var link=new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"vendor=explicit-api-key",true),
                new ActivityTagsCollection { ["input.value"]="PRIVATE_LINK_INPUT",["password"]="PRIVATE_LINK_PASSWORD" });
            using(var activity=source.StartActivity("chat privacy",ActivityKind.Client,default(ActivityContext),links:new[] { link }))
            {
                activity!.TraceStateString="vendor=explicit-secret-key"; activity.SetTag("input.value","PRIVATE_INPUT");
                activity.SetTag("note","explicit-api-key"); activity.SetTag("nonfinite",double.NaN);
                activity.SetStatus(ActivityStatusCode.Error,"error explicit-secret-key");
                activity.AddEvent(new ActivityEvent("event explicit-api-key",tags:new ActivityTagsCollection { ["input.value"]="PRIVATE_EVENT_INPUT",["exception.message"]="explicit-secret-key" }));
            }
            Console.WriteLine("privacy-complete"); return;
        }
        string operation=mode["round4_parity_".Length..];
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,url) {
            Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        using var response=operation=="sync"?client.Send(request,HttpCompletionOption.ResponseHeadersRead):
            await client.SendAsync(request,operation=="buffered"?HttpCompletionOption.ResponseContentRead:HttpCompletionOption.ResponseHeadersRead);
        bool sameResponse=ReferenceEquals(response,response.EnsureSuccessStatusCode());
        string contentType=response.Content.GetType().FullName!;
        byte[]? bytes=null; string? failure=null; bool sameStream=true,canRead=false,canWrite=false,canSeek=false,canTimeout=false,tokenMatches=true;
        if(operation!="unread")
        {
            using var body=operation=="sync"?response.Content.ReadAsStream():await response.Content.ReadAsStreamAsync();
            sameStream=ReferenceEquals(body,operation=="sync"?response.Content.ReadAsStream():await response.Content.ReadAsStreamAsync());
            canRead=body.CanRead; canWrite=body.CanWrite; canSeek=body.CanSeek; canTimeout=body.CanTimeout;
            using var target=new MemoryStream();
            if(operation=="invalid")
            {
                string Describe(Exception e)=>e.GetType().FullName+":"+(e is ArgumentException argument?argument.ParamName:"");
                string Sync(Action action) { try { action(); return "none"; } catch(Exception e) { return Describe(e); } }
                async Task<string> Async(Func<Task> action)
                {
                    Task pending;
                    try { pending=action(); } catch(Exception e) { return "sync:"+Describe(e); }
                    try { await pending; return "none"; } catch(Exception e) { return "async:"+Describe(e); }
                }
                failure=string.Join("|",Sync(()=>body.Read(null!,0,0)),
                    await Async(()=>body.ReadAsync(null!,0,0)),await Async(()=>body.CopyToAsync(null!)),
                    await Async(()=>body.CopyToAsync(Stream.Null,0)),Sync(()=>body.CopyTo(null!)),Sync(()=>body.CopyTo(Stream.Null,0)));
            }
            else if(operation=="cancel")
            {
                using var cancel=new CancellationTokenSource(); cancel.Cancel();
                try { await body.ReadAsync(new byte[32],cancel.Token); }
                catch(Exception error) { failure=error.GetType().FullName; tokenMatches=error is OperationCanceledException canceled && canceled.CancellationToken==cancel.Token; }
            }
            else if(operation=="partial") { var prefix=new byte[2]; int count=await body.ReadAsync(prefix); bytes=prefix[..count]; }
            else
            {
                if(operation=="zero") { int count=await body.ReadAsync(Memory<byte>.Empty); if(count!=0) throw new Exception("nonzero empty read"); }
                if(operation=="sync") body.CopyTo(target); else await body.CopyToAsync(target);
                bytes=target.ToArray();
            }
        }
        Console.WriteLine(JsonSerializer.Serialize(new { sameResponse,contentType,sameStream,canRead,canWrite,canSeek,canTimeout,bytes,failure,tokenMatches }));
    }
    private static async Task Termination(string url,string mode)
    {
        bool cancel=mode.Contains("_cancel_",StringComparison.Ordinal);
        using var signals=new SemaphoreSlim(0);
        using var handler=cancel?System.Runtime.InteropServices.PosixSignalRegistration.Create(System.Runtime.InteropServices.PosixSignal.SIGTERM,context=> {
            context.Cancel=true; signals.Release();
        }):null;
        using var source=new ActivitySource("OpenAI.Round4.Termination");
        using var client=new HttpClient();
        async Task Send()
        {
            using var model=mode.EndsWith("_library",StringComparison.Ordinal)?source.StartActivity("chat termination",ActivityKind.Client):null;
            using var response=await client.PostAsync(url,new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
            await response.Content.ReadAsByteArrayAsync();
        }
        await Send(); // Stop the model/wire span before announcing signal readiness.
        Console.WriteLine("ready");
        if(!cancel) { await Task.Delay(Timeout.Infinite); return; }
        for(int i=0;i<2;i++)
        {
            await signals.WaitAsync(); await Send(); Console.WriteLine("continued");
        }
        await Console.In.ReadLineAsync();
    }
}
