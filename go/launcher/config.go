package launcher

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

func stripComment(line string) string {
	var quote rune
	escaped := false
	for i, c := range line {
		if escaped {
			escaped = false
			continue
		}
		if c == '\\' && quote == '"' {
			escaped = true
			continue
		}
		if quote != 0 {
			if c == quote {
				quote = 0
			}
			continue
		}
		if c == '"' || c == '\'' {
			quote = c
			continue
		}
		if c == '#' {
			return line[:i]
		}
	}
	return line
}
func tomlValue(value string) (string, error) {
	value = strings.TrimSpace(value)
	if len(value) >= 2 && value[0] == '\'' && value[len(value)-1] == '\'' {
		return value[1 : len(value)-1], nil
	}
	if strings.HasPrefix(value, "\"") {
		return strconv.Unquote(value)
	}
	if strings.HasPrefix(value, "[") && strings.HasSuffix(value, "]") {
		inner := strings.TrimSpace(value[1 : len(value)-1])
		if inner == "" {
			return "", nil
		}
		// Arrays in the common contract are single-line strings only.
		var values []string
		start := 0
		var quote byte
		escaped := false
		for i := 0; i <= len(inner); i++ {
			if i < len(inner) {
				c := inner[i]
				if escaped {
					escaped = false
					continue
				}
				if c == '\\' && quote == '"' {
					escaped = true
					continue
				}
				if quote != 0 {
					if c == quote {
						quote = 0
					}
					continue
				}
				if c == '"' || c == '\'' {
					quote = c
					continue
				}
				if c != ',' {
					continue
				}
			}
			part := strings.TrimSpace(inner[start:i])
			start = i + 1
			if part == "" && i == len(inner) {
				break
			}
			if len(part) < 2 || part[0] != '"' && part[0] != '\'' {
				return "", fmt.Errorf("expected string array")
			}
			parsed, err := tomlValue(part)
			if err != nil {
				return "", err
			}
			values = append(values, parsed)
		}
		if quote != 0 {
			return "", fmt.Errorf("unclosed string")
		}
		return strings.Join(values, ","), nil
	}
	if value == "true" || value == "false" {
		return value, nil
	}
	if _, err := strconv.ParseFloat(value, 64); err == nil {
		return value, nil
	}
	return "", fmt.Errorf("invalid value")
}

