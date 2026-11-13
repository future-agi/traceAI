using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>Framing and output recognition run after capture. Arrival offsets are
/// recorded before copying bytes at the transport boundary, never at parse time.</summary>
internal static class StreamMilestones
{
    internal const int MaxArrivals=4096;
    internal static IEnumerable<(string Text,int End)> Frames(byte[] bytes,bool sse)
    {
        int start=0,line=0;
        for(int i=0;i<bytes.Length;i++)
        {
            if(bytes[i]!='\n') continue;
            int length=i-line;
            if(length>0&&bytes[i-1]=='\r') length--;
            if(!sse || length==0)
            {
                yield return (Encoding.UTF8.GetString(bytes,start,i+1-start),i+1);
                start=i+1;
            }
            line=i+1;
        }
        // Even at EOF the delimiter belongs to the frame. An otherwise complete
        // JSON payload without its SSE blank line / NDJSON newline is unobserved.
    }
    internal static JsonNode? Event(string frame,bool sse)
    {
        string? eventName=null;
        var payload=frame;
        if(sse)
        {
            var data=new List<string>();
            foreach(var raw in frame.Split('\n'))
            {
                var line=raw.TrimEnd('\r');
                if(line.StartsWith("data:",StringComparison.Ordinal)) data.Add(line[5..].StartsWith(' ')?line[6..]:line[5..]);
                else if(line.StartsWith("event:",StringComparison.Ordinal)) eventName=line[6..].Trim();
            }
            payload=string.Join('\n',data);
        }
        var node=WireCore.Parse(Encoding.UTF8.GetBytes(payload));
        if(node is JsonObject obj && obj["type"]==null && !string.IsNullOrEmpty(eventName)) obj["type"]=eventName;
        return node;
    }
    internal static List<(JsonNode Item,int End)> ArrayItems(byte[] bytes)
    {
        var items=new List<(JsonNode,int)>();
        // Gemini JSON-array streaming can end between members. Accept complete
        // members only; never synthesize the unfinished usage object or wait for
        // the outer closing bracket to timestamp an already-complete member.
        try
        {
            var reader=new Utf8JsonReader(bytes,isFinalBlock:false,state:default);
            if(!reader.Read()||reader.TokenType!=JsonTokenType.StartArray) return items;
            while(items.Count<WirePrivacy.MaxAttributeNodes && reader.Read() && reader.TokenType!=JsonTokenType.EndArray)
            {
                if(!JsonDocument.TryParseValue(ref reader,out var document)) break;
                using(document)
                {
                    if(JsonNode.Parse(document.RootElement.GetRawText()) is {} item) items.Add((item,checked((int)reader.BytesConsumed)));
                }
            }
        }
        catch(JsonException) { /* Retain only prior, complete members. */ }
        return items;
    }
    internal static int JsonEnd(byte[] bytes)
    {
        var reader=new Utf8JsonReader(bytes,isFinalBlock:false,state:default);
        using var document=JsonDocument.ParseValue(ref reader);
        return checked((int)reader.BytesConsumed);
    }
    private static bool Nonempty(JsonNode? value)=>WireCore.S(value) is { Length:>0 };
    private static bool Arguments(JsonNode? value,bool objects)=>Nonempty(value)||objects&&value is JsonObject { Count:>0 };
    private static bool Function(JsonNode? node,bool objects=false)=>Nonempty(WireCore.G(node,"name")) ||
        Arguments(WireCore.G(node,"arguments"),objects)||Arguments(WireCore.G(node,"args"),objects);
    private static bool Calls(JsonNode? node,bool objects=false)=>node is JsonArray array?array.Any(call=>Function(WireCore.G(call,"function")??call,objects)):
        Function(WireCore.G(node,"function")??node,objects);
    private static bool ContentPart(JsonNode? part)
    {
        string? type=WireCore.S(WireCore.G(part,"type"));
        if(type is not (null or "text" or "thinking")) return false;
        return Nonempty(part)||Nonempty(WireCore.G(part,"text"))||Nonempty(WireCore.G(part,"thinking"))||
            WireCore.A(WireCore.G(part,"thinking")).Any(text=>WireCore.S(WireCore.G(text,"type"))=="text"&&Nonempty(WireCore.G(text,"text")));
    }
    private static bool Content(JsonNode? value)=>value is JsonArray?WireCore.A(value).Any(ContentPart):ContentPart(value);
    private static bool Delta(JsonNode? node,bool objects=false)=>Content(WireCore.G(node,"content")) || Nonempty(WireCore.G(node,"text")) ||
        Nonempty(WireCore.G(node,"refusal")) || Nonempty(WireCore.G(node,"reasoning")) || Nonempty(WireCore.G(node,"reasoning_content")) || Nonempty(WireCore.G(node,"thinking")) ||
        Nonempty(WireCore.G(node,"audio","data")) || Nonempty(WireCore.G(node,"audio","transcript")) || Calls(WireCore.G(node,"tool_calls"),objects) || Function(WireCore.G(node,"function_call"));
    private static bool AudioPart(JsonNode? part)
    {
        var data=WireCore.G(part,"inlineData")??WireCore.G(part,"inline_data");
        var mime=WireCore.S(WireCore.G(data,"mimeType"))??WireCore.S(WireCore.G(data,"mime_type"));
        return mime?.StartsWith("audio/",StringComparison.OrdinalIgnoreCase)==true&&Nonempty(WireCore.G(data,"data"));
    }
    private static bool ResponseItem(JsonNode? item)=>WireCore.S(WireCore.G(item,"type")) switch {
        "function_call" or "tool_call" or "mcp_call"=>Function(item)||Function(WireCore.G(item,"function")),
        "custom_tool_call"=>Function(item)||Function(WireCore.G(item,"function"))||Nonempty(WireCore.G(item,"input")),
        "code_interpreter_call"=>Nonempty(WireCore.G(item,"code")),
        _=>false
    };
    internal static bool HasOutput(string format,JsonNode item)
    {
        var type=WireCore.S(WireCore.G(item,"type"))??WireCore.S(WireCore.G(item,"event_type"));
        switch(format)
        {
            case "openai.chat": case "openai.completions":
                return WireCore.A(WireCore.G(item,"choices")).Any(choice=>Delta(WireCore.G(choice,"delta"))||Nonempty(WireCore.G(choice,"text")));
            case "openai.responses":
                return type is "response.output_text.delta" or "response.refusal.delta" or "response.reasoning_text.delta" or "response.reasoning_summary_text.delta" or
                    "response.function_call_arguments.delta" or "response.mcp_call_arguments.delta" or "response.code_interpreter_call_code.delta" or "response.custom_tool_call_input.delta" or
                    "response.audio.delta" or "response.audio.transcript.delta" or "response.output_audio.delta" or "response.audio_transcript.delta"
                    ?Nonempty(WireCore.G(item,"delta")):
                    type=="response.output_item.added"&&ResponseItem(WireCore.G(item,"item"));
            case "anthropic.messages": case "bedrock.invoke":
                return type=="content_block_delta"&&(Nonempty(WireCore.G(item,"delta","text"))||Nonempty(WireCore.G(item,"delta","thinking"))||Nonempty(WireCore.G(item,"delta","partial_json"))) ||
                    type=="content_block_start"&&(Nonempty(WireCore.G(item,"content_block","text"))||Nonempty(WireCore.G(item,"content_block","thinking"))||
                        WireCore.S(WireCore.G(item,"content_block","type")) is "tool_use" or "server_tool_use" or "mcp_tool_use"&&Nonempty(WireCore.G(item,"content_block","name")));
            case "gemini.generate_content":
                return WireCore.A(WireCore.G(item,"candidates")).Any(candidate=>WireCore.A(WireCore.G(candidate,"content","parts")).Any(part=>
                    Nonempty(WireCore.G(part,"text"))||Function(WireCore.G(part,"functionCall"),true)||Nonempty(WireCore.G(part,"executableCode","code"))||AudioPart(part)));
            case "bedrock.converse":
                return Nonempty(WireCore.G(item,"contentBlockDelta","delta","text"))||Nonempty(WireCore.G(item,"contentBlockDelta","delta","reasoningContent","text"))||
                    Nonempty(WireCore.G(item,"contentBlockDelta","delta","toolUse","input"))||Nonempty(WireCore.G(item,"contentBlockDelta","delta","toolUse","name"))||
                    Nonempty(WireCore.G(item,"contentBlockStart","start","toolUse","name"));
            case "cohere.chat":
                return type is "content-start" or "content-delta"&&Content(WireCore.G(item,"delta","message","content")) ||
                    type=="text-generation"&&Nonempty(WireCore.G(item,"text")) ||
                    type is "tool-call-start" or "tool-call-delta"&&Calls(WireCore.G(item,"delta","message","tool_calls")) ||
                    type=="tool-plan-delta"&&Nonempty(WireCore.G(item,"delta","message","tool_plan")) ||
                    type=="tool-calls-chunk"&&(Nonempty(WireCore.G(item,"text"))||CohereTool(WireCore.G(item,"tool_call_delta"))) ||
                    type=="tool-calls-generation"&&WireCore.A(WireCore.G(item,"tool_calls")).Any(CohereTool);
            case "ollama.chat": case "ollama.generate":
                return Delta(WireCore.G(item,"message"),true)||Nonempty(WireCore.G(item,"response"))||Nonempty(WireCore.G(item,"thinking"));
            case "openai.audio_transcription": return type=="transcript.text.delta"&&Nonempty(WireCore.G(item,"delta"));
            case "cartesia.tts": return type=="chunk"&&Nonempty(WireCore.G(item,"data"));
            case "openai.audio_speech": case "deepgram.speak": case "elevenlabs.tts":
                return Nonempty(WireCore.G(item,"data"))||Nonempty(WireCore.G(item,"audio_base64"))||Nonempty(WireCore.G(item,"audio"));
            default: return false;
        }
    }
    private static bool CohereTool(JsonNode? tool)=>Nonempty(WireCore.G(tool,"name"))||
        Arguments(WireCore.G(tool,"parameters"),true)||Nonempty(WireCore.G(tool,"text"));
}
