// Package launcher implements traceai and the platform CLI's fi run entry point.
// Prepare is side-effect-free with respect to the project: it writes only cache
// files. Main replaces the current process on Unix for transparent signal/exit
// semantics, so an embedding CLI should call it only from its run entry point.
package launcher

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

var Version = "0.1.0"

// Options are overrides, not defaults: an empty field preserves the environment.
type Options struct {
	Project, ProjectType, BaseURL, Disable, Only, SampleRate string
	Debug, GoInject                                          bool
	Runtimes                                                 []string
}

// Plan retains the entire original environment; Changes contains only injection
// and flag overrides. Values are NOT masked here; use Masked before displaying.
type Plan struct {
	Environment []string          `json:"-"`
	Changes     map[string]string `json:"environment"`
	Agents      Agents            `json:"agents"`
	Warnings    []string          `json:"warnings,omitempty"`
}

func environmentMap(env []string) map[string]string {
	m := make(map[string]string, len(env))
	for _, entry := range env {
		if key, value, ok := strings.Cut(entry, "="); ok {
			m[key] = value
		}
	}
	return m
}
func environmentList(env map[string]string) []string {
	keys := make([]string, 0, len(env))
	for key := range env {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]string, 0, len(keys))
	for _, key := range keys {
		out = append(out, key+"="+env[key])
	}
	return out
}
func (o Options) overrides() map[string]string {
	out := map[string]string{}
	for key, value := range map[string]string{"FI_PROJECT_NAME": o.Project, "FI_PROJECT_TYPE": o.ProjectType, "FI_BASE_URL": o.BaseURL, "FI_DISABLED_INSTRUMENTATIONS": o.Disable, "FI_INSTRUMENTATIONS": o.Only, "FI_SAMPLE_RATE": o.SampleRate} {
		if value != "" {
			out[key] = value
		}
	}
	if o.Debug {
		out["FI_AUTO_DEBUG"] = "true"
		out["TRACEAI_DEBUG"] = "true"
	}
	return out
}
func hasRuntime(selected []string, name string) bool {
	if len(selected) == 0 {
		return true
	}
	for _, s := range selected {
		if s == name || s == "jvm" && name == "java" {
			return true
		}
	}
	return false
}
func prependPath(old, path string) string {
	for _, item := range strings.Split(old, string(os.PathListSeparator)) {
		if item == path {
			return old
		}
	}
	if old == "" {
		return path
	}
	return path + string(os.PathListSeparator) + old
}

// optionWords recognizes quoted JVM option paths. Other runtimes have their
// own parsers; the original string is never reserialized.
func optionWords(value string) ([]string, error) {
	var words []string
	var word strings.Builder
	var quote rune
	started := false
	// HotSpot's JAVA_TOOL_OPTIONS parser groups single/double quotes, but does
	// not interpret backslash escapes, even inside quotes.
	for _, ch := range value {
		if quote != 0 {
			if ch == quote {
				quote = 0
			} else {
				word.WriteRune(ch)
			}
			started = true
			continue
		}
		if ch == '\'' || ch == '"' {
			quote = ch
			started = true
			continue
		}
		if ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t' {
			if started {
				words = append(words, word.String())
				word.Reset()
				started = false
			}
			continue
		}
		word.WriteRune(ch)
		started = true
	}
	if quote != 0 {
		return nil, fmt.Errorf("unclosed option quote")
	}
	if started {
		words = append(words, word.String())
	}
	return words, nil
}

// Node recognizes only double quotes, literal tabs, and backslash escapes
// inside double quotes. JSON/Go string escaping (e.g. \\t or \\u0085) is not valid.
func nodeOptionWords(value string) ([]string, error) {
	var words []string
	var word strings.Builder
	quoted, started := false, false
	for i := 0; i < len(value); i++ {
		c := value[i]
		if c == '\\' && quoted {
			i++
			if i == len(value) {
				return nil, fmt.Errorf("unclosed Node option escape")
			}
			word.WriteByte(value[i])
			started = true
		} else if c == '"' {
			quoted = !quoted
			started = true
		} else if c == ' ' && !quoted {
			if started {
				words = append(words, word.String())
				word.Reset()
				started = false
			}
		} else {
			word.WriteByte(c)
			started = true
		}
	}
	if quoted {
		return nil, fmt.Errorf("unclosed Node option quote")
	}
	if started {
		words = append(words, word.String())
	}
	return words, nil
}

