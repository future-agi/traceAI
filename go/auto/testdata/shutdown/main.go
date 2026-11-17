// A short-lived program that returns from main right after its AI call.
// Mode "shutdown" flushes with auto.Shutdown; mode "immediate" does not.
package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/future-agi/traceAI/go/auto"
)

func main() {
	if os.Args[2] == "shutdown" {
		defer func() {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = auto.Shutdown(ctx)
		}()
	}
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
