package launcher

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
)

func TestReleaseRedirectsNeverForwardSignedURLReferer(t *testing.T) {
	var archive bytes.Buffer
	writer := zip.NewWriter(&archive)
	manifest, err := writer.Create("META-INF/MANIFEST.MF")
	if err != nil {
		t.Fatal(err)
	}
	fmt.Fprint(manifest, "Manifest-Version: 1.0\nPremain-Class: example.Agent\n")
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(archive.Bytes())
	for _, mode := range []string{"same_origin", "cross_origin", "multiple_hops"} {
		t.Run(mode, func(t *testing.T) {
			// A signed source URL is not a safe Referer, even when the target
			// shares its origin. A subsequent redirect must not restore it.
			const signature = "VERIFY_SIGNED_URL_SECRET_928401"
			checkHeaders := func(r *http.Request) {
				if r.Header.Get("Referer") != "" {
					t.Error("release request forwarded a Referer")
				}
				for _, key := range []string{"Authorization", "X-Api-Key", "X-Secret-Key"} {
					if r.Header.Get(key) != "" {
						t.Error("release request forwarded credentials", key)
					}
				}
			}
			destination := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				checkHeaders(r)
				w.Write(archive.Bytes())
			}))
			defer destination.Close()
			source := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				checkHeaders(r)
				switch r.URL.Path {
				case "/start":
					target := destination.URL + "/agent.jar"
					if mode == "same_origin" {
						target = "/agent.jar"
					} else if mode == "multiple_hops" {
						target = "/next?X-Amz-Signature=" + signature + "_NEXT"
					}
					http.Redirect(w, r, target, http.StatusFound)
				case "/next":
					http.Redirect(w, r, destination.URL+"/agent.jar", http.StatusTemporaryRedirect)
				case "/agent.jar":
					w.Write(archive.Bytes())
				default:
					http.NotFound(w, r)
				}
			}))
			defer source.Close()
			// Both local fixtures use httptest's certificate; trust only that
			// certificate for this serial test. Production TLS remains unchanged.
			originalTransport := http.DefaultTransport
			http.DefaultTransport = source.Client().Transport
			defer func() { http.DefaultTransport = originalTransport }()
			env := baseEnv(t)
			if err := installRelease("java", source.URL+"/start?X-Amz-Signature="+signature, fmt.Sprintf("%x", digest), env); err != nil {
				t.Fatal(err)
			}
			cache, err := cacheDirectory(environmentMap(env))
			if err != nil {
				t.Fatal(err)
			}
			installed := cachedAgent(cache, "java")
			if installed == "" || filepath.Base(installed) != agentEntry("java") {
				t.Fatal("checksum-valid redirect did not publish the agent")
			}
		})
	}
}
