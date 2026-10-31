package main

import (
    "bytes"
    "encoding/base64"
    "encoding/json"
    "fmt"
    "io"
    "net/http"
    "os"
    "strconv"
    "time"
)

func emit(value any) { if err := json.NewEncoder(os.Stdout).Encode(value); err != nil { panic(err) } }
func number(key string) int { value, err := strconv.Atoi(os.Getenv(key)); if err != nil { panic(err) }; return value }
func main() {
    emit(map[string]any{"kind":"ready"})
    if os.Getenv("BENCH_STARTUP_ONLY") == "1" { return }
    base := os.Getenv("BENCH_PROVIDER")
    client := &http.Client{Timeout:20*time.Second}
    n, warmup := number("BENCH_N"), number("BENCH_WARMUP")
    for _, stream := range []bool{false,true} {
        for i:=0; i<n+warmup; i++ {
            s:=0; if stream { s=1 }
            id:=fmt.Sprintf("%s-%d-%d",os.Getenv("BENCH_PAIR"),s,i)
            body,_:=json.Marshal(map[string]any{"model":"gpt-4o-mini","messages":[]any{map[string]any{"role":"user","content":id}},"stream":stream,"stream_options":map[string]bool{"include_usage":true}})
            start:=time.Now()
            resp,err:=client.Post(base+"/v1/chat/completions","application/json",bytes.NewReader(body)); if err!=nil { panic(err) }
            if resp.StatusCode!=200 { panic(resp.Status) }
            var result bytes.Buffer; var first time.Time
            buffer:=make([]byte,4096)
            for { count,err:=resp.Body.Read(buffer); if count>0 { if first.IsZero() { first=time.Now() }; result.Write(buffer[:count]) }; if err==io.EOF { break }; if err!=nil { panic(err) } }
            resp.Body.Close(); end:=time.Now()
            emit(map[string]any{"kind":"call","id":id,"stream":stream,"warmup":i<warmup,"latency_us":float64(end.Sub(start).Nanoseconds())/1000,"ttfc_us":float64(first.Sub(start).Nanoseconds())/1000,"body_b64":base64.StdEncoding.EncodeToString(result.Bytes())})
        }
    }
    resp,err:=client.Get(base+"/health"); if err!=nil { panic(err) }; io.Copy(io.Discard,resp.Body); resp.Body.Close()
    resp,err=client.Post(base+"/v1/chat/completions","application/json",bytes.NewBufferString(`{"not_ai":true}`)); if err!=nil { panic(err) }; io.Copy(io.Discard,resp.Body); resp.Body.Close()
    emit(map[string]any{"kind":"done"})
    seconds,err:=strconv.ParseFloat(os.Getenv("BENCH_SETTLE_SECONDS"),64); if err!=nil { panic(err) }; time.Sleep(time.Duration(seconds*float64(time.Second)))
}
