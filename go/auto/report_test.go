package auto_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	"github.com/future-agi/traceAI/go/auto/internal/wire"
)

// Every wire protocol the coverage report names is an endpoint id from the
// shared catalog, so doctor output can be matched against wire-protocols.json.
// Entries with spaces describe a module whose own calls go through other
// protocols and are not ids.
func TestReportUsesCatalogEndpointIDs(t *testing.T) {
	var catalog struct {
		Endpoints []struct {
			ID string `json:"id"`
		} `json:"endpoints"`
	}
	if err := json.Unmarshal(wire.CatalogJSON, &catalog); err != nil {
		t.Fatal(err)
	}
	ids := map[string]bool{}
	for _, endpoint := range catalog.Endpoints {
		ids[endpoint.ID] = true
	}
	setup(t)
	checked := 0
	for _, integration := range auto.Report().Integrations {
		for _, protocol := range integration.WireProtocols {
			if strings.Contains(protocol, " ") {
				continue
			}
			checked++
			if !ids[protocol] {
				t.Errorf("%s lists %q, which is not a catalog endpoint id", integration.Name, protocol)
			}
		}
	}
	if checked == 0 {
		t.Fatal("the report listed no wire protocols")
	}
}
