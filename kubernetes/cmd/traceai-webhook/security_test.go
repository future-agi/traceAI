package main

import (
	"strings"
	"testing"
)

func TestServerRejectsInvalidConcurrencyAndInitResources(t *testing.T) {
	for _, args := range [][]string{
		{"--max-concurrent-admissions=0"},
		{"--max-concurrent-admissions=1025"},
		{"--max-concurrent-lookups=0"},
		{"--max-concurrent-admissions=2", "--max-concurrent-lookups=3"},
		{`--init-resources={"requests":{"cpu":"bad-sensitive-value"}}`},
		{`--init-resources={"requests":{"cpu":"200m","memory":"32Mi"},"limits":{"cpu":"100m","memory":"128Mi"}}`},
		{`--init-resources={"requests":{"cpu":"10m","memory":"16Mi","nvidia.com/gpu":"1"},"limits":{"cpu":"100m","memory":"128Mi"}}`},
	} {
		err := run(append([]string{"--agent-image=example/image:version"}, args...))
		if err == nil || strings.Contains(err.Error(), "sensitive") || strings.Contains(err.Error(), "serving certificate") {
			t.Fatalf("invalid controls not rejected safely before TLS startup: %v", err)
		}
	}
}
