package zerocode_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	coltrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

const (
	fiKey     = "fi-api-key-0001"
	fiSecret  = "fi-secret-key-0002"
	modelKey  = "sk-model-key-0003"
	einoScope = "github.com/future-agi/traceAI/go/eino"
	wireScope = "github.com/future-agi/traceAI/go/auto"
)

type exported struct {
	scope, name, trace string
	attrs              map[string]string
}

type collector struct {
	*httptest.Server
	mu    sync.Mutex
	spans []exported
	raw   []byte
}

func newCollector(t *testing.T) *collector {
	t.Helper()
	c := &collector{}
	c.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		var req coltrace.ExportTraceServiceRequest
		if err := proto.Unmarshal(b, &req); err != nil {
			t.Errorf("export is not OTLP protobuf: %v", err)
		}
		c.mu.Lock()
		c.raw = append(c.raw, b...)
		for _, rs := range req.ResourceSpans {
			for _, ss := range rs.ScopeSpans {
				for _, s := range ss.Spans {
					attrs := map[string]string{}
					for _, kv := range s.Attributes {
						attrs[kv.Key] = kv.Value.GetStringValue()
					}
					c.spans = append(c.spans, exported{ss.Scope.GetName(), s.Name, fmt.Sprintf("%x", s.TraceId), attrs})
				}
			}
		}
		c.mu.Unlock()
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	t.Cleanup(c.Close)
	return c
}

func (c *collector) snapshot() ([]exported, []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]exported(nil), c.spans...), append([]byte(nil), c.raw...)
}