// ResolveConfig follows environment > file > defaults. Errors never include
// source values, which may contain credentials.
func ResolveConfig(dir string, original []string) (map[string]string, error) {
	env := environmentMap(original)
	project := env["OTEL_SERVICE_NAME"]
	if project == "" {
		project = filepath.Base(dir)
	}
	config := map[string]string{"FI_PROJECT_NAME": project, "FI_PROJECT_TYPE": "observe", "FI_BASE_URL": "https://api.futureagi.com", "FI_SAMPLE_RATE": "1.0", "FI_AUTO_DEBUG": "false", "FI_WIRE_CAPTURE": "true", "FI_WIRE_MAX_BODY_BYTES": "4194304"}
	path := env["FI_CONFIG_FILE"]
	explicit := path != ""
	if !explicit {
		path = "futureagi.toml"
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(dir, path)
	}
	info, err := os.Stat(path)
	if err == nil && (!info.Mode().IsRegular() || info.Size() > 1<<20) {
		return nil, fmt.Errorf("configuration must be a regular file of at most 1 MiB")
	}
	var f *os.File
	if err == nil {
		f, err = os.Open(path)
	}
	if err == nil {
		defer f.Close()
		scanner := bufio.NewScanner(io.LimitReader(f, 1<<20))
		n := 0
		inTable := false
		for scanner.Scan() {
			n++
			line := strings.TrimSpace(stripComment(scanner.Text()))
			if strings.HasPrefix(line, "[") {
				// TOML keys after a table header belong to that table, not to
				// the root. There is no header that reopens the root table.
				inTable = true
			}
			if line == "" || inTable {
				continue
			}
			key, value, ok := strings.Cut(line, "=")
			if !ok {
				return nil, fmt.Errorf("invalid configuration at line %d", n)
			}
			parsed, e := tomlValue(value)
			if e != nil {
				return nil, fmt.Errorf("invalid configuration at line %d", n)
			}
			key = strings.ToUpper(strings.TrimSpace(key))
			if key == "DEBUG" {
				key = "AUTO_DEBUG"
			}
			config["FI_"+key] = parsed
		}
		if scanner.Err() != nil {
			return nil, fmt.Errorf("cannot read configuration")
		}
	} else if explicit || !os.IsNotExist(err) {
		return nil, fmt.Errorf("cannot read configuration file")
	}
	for key, value := range env {
		if strings.HasPrefix(key, "FI_") || key == "OTEL_SDK_DISABLED" {
			config[key] = value
		}
	}
	if config["FI_PROJECT_TYPE"] != "observe" && config["FI_PROJECT_TYPE"] != "experiment" {
		return nil, fmt.Errorf("FI_PROJECT_TYPE must be observe or experiment")
	}
	rate, err := strconv.ParseFloat(config["FI_SAMPLE_RATE"], 64)
	if err != nil || math.IsNaN(rate) || math.IsInf(rate, 0) || rate < 0 || rate > 1 {
		return nil, fmt.Errorf("FI_SAMPLE_RATE must be between 0 and 1")
	}
	u, err := url.Parse(config["FI_BASE_URL"])
	if err != nil || u.Host == "" || u.Scheme != "https" && u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, fmt.Errorf("FI_BASE_URL must be an HTTP(S) URL without credentials, query or fragment")
	}
	limit, err := strconv.ParseInt(config["FI_WIRE_MAX_BODY_BYTES"], 10, 64)
	if err != nil || limit <= 0 {
		return nil, fmt.Errorf("FI_WIRE_MAX_BODY_BYTES must be positive")
	}
	for _, key := range []string{"FI_AUTO_DEBUG", "FI_WIRE_CAPTURE", "OTEL_SDK_DISABLED"} {
		if v, ok := config[key]; ok && v != "true" && v != "false" && v != "1" && v != "0" {
			return nil, fmt.Errorf("%s must be a boolean", key)
		}
	}
	return config, nil
}

// Masked returns a display-only copy. Known credential values are removed even
// when accidentally repeated in project names, paths or unrelated settings.
func Masked(values map[string]string, sources ...map[string]string) map[string]string {
	secrets := credentialValues(append(sources, values)...)
	out := map[string]string{}
	for key, value := range values {
		// Runtime option strings are opaque application input: arbitrary
		// credentials may exist only inside a flag, not a named secret variable.
		// They are safe to use for the child, never to dump as diagnostics.
		if (secretName(key) || opaqueOptions(key)) && value != "" {
			out[key] = "***"
			continue
		}
		if strings.Contains(strings.ToUpper(key), "URL") {
			if u, err := url.Parse(value); err == nil {
				if u.User != nil {
					u.User = url.User("***")
				}
				if u.RawQuery != "" {
					u.RawQuery = "REDACTED"
				}
				value = u.String()
			} else {
				value = "<invalid URL>"
			}
		}
		for _, secret := range secrets {
			value = strings.ReplaceAll(value, secret, "***")
		}
		out[key] = value
	}
	return out
}

// credentialValues includes the reversible encodings the launcher generates
// before displaying values. Node escaping differs from Go/JSON escaping for
// tabs and Unicode: recognizing only the raw value or strconv.Quote is unsafe.
func credentialValues(sources ...map[string]string) []string {
	unique := map[string]bool{}
	add := func(value string) {
		unique[value] = true
		for _, quoted := range []string{strconv.Quote(value), quoteNodeOption(value)} {
			unique[quoted[1:len(quoted)-1]] = true
		}
	}
	for _, values := range sources {
		for key, value := range values {
			if secretName(key) && value != "" {
				add(value)
				// A standard auth header is sensitive with or without its scheme.
				if strings.Contains(strings.ToUpper(key), "AUTHORIZATION") {
					parts := strings.Fields(value)
					if len(parts) == 2 && (strings.EqualFold(parts[0], "Bearer") || strings.EqualFold(parts[0], "Basic")) {
						add(parts[1])
					}
				}
			}
		}
	}
	secrets := make([]string, 0, len(unique))
	for value := range unique {
		secrets = append(secrets, value)
	}
	// Longer first: masking a short prefix must not expose a longer secret's suffix.
	sort.Slice(secrets, func(i, j int) bool {
		if len(secrets[i]) == len(secrets[j]) {
			return secrets[i] < secrets[j]
		}
		return len(secrets[i]) > len(secrets[j])
	})
	return secrets
}

