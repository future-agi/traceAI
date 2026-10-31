package launcher

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"
)

const maxDownloadBytes = 64 << 20

// Release downloads are explicit setup only. There is deliberately no guessed
// release URL: a release publisher supplies both the HTTPS URL and its digest.
func installRelease(runtime, address, digest string, original []string) error {
	if runtime != "java" && runtime != "dotnet" {
		return fmt.Errorf("release URLs support java (jar) or dotnet (zip) only")
	}
	u, err := url.Parse(address)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Fragment != "" {
		return fmt.Errorf("release URL must use HTTPS without user credentials or fragment")
	}
	checksum, err := hex.DecodeString(digest)
	if err != nil || len(checksum) != sha256.Size {
		return fmt.Errorf("release downloads require --sha256 with a trusted 64-character digest")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return fmt.Errorf("cannot create release download")
	}
	client := &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(req *http.Request, via []*http.Request) error {
		// net/http adds the previous URL as Referer before this callback.
		// Release URLs may be signed: never forward their path/query, even
		// on a same-origin redirect or on a later hop in the redirect chain.
		req.Header.Del("Referer")
		if len(via) >= 5 || req.URL.Scheme != "https" || req.URL.User != nil {
			return fmt.Errorf("unsafe release redirect")
		}
		return nil
	}}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("release download failed")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("release download returned HTTP %d", response.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxDownloadBytes+1))
	if err != nil || len(data) > maxDownloadBytes {
		return fmt.Errorf("release download unreadable or larger than 64 MiB")
	}
	files, err := releaseFiles(runtime, data, checksum)
	if err != nil {
		return err
	}
	cache, err := cacheDirectory(environmentMap(original))
	if err != nil {
		return fmt.Errorf("agent cache unavailable")
	}
	_, err = installFiles(cache, runtime, files)
	return err
}

func releaseFiles(runtime string, data, checksum []byte) (map[string][]byte, error) {
	actual := sha256.Sum256(data)
	if !bytes.Equal(actual[:], checksum) {
		return nil, fmt.Errorf("release SHA-256 mismatch; nothing installed")
	}
	if runtime == "java" {
		// Check the archive structure rather than ever handing an HTML error page
		// to JAVA_TOOL_OPTIONS, where it would stop every JVM from starting.
		archive, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
		if err != nil {
			return nil, fmt.Errorf("release is not a jar")
		}
		for _, file := range archive.File {
			if file.Name == "META-INF/MANIFEST.MF" {
				return map[string][]byte{agentEntry(runtime): data}, nil
			}
		}
		return nil, fmt.Errorf("jar manifest missing")
	}
	archive, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil || len(archive.File) > 1024 {
		return nil, fmt.Errorf("release is not a bounded zip archive")
	}
	files := map[string][]byte{}
	var total uint64
	for _, file := range archive.File {
		name := file.Name
		if strings.Contains(name, "\\") || path.IsAbs(name) || path.Clean(name) != strings.TrimSuffix(name, "/") || strings.HasPrefix(name, "../") || strings.Contains(name, ":") || file.Mode()&^0777 != 0 && !file.FileInfo().IsDir() {
			return nil, fmt.Errorf("unsafe release archive entry")
		}
		if file.FileInfo().IsDir() {
			continue
		}
		total += file.UncompressedSize64
		if total > 128<<20 || file.UncompressedSize64 > maxDownloadBytes {
			return nil, fmt.Errorf("unpacked release exceeds size limit")
		}
		if _, duplicate := files[name]; duplicate {
			return nil, fmt.Errorf("duplicate release archive entry")
		}
		reader, err := file.Open()
		if err != nil {
			return nil, fmt.Errorf("cannot read release archive")
		}
		content, err := io.ReadAll(io.LimitReader(reader, maxDownloadBytes+1))
		reader.Close()
		if err != nil || len(content) > maxDownloadBytes {
			return nil, fmt.Errorf("cannot read bounded release archive entry")
		}
		files[name] = content
	}
	return files, nil
}
