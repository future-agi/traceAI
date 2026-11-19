package main

import (
	"os"
	"testing"
)

func TestTracesEndpointDefaultsToHostedAPI(t *testing.T) {
	t.Setenv("FI_BASE_URL", "")
	if got, want := tracesEndpoint(), "https://api.futureagi.com/tracer/v1/traces"; got != want {
		t.Errorf("FI_BASE_URL empty: tracesEndpoint() = %q, want %q", got, want)
	}
	os.Unsetenv("FI_BASE_URL")
	if got, want := tracesEndpoint(), "https://api.futureagi.com/tracer/v1/traces"; got != want {
		t.Errorf("FI_BASE_URL unset: tracesEndpoint() = %q, want %q", got, want)
	}
}

func TestTracesEndpointUsesBaseURL(t *testing.T) {
	for _, tc := range []struct{ base, want string }{
		{"http://127.0.0.1:8000", "http://127.0.0.1:8000/tracer/v1/traces"},
		{"http://127.0.0.1:8000/", "http://127.0.0.1:8000/tracer/v1/traces"},
		{"http://127.0.0.1:8000//", "http://127.0.0.1:8000/tracer/v1/traces"},
		{"https://observe.example.com/futureagi/", "https://observe.example.com/futureagi/tracer/v1/traces"},
		{"/", "https://api.futureagi.com/tracer/v1/traces"},
	} {
		t.Setenv("FI_BASE_URL", tc.base)
		if got := tracesEndpoint(); got != tc.want {
			t.Errorf("FI_BASE_URL=%q: tracesEndpoint() = %q, want %q", tc.base, got, tc.want)
		}
	}
}
