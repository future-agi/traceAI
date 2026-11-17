package wire

import (
	"bufio"
	"bytes"
	"compress/flate"
	"compress/gzip"
	"compress/zlib"
	"io"
	"strings"
)

// ReadEncodedMultipart decodes only the observed upload copy. Files are drained
// directly from the decoder, not buffered or constrained by the JSON body limit.
// This keeps model/text fields available even on very large compressed uploads.
func ReadEncodedMultipart(r io.Reader, contentType, encoding string, limit int64) (map[string]string, []FilePart, bool, string) {
	encoding = strings.ToLower(strings.TrimSpace(encoding))
	var closers []io.Closer
	defer func() {
		for i := len(closers) - 1; i >= 0; i-- {
			_ = closers[i].Close()
		}
	}()
	encodings := strings.Split(encoding, ",")
	for i := len(encodings) - 1; i >= 0; i-- {
		var decoder io.ReadCloser
		var err error
		switch strings.TrimSpace(encodings[i]) {
		case "", "identity":
			continue
		case "gzip", "x-gzip":
			decoder, err = gzip.NewReader(r)
		case "deflate":
			// Sniff rather than consuming a failed zlib header: the same bytes
			// must remain available when a server uses raw RFC1951 deflate.
			buffered := bufio.NewReader(r)
			header, _ := buffered.Peek(2)
			if len(header) == 2 && header[0]&15 == 8 && (int(header[0])*256+int(header[1]))%31 == 0 {
				decoder, err = zlib.NewReader(buffered)
			} else {
				decoder = flate.NewReader(buffered)
			}
		default:
			return nil, nil, false, encoding
		}
		if err != nil {
			return nil, nil, false, encoding
		}
		closers = append(closers, decoder)
		r = decoder
	}
	fields, files, truncated := ReadMultipart(r, contentType, limit)
	return fields, files, truncated, ""
}

// Decode only bounded copies, never the application's reader. Both encoded and
// decoded sizes are capped, including for compression bombs. Go's standard
// library has no br/zstd decoder; these are reported rather than parsed as text.
func decodedBytes(data []byte, encoding string, limit int64, truncated bool) ([]byte, bool, string) {
	encoding = strings.ToLower(strings.TrimSpace(encoding))
	if truncated {
		return nil, true, ""
	}
	if int64(len(data)) > limit {
		return nil, true, ""
	}
	if encoding == "" || encoding == "identity" {
		return data, false, ""
	}
	encodings := strings.Split(encoding, ",")
	for i := len(encodings) - 1; i >= 0; i-- {
		var r io.ReadCloser
		var err error
		switch strings.TrimSpace(encodings[i]) {
		case "identity":
			continue
		case "gzip", "x-gzip":
			r, err = gzip.NewReader(bytes.NewReader(data))
		case "deflate":
			r, err = zlib.NewReader(bytes.NewReader(data))
			if err != nil {
				r = flate.NewReader(bytes.NewReader(data))
				err = nil
			}
		default:
			return nil, false, encoding
		}
		if err != nil {
			return nil, false, encoding
		}
		decoded, readErr := io.ReadAll(io.LimitReader(r, limit+1))
		_ = r.Close()
		if int64(len(decoded)) > limit {
			return nil, true, ""
		}
		// gzip/deflate can yield complete SSE events before a truncated trailer.
		if readErr != nil {
			return decoded, false, encoding
		}
		data = decoded
	}
	return data, false, ""
}
