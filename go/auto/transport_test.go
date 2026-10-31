package auto_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/textproto"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
)

func TestRequestWithoutReplayAndFailedReplay(t *testing.T) {
	for _, replay := range []bool{false, true} {
		t.Run(map[bool]string{false: "no_GetBody", true: "failed_GetBody"}[replay], func(t *testing.T) {
			rec, _ := setup(t)
			server := localServer(t, responseJSON, "application/json", 200)
			req, _ := http.NewRequest("POST", server.URL+"/v1/chat/completions", io.NopCloser(strings.NewReader(requestJSON)))
			req.Header.Set("Content-Type", "application/json")
			if replay {
				req.GetBody = func() (io.ReadCloser, error) { return nil, errors.New("cannot replay") }
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if len(wires(rec)) != 1 {
				t.Fatal("no capture for non-replayable body")
			}
		})
	}
}
func TestMultipartStreamingNeverBuffersFile(t *testing.T) {
	rec, _ := setup(t)
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	_ = w.WriteField("model", "whisper-1")
	_ = w.WriteField("language", "en")
	header := textproto.MIMEHeader{"Content-Disposition": []string{`form-data; name="file"; filename="audio.wav"`}, "Content-Type": []string{"audio/wav"}}
	part, _ := w.CreatePart(header)
	audio := bytes.Repeat([]byte{0, 255, 17, 0}, 1024)
	_, _ = part.Write(audio)
	_ = w.Close()
	original := append([]byte(nil), buf.Bytes()...)
	base := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		got, _ := io.ReadAll(r.Body)
		if !bytes.Equal(got, original) {
			t.Error("multipart request changed")
		}
		_ = r.Body.Close()
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"text":"Hello"}`))}, nil
	}))
	req, _ := http.NewRequest("POST", "https://api.openai.com/v1/audio/transcriptions", io.NopCloser(bytes.NewReader(original)))
	req.Header.Set("Content-Type", w.FormDataContentType())
	resp, err := base.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	ss := wires(rec)
	if len(ss) != 1 || value(ss[0], "traceai.audio.input_bytes") != int64(len(audio)) || value(ss[0], "output.value") != "Hello" {
		t.Fatal("multipart capture failed")
	}
	if strings.Contains(value(ss[0], "input.value").(string), "\\u00ff") {
		t.Fatal("file bytes captured")
	}
}
func TestTruncationKeepsSpanAndApplicationBytes(t *testing.T) {
	for _, side := range []string{"request", "response"} {
		t.Run(side, func(t *testing.T) {
			t.Setenv("FI_WIRE_MAX_BODY_BYTES", "128")
			rec, _ := setup(t)
			rq := requestJSON
			rp := responseJSON
			if side == "request" {
				rq = `{"model":"test","messages":[],"oversize":"` + strings.Repeat("x", 1000) + `"}`
			}
			server := httptestServer(t, func(r *http.Request) string {
				got, _ := io.ReadAll(r.Body)
				if string(got) != rq {
					t.Error("truncated application request")
				}
				return rp
			})
			resp, err := http.Post(server+"/v1/chat/completions", "application/json", strings.NewReader(rq))
			if err != nil {
				t.Fatal(err)
			}
			got, _ := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if string(got) != rp {
				t.Fatal("truncated application response")
			}
			ss := wires(rec)
			if side == "request" {
				// The revised contract does not let an oversized body claim a
				// proxy/application route without body validation.
				if len(ss) != 0 {
					t.Fatal("unknown host's oversized request bypassed validation")
				}
			} else if len(ss) != 1 || value(ss[0], "traceai.wire.truncated") != true {
				t.Fatalf("truncated response lost span: %d", len(ss))
			}
		})
	}
}
func httptestServer(t *testing.T, f func(*http.Request) string) string {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, f(r))
	}))
	t.Cleanup(s.Close)
	return s.URL
}
func TestAudioOutputAndEarlyClose(t *testing.T) {
	rec, _ := setup(t)
	audio := bytes.Repeat([]byte{19}, 1000)
	base := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, r.Body)
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"audio/mpeg"}}, Body: io.NopCloser(bytes.NewReader(audio))}, nil
	}))
	req, _ := http.NewRequest("POST", "https://api.openai.com/v1/audio/speech", strings.NewReader(`{"model":"tts","input":"Hi"}`))
	req.Header.Set("Content-Type", "application/json")
	resp, _ := base.RoundTrip(req)
	out := make([]byte, 100)
	_, _ = io.ReadFull(resp.Body, out)
	_ = resp.Body.Close()
	_ = resp.Body.Close()
	ss := wires(rec)
	if len(ss) != 1 || value(ss[0], "traceai.audio.output_bytes") != int64(100) || value(ss[0], "output.value") != nil {
		t.Fatal("audio count or close finalization")
	}
	if !bytes.Equal(out, audio[:100]) {
		t.Fatal("audio bytes changed")
	}
	_ = auto.ForceFlush(context.Background())
}

func TestProviderCredentialsCannotLeakThroughErrorEcho(t *testing.T) {
	rec, _ := setup(t)
	base := auto.Transport(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 401, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"error":{"message":"invalid provider-secret-value query-secret-value"}}`))}, nil
	}))
	req, _ := http.NewRequest("POST", "https://api.openai.com/v1/chat/completions?api_key=query-secret-value", strings.NewReader(requestJSON))
	req.Header.Set("Authorization", "Bearer provider-secret-value")
	req.Header.Set("Content-Type", "application/json")
	resp, err := base.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	ss := wires(rec)
	if len(ss) != 1 {
		t.Fatal("missing error span")
	}
	if strings.Contains(ss[0].Status().Description, "secret-value") {
		t.Fatal("provider credential leaked in error description")
	}
}
