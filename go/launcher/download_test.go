package launcher

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReleaseDownloadToCache(t *testing.T) {
	for _, runtime := range []string{"java", "dotnet"} {
		t.Run(runtime, func(t *testing.T) {
			var archive bytes.Buffer
			writer := zip.NewWriter(&archive)
			name, content := agentEntry(runtime), "startup-hook"
			if runtime == "java" {
				name, content = "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPremain-Class: example.Agent\n"
			}
			file, err := writer.Create(name)
			if err != nil {
				t.Fatal(err)
			}
			fmt.Fprint(file, content)
			if err := writer.Close(); err != nil {
				t.Fatal(err)
			}
			digest := sha256.Sum256(archive.Bytes())
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("X-Api-Key") != "" || r.Header.Get("Authorization") != "" || r.Header.Get("X-Secret-Key") != "" {
					t.Error("release download sent collector credentials")
				}
				if r.URL.Path == "/redirect" {
					http.Redirect(w, r, "http://example.com/agent", http.StatusFound)
					return
				}
				if r.URL.Path != "/agent" {
					http.NotFound(w, r)
					return
				}
				w.Write(archive.Bytes())
			}))
			defer server.Close()
			// Trust this local fixture's certificate only for the duration of this
			// serial test. Production still uses the standard system TLS roots.
			originalTransport := http.DefaultTransport
			http.DefaultTransport = server.Client().Transport
			defer func() { http.DefaultTransport = originalTransport }()
			env := baseEnv(t)
			if err := installRelease(runtime, server.URL+"/agent", fmt.Sprintf("%x", digest), env); err != nil {
				t.Fatal(err)
			}
			cache, _ := cacheDirectory(environmentMap(env))
			pointer := filepath.Join(cache, "agents", runtime+".json")
			before, err := os.ReadFile(pointer)
			if err != nil || len(before) == 0 {
				t.Fatal("no published payload", err)
			}
			for _, address := range []string{server.URL + "/redirect", server.URL + "/missing"} {
				if err := installRelease(runtime, address, fmt.Sprintf("%x", digest), env); err == nil {
					t.Fatal("invalid release accepted", address)
				}
			}
			if err := installRelease(runtime, server.URL+"/agent", strings.Repeat("0", 64), env); err == nil {
				t.Fatal("bad checksum accepted")
			}
			after, _ := os.ReadFile(pointer)
			if !bytes.Equal(before, after) {
				t.Fatal("failed download replaced an installed payload")
			}
		})
	}
}
