package wire

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
)

//go:embed privacy-fixtures.json
var privacyFixturesJSON []byte

func TestPrivacyFixtures(t *testing.T) {
	var bundle struct {
		Version        int
		CatalogVersion int `json:"catalog_version"`
		Cases          []struct {
			Name  string
			Input struct {
				Site, Path string
				Value      any
				Siblings   map[string]any
				Secrets    []string `json:"known_secrets"`
			}
			Expected any
		}
	}
	if err := json.Unmarshal(privacyFixturesJSON, &bundle); err != nil {
		t.Fatal(err)
	}
	if bundle.Version != 1 || bundle.CatalogVersion != 1 || len(bundle.Cases) == 0 {
		t.Fatal("unsupported/empty privacy fixtures")
	}
	for _, fixture := range bundle.Cases {
		t.Run(fixture.Name, func(t *testing.T) {
			in := fixture.Input
			budget := newPrivacyBudget()
			got := privacyWalk(in.Value, in.Path, in.Secrets, budget, 0, mediaHint{}, indexHints(in.Siblings, "", nil), in.Site != "free_text_urls")
			if !reflect.DeepEqual(got, fixture.Expected) {
				t.Fatalf("site %s path %q: got %s; want %s", in.Site, in.Path, dump(got), dump(fixture.Expected))
			}
			again := privacyWalk(got, in.Path, in.Secrets, newPrivacyBudget(), 0, mediaHint{}, indexHints(in.Siblings, "", nil), in.Site != "free_text_urls")
			if !reflect.DeepEqual(got, again) {
				t.Fatalf("not idempotent: %s -> %s", dump(got), dump(again))
			}
		})
	}
}

func TestPrivacyCatalogByteIdentity(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	spec := filepath.Join(filepath.Dir(file), "../../../../spec/auto-instrumentation")
	for name, shipped := range map[string][]byte{"privacy.json": PrivacyCatalogJSON, "privacy-fixtures.json": privacyFixturesJSON} {
		expected, err := os.ReadFile(filepath.Join(spec, name))
		if os.IsNotExist(err) {
			t.Skip("spec checkout unavailable in standalone module")
		}
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(shipped, expected) {
			t.Errorf("%s differs from spec; sync shipping copy", name)
		}
	}
}
