package auto

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/sdk/resource"
)

// Config is resolved in the order options > environment > file > defaults.
// Report returns a copy with credentials masked.
type Config struct {
	APIKey                   string      `json:"api_key"`
	SecretKey                string      `json:"secret_key"`
	BaseURL                  string      `json:"base_url"`
	ProjectName              string      `json:"project_name"`
	ProjectType              string      `json:"project_type"`
	ProjectVersionName       string      `json:"project_version_name,omitempty"`
	ProjectVersionID         string      `json:"project_version_id,omitempty"`
	ServiceName              string      `json:"service_name"`
	SampleRate               float64     `json:"sample_rate"`
	Instrumentations         []string    `json:"instrumentations"`
	DisabledInstrumentations []string    `json:"disabled_instrumentations"`
	WireCapture              bool        `json:"wire_capture"`
	WireMaxBodyBytes         int64       `json:"wire_max_body_bytes"`
	Debug                    bool        `json:"debug"`
	Disabled                 bool        `json:"disabled"`
	ConfigFile               string      `json:"config_file"`
	TraceConfig              TraceConfig `json:"trace_config"`
}

// otelResource is normal OpenTelemetry resource detection (A6): the
// unknown_service:<executable> fallback, then OTEL_RESOURCE_ATTRIBUTES and
// OTEL_SERVICE_NAME, plus SDK identity. It is computed fresh (resource.Default
// caches its first detection) and never makes a network call.
func otelResource() *resource.Resource {
	r, _ := resource.New(context.Background(), resource.WithDetectors(resource.StringDetector("", "service.name", func() (string, error) {
		executable, err := os.Executable()
		if err != nil {
			return "unknown_service:go", nil
		}
		return "unknown_service:" + filepath.Base(executable), nil
	})), resource.WithFromEnv(), resource.WithTelemetrySDK())
	if r == nil {
		return resource.Empty()
	}
	return r
}

// otelServiceName is the service.name OpenTelemetry would detect. It is never
// derived from the FI project or its working-directory default.
func otelServiceName() string {
	if v, ok := otelResource().Set().Value("service.name"); ok && v.AsString() != "" {
		return v.AsString()
	}
	return "unknown_service:go"
}

func splitList(v string) []string {
	ss := []string{}
	for _, s := range strings.Split(v, ",") {
		if s = strings.TrimSpace(s); s != "" {
			ss = append(ss, strings.ToLower(s))
		}
	}
	return ss
}
func boolValue(s string) bool { return strings.EqualFold(strings.TrimSpace(s), "true") }
func readConfig(overrides ...func(*Config)) (Config, error) {
	return readConfigWithOverrides(nil, overrides...)
}
func readConfigWithOverrides(ignore map[string]bool, overrides ...func(*Config)) (Config, error) {
	path := os.Getenv("FI_CONFIG_FILE")
	if path == "" {
		path = "./futureagi.toml"
	}
	values, err := readFlatConfig(path, ignore)
	if err != nil {
		return Config{ConfigFile: path}, err
	}
	get := func(k, def string) string {
		if v, ok := os.LookupEnv("FI_" + k); ok {
			return v
		}
		if v, ok := values[k]; ok {
			return v
		}
		return def
	}
	service := os.Getenv("OTEL_SERVICE_NAME")
	if service == "" {
		if wd, e := os.Getwd(); e == nil {
			service = filepath.Base(wd)
		}
		if service == "" || service == "." || service == string(filepath.Separator) {
			service = "DEFAULT_PROJECT_NAME"
		}
	}
	apiKey, secretKey := credentialPair("environment", os.Getenv("FI_API_KEY"), os.Getenv("FI_SECRET_KEY"))
	if apiKey == "" && secretKey == "" {
		apiKey, secretKey = credentialPair("config file", values["API_KEY"], values["SECRET_KEY"])
	}
	c := Config{APIKey: apiKey, SecretKey: secretKey, BaseURL: get("BASE_URL", "https://api.futureagi.com"), ProjectName: get("PROJECT_NAME", service), ProjectType: get("PROJECT_TYPE", "observe"), ProjectVersionName: get("PROJECT_VERSION_NAME", "DEFAULT_PROJECT_VERSION_NAME"), ProjectVersionID: get("PROJECT_VERSION_ID", ""), ServiceName: otelServiceName(), SampleRate: 1, WireCapture: boolValue(get("WIRE_CAPTURE", "true")), WireMaxBodyBytes: 4194304, Debug: boolValue(get("AUTO_DEBUG", "false")), Disabled: boolValue(os.Getenv("OTEL_SDK_DISABLED")), ConfigFile: path, Instrumentations: splitList(get("INSTRUMENTATIONS", "")), DisabledInstrumentations: splitList(get("DISABLED_INSTRUMENTATIONS", ""))}
	if s := get("SAMPLE_RATE", "1"); s != "" {
		c.SampleRate, err = strconv.ParseFloat(s, 64)
		if err != nil {
			c.SampleRate = math.NaN()
		}
	}
	if c.WireMaxBodyBytes, err = strconv.ParseInt(get("WIRE_MAX_BODY_BYTES", "4194304"), 10, 64); err != nil {
		c.WireMaxBodyBytes = -1
	}
	c.TraceConfig = traceConfigValues(get)
	// Validate the resolved configuration, not an inherited value that a code
	// option replaces. Invalid sentinels must survive until overrides run.
	for _, override := range overrides {
		apiKey, secretKey := c.APIKey, c.SecretKey
		override(&c)
		if c.APIKey != apiKey || c.SecretKey != secretKey {
			c.APIKey, c.SecretKey = credentialPair("explicit options", c.APIKey, c.SecretKey)
			if c.APIKey == "" && c.SecretKey == "" {
				c.APIKey, c.SecretKey = apiKey, secretKey
			}
		}
	}
	if math.IsNaN(c.SampleRate) || math.IsInf(c.SampleRate, 0) || c.SampleRate < 0 || c.SampleRate > 1 {
		return c, fmt.Errorf("invalid sample_rate")
	}
	if c.WireMaxBodyBytes <= 0 {
		return c, fmt.Errorf("invalid wire_max_body_bytes")
	}
	if c.ProjectName == "" {
		c.ProjectName = service
	}
	if c.ServiceName == "" {
		c.ServiceName = otelServiceName()
	}
	if c.ProjectVersionName == "" {
		c.ProjectVersionName = "DEFAULT_PROJECT_VERSION_NAME"
	}
	// Upstream's kill switch cannot be overridden by an agent option.
	c.Disabled = c.Disabled || boolValue(os.Getenv("OTEL_SDK_DISABLED"))
	return c, nil
}