// RUBYOPT uses whitespace splitting, not shell quotes or escaping. Apostrophes
// and backslashes in an -I directory are literal path characters.
func rubyOptionWords(value string) []string {
	return strings.FieldsFunc(value, func(c rune) bool {
		return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\v' || c == '\f'
	})
}

func quoteNodeOption(value string) string {
	return "\"" + strings.NewReplacer("\\", "\\\\", "\"", "\\\"").Replace(value) + "\""
}

func prependOption(old, formatted string, tokens ...string) string {
	words, _ := optionWords(old)
	return prependWords(old, formatted, words, tokens...)
}

func containsWords(words, tokens []string) bool {
	for i := range words {
		if len(words)-i < len(tokens) {
			continue
		}
		match := true
		for j := range tokens {
			if words[i+j] != tokens[j] {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}

func prependWords(old, formatted string, words []string, tokens ...string) string {
	if containsWords(words, tokens) {
		return old
	}
	if old == "" {
		return formatted
	}
	return formatted + " " + old
}

// Prepare applies universal, merge-only injection. Missing agents and cache
// errors are warnings, never errors returned to the user's program. argv is used
// only for Go build overlays and compiled Go binary diagnostics.
func Prepare(argv, original []string, dir string, opts Options) (plan Plan) {
	env := environmentMap(original)
	plan.Changes = map[string]string{}
	plan.Environment = append([]string(nil), original...)
	// A cache/config bug must not stop the command. Keep successful flag overrides.
	defer func() {
		if recover() != nil {
			plan.Warnings = append(plan.Warnings, "instrumentation unavailable; running the program unchanged")
		}
		plan.Environment = environmentList(env)
	}()
	set := func(k, v string) { env[k] = v; plan.Changes[k] = v }
	for k, v := range opts.overrides() {
		set(k, v)
	}
	set("TRACEAI_LAUNCHER", Version)
	goInject := opts.GoInject || env["TRACEAI_GO_INJECT"] == "1"
	if len(argv) > 0 && hasRuntime(opts.Runtimes, "go") && !goInject {
		// Default Go handling never invokes go env/list or changes build flags.
		// Carry the hint marker to nested launchers, not a global once-per-host flag.
		if warning := inspectGo(argv, dir, env); warning != "" && env["TRACEAI_GO_HINT"] != "1" {
			plan.Warnings = append(plan.Warnings, warning)
			set("TRACEAI_GO_HINT", "1")
		}
	}
	cache, err := cacheDirectory(env)
	if err != nil {
		plan.Warnings = append(plan.Warnings, "cache unavailable; running without injection")
		return
	}
	plan.Agents = discoverAgents(cache, env)
	if hasRuntime(opts.Runtimes, "python") {
		data, _ := shims.ReadFile("shims/python/sitecustomize.py")
		path, e := writeShim(cache, "python", "sitecustomize.py", data)
		if e != nil {
			plan.Warnings = append(plan.Warnings, "Python shim cache is not writable")
		} else if strings.ContainsRune(filepath.Dir(path), os.PathListSeparator) {
			plan.Warnings = append(plan.Warnings, "Python shim path contains the path-list separator")
		} else {
			set("PYTHONPATH", prependPath(env["PYTHONPATH"], filepath.Dir(path)))
			// Do not enable the package's .pth bootstrap: Python runs .pth
			// files before sitecustomize, outside this shim's BaseException guard.
		}
	}
	if hasRuntime(opts.Runtimes, "node") {
		data, _ := shims.ReadFile("shims/node/preload.cjs")
		fallback, _ := json.Marshal(plan.Agents.Node)
		data = []byte(strings.ReplaceAll(string(data), "__TRACEAI_NODE_BUNDLE__", string(fallback)))
		path, e := writeShim(cache, "node", "preload.cjs", data)
		if e != nil {
			plan.Warnings = append(plan.Warnings, "Node shim cache is not writable")
		} else if strings.ContainsAny(path, "\r\n") {
			plan.Warnings = append(plan.Warnings, "Node shim path contains an unsupported newline")
		} else {
			words, _ := nodeOptionWords(env["NODE_OPTIONS"])
			// Run before application preloads. The shim waits only for a PnP
			// resolution loader, not for ordinary preloads that may make requests.
			set("NODE_OPTIONS", prependWords(env["NODE_OPTIONS"], "--require "+quoteNodeOption(path), words, "--require", path))
		}
	}
	if hasRuntime(opts.Runtimes, "java") && plan.Agents.Java != "" {
		path := filepath.ToSlash(plan.Agents.Java)
		// HotSpot prints JAVA_TOOL_OPTIONS before the agent can initialize,
		// even when OTEL_SDK_DISABLED=true. Never introduce a known credential
		// into that native diagnostic; leave inherited options untouched.
		if containsCredential(plan.Agents.Java, env) || containsCredential(path, env) {
			plan.Warnings = append(plan.Warnings, "Java agent path contains a known credential; injection skipped")
		} else if strings.ContainsAny(path, "\"'\r\n") {
			plan.Warnings = append(plan.Warnings, "Java agent path contains an unsupported quote or newline")
		} else {
			token := "-javaagent:" + path
			formatted := token
			if strings.ContainsAny(token, " \t") {
				formatted = "\"" + token + "\""
			}
			set("JAVA_TOOL_OPTIONS", prependOption(env["JAVA_TOOL_OPTIONS"], formatted, token))
		}
	}
	if hasRuntime(opts.Runtimes, "dotnet") && plan.Agents.Dotnet != "" {
		if strings.ContainsRune(plan.Agents.Dotnet, os.PathListSeparator) {
			plan.Warnings = append(plan.Warnings, ".NET agent path contains the path-list separator")
		} else {
			set("DOTNET_STARTUP_HOOKS", prependPath(env["DOTNET_STARTUP_HOOKS"], plan.Agents.Dotnet))
		}
	}
	if hasRuntime(opts.Runtimes, "ruby") {
		data, _ := shims.ReadFile("shims/ruby/preload.rb")
		// Ruby single-quoted strings avoid interpolation of paths containing #{}.
		fallback := "'" + strings.NewReplacer("\\", "\\\\", "'", "\\'").Replace(plan.Agents.Ruby) + "'"
		data = []byte(strings.ReplaceAll(string(data), "__TRACEAI_RUBY_LIB__", fallback))
		name := "traceai_launcher_" + hashBytes(data) + ".rb"
		path, e := writeShim(cache, "ruby", name, data)
		if e != nil {
			plan.Warnings = append(plan.Warnings, "Ruby shim cache is not writable")
		} else {
			// RUBYOPT is whitespace-split, unlike NODE_OPTIONS. RUBYLIB is a
			// path list and safely supports spaces. The shim loads the gem first.
			directory := filepath.Dir(path)
			require := "-r" + strings.TrimSuffix(name, ".rb")
			if strings.ContainsRune(directory, os.PathListSeparator) {
				plan.Warnings = append(plan.Warnings, "Ruby shim path contains the path-list separator")
			} else if !strings.ContainsAny(directory, " \t\r\n\v\f") {
				include := "-I" + filepath.ToSlash(directory)
				set("RUBYOPT", prependWords(env["RUBYOPT"], include+" "+require, rubyOptionWords(env["RUBYOPT"]), include, require))
			} else {
				set("RUBYLIB", prependPath(env["RUBYLIB"], directory))
				set("RUBYOPT", prependWords(env["RUBYOPT"], require, rubyOptionWords(env["RUBYOPT"]), require))
			}
		}
	}
	if goInject && len(argv) > 0 && hasRuntime(opts.Runtimes, "go") {
		if warning := prepareGo(argv, dir, cache, plan.Agents.Go, env, set); warning != "" {
			plan.Warnings = append(plan.Warnings, warning)
		}
	}
	return
}
