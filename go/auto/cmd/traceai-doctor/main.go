// traceai-doctor prints local resolved configuration and integration coverage.
// It deliberately does not send a test export or contact a collector.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/future-agi/traceAI/go/auto"
)

func writeReport(w io.Writer, report auto.AgentReport) error {
	e := json.NewEncoder(w)
	e.SetIndent("", "  ")
	if err := e.Encode(report); err != nil {
		return err
	}
	for _, integration := range report.Integrations {
		if integration.Status == "failed" {
			return fmt.Errorf("invalid agent configuration")
		}
	}
	return nil
}

func main() {
	if err := writeReport(os.Stdout, auto.Report()); err != nil {
		fmt.Fprintln(os.Stderr, "traceai doctor: cannot report a healthy agent configuration")
		os.Exit(1)
	}
}
