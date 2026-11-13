package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	_ "github.com/future-agi/traceAI/go/auto"
)

func main() {
	for i := 0; i < 2; i++ {
		resp, err := http.Post(os.Args[1]+"/v1/chat/completions", "application/json", strings.NewReader(`{"model":"test-model","messages":[{"role":"user","content":"Hi"}]}`))
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		b, err := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if err != nil {
			os.Exit(2)
		}
		_, _ = os.Stdout.Write(b)
	}
	// Only needed because this intentionally has no tracing calls at all.
	// Real short-lived programs should call auto.Shutdown instead of sleeping.
	time.Sleep(800 * time.Millisecond)
}
