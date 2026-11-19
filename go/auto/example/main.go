// Command example makes one OpenAI-compatible chat request with plain
// net/http. Importing github.com/future-agi/traceAI/go/auto is the only tracing
// change: package initialization wraps http.DefaultTransport, so the request is
// exported to Future AGI as a wire-layer span.
//
//	FI_API_KEY=... FI_SECRET_KEY=... OPENAI_API_KEY=... go run ./example
//
// OPENAI_BASE_URL (default https://api.openai.com/v1) and OPENAI_MODEL
// (default gpt-4o-mini) select any OpenAI-compatible provider.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/future-agi/traceAI/go/auto"
)

func main() {
	err := run()
	flush()
	if err != nil {
		log.Fatal(err)
	}
}

// flush exports queued spans. Go has no exit hook, so a short program calls
// auto.Shutdown (or auto.ForceFlush) before main returns.
func flush() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := auto.Shutdown(ctx); err != nil {
		log.Printf("traceai shutdown: %v", err)
	}
}

func run() error {
	baseURL := strings.TrimRight(getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"), "/")
	body, err := json.Marshal(map[string]any{
		"model":    getenv("OPENAI_MODEL", "gpt-4o-mini"),
		"messages": []map[string]string{{"role": "user", "content": "Say hello in five words."}},
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, baseURL+"/chat/completions", strings.NewReader(string(body)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+os.Getenv("OPENAI_API_KEY"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("provider returned %s: %s", resp.Status, data)
	}
	fmt.Println(string(data))
	return nil
}

func getenv(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