// A source either contributes both FI credentials or contributes neither. Never
// include credential values in warnings (including the half that was supplied).
func credentialPair(source, apiKey, secretKey string) (string, string) {
	if apiKey != "" && secretKey != "" {
		return apiKey, secretKey
	}
	if apiKey != "" {
		log.Printf("traceai auto: ignoring incomplete %s credential pair (missing FI_SECRET_KEY)", source)
	}
	if secretKey != "" {
		log.Printf("traceai auto: ignoring incomplete %s credential pair (missing FI_API_KEY)", source)
	}
	return "", ""
}

// stripComment respects quoted # characters and escaped quotes.
func stripComment(s string) string {
	quote := byte(0)
	escaped := false
	for i := 0; i < len(s); i++ {
		c := s[i]
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
		}
		if c == '#' {
			return s[:i]
		}
	}
	return s
}
func readFlatConfig(path string, ignore map[string]bool) (map[string]string, error) {
	values := map[string]string{}
	credentialOnly := ignore["*"] && ignore["credential_fallback"]
	if ignore["*"] && !credentialOnly {
		return values, nil
	}
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return values, nil
	}
	if err != nil {
		return nil, fmt.Errorf("cannot read FI_CONFIG_FILE")
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(stripComment(scanner.Text()))
		if line == "" || strings.HasPrefix(line, "[") {
			continue
		}
		key, v, ok := strings.Cut(line, "=")
		if !ok {
			if credentialOnly {
				continue
			}
			return nil, fmt.Errorf("invalid config line")
		}
		key = strings.ToUpper(strings.TrimSpace(key))
		if key == "DEBUG" {
			key = "AUTO_DEBUG"
		}
		isCredential := key == "API_KEY" || key == "SECRET_KEY"
		if credentialOnly && !isCredential {
			continue
		}
		_, overridden := os.LookupEnv("FI_" + key)
		if isCredential {
			// An incomplete environment source must not discard half of the
			// complete file pair. Empty env values are absence, not a pair.
			overridden = os.Getenv("FI_API_KEY") != "" && os.Getenv("FI_SECRET_KEY") != ""
		}
		if overridden || ignore[key] {
			continue
		}
		v = strings.TrimSpace(v)
		if strings.HasPrefix(v, "[") {
			var ss []string
			if json.Unmarshal([]byte(v), &ss) != nil {
				return nil, fmt.Errorf("invalid config array")
			}
			v = strings.Join(ss, ",")
		} else if strings.HasPrefix(v, "\"") {
			var s string
			if json.Unmarshal([]byte(v), &s) != nil {
				return nil, fmt.Errorf("invalid config string")
			}
			v = s
		} else if strings.HasPrefix(v, "'") {
			if len(v) < 2 || !strings.HasSuffix(v, "'") {
				return nil, fmt.Errorf("invalid config string")
			}
			v = v[1 : len(v)-1]
		}
		values[key] = v
	}
	if scanner.Err() != nil {
		return nil, fmt.Errorf("cannot parse config file")
	}
	return values, nil
}
func allowed(c Config, name string) bool {
	if c.Disabled {
		return false
	}
	for _, v := range c.DisabledInstrumentations {
		if v == name {
			return false
		}
	}
	if len(c.Instrumentations) == 0 {
		return true
	}
	for _, v := range c.Instrumentations {
		if v == name || v == "all" {
			return true
		}
	}
	return false
}
