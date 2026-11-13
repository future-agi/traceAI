package auto

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestS6AtomicPairsExporterAuth(t *testing.T) {
	for _, half := range []string{"API_KEY", "SECRET_KEY"} {
		t.Run(half, func(t *testing.T) {
			provider := exporterRegressionSetup(t)
			t.Setenv("FI_API_KEY", "")
			t.Setenv("FI_SECRET_KEY", "")
			t.Setenv("FI_"+half, "ENV_ONLY_PRIVATE")
			file := filepath.Join(t.TempDir(), "futureagi.toml")
			if err := os.WriteFile(file, []byte("api_key = 'FILE_API_PRIVATE'\nsecret_key = 'FILE_SECRET_PRIVATE'\n"), 0600); err != nil {
				t.Fatal(err)
			}
			t.Setenv("FI_CONFIG_FILE", file)
			headers := make(chan http.Header, 1)
			collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.Copy(io.Discard, r.Body)
				select {
				case headers <- r.Header.Clone():
				default:
				}
				w.Header().Set("Content-Type", "application/x-protobuf")
			}))
			defer collector.Close()
			t.Setenv("FI_BASE_URL", collector.URL)
			if _, err := InitForTest(context.Background()); err != nil {
				t.Fatal(err)
			}
			exportRegressionSpan(t, provider)
			select {
			case h := <-headers:
				if h.Get("X-Api-Key") != "FILE_API_PRIVATE" || h.Get("X-Secret-Key") != "FILE_SECRET_PRIVATE" {
					t.Fatal("OTLP authentication mixed credential sources")
				}
			case <-time.After(time.Second):
				t.Fatal("no loopback collector export")
			}
		})
	}
}
