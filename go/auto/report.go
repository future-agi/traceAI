package auto

import (
	"math"
	"net/url"
	"runtime/debug"
	"strings"
)

// IntegrationStatus describes wire coverage separately from manual middleware.
type IntegrationStatus struct {
	Name          string   `json:"name"`
	Status        string   `json:"status"`
	Option        string   `json:"option,omitempty"`
	WireProtocols []string `json:"wire_protocols,omitempty"`
	Installed     bool     `json:"installed"`
	Reason        string   `json:"reason,omitempty"`
}
type AgentReport struct {
	Config       Config              `json:"config"`
	Integrations []IntegrationStatus `json:"integrations"`
	Collector    string              `json:"collector"`
	Limitations  []string            `json:"limitations"`
}

func maskReportConfig(c Config) Config {
	original := c
	// Failed configuration must still be representable by the doctor JSON API.
	// The integration's failed status preserves the rejection; never export NaN.
	if math.IsNaN(c.SampleRate) || math.IsInf(c.SampleRate, 0) {
		c.SampleRate = 0
	}
	c.ProjectType = safeString(c.ProjectType, original)
	baseURL := c.BaseURL
	if u, err := url.Parse(baseURL); err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		// Rejected/malformed userinfo must not become a diagnostic credential
		// leak. Keep ordinary invalid URLs useful (e.g. ://invalid-endpoint).
		if strings.Contains(baseURL, "@") {
			baseURL = "<invalid FI_BASE_URL>"
		}
	} else if u.User != nil {
		u.User = nil
		baseURL = u.String()
	}
	c.BaseURL = safeString(baseURL, original)
	c.ProjectName = safeString(c.ProjectName, original)
	c.ProjectVersionName = safeString(c.ProjectVersionName, original)
	c.ProjectVersionID = safeString(c.ProjectVersionID, original)
	c.ServiceName = safeString(c.ServiceName, original)
	c.ConfigFile = safeString(c.ConfigFile, original)
	c.Instrumentations = append([]string(nil), c.Instrumentations...)
	c.DisabledInstrumentations = append([]string(nil), c.DisabledInstrumentations...)
	for i, value := range c.Instrumentations {
		c.Instrumentations[i] = safeString(value, original)
	}
	for i, value := range c.DisabledInstrumentations {
		c.DisabledInstrumentations[i] = safeString(value, original)
	}
	if c.APIKey != "" {
		c.APIKey = "***"
	}
	if c.SecretKey != "" {
		c.SecretKey = "***"
	}
	return c
}

func makeReport(c Config, wire bool, reason string) AgentReport {
	r := AgentReport{Config: maskReportConfig(c), Collector: "not tested (startup is network-free)", Limitations: []string{"Go requires a blank import and rebuild; compiled libraries cannot be patched at runtime", "custom transports require auto.Transport(base)", "existing providers keep their own sampling policy", "no automatic incoming HTTP SERVER spans or bootstrap installer", "call Shutdown before process exit; no Go atexit hook", "context keys from other middleware modules are private; use auto.ContextWithAttributes"}}
	installed := map[string]bool{}
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, dep := range info.Deps {
			installed[dep.Path] = true
		}
	}
	status := "disabled"
	if wire {
		status = "instrumented"
	}
	if reason == "failed" {
		status = "failed"
	}
	r.Integrations = append(r.Integrations, IntegrationStatus{Name: "wire", Status: status, Reason: reason})
	entries := []struct {
		name, target, option string
		protocols            []string
	}{
		{"openai", "github.com/openai/openai-go/v3", "option.WithMiddleware(traceaiopenai.Middleware())", []string{"openai.chat", "openai.responses", "openai.completions", "openai.embeddings", "openai.audio_transcription", "openai.audio_speech", "openai.images", "openai.moderations"}},
		{"anthropic", "github.com/anthropics/anthropic-sdk-go", "option.WithMiddleware(traceaianthropic.Middleware())", []string{"anthropic.messages"}},
		{"genai", "google.golang.org/genai", "genai.ClientConfig{HTTPClient: traceaigenai.NewHTTPClient()}", []string{"gemini.generate_content", "gemini.embed_content"}},
		{"eino", "github.com/cloudwego/eino", "callbacks.AppendGlobalHandlers(traceaieino.NewHandler())", []string{"underlying model HTTP protocols only (not framework steps)"}},
		{"a2a", "github.com/a2aproject/a2a-go", "a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor())", []string{"underlying model HTTP protocols only (not A2A RPCs)"}},
	}
	for _, e := range entries {
		s := "manual"
		why := "Go cannot patch compiled libraries; middleware is opt-in"
		if !allowed(c, e.name) {
			s = "disabled"
			why = "disabled by resolved configuration"
		}
		protocols := e.protocols
		if !wire {
			protocols = nil
		}
		r.Integrations = append(r.Integrations, IntegrationStatus{Name: e.name, Status: s, Option: e.option, Installed: installed[e.target], WireProtocols: protocols, Reason: why})
	}
	return r
}

// Report returns resolved configuration with masked secrets and honest coverage.
func Report() AgentReport {
	agent.Lock()
	defer agent.Unlock()
	r := agent.last
	// A credential may be learned after initialization built agent.last. Never
	// return that stale diagnostic copy without the current registry's masker.
	r.Config = maskReportConfig(r.Config)
	r.Integrations = append([]IntegrationStatus(nil), r.Integrations...)
	for i := range r.Integrations {
		r.Integrations[i].WireProtocols = append([]string(nil), r.Integrations[i].WireProtocols...)
	}
	r.Limitations = append([]string(nil), r.Limitations...)
	return r
}