// fakeModel answers Chat Completions: a get_weather call first, then text.
func fakeModel(t *testing.T) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Messages []struct {
				Role string `json:"role"`
			} `json:"messages"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		tool := false
		for _, m := range req.Messages {
			tool = tool || m.Role == "tool"
		}
		w.Header().Set("Content-Type", "application/json")
		msg, finish := `{"role":"assistant","content":"Pack a light rain jacket."}`, "stop"
		if !tool {
			msg, finish = `{"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"Kyoto\"}"}}]}`, "tool_calls"
		}
		fmt.Fprintf(w, `{"id":"c1","object":"chat.completion","model":"gpt-test-1","choices":[{"index":0,"message":%s,"finish_reason":%q}],"usage":{"prompt_tokens":40,"completion_tokens":12,"total_tokens":52}}`, msg, finish)
	}))
	t.Cleanup(s.Close)
	return s
}

var (
	buildOnce sync.Once
	appBin    string
	buildErr  error
)

func buildApp(t *testing.T) string {
	t.Helper()
	buildOnce.Do(func() {
		dir, err := os.MkdirTemp("", "zerocodeapp")
		if err != nil {
			buildErr = err
			return
		}
		appBin = filepath.Join(dir, "app")
		cmd := exec.Command("go", "build", "-o", appBin, "./app")
		cmd.Env = append(os.Environ(), "GOFLAGS=-mod=readonly")
		if out, err := cmd.CombinedOutput(); err != nil {
			buildErr = fmt.Errorf("build: %v\n%s", err, out)
		}
	})
	if buildErr != nil {
		t.Fatal(buildErr)
	}
	return appBin
}

type result struct {
	fi, app       []exported
	fiRaw, appRaw []byte
	stdout        string
}

// runApp runs the program with the given environment, a fake model and fake
// FI and application collectors, and returns what each received.
func runApp(t *testing.T, env ...string) result {
	t.Helper()
	bin := buildApp(t)
	fi, app, model := newCollector(t), newCollector(t), fakeModel(t)
	cmd := exec.Command(bin)
	cmd.Env = []string{
		"PATH=" + os.Getenv("PATH"), "HOME=" + t.TempDir(),
		"FI_API_KEY=" + fiKey, "FI_SECRET_KEY=" + fiSecret, "FI_PROJECT_NAME=zerocode", "FI_BASE_URL=" + fi.URL,
		"FI_CONFIG_FILE=" + filepath.Join(t.TempDir(), "absent.toml"),
		"OPENAI_BASE_URL=" + model.URL + "/v1", "APP_COLLECTOR=" + app.URL,
		"APP_QUESTION=I am in Kyoto, ref QMARK-42. What should I pack?",
	}
	cmd.Env = append(cmd.Env, env...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("app: %v\nstdout: %s\nstderr: %s", err, stdout.String(), stderr.String())
	}
	var r result
	r.fi, r.fiRaw = fi.snapshot()
	r.app, r.appRaw = app.snapshot()
	r.stdout = stdout.String()
	return r
}

func inScope(spans []exported, scope string) []exported {
	var out []exported
	for _, s := range spans {
		if s.scope == scope {
			out = append(out, s)
		}
	}
	return out
}

func kinds(spans []exported) map[string]int {
	m := map[string]int{}
	for _, s := range spans {
		m[s.attrs["gen_ai.span.kind"]]++
	}
	return m
}

// One blank import: the agent, chain, tool and model spans of an Eino program
// reach Future AGI although the application installs no tracer provider, and
// the model's HTTP call adds no second (wire) span under the Eino model span.
func TestOneImportTracesEino(t *testing.T) {
	r := runApp(t)
	eino := inScope(r.fi, einoScope)
	k := kinds(eino)
	for _, want := range []string{"AGENT", "CHAIN", "TOOL", "LLM"} {
		if k[want] == 0 {
			t.Errorf("no %s span reached Future AGI; got kinds %v", want, k)
		}
	}
	if wire := inScope(r.fi, wireScope); len(wire) != 0 {
		t.Errorf("model call under an Eino LLM span also produced %d wire spans", len(wire))
	}
	if !strings.Contains(r.stdout, `"name":"eino","status":"auto"`) {
		t.Errorf("Report does not show eino as auto: %s", r.stdout)
	}
}

// perTrace counts the Eino spans of each trace.
func perTrace(spans []exported) map[string]int {
	m := map[string]int{}
	for _, s := range inScope(spans, einoScope) {
		m[s.trace]++
	}
	return m
}

func sameCounts(t *testing.T, where string, got map[string]int, traces, each int) {
	t.Helper()
	if len(got) != traces {
		t.Errorf("%s: %d traces, want %d (%v)", where, len(got), traces, got)
	}
	for id, n := range got {
		if n != each {
			t.Errorf("%s: trace %s has %d Eino spans, want %d", where, id, n, each)
		}
	}
}

// Each way an application can also handle Eino itself leaves every run traced
// exactly once: by the application's handler where it handles the run, by the
// import everywhere else.
func TestApplicationHandlersAndTheImportTraceEachRunOnce(t *testing.T) {
	reference := runApp(t, "APP_MODE=ownprovider")
	each := len(inScope(reference.app, einoScope))
	if each == 0 {
		t.Fatal("reference run exported no Eino spans")
	}
	for _, tc := range []struct {
		mode   string
		traces int
	}{
		{"manual", 1},       // a global handler: the import steps aside
		{"unregistered", 1}, // a built handler nobody uses: nothing changes
		{"perrun", 2},       // question A has its own handler, question B has the import
		{"midrun", 2},       // registered while A runs: A is unaffected, B uses it
	} {
		t.Run(tc.mode, func(t *testing.T) {
			r := runApp(t, "APP_MODE="+tc.mode)
			sameCounts(t, "application collector", perTrace(r.app), tc.traces, each)
			sameCounts(t, "Future AGI", perTrace(r.fi), tc.traces, each)
		})
	}
}

// Concurrent runs are traced separately and completely.
func TestConcurrentRunsAreTracedOnceEach(t *testing.T) {
	reference := runApp(t)
	each := len(inScope(reference.fi, einoScope))
	r := runApp(t, "APP_MODE=concurrent")
	sameCounts(t, "Future AGI", perTrace(r.fi), 8, each)
}

// An application with its own global provider keeps receiving the Eino spans,
// and Future AGI receives them too.
func TestOwnGlobalProviderStillReceivesEinoSpans(t *testing.T) {
	r := runApp(t, "APP_MODE=ownprovider")
	if len(inScope(r.app, einoScope)) == 0 {
		t.Error("the application's own provider received no Eino spans")
	}
	if len(inScope(r.fi, einoScope)) == 0 {
		t.Error("Future AGI received no Eino spans")
	}
}

// FI_INSTRUMENTATIONS without eino: no Eino spans, the wire span remains.
func TestInstrumentationsWithoutEinoRecordsNoEinoSpans(t *testing.T) {
	r := runApp(t, "FI_INSTRUMENTATIONS=wire")
	if got := inScope(r.fi, einoScope); len(got) != 0 {
		t.Errorf("%d Eino spans exported with FI_INSTRUMENTATIONS=wire", len(got))
	}
	if len(inScope(r.fi, wireScope)) == 0 {
		t.Error("the wire span disappeared")
	}
}

func TestSDKDisabledExportsNothing(t *testing.T) {
	r := runApp(t, "OTEL_SDK_DISABLED=true")
	if len(r.fi) != 0 {
		t.Errorf("OTEL_SDK_DISABLED=true exported %d spans", len(r.fi))
	}
}

// shown lists the spans that carry text in an attribute of the hidden family.
// A node that echoes its input as its output (Eino's Init and Lambda nodes) is
// output, so it is judged by the output switches.
func shown(spans []exported, hidden string, families ...string) []string {
	var out []string
	for _, s := range inScope(spans, einoScope) {
		for k, v := range s.attrs {
			for _, f := range families {
				if strings.HasPrefix(k, f) && strings.Contains(v, hidden) {
					out = append(out, s.name+" "+k)
				}
			}
		}
	}
	return out
}

// Privacy switches apply to Eino spans on both copies: the one Future AGI
// receives and the one the application's own provider exports.
func TestPrivacyAppliesToEinoSpansOnBothCopies(t *testing.T) {
	in := []string{"input.", "gen_ai.input."}
	out := []string{"output.", "gen_ai.output."}
	for _, tc := range []struct {
		name     string
		env      string
		hidden   string
		families []string
	}{
		{"inputs", "FI_HIDE_INPUTS", "QMARK-42", in},
		{"input messages", "FI_HIDE_INPUT_MESSAGES", "QMARK-42", in},
		{"input text", "FI_HIDE_INPUT_TEXT", "QMARK-42", in},
		{"outputs", "FI_HIDE_OUTPUTS", "Pack a light rain jacket", out},
		{"output messages", "FI_HIDE_OUTPUT_MESSAGES", "Pack a light rain jacket", out},
		{"output text", "FI_HIDE_OUTPUT_TEXT", "Pack a light rain jacket", out},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := runApp(t, "APP_MODE=ownprovider", tc.env+"=true")
			if len(inScope(r.fi, einoScope)) == 0 || len(inScope(r.app, einoScope)) == 0 {
				t.Fatal("no Eino spans")
			}
			if got := shown(r.fi, tc.hidden, tc.families...); len(got) != 0 {
				t.Errorf("%s=true: %q reached Future AGI in %v", tc.env, tc.hidden, got)
			}
			if got := shown(r.app, tc.hidden, tc.families...); len(got) != 0 {
				t.Errorf("%s=true: %q reached the application's exporter in %v", tc.env, tc.hidden, got)
			}
		})
	}
}

// A secret the agent knows is replaced in what Future AGI receives, even when
// it appears in a prompt: the FI key pair is known from the start, the model
// key once the model call is made.
func TestKnownSecretsAreReplacedInEinoSpans(t *testing.T) {
	r := runApp(t, "APP_QUESTION=my keys are "+fiSecret+" and "+modelKey)
	if len(inScope(r.fi, einoScope)) == 0 {
		t.Fatal("no Eino spans")
	}
	for _, secret := range []string{fiSecret, modelKey} {
		if bytes.Contains(r.fiRaw, []byte(secret)) {
			t.Errorf("secret %q was exported to Future AGI", secret)
		}
	}
}