func containsCredential(value string, sources ...map[string]string) bool {
	for _, secret := range credentialValues(sources...) {
		if strings.Contains(value, secret) {
			return true
		}
	}
	return false
}

// redactText also covers quoted flag-parser/OS errors. Only launcher diagnostics
// use this helper; the application's own stdout/stderr are never filtered.
func redactText(text string, original []string) string {
	for _, secret := range credentialValues(environmentMap(original)) {
		text = strings.ReplaceAll(text, secret, "***")
	}
	return text
}

func opaqueOptions(key string) bool {
	switch strings.ToUpper(key) {
	case "GOFLAGS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "NODE_OPTIONS", "RUBYOPT":
		return true
	}
	return false
}

func secretName(key string) bool {
	upper := strings.ToUpper(key)
	return strings.Contains(upper, "KEY") || strings.Contains(upper, "TOKEN") || strings.Contains(upper, "SECRET") || strings.Contains(upper, "PASSWORD") || strings.Contains(upper, "CREDENTIAL") || strings.Contains(upper, "AUTHORIZATION")
}

type CollectorResult struct {
	OK     bool   `json:"ok"`
	Detail string `json:"detail"`
}

// CheckCollector sends an empty OTLP protobuf, never follows a redirect with
// credentials, and uses bounded time and response reads. It is diagnostics-only;
// run never performs a network request.
func CheckCollector(ctx context.Context, config map[string]string) CollectorResult {
	if config["FI_API_KEY"] == "" || config["FI_SECRET_KEY"] == "" {
		return CollectorResult{false, "FI_API_KEY and FI_SECRET_KEY are not set"}
	}
	endpoint := strings.TrimRight(config["FI_BASE_URL"], "/")
	if !strings.HasSuffix(endpoint, "/tracer/v1/traces") {
		endpoint += "/tracer/v1/traces"
	}
	u, err := url.Parse(endpoint)
	if err != nil || u.Host == "" || u.User != nil || u.Scheme != "http" && u.Scheme != "https" || u.RawQuery != "" || u.Fragment != "" {
		return CollectorResult{false, "invalid FI_BASE_URL"}
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(nil))
	if err != nil {
		return CollectorResult{false, "cannot create test export"}
	}
	req.Header.Set("Content-Type", "application/x-protobuf")
	req.Header.Set("X-Api-Key", config["FI_API_KEY"])
	req.Header.Set("X-Secret-Key", config["FI_SECRET_KEY"])
	client := &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(req)
	if err != nil {
		return CollectorResult{false, "collector unreachable; check FI_BASE_URL and network connectivity"}
	}
	defer response.Body.Close()
	code := response.StatusCode
	switch {
	case code == 401 || code == 403:
		return CollectorResult{false, fmt.Sprintf("collector rejected the keys (HTTP %d)", code)}
	case code == 404 || code >= 500:
		return CollectorResult{false, fmt.Sprintf("collector answered HTTP %d; check FI_BASE_URL", code)}
	case code >= 300 && code < 400:
		return CollectorResult{false, fmt.Sprintf("collector redirected (HTTP %d); configure the final FI_BASE_URL", code)}
	case code >= 400:
		return CollectorResult{false, fmt.Sprintf("collector rejected the test export (HTTP %d)", code)}
	case code >= 200 && code < 300:
		return CollectorResult{true, fmt.Sprintf("collector accepted a test export (HTTP %d)", code)}
	default:
		return CollectorResult{false, fmt.Sprintf("collector did not accept the test export (HTTP %d)", code)}
	}
}
