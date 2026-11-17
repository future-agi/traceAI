package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/future-agi/traceAI/go/auto"
	"github.com/future-agi/traceAI/go/auto/internal/testhost"
)

func doctorCommand(args ...string) *exec.Cmd {
	return testhost.Command(os.Args[0], args...)
}

func TestDoctorInvalidCollectorProcess(t *testing.T) {
	if os.Getenv("TRACEAI_DOCTOR_INVALID_URL_CHILD") == "1" {
		main()
		os.Exit(0)
	}
	// Re-exec the built test binary to exercise package initialization with a
	// fresh process, rather than a mocked AgentReport or an extra Go build.
	cmd := doctorCommand("-test.run=^TestDoctorInvalidCollectorProcess$")
	for _, value := range os.Environ() {
		if !strings.HasPrefix(value, "FI_") && !strings.HasPrefix(value, "OTEL_") && !strings.HasPrefix(value, "TRACEAI_DOCTOR_INVALID_URL_CHILD=") {
			cmd.Env = append(cmd.Env, value)
		}
	}
	cmd.Env = append(cmd.Env, "TRACEAI_DOCTOR_INVALID_URL_CHILD=1", "FI_CONFIG_FILE="+filepath.Join(t.TempDir(), "absent.toml"), "FI_API_KEY=DUMMY_API_PRIVATE", "FI_SECRET_KEY=DUMMY_SECRET_PRIVATE", "FI_BASE_URL=://invalid-endpoint")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 1 {
		t.Fatalf("invalid collector doctor exit=%v stderr=%s", err, stderr.String())
	}
	var report auto.AgentReport
	if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
		t.Fatalf("invalid doctor JSON: %v %s", err, stdout.String())
	}
	if report.Config.BaseURL != "://invalid-endpoint" || len(report.Integrations) == 0 || report.Integrations[0].Status != "failed" {
		t.Fatalf("invalid collector reported healthy: %+v", report)
	}
}

func TestDoctorMalformedLiteralConfigProcess(t *testing.T) {
	if os.Getenv("TRACEAI_DOCTOR_BAD_LITERAL_CHILD") == "1" {
		main()
		os.Exit(0)
	}
	path := filepath.Join(t.TempDir(), "bad.toml")
	if err := os.WriteFile(path, []byte("api_key = '\n"), 0600); err != nil {
		t.Fatal(err)
	}
	// Re-exec, without a second build or sleeps, to exercise Init's real
	// blank-import path and doctor's actual process exit status.
	cmd := doctorCommand("-test.run=^TestDoctorMalformedLiteralConfigProcess$")
	for _, value := range os.Environ() {
		if !strings.HasPrefix(value, "FI_") && !strings.HasPrefix(value, "OTEL_") && !strings.HasPrefix(value, "TRACEAI_DOCTOR_BAD_LITERAL_CHILD=") {
			cmd.Env = append(cmd.Env, value)
		}
	}
	cmd.Env = append(cmd.Env, "TRACEAI_DOCTOR_BAD_LITERAL_CHILD=1", "FI_CONFIG_FILE="+path)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 1 {
		t.Fatalf("malformed config doctor exit=%v stderr=%s", err, stderr.String())
	}
	var report auto.AgentReport
	if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
		t.Fatalf("invalid doctor JSON: %v %s", err, stdout.String())
	}
	if report.Config.ConfigFile != path || len(report.Integrations) == 0 || report.Integrations[0].Status != "failed" {
		t.Fatalf("malformed literal reported healthy: %+v", report)
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestDoctorReportFailures(t *testing.T) {
	for _, tc := range []struct {
		name      string
		report    auto.AgentReport
		writer    io.Writer
		wantError bool
	}{
		{"healthy", auto.AgentReport{Integrations: []auto.IntegrationStatus{{Name: "wire", Status: "instrumented"}}}, &bytes.Buffer{}, false},
		{"failed-config", auto.AgentReport{Integrations: []auto.IntegrationStatus{{Name: "wire", Status: "failed"}}}, &bytes.Buffer{}, true},
		{"encoding-error", auto.AgentReport{Config: auto.Config{SampleRate: math.NaN()}}, &bytes.Buffer{}, true},
		{"write-error", auto.AgentReport{}, failingWriter{}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := writeReport(tc.writer, tc.report)
			if (err != nil) != tc.wantError {
				t.Fatalf("err=%v wantError=%v", err, tc.wantError)
			}
			if tc.name == "write-error" && !errors.Is(err, io.ErrClosedPipe) {
				t.Fatalf("writer error discarded: %v", err)
			}
			if tc.name == "failed-config" {
				var r auto.AgentReport
				if json.Unmarshal(tc.writer.(*bytes.Buffer).Bytes(), &r) != nil || len(r.Integrations) != 1 || r.Integrations[0].Status != "failed" {
					t.Fatal("failed report not printed as JSON")
				}
			}
		})
	}
}
