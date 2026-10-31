package wire

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

const fixtureRoot = "../../../../spec/auto-instrumentation/fixtures"

// Recursive discovery: baseline files plus every additive round6 case. Other
// spec work may append fixtures, so assert a floor rather than an exact count.
func fixtureFiles(t *testing.T) []string {
	t.Helper()
	var files []string
	err := filepath.WalkDir(fixtureRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && strings.HasSuffix(path, ".json") {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(files)
	baseline := 0
	for _, f := range files {
		if filepath.Dir(f) == filepath.Clean(fixtureRoot) {
			baseline++
		}
	}
	if baseline < 47 || len(files)-baseline < 293 {
		t.Fatalf("want at least 47 baseline and 293 round6 fixtures, got %d and %d", baseline, len(files)-baseline)
	}
	return files
}

func fixtureLabel(path string) string {
	rel, err := filepath.Rel(fixtureRoot, path)
	if err != nil {
		return filepath.Base(path)
	}
	return rel
}

func readFixture(t *testing.T, path string) object {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture object
	if err = json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	return fixture
}

// Spec A10: fixtures 157-166 carry decoded Bedrock payloads in a synthetic SSE
// envelope. They are routed to the decoded-event recognizer harness.
func decodedRecognizerFixture(fixture object) bool {
	return strings.HasPrefix(str(fixture["name"]), "milestone-a10-bedrock-")
}

// replayChunks builds the complete wire body, partitions it exactly as the spec
// describes (chunk_sizes, else one chunk per SSE/NDJSON item, one JSON/text
// chunk, binary/gzip in halves) and then applies error_after_chunks.
func replayChunks(t *testing.T, spec object) (http Response, chunks [][]byte, offsets []float64, failed bool) {
	t.Helper()
	header, data := fixtureBody(spec, true)
	status, _ := integer(spec["status"])
	http = Response{Status: int(status), Header: header}
	switch {
	case len(arr(spec["chunk_sizes"])) > 0:
		offset := 0
		for _, raw := range arr(spec["chunk_sizes"]) {
			n, ok := integer(raw)
			if !ok || n < 0 || n > int64(len(data)-offset) {
				t.Fatal("invalid fixture chunk sizes")
			}
			chunks = append(chunks, data[offset:offset+int(n)])
			offset += int(n)
		}
		if offset != len(data) {
			t.Fatalf("chunk sizes cover %d bytes, wire body has %d", offset, len(data))
		}
	case spec["gzip"] == true || Binary(ContentType(header)):
		chunks = [][]byte{data[:len(data)/2], data[len(data)/2:]}
	case spec["sse"] != nil || spec["ndjson"] != nil:
		sep := []byte("\n")
		if spec["sse"] != nil {
			sep = []byte("\n\n")
		}
		// Each item is one chunk; an SSE item includes its event: line.
		for _, part := range bytes.SplitAfter(data, sep) {
			if len(part) > 0 {
				chunks = append(chunks, part)
			}
		}
	default:
		chunks = [][]byte{data}
	}
	if raw := arr(spec["arrival_offsets_ms"]); raw != nil {
		if len(raw) != len(chunks) {
			t.Fatalf("%d arrival offsets for %d chunks", len(raw), len(chunks))
		}
		for _, v := range raw {
			ms, ok := v.(float64)
			if !ok || ms < 0 || len(offsets) > 0 && ms < offsets[len(offsets)-1] {
				t.Fatal("invalid arrival offsets")
			}
			offsets = append(offsets, ms)
		}
	}
	if count, ok := integer(spec["error_after_chunks"]); ok {
		if count < 0 || count > int64(len(chunks)) {
			t.Fatal("invalid failure boundary")
		}
		chunks = chunks[:count]
		if offsets != nil {
			offsets = offsets[:count]
		}
		failed = true
	}
	return http, chunks, offsets, failed
}

// replayResponse injects the recorded transport observations the real hook
// produces: delivered bytes, per-chunk arrival times (controlled clock, never
// sleeps), first non-empty chunk time, and a timeout after a failure boundary.
func replayResponse(t *testing.T, spec object) Response {
	t.Helper()
	resp, chunks, offsets, failed := replayChunks(t, spec)
	var size int64
	for i, chunk := range chunks {
		size += int64(len(chunk))
		if offsets == nil || len(chunk) == 0 {
			continue // Empty transport chunks establish no milestone.
		}
		seconds := offsets[i] / 1000
		if resp.FirstChunkSeconds == nil {
			first := seconds
			resp.FirstChunkSeconds = &first
		}
		resp.Arrivals = append(resp.Arrivals, Arrival{End: size, Seconds: seconds})
	}
	resp.Body = bytes.Join(chunks, nil)
	if resp.Body == nil {
		resp.Body = []byte{}
	}
	resp.Size = size
	if failed {
		resp.Error = context.DeadlineExceeded
	}
	if Binary(ContentType(resp.Header)) {
		resp.Body = nil
	}
	return resp
}

func fixtureRequest(spec object) Request {
	h, b := fixtureBody(spec, false)
	req := Request{Method: str(spec["method"]), URL: str(spec["url"]), Header: h, Body: b, Size: int64(len(b)), Unavailable: spec["unavailable"] == true}
	if req.Unavailable {
		req.Body = nil
	}
	return req
}

func fixtureValue(got any) any {
	var normalized any
	_ = json.Unmarshal([]byte(dump(got)), &normalized)
	return normalized
}

func checkFixture(t *testing.T, fixture object) {
	t.Helper()
	rq := obj(fixture["request"])
	req := fixtureRequest(rq)
	resp := replayResponse(t, obj(fixture["response"]))
	result := Build(Find(req.Method, req.URL), req, resp)
	expect := obj(fixture["expect"])
	if expect["captured"] == false {
		if result != nil {
			t.Fatalf("unexpected capture %s", result.Name)
		}
		return
	}
	if result == nil {
		t.Fatal("not captured")
	}
	checkAttributes(t, result.Attributes, expect)
	if name, ok := expect["name"]; ok && result.Name != str(name) {
		t.Errorf("name %q want %q", result.Name, name)
	}
	if status, ok := expect["status"]; ok && result.OK != (status == "OK") {
		t.Errorf("status OK=%v want %v", result.OK, status)
	}
	if d, ok := expect["status_description"]; ok && result.Description != str(d) {
		t.Errorf("description %q want %q", result.Description, d)
	}
}

func checkAttributes(t *testing.T, attrs map[string]any, expect object) {
	t.Helper()
	for k, want := range obj(expect["attributes"]) {
		got, ok := attrs[k]
		if !ok {
			t.Errorf("missing %s (want %v)", k, want)
			continue
		}
		normalized := fixtureValue(got)
		if w, ok := want.(float64); ok {
			if g, ok := normalized.(float64); ok && math.Abs(g-w) < 1e-9 {
				continue
			}
		}
		if !reflect.DeepEqual(want, normalized) {
			t.Errorf("%s=%#v want %#v", k, got, want)
		}
	}
	for _, k := range arr(expect["present"]) {
		if _, yes := attrs[str(k)]; !yes {
			t.Errorf("missing %s", k)
		}
	}
	for key, forbidden := range obj(expect["not_contains"]) {
		if got, exists := attrs[key]; exists {
			for _, value := range arr(forbidden) {
				if strings.Contains(fmt.Sprint(got), str(value)) {
					t.Errorf("%s contains forbidden substring %q", key, value)
				}
			}
		}
	}
	for _, k := range arr(expect["absent"]) {
		if v, yes := attrs[str(k)]; yes {
			t.Errorf("unexpected %s=%v", k, v)
		}
	}
}

// Decoded-event recognizer harness (spec A10 fixtures 157-166): the decoded
// payloads keep their supplied arrival timestamps and go straight to the same
// frame/recognizer code the wire layer uses, bypassing endpoint routing.
func TestA10DecodedRecognizerFixtures(t *testing.T) {
	count := 0
	for _, f := range fixtureFiles(t) {
		fixture := readFixture(t, f)
		if !decodedRecognizerFixture(fixture) {
			continue
		}
		count++
		t.Run(fixtureLabel(f), func(t *testing.T) {
			resp := replayResponse(t, obj(fixture["response"]))
			attrs := object{}
			for k, v := range streamMilestones(resp, decodeBody(resp), false) {
				attrs[k] = v
			}
			expect := obj(fixture["expect"])
			milestones := object{"attributes": object{}, "absent": []any{}}
			for k, v := range obj(expect["attributes"]) {
				if strings.HasPrefix(k, "gen_ai.response.time_to_first_") {
					obj(milestones["attributes"])[k] = v
				}
			}
			for _, k := range arr(expect["absent"]) {
				if strings.HasPrefix(str(k), "gen_ai.response.time_to_first_") {
					milestones["absent"] = append(arr(milestones["absent"]), k)
				}
			}
			checkAttributes(t, attrs, milestones)
		})
	}
	if count < 10 {
		t.Fatalf("want at least 10 decoded Bedrock recognizer fixtures, found %d", count)
	}
}
