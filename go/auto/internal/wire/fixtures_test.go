package wire

import (
	"bytes"
	"compress/gzip"
	"fmt"
	"net/http"
	"os"
	"testing"
)

func fixtureBody(spec object, response bool) (header http.Header, data []byte) {
	defer func() {
		if response && spec["gzip"] == true {
			var buf bytes.Buffer
			w := gzip.NewWriter(&buf)
			_, _ = w.Write(data)
			_ = w.Close()
			data = buf.Bytes()
			header.Set("Content-Encoding", "gzip")
		}
	}()
	h := http.Header{}
	for k, v := range obj(spec["headers"]) {
		h.Set(k, str(v))
	}
	if v, ok := spec["json"]; ok {
		if h.Get("Content-Type") == "" {
			h.Set("Content-Type", "application/json")
		}
		return h, []byte(dump(v))
	}
	if v, ok := spec["text"]; ok {
		return h, []byte(str(v))
	}
	if v, ok := spec["binary"]; ok {
		b := obj(v)
		if h.Get("Content-Type") == "" {
			h.Set("Content-Type", str(b["content_type"]))
		}
		n, _ := integer(b["size"])
		return h, bytes.Repeat([]byte{1}, int(n))
	}
	if v, ok := spec["multipart"]; ok {
		var buf bytes.Buffer
		const boundary = "traceaifixtureboundary"
		m := obj(v)
		for k, v := range obj(m["fields"]) {
			fmt.Fprintf(&buf, "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n", boundary, k, str(v))
		}
		for _, v := range arr(m["files"]) {
			f := obj(v)
			fmt.Fprintf(&buf, "--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n", boundary, str(f["name"]), str(f["filename"]), str(f["content_type"]))
			n, _ := integer(f["size"])
			buf.Write(make([]byte, int(n)))
			buf.WriteString("\r\n")
		}
		fmt.Fprintf(&buf, "--%s--\r\n", boundary)
		h.Set("Content-Type", "multipart/form-data; boundary="+boundary)
		return h, buf.Bytes()
	}
	var buf bytes.Buffer
	for _, v := range arr(spec["sse"]) {
		if s, ok := v.(string); ok {
			fmt.Fprintf(&buf, "data: %s\n\n", s)
		} else {
			e := obj(v)
			if event := str(e["event"]); event != "" {
				fmt.Fprintf(&buf, "event: %s\n", event)
				delete(e, "event")
			}
			fmt.Fprintf(&buf, "data: %s\n\n", dump(e))
		}
	}
	for _, v := range arr(spec["ndjson"]) {
		fmt.Fprintln(&buf, dump(v))
	}
	return h, buf.Bytes()
}
func TestCatalogIdentical(t *testing.T) {
	b, err := os.ReadFile("../../../../spec/auto-instrumentation/wire-protocols.json")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(b, CatalogJSON) {
		t.Fatal("embedded wire catalog is stale")
	}
}
func TestFixtures(t *testing.T) {
	files := fixtureFiles(t)
	for _, f := range files {
		fixture := readFixture(t, f)
		if decodedRecognizerFixture(fixture) {
			// Spec A10 scope: synthetic Bedrock decoded-payload carriers are not
			// wire cases; TestA10DecodedRecognizerFixtures runs them instead.
			continue
		}
		t.Run(fixtureLabel(f), func(t *testing.T) { checkFixture(t, fixture) })
	}
}
