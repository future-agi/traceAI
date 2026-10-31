package wire

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// Replay only bytes actually delivered before failure, after applying the
// fixture's transport partition. A complete member need not close its array;
// an SSE event does need its blank-line delimiter. No wall-clock sleeps.
func observedUsageFixture(t *testing.T, spec object) Response {
	t.Helper()
	return replayResponse(t, spec)
}

func checkUsageFixture(t *testing.T, fixture object) {
	t.Helper()
	req, resp := obj(fixture["request"]), obj(fixture["response"])
	rh, rb := fixtureBody(req, false)
	result := Build(Find(str(req["method"]), str(req["url"])), Request{Method: str(req["method"]), URL: str(req["url"]), Header: rh, Body: rb}, observedUsageFixture(t, resp))
	if result == nil {
		t.Fatal("usage fixture was not captured")
	}
	expect := obj(fixture["expect"])
	if result.OK != (expect["status"] == "OK") {
		t.Fatal("observed failure outcome lost")
	}
	// A8 tests usage, not A10's separately scoped first-text milestones.
	for name, value := range obj(expect["attributes"]) {
		if !strings.HasPrefix(name, "gen_ai.usage.") {
			continue
		}
		want, ok := integer(value)
		if !ok {
			t.Fatalf("invalid fixture counter: %s", name)
		}
		if got, ok := integer(result.Attributes[name]); !ok || got != want {
			t.Errorf("%s = %v; want %d", name, result.Attributes[name], want)
		}
	}
	for _, raw := range arr(expect["absent"]) {
		name := str(raw)
		if strings.HasPrefix(name, "gen_ai.usage.") {
			if _, ok := result.Attributes[name]; ok {
				t.Errorf("unknown counter was invented: %s", name)
			}
		}
	}
}

func runUsageFixtures(t *testing.T, algebraOnly bool) {
	t.Helper()
	files, err := filepath.Glob("../../../../spec/auto-instrumentation/fixtures/round6/*.json")
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, file := range files {
		n, _ := strconv.Atoi(strings.SplitN(filepath.Base(file), "-", 2)[0])
		if algebraOnly && (n < 48 || n > 63) {
			continue
		}
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var fixture object
		if err = json.Unmarshal(data, &fixture); err != nil {
			t.Fatal(err)
		}
		expect := obj(fixture["expect"])
		usage := false
		for name := range obj(expect["attributes"]) {
			usage = usage || strings.HasPrefix(name, "gen_ai.usage.")
		}
		for _, name := range arr(expect["absent"]) {
			usage = usage || strings.HasPrefix(str(name), "gen_ai.usage.")
		}
		if !usage {
			continue
		}
		count++
		t.Run(filepath.Base(file), func(t *testing.T) { checkUsageFixture(t, fixture) })
	}
	minimum := 32
	if algebraOnly {
		minimum = 16
	}
	if count < minimum {
		t.Fatalf("expected at least %d binding usage fixtures, found %d", minimum, count)
	}
}

func TestS6TokenAlgebraFixtures(t *testing.T)  { runUsageFixtures(t, true) }
func TestS6ObservedUsageFixtures(t *testing.T) { runUsageFixtures(t, false) }
